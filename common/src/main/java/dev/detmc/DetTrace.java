package dev.detmc;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.level.entity.EntityAccess;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.Vec3;

/**
 * Phase 2 diagnostic, off unless {@code -Ddetmc.traceEntities=true}.
 *
 * <p>Logs every entity handed to {@code PersistentEntitySectionManager.addEntity}
 * in arrival order with its source, so two runs can be diffed to see which entity
 * appeared where, rather than inferring it from the tick-0 dump.
 */
public final class DetTrace {
    public static final String PROP_TRACE = "detmc.traceEntities";
    /**
     * Phase 3 diagnostic, off unless {@code -Ddetmc.traceSpawnLight=true}. Logs every
     * {@code Animal.isBrightEnoughToSpawn} call in order with the chunk whose SPAWN step
     * is running, the block position, the raw brightness the check read and the verdict.
     * Diffing two runs dates a worldgen mob divergence to the exact light read that
     * disagreed, which the entity-add trace can only do after the fact.
     */
    public static final String PROP_TRACE_SPAWN_LIGHT = "detmc.traceSpawnLight";
    /**
     * Phase 4 probe, off unless {@code -Ddetmc.traceIo=true}. Two things:
     *
     * <ul>
     *   <li>{@link #tickDigest} writes one line per tick holding a hash over every
     *       entity's UUID, position and motion. Diffing two runs dates a divergence to
     *       the exact tick, which no checkpoint dump can do.
     *   <li>{@link #io} labels each point where a background result enters main-thread
     *       game state, stamped with the same tick number, so the events in the
     *       neighbourhood of the first divergent tick can be read off.
     * </ul>
     */
    public static final String PROP_TRACE_IO = "detmc.traceIo";
    /**
     * Phase 6 probe, off unless {@code -Ddetmc.traceEntityWindow=lo:hi} is set (gametimes,
     * inclusive). The per-tick digest in {@link #tickDigest} dates a divergence but cannot
     * localise it: it is one hash over every entity. Inside the window this dumps one line
     * per entity per tick, so a diff names the entity and the field that moved first.
     */
    public static final String PROP_TRACE_ENTITY_WINDOW = "detmc.traceEntityWindow";

    /**
     * Phase 9 probe, off unless {@code -Ddetmc.traceDraws=lo:hi} is set (gametimes,
     * inclusive, several ranges allowed). The per-tick digest's {@code levelRng=draws,state}
     * field dates a divergence to the tick on which two runs took a different <em>number</em>
     * of draws from the level {@code RandomSource}, but it cannot say who took the extra one.
     * Inside the window this logs one line per draw <em>from the overworld level's own
     * {@code RandomSource}</em> with the calling frames, so a diff names the call site. Entity
     * randoms are left out on purpose: {@code traceEntityWindow} already carries their draw
     * counts, and tracing all 208 of them would be thousands of stack walks per tick.
     */
    public static final String PROP_TRACE_DRAWS = "detmc.traceDraws";

    private static final boolean TRACE = Boolean.getBoolean(PROP_TRACE);
    private static final boolean TRACE_SPAWN_LIGHT = Boolean.getBoolean(PROP_TRACE_SPAWN_LIGHT);
    private static final AtomicLong SEQ = new AtomicLong();
    private static final AtomicLong LIGHT_SEQ = new AtomicLong();
    private static final ThreadLocal<String> SOURCE = ThreadLocal.withInitial(() -> "other");
    private static final ThreadLocal<String> SPAWN_CHUNK = ThreadLocal.withInitial(() -> "-");
    private static final boolean TRACE_IO = Boolean.getBoolean(PROP_TRACE_IO);
    /** Phase 7: several windows, {@code lo:hi[,lo:hi...]}, inclusive gametimes. */
    private static final long[][] WINDOWS = parseWindows(PROP_TRACE_ENTITY_WINDOW);
    /** Phase 9: the same shape, for {@link #PROP_TRACE_DRAWS}. */
    private static final long[][] DRAW_WINDOWS = parseWindows(PROP_TRACE_DRAWS);

    private static long[][] parseWindows(String property) {
        List<long[]> windows = new ArrayList<>();
        for (String part : System.getProperty(property, "").split(",")) {
            int colon = part.indexOf(':');
            if (colon <= 0) {
                continue;
            }
            try {
                windows.add(new long[] {
                        Long.parseLong(part.substring(0, colon).trim()),
                        Long.parseLong(part.substring(colon + 1).trim())});
            } catch (NumberFormatException ignored) {
                // malformed window: ignored
            }
        }
        return windows.toArray(new long[0][]);
    }

    private static boolean inWindow(long[][] windows, long gameTime) {
        for (long[] w : windows) {
            if (gameTime >= w[0] && gameTime <= w[1]) {
                return true;
            }
        }
        return false;
    }

    private static boolean inWindow(long gameTime) {
        return inWindow(WINDOWS, gameTime);
    }

    /**
     * Phase 9 — one line per RNG draw inside {@code -Ddetmc.traceDraws}, naming the caller.
     *
     * <p>Off by default and guarded by a static array length, so a normal run pays one
     * array load per {@code next(bits)} call. Inside the window it pays a {@link StackWalker}
     * walk, which is why the window has to be a handful of ticks.
     */
    public static void drawSite(Object rng, long draws) {
        if (DRAW_WINDOWS.length == 0) {
            return;
        }
        MinecraftServer instance = server;
        if (instance == null) {
            return;
        }
        ServerLevel overworld = instance.overworld();
        if (overworld == null) {
            return;
        }
        if (rng != overworld.getRandom()) {
            return;                     // level RandomSource only: see the field doc
        }
        long gameTime = overworld.getGameTime();
        if (!inWindow(DRAW_WINDOWS, gameTime)) {
            return;
        }
        String caller = StackWalker.getInstance()
                .walk(frames -> frames
                        .filter(f -> !f.getClassName().startsWith("dev.detmc")
                                && !f.getClassName().equals("net.minecraft.world.level.levelgen.LegacyRandomSource")
                                && !f.getClassName().equals("net.minecraft.util.RandomSource"))
                        .limit(8)
                        .map(f -> f.getClassName().replaceAll("^.*\\.", "")
                                + "." + f.getMethodName() + ":" + f.getLineNumber())
                        .reduce((a, b) -> a + "<" + b)
                        .orElse("?"));
        DetRng.LOGGER.info("[detmc-dr] gt={} n={} at={}", gameTime, draws, caller);
    }

    /**
     * Phase 6 — how many {@code shouldRun(TickTask)} decisions vanilla would have handed
     * to the wall clock this tick, i.e. tasks whose {@code getTick() + 3 < tickCount}
     * clause was false so the verdict came from {@code haveTime()}. Counted on the main
     * thread only and printed by {@link #tickDigest}.
     */
    /** Half-width in blocks of the box {@link #tickDigest} hashes. */
    private static final double DIGEST_RADIUS =
            Double.parseDouble(System.getProperty("detmc.digestRadius", "2048"));

    private static int clockDecidedTasks;
    private static volatile MinecraftServer server;

    private DetTrace() {
    }

    public static boolean tracing() {
        return TRACE;
    }

    public static void pushSource(String source) {
        SOURCE.set(source);
    }

    public static void popSource() {
        SOURCE.set("other");
    }

    public static boolean tracingSpawnLight() {
        return TRACE_SPAWN_LIGHT;
    }

    public static void pushSpawnChunk(String chunk) {
        SPAWN_CHUNK.set(chunk);
    }

    public static void popSpawnChunk() {
        SPAWN_CHUNK.set("-");
    }

    public static void spawnLightCheck(int x, int y, int z, int rawBrightness, boolean ok) {
        if (!TRACE_SPAWN_LIGHT) {
            return;
        }
        DetRng.LOGGER.info("[detmc-sl] {} chunk={} pos={},{},{} raw={} ok={}",
                LIGHT_SEQ.incrementAndGet(), SPAWN_CHUNK.get(), x, y, z, rawBrightness, ok);
    }

    public static boolean tracingIo() {
        return TRACE_IO;
    }

    public static void bindServer(MinecraftServer instance) {
        server = instance;
    }

    /**
     * Phase 8 — a world clock in milliseconds, for the handful of game-logic sites that
     * read {@code Util.getMillis()}.
     *
     * <p>{@code gameTime * 50} is what the wall clock would read on a server that never
     * missed a tick, and it is the same substitution {@code ChunkMapMixin} already makes
     * for the eager-save schedule. Returns 0 before the server is bound (nothing on this
     * path runs that early), so the value is still a function of the world and not of the
     * host.
     */
    public static long gameTimeMillis() {
        MinecraftServer instance = server;
        if (instance == null) {
            return 0L;
        }
        ServerLevel overworld = instance.overworld();
        return overworld == null ? 0L : overworld.getGameTime() * 50L;
    }

    /** Tick number, or -1 before the first server tick / from a thread with no server. */
    private static int tick() {
        MinecraftServer instance = server;
        return instance == null ? -1 : instance.getTickCount();
    }

    /**
     * One line per async-to-main-thread hand-off. {@code kind} is the call site,
     * {@code detail} is normally a chunk position.
     */
    public static void io(String kind, Object detail) {
        if (!TRACE_IO) {
            return;
        }
        DetRng.LOGGER.info("[detmc-io] t={} {} {}", tick(), kind, detail);
    }

    /** Called from the {@code shouldRun} override; main thread only, so no atomics. */
    public static void clockDecidedTask() {
        clockDecidedTasks++;
    }

    /**
     * FNV-1a over every entity's UUID, position and motion, sorted by UUID so the
     * hash does not depend on iteration order. Called at {@code tickServer} TAIL.
     */
    public static void tickDigest(MinecraftServer instance) {
        if (!TRACE_IO) {
            return;
        }
        ServerLevel level = instance.overworld();
        if (level == null) {
            return;
        }
        List<String> rows = new ArrayList<>();
        // ServerLevel.getAllEntities() is the VISIBLE lookup only: LevelEntityGetterAdapter.getAll()
        // reads `visibleEntities`, so an entity whose chunk is loaded but not tracked never reaches
        // it.  Measured 2026-09-13: 208 of the 353 entities in the villager checkpoint are visible,
        // and two zombies that the save shows moving apart were invisible to this digest for a whole
        // 6,000-tick pair.  getEntities(except, box, predicate) goes through the section storage
        // instead, so the digest covers every loaded entity.
        int visible = 0;
        for (Entity ignored : level.getAllEntities()) {
            visible++;
        }
        // Bounded on purpose.  EntitySectionStorage walks every section COLUMN in the box,
        // not just the loaded ones, so a world-sized AABB is 3.75 million iterations a tick and
        // the server stops making progress (measured 2026-09-13: a 6,000-tick segment made no
        // headway in 10 minutes).  DIGEST_RADIUS blocks covers every region file this harness
        // writes; an entity outside it is reported by `vis` versus `n` rather than hashed.
        List<Entity> all = level.getEntities((Entity) null,
                new AABB(-DIGEST_RADIUS, level.getMinY() - 64, -DIGEST_RADIUS,
                        DIGEST_RADIUS, level.getMaxY() + 64, DIGEST_RADIUS),
                e -> true);
        for (Entity e : all) {
            Vec3 motion = e.getDeltaMovement();
            rows.add(e.getUUID()
                    + "|" + e.getType().toString()
                    + "|" + e.getId()
                    + "|" + Double.doubleToRawLongBits(e.getX())
                    + "," + Double.doubleToRawLongBits(e.getY())
                    + "," + Double.doubleToRawLongBits(e.getZ())
                    + "|" + Double.doubleToRawLongBits(motion.x)
                    + "," + Double.doubleToRawLongBits(motion.y)
                    + "," + Double.doubleToRawLongBits(motion.z)
                    + "|" + Float.floatToRawIntBits(e.getYRot())
                    + "," + Float.floatToRawIntBits(e.getXRot())
                    + "|" + e.tickCount
                    + "|" + rngOf(e.getRandom()));
        }
        Collections.sort(rows);
        long hash = 0xcbf29ce484222325L;
        for (String row : rows) {
            for (int i = 0; i < row.length(); i++) {
                hash ^= row.charAt(i);
                hash *= 0x100000001b3L;
            }
            hash ^= '\n';
            hash *= 0x100000001b3L;
        }
        long gameTime = level.getGameTime();
        DetRng.LOGGER.info("[detmc-dg] t={} gt={} n={} vis={} h={} clockTasks={} levelRng={}",
                instance.getTickCount(), gameTime, rows.size(), visible, Long.toHexString(hash),
                clockDecidedTasks, rngOf(level.getRandom()));
        clockDecidedTasks = 0;
        if (inWindow(gameTime)) {
            for (String row : rows) {
                DetRng.LOGGER.info("[detmc-ew] gt={} {}", gameTime, row);
            }
        }
    }

    /** Phase 7: {@code draws,state} of a vanilla legacy RNG, or {@code -} for any other kind. */
    private static String rngOf(Object random) {
        if (random instanceof DetDrawCounter c) {
            return c.detmc$draws() + "," + Long.toHexString(c.detmc$state());
        }
        return "-";
    }

    /**
     * Every block change in the overworld, stamped with the tick. The per-tick entity
     * digest cannot see a block divergence: an enderman moving a different block changes
     * no entity field, so the two runs stay bit-identical until some mob later walks into
     * the difference. Volume is low -- a quiet forceloaded world at midnight with
     * advance_time false changes a few hundred blocks over tens of thousands of ticks.
     */
    public static void blockSet(Object pos, Object oldState, Object newState) {
        if (!TRACE_IO) {
            return;
        }
        DetRng.LOGGER.info("[detmc-io] t={} setBlock {} {} -> {}", tick(), pos, oldState, newState);
    }

    /**
     * Phase 7 probe, on with {@code -Ddetmc.traceEntities=true}: one line per {@code Entity}
     * constructor return, with the id just assigned, the thread, and the first three frames
     * outside the entity constructors. Construction is what assigns the id and the RNG
     * ordinal, so this is the order that has to be reproducible.
     */
    public static void entityConstructed(Entity e) {
        if (!TRACE) {
            return;
        }
        String caller = StackWalker.getInstance()
                .walk(frames -> frames
                        .filter(f -> !f.getClassName().startsWith("dev.detmc")
                                && !f.getMethodName().equals("<init>"))
                        .limit(4)
                        .map(f -> f.getClassName().replaceAll("^.*\\.", "") + "." + f.getMethodName() + ":" + f.getLineNumber())
                        .reduce((a, b) -> a + "<" + b)
                        .orElse("?"));
        DetRng.LOGGER.info("[detmc-ec] t={} id={} ordinal={} type={} thread={} at={}",
                tick(), e.getId(), DetRng.entitiesSeeded(), e.getType(), Thread.currentThread().getName(), caller);
    }

    public static void entityAdded(EntityAccess entity, boolean loaded) {
        if (!TRACE) {
            return;
        }
        long n = SEQ.incrementAndGet();
        String type = "?";
        String uuid = "?";
        String pos = "?";
        if (entity instanceof Entity e) {
            type = e.getType().toString();
            uuid = e.getUUID().toString();
            pos = String.format("%.3f,%.3f,%.3f", e.getX(), e.getY(), e.getZ());
        }
        DetRng.LOGGER.info("[detmc-ea] {} src={} loaded={} id={} type={} uuid={} pos={}",
                n, SOURCE.get(), loaded, entity.getId(), type, uuid, pos);
    }
}
