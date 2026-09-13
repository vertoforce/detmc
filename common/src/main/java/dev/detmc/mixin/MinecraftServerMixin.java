package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetMcBootstrap;
import dev.detmc.DetRng;
import dev.detmc.DetTrace;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.TickTask;
import net.minecraft.server.level.ChunkMap;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.util.RandomSource;
import net.minecraft.world.level.levelgen.WorldOptions;
import net.minecraft.world.level.storage.LevelResource;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(MinecraftServer.class)
public abstract class MinecraftServerMixin {
    /**
     * Phase 4 fix — autosave fired on a server-tick countdown, which includes frozen ticks.
     *
     * <p>{@code ticksUntilAutosave} is decremented once per {@code tickServer}, and
     * {@code tickServer} runs while the tick rate manager is frozen too. Two replicas sit
     * frozen for a different number of ticks during the scripted setup (settle polls,
     * 60 summons), so the autosave lands at a different <em>gametime</em> on each of them,
     * and an autosave is a {@code saveAllChunks} over every visible chunk — the same
     * {@code ChunkMap.save} path whose timing moves chunk unloads.
     *
     * <p>So: hold vanilla's countdown off, and fire the save from the tick tail on a fixed
     * gametime grid instead. Same work, same period (6000 ticks = 300 s at 20 tps), but
     * placed by the world clock.
     */
    private static final long DETMC_AUTOSAVE_TICKS = 6000L;

    @Shadow
    private int ticksUntilAutosave;

    @Shadow
    protected abstract boolean pollTask();

    @Unique
    private long detmc$lastAutosaveGameTime = Long.MIN_VALUE;

    /** {@code MinecraftServer.random} field init; one ctor, so one match. */
    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;create()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$random() {
        return DetRng.create();
    }

    /**
     * {@code createLevels()V} reads {@code this.worldGenSettings.options().seed()}
     * exactly once, before the overworld {@code ServerLevel} is constructed. That
     * is the earliest guaranteed point at which the world seed exists, so bind it
     * here rather than shadowing a private field.
     */
    @Redirect(
            method = "createLevels()V",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/world/level/levelgen/WorldOptions;seed()J")
    )
    private long detmc$bindWorldSeed(WorldOptions options) {
        long seed = options.seed();
        DetRng.bindWorldSeed(seed);
        // Phase 5: a restart rekeys the stream, so the shared counter and the entity
        // ordinal would both restart at 0. Restore them here -- after the rekey, and
        // before prepareLevels constructs the first entity.
        DetRng.restoreState(((MinecraftServer) (Object) this).getWorldPath(LevelResource.ROOT));
        return seed;
    }

    /**
     * Phase 5 — carry the RNG stream position into the save, so a run resumed from it
     * continues the same sequence of seeds instead of starting the stream over.
     * Covers both the autosave below and the {@code save-all flush} command.
     */
    @Inject(method = "saveEverything(ZZZ)Z", at = @At("TAIL"))
    private void detmc$persistRng(boolean silent, boolean flush, boolean force, CallbackInfoReturnable<Boolean> cir) {
        DetRng.persistState(((MinecraftServer) (Object) this).getWorldPath(LevelResource.ROOT));
        // Phase 7: SaveAllCommand is silent and `save-all flush` over rcon times out on a busy
        // server, so this is the only evidence that the save actually finished. The harness
        // waits for it before hashing the region files.
        ServerLevel overworld = ((MinecraftServer) (Object) this).overworld();
        DetRng.LOGGER.info("[detmc] saveEverything done: flush={} force={} gt={}", flush, force,
                overworld == null ? -1L : overworld.getGameTime());
    }

    @Inject(method = "tickServer(Ljava/util/function/BooleanSupplier;)V", at = @At("HEAD"))
    private void detmc$firstTick(BooleanSupplier haveTime, CallbackInfo ci) {
        DetTrace.bindServer((MinecraftServer) (Object) this);
        DetMcBootstrap.onFirstServerTick((MinecraftServer) (Object) this);
        DetTrace.io("phase", "tickStart");
    }

    /** Phase 7 probe: where the between-tick task loop begins. */
    @Inject(method = "waitUntilNextTick()V", at = @At("HEAD"))
    private void detmc$traceWait(CallbackInfo ci) {
        DetTrace.io("phase", "wait");
    }

    /**
     * Phase 4 — end every tick with an empty chunk-task queue.
     *
     * <p>{@code pollTaskInternal} only reaches the chunk sources when
     * {@code isSprinting() || shouldRunAllTasks() || haveTime()}. Outside a sprint that
     * is the wall clock, so the number of chunk generation, lighting and unload tasks
     * that run between two ticks is a property of the host, not of the world. Tick-level
     * work ({@code ChunkMap.tick}, {@code processUnloads}, level ticking) is therefore
     * interleaved into the chunk-task sequence at a different point in every run.
     *
     * <p>Draining to a fixpoint at the end of the tick makes the tick boundary a
     * barrier: no chunk task ever straddles it, so the interleaving is fixed. Total work
     * is unchanged; it is just no longer sliced by a clock. Same-thread only, and capped,
     * because a chunk task can legitimately enqueue its successor.
     */
    @Inject(method = "tickServer(Ljava/util/function/BooleanSupplier;)V", at = @At("TAIL"))
    private void detmc$drainChunkTasksAtTickEnd(BooleanSupplier haveTime, CallbackInfo ci) {
        if (!DetExecutors.syncChunks()) {
            return;
        }
        MinecraftServer self = (MinecraftServer) (Object) this;
        DetTrace.io("phase", "drainStart");
        for (int round = 0; round < 16; round++) {
            int guard = 0;
            boolean more = true;
            while (more && guard++ < 4_000_000) {
                more = false;
                for (ServerLevel level : self.getAllLevels()) {
                    if (level.getChunkSource().pollTask()) {
                        more = true;
                    }
                }
                // Phase 6: the main-thread queue too, so no TickTask straddles the boundary.
                if (DetExecutors.syncTasks()) {
                    while (this.pollTask()) {
                        more = true;
                    }
                }
            }
            if (guard >= 4_000_000) {
                DetRng.LOGGER.warn("[detmc] chunk-task drain hit the {} poll cap at tick end", guard);
            }
            if (!DetExecutors.ioBarrier() || !detmc$ioBarrier(self)) {
                break;
            }
        }
        DetTrace.io("phase", "drainEnd");
        detmc$autosaveOnWorldClock(self);
        DetTrace.tickDigest(self);
    }

    /**
     * Phase 7 — chunk work before the command queue.
     *
     * <p>Measured 2026-09-12 (test/phase7/K1..K3, `[detmc-io] phase` markers): two runs of one
     * jar forked inside the tick-18 drain at the point where {@code ServerChunkCache.pollTask}
     * either found new distance-manager work (new worldgen submits, chunk reads) or ran the
     * queued full-status-change tasks; the construction order of a chicken and a goat, and
     * with it their ids and RNG seeds, followed. The new tickets come from the harness's
     * rcon commands ({@code forceload add}), which land on the {@code MinecraftServer} queue
     * at wall-clock times, and vanilla {@code pollTaskInternal} polls that queue <em>before</em>
     * chunk work. So a command ran at an arbitrary point inside in-flight chunk generation.
     *
     * <p>Polling chunk work first means a queued command runs only when the chunk pipeline
     * has nothing to do, i.e. at a quiescent point, whatever the clock said when it arrived.
     * Under {@code -Ddetmc.syncTasks} (default on).
     */
    @Inject(method = "pollTaskInternal()Z", at = @At("HEAD"), cancellable = true)
    private void detmc$chunkWorkBeforeCommands(CallbackInfoReturnable<Boolean> cir) {
        if (!DetExecutors.syncTasks()) {
            return;
        }
        for (ServerLevel level : ((MinecraftServer) (Object) this).getAllLevels()) {
            if (level.getChunkSource().pollTask()) {
                cir.setReturnValue(true);
                return;
            }
        }
    }

    /**
     * Phase 6 — the last wall-clock gate that is live inside a {@code tick sprint}.
     *
     * <p>Vanilla: {@code task.getTick() + 3 < this.tickCount || this.haveTime()}. The first
     * clause only fires for a task that has already been queued for four ticks, so in
     * practice the verdict comes from {@code haveTime()}, i.e. {@code Util.getNanos()}
     * against the tick deadline. A task queued by an IO completion therefore runs in the
     * tick the host's clock picks. Every other {@code haveTime} consumer is already pinned:
     * the supplier handed to {@code tickServer} is a constant {@code () -> false} while
     * sprinting, {@code ChunkMap.tick}'s copy is forced true by {@code ChunkMapMixin},
     * {@code SectionStorage.tick}'s by {@code SectionStorageMixin}, and
     * {@code pollTaskInternal} short-circuits on {@code isSprinting()}.
     *
     * <p>Pinned to "always run": deferral is what varies, so removing it is what makes the
     * two runs agree. Combined with the tick-end drain below, the queue is empty at every
     * tick boundary, so no task can slide into a later tick at all.
     */
    @Inject(method = "shouldRun(Lnet/minecraft/server/TickTask;)Z", at = @At("HEAD"), cancellable = true)
    private void detmc$shouldRunWithoutAClock(TickTask task, CallbackInfoReturnable<Boolean> cir) {
        if (!DetExecutors.syncTasks()) {
            return;
        }
        // Evidence, not decoration: this counts the decisions vanilla would have taken on
        // the clock, so the probe can show whether the gate was ever reached at all.
        if (DetTrace.tracingIo() && task.getTick() + 3 >= ((MinecraftServer) (Object) this).getTickCount()) {
            DetTrace.clockDecidedTask();
        }
        cir.setReturnValue(true);
    }

    /**
     * Phase 6 — make the tick boundary an IO barrier as well as a task barrier.
     *
     * <p>{@code ChunkMap.save} hands the encoded chunk to {@code Util.ioPool()}, which is
     * deliberately still a real pool, and {@code scheduleUnload} chains the unload onto
     * that write's completion via {@code unloadQueue::add}. So whether a chunk unloads in
     * tick N or N+1 — and with it {@code level.unload} and
     * {@code lightEngine.updateChunkStatus} — depends on how fast the disk was. Waiting
     * for the in-flight writes before the tick ends pins the unload to the tick that
     * issued the save.
     *
     * @return true if anything was actually in flight, i.e. the caller should drain again
     */
    @Unique
    private boolean detmc$ioBarrier(MinecraftServer self) {
        boolean waited = false;
        for (ServerLevel level : self.getAllLevels()) {
            ChunkMap chunkMap = level.getChunkSource().chunkMap;
            AtomicInteger activeWrites = ((ChunkMapAccessor) (Object) chunkMap).detmc$activeChunkWrites();
            if (activeWrites.get() > 0) {
                waited = true;
            }
            try {
                chunkMap.synchronize(false).join();
            } catch (RuntimeException e) {
                DetRng.LOGGER.warn("[detmc] chunk write barrier failed", e);
            }
            // join() returns when the store future completes; the handle() that decrements
            // this counter is a dependent of the same future and may not have run yet.
            long spins = 0L;
            while (activeWrites.get() > 0 && spins++ < 200_000_000L) {
                Thread.onSpinWait();
            }
            if (activeWrites.get() > 0) {
                DetRng.LOGGER.warn("[detmc] chunk write barrier gave up with {} writes in flight",
                        activeWrites.get());
            }
        }
        return waited;
    }

    @Unique
    private void detmc$autosaveOnWorldClock(MinecraftServer self) {
        ServerLevel overworld = self.overworld();
        if (overworld == null) {
            return;
        }
        // Vanilla's countdown never reaches zero again; we own the schedule now.
        this.ticksUntilAutosave = Integer.MAX_VALUE;
        long gameTime = overworld.getGameTime();
        if (gameTime == this.detmc$lastAutosaveGameTime) {
            return; // frozen tick: the world did not advance, so nothing is due
        }
        this.detmc$lastAutosaveGameTime = gameTime;
        if (gameTime > 0L && gameTime % DETMC_AUTOSAVE_TICKS == 0L) {
            DetTrace.io("autosave", "gt=" + gameTime);
            self.saveEverything(true, false, false);
        }
    }
}
