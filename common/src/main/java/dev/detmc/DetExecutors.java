package dev.detmc;

import java.util.ArrayDeque;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.AbstractExecutorService;
import java.util.concurrent.Executor;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Phase 2: ordering control.
 *
 * <p>{@link #sameThread()} is a <em>trampolining</em> same-thread executor. A
 * naive {@code Runnable::run} cannot be used as the dispatcher of
 * {@code AbstractConsecutiveExecutor}, because its {@code run()} ends with
 * {@code registerForExecution()}, which calls {@code executor.execute(this)}
 * again — with an inline executor that recurses once per queued task and blows
 * the stack on a worldgen burst (hundreds of chunk tasks deep).
 *
 * <p>So: the first {@code execute} on a given thread becomes the drain loop; any
 * {@code execute} issued from inside that loop is appended to the thread's queue
 * and returns immediately. Depth stays at 1, and the task order is exactly the
 * submission order — which is the property phase 2 needs.
 */
public final class DetExecutors {
    public static final String PROP_SYNC_CHUNKS = "detmc.syncChunks";
    /**
     * Phase 6. {@code MinecraftServer.shouldRun(TickTask)} is
     * {@code task.getTick() + 3 < tickCount || haveTime()}, so a queued main-thread task
     * runs either in the tick that queued it or up to three ticks later, and which of the
     * two happens is decided by {@code Util.getNanos()}. It is the only wall-clock gate
     * still live inside a {@code tick sprint}: the {@code haveTime} supplier handed to
     * {@code tickServer} is a constant {@code () -> false} while sprinting, and
     * {@code pollTaskInternal} short-circuits on {@code isSprinting()}.
     * {@code -Ddetmc.syncTasks=false} opts out.
     */
    public static final String PROP_SYNC_TASKS = "detmc.syncTasks";
    /**
     * Phase 6. Chunk saves complete on a real IO thread ({@code Util.ioPool()} is left
     * alone on purpose), and their completion is what pushes {@code scheduleUnload}'s
     * body onto {@code unloadQueue}. So a chunk unload — which calls
     * {@code level.unload} and {@code lightEngine.updateChunkStatus} — lands in a
     * wall-clock-chosen tick. This makes the tick boundary an IO barrier: wait for the
     * in-flight writes before the tick ends, so the unload is always queued in the tick
     * that issued the save. {@code -Ddetmc.ioBarrier=false} opts out.
     */
    public static final String PROP_IO_BARRIER = "detmc.ioBarrier";
    /**
     * Phase 7. Region reads (chunk, entity, POI) return only once the IO worker has the
     * bytes, so their continuations -- entity construction above all -- run at the request
     * point. See {@code IOWorkerMixin}. {@code -Ddetmc.syncReads=false} opts out.
     */
    public static final String PROP_SYNC_READS = "detmc.syncReads";

    /**
     * Phase 8. The JDK-{@code Random} sweep: every remaining server-reachable site that
     * drew from {@code java.util.Random}/{@code ThreadLocalRandom} or seeded a
     * {@code RandomSource} from the wall clock is routed through a seeded stream instead
     * (see {@code InsideBrownianWalkMixin}, {@code EntitySelectorParserMixin},
     * {@code PlayerSpawnFinderMixin}, {@code SpreadPlayersCommandMixin},
     * {@code StructureBlockEntityMixin}, {@code StructurePlaceSettingsMixin},
     * {@code StopwatchesMixin}). {@code -Ddetmc.syncRandom=false} restores vanilla at all
     * seven sites at once. {@code LongJumpToRandomPosMixin} predates this flag and stays
     * on {@code detmc.syncChunks}.
     */
    public static final String PROP_SYNC_RANDOM = "detmc.syncRandom";

    private static final ThreadLocal<ArrayDeque<Runnable>> PENDING =
            ThreadLocal.withInitial(ArrayDeque::new);
    private static final ThreadLocal<boolean[]> DRAINING =
            ThreadLocal.withInitial(() -> new boolean[1]);

    private static final Executor SAME_THREAD = DetExecutors::runHere;
    private static final ExecutorService SAME_THREAD_SERVICE = new SameThreadService();

    private DetExecutors() {
    }

    /** Default on: this is the whole point of phase 2. {@code -Ddetmc.syncChunks=false} opts out. */
    public static boolean syncChunks() {
        return !"false".equalsIgnoreCase(System.getProperty(PROP_SYNC_CHUNKS, "true"));
    }

    /** Default on. {@code -Ddetmc.syncTasks=false} opts out. */
    public static boolean syncTasks() {
        return !"false".equalsIgnoreCase(System.getProperty(PROP_SYNC_TASKS, "true"));
    }

    /**
     * Default <b>off</b>, opt in with {@code -Ddetmc.ioBarrier=true}. Measured 2026-09-12:
     * with the barrier on, the 10000-tick pair still first diverges at gametime 2199, the
     * same gametime as with it off, and run A's digest is byte-identical either way. It
     * buys no determinism that has been measured, and it costs a {@code synchronize} join
     * plus a spin at every tick end, so it is not paid for by default.
     */
    public static boolean ioBarrier() {
        return "true".equalsIgnoreCase(System.getProperty(PROP_IO_BARRIER, "false"));
    }

    /**
     * Phase 7 experiment flag. {@code true} keeps the phase 2 "lowest ChunkPos in the top
     * priority level" pop; {@code false} restores vanilla insertion order.
     */
    public static final String PROP_SORTED_CHUNK_POP = "detmc.sortedChunkPop";

    public static boolean sortedChunkPop() {
        return !"false".equalsIgnoreCase(System.getProperty(PROP_SORTED_CHUNK_POP, "true"));
    }

    /** Default on. {@code -Ddetmc.syncRandom=false} opts out. */
    public static boolean syncRandom() {
        return !"false".equalsIgnoreCase(System.getProperty(PROP_SYNC_RANDOM, "true"));
    }

    /** Default on. {@code -Ddetmc.syncReads=false} opts out. */
    public static boolean syncReads() {
        return !"false".equalsIgnoreCase(System.getProperty(PROP_SYNC_READS, "true"));
    }

    public static Executor sameThread() {
        return SAME_THREAD;
    }

    /**
     * A <em>direct</em> {@code ExecutorService} for wrapping in a
     * {@code TracingExecutor} in place of {@code Util.backgroundExecutor()}.
     *
     * <p>This is the same thing vanilla would build itself if it could:
     * {@code Util.makeExecutor} falls back to
     * {@code MoreExecutors.newDirectExecutorService()} when the thread count is
     * not positive, but {@code maxAllowedExecutorThreads()} clamps to a minimum
     * of 1, so that branch is unreachable from a system property.
     */
    public static ExecutorService sameThreadService() {
        return SAME_THREAD_SERVICE;
    }

    /** Returns {@code original} unchanged when {@code -Ddetmc.syncChunks=false}. */
    public static Executor maybeSameThread(Executor original) {
        return syncChunks() ? SAME_THREAD : original;
    }

    private static final class SameThreadService extends AbstractExecutorService {
        private volatile boolean shutdown;

        @Override
        public void execute(Runnable command) {
            // Direct, not trampolined, and this is load-bearing. Measured:
            // IOWorker.createOldDataForRegion does
            // supplyAsync(..., Util.backgroundExecutor()) and
            // IOWorker.isOldChunkAround immediately join()s the result on the
            // server thread. The trampoline enqueues instead of running when the
            // current thread is already draining, so the future was still
            // incomplete at the join and the server parked forever at
            // "Selecting global world spawn..." (thread dump: Server thread
            // WAITING in CompletableFuture.waitingGet from IOWorker:92).
            // Running inline completes the future before supplyAsync returns.
            //
            // Safe here because nothing uses backgroundExecutor as an
            // AbstractConsecutiveExecutor dispatcher -- every call site is a
            // supplyAsync/thenApplyAsync -- so the recursion the trampoline
            // exists to prevent cannot happen on this executor.
            command.run();
        }

        @Override
        public void shutdown() {
            this.shutdown = true;
        }

        @Override
        public List<Runnable> shutdownNow() {
            this.shutdown = true;
            return Collections.emptyList();
        }

        @Override
        public boolean isShutdown() {
            return this.shutdown;
        }

        @Override
        public boolean isTerminated() {
            return this.shutdown;
        }

        @Override
        public boolean awaitTermination(long timeout, TimeUnit unit) {
            // Nothing is ever in flight on another thread, so termination is
            // immediate. Returning false here would make MinecraftServer's
            // shutdownExecutors call shutdownNow and log a scary warning.
            return true;
        }
    }

    private static void runHere(Runnable task) {
        ArrayDeque<Runnable> queue = PENDING.get();
        queue.addLast(task);
        boolean[] draining = DRAINING.get();
        if (draining[0]) {
            return;
        }
        draining[0] = true;
        try {
            Runnable next;
            while ((next = queue.pollFirst()) != null) {
                try {
                    next.run();
                } catch (Throwable t) {
                    DetRng.LOGGER.error("[detmc] same-thread task threw", t);
                }
            }
        } finally {
            draining[0] = false;
        }
    }
}
