package dev.detmc;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.Properties;
import net.minecraft.util.RandomSource;
import net.minecraft.world.level.levelgen.ThreadSafeLegacyRandomSource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Server-wide deterministic seed source.
 *
 * <p>Every vanilla call site that would have produced a wall-clock or
 * {@code ThreadLocalRandom} derived seed is redirected here. The stream is a
 * xoroshiro128++ generator (the same shape Minecraft uses for its own
 * {@code XoroshiroRandomSource}), keyed on
 * {@code -Ddetmc.seed} XOR the world seed.
 *
 * <p>The {@code RandomSource} instances handed out are still vanilla
 * {@code LegacyRandomSource}/{@code SingleThreadedRandomSource} objects, so
 * per-object draw sequences are bit-identical to vanilla for a given seed. Only
 * the <em>seed</em> becomes reproducible.
 *
 * <p>Ordering caveat: seeds are handed out from a single counter, so this is
 * only reproducible while the <em>order</em> of allocation is reproducible.
 * Fixing the remaining order hazards (async light engine, chunk iteration,
 * executors) is phase 2.
 */
public final class DetRng {
    public static final String MOD_ID = "detmc";
    public static final Logger LOGGER = LoggerFactory.getLogger("detmc");

    public static final String PROP_SEED = "detmc.seed";
    public static final String PROP_FREEZE = "detmc.freezeOnStart";
    public static final String PROP_TRACE_SEEDS = "detmc.traceSeeds";
    public static final String PROP_PERSIST = "detmc.persistRng";

    /** Sidecar file in the world root that carries the stream position across a restart. */
    public static final String STATE_FILE = "detmc-rng.properties";

    private static final long GOLDEN_RATIO_64 = -7046029254386353131L;
    private static final long SILVER_RATIO_64 = 7640891576956012809L;

    private static final Object LOCK = new Object();

    private static final long PROP_SEED_VALUE = readSeedProperty();
    private static final boolean FREEZE_ON_START = Boolean.getBoolean(PROP_FREEZE);
    /**
     * Phase 2 diagnostic. Every seed comes off one counter, so a single extra or
     * missing allocation anywhere shifts every UUID after it. This logs who took
     * each one, in order, so two runs can be diffed to the exact call site where
     * they part company.
     */
    private static final boolean TRACE_SEEDS = Boolean.getBoolean(PROP_TRACE_SEEDS);

    /**
     * Phase 5. A restart re-runs the static initialiser and {@code bindWorldSeed}, both of
     * which call {@code rekey}, so without this the shared counter and the entity ordinal
     * both restart at 0 and every seed handed out after the restart differs from the one
     * the uninterrupted timeline would have used. On by default; there is nothing to
     * restore on a fresh world, so it is inert there.
     */
    private static final boolean PERSIST = !"false".equals(System.getProperty(PROP_PERSIST, "true"));

    private static long worldSeed;
    private static boolean worldSeedBound;
    /** Number of seeds handed out since the stream was last (re)keyed. */
    private static long issued;

    private static long stateLo;
    private static long stateHi;

    /**
     * The seed the current master stream was keyed from. Equal to
     * {@code PROP_SEED_VALUE ^ worldSeed} on a normal server; changed by
     * {@link #reseed(long)}. Declared without an initialiser on purpose: the static
     * initialiser below calls {@link #rekey(long)}, which assigns it, and a field
     * initialiser here would run afterwards and overwrite it.
     */
    private static long currentMaster;
    /** {@code PROP_SEED_VALUE ^ worldSeed}, i.e. what the master would be with no reseed. */
    private static long baseMaster;
    /** Argument of the last {@code /detmc reseed}, and whether there has been one. */
    private static long reseedArg;
    private static boolean reseeded;

    static {
        rekey(PROP_SEED_VALUE);
        baseMaster = PROP_SEED_VALUE;
        LOGGER.info("[detmc] seed source armed: -D{}={} (world seed not yet known), -D{}={}",
                PROP_SEED, PROP_SEED_VALUE, PROP_FREEZE, FREEZE_ON_START);
    }

    private DetRng() {
    }

    private static long readSeedProperty() {
        String raw = System.getProperty(PROP_SEED);
        if (raw == null || raw.isBlank()) {
            return 0L;
        }
        try {
            return Long.parseLong(raw.trim());
        } catch (NumberFormatException e) {
            LOGGER.warn("[detmc] -D{}={} is not a long; falling back to 0", PROP_SEED, raw);
            return 0L;
        }
    }

    public static boolean freezeOnStart() {
        return FREEZE_ON_START;
    }

    public static long propertySeed() {
        return PROP_SEED_VALUE;
    }

    /** The seed the live stream is keyed from. After a {@code /detmc reseed} this is the new one. */
    public static long masterSeed() {
        synchronized (LOCK) {
            return currentMaster;
        }
    }

    /** What {@link #masterSeed()} would be without a reseed: {@code -Ddetmc.seed} XOR the world seed. */
    public static long baseMasterSeed() {
        synchronized (LOCK) {
            return baseMaster;
        }
    }

    public static boolean isReseeded() {
        synchronized (LOCK) {
            return reseeded;
        }
    }

    /** Argument of the last {@code /detmc reseed}; meaningless unless {@link #isReseeded()}. */
    public static long reseedArgument() {
        synchronized (LOCK) {
            return reseedArg;
        }
    }

    public static long seedsIssued() {
        synchronized (LOCK) {
            return issued;
        }
    }

    /**
     * Called once from {@code MinecraftServer.createLevels}, before any
     * {@code Level} or {@code Entity} exists, to fold the world seed into the
     * stream. Anything seeded before this point (the {@code MinecraftServer}
     * random itself) came from the property-only stream, which is equally
     * reproducible.
     */
    public static void bindWorldSeed(long seed) {
        synchronized (LOCK) {
            if (worldSeedBound) {
                return;
            }
            long before = issued;
            worldSeed = seed;
            worldSeedBound = true;
            baseMaster = PROP_SEED_VALUE ^ seed;
            rekey(baseMaster);
            LOGGER.info("[detmc] world seed {} bound; master seed = {} ^ {} = {} ({} seed(s) had already been issued from the pre-bind stream)",
                    seed, PROP_SEED_VALUE, seed, PROP_SEED_VALUE ^ seed, before);
        }
    }

    private static void rekey(long seed) {
        currentMaster = seed;
        // splitmix64 expansion, avoids the all-zero xoroshiro state.
        long z = seed;
        stateLo = mixStafford13(z += GOLDEN_RATIO_64);
        stateHi = mixStafford13(z + GOLDEN_RATIO_64);
        if (stateLo == 0L && stateHi == 0L) {
            stateLo = SILVER_RATIO_64;
            stateHi = GOLDEN_RATIO_64;
        }
        issued = 0L;
    }

    private static long mixStafford13(long z) {
        z = (z ^ z >>> 30) * -4658895280553007687L;
        z = (z ^ z >>> 27) * -7723592293110705685L;
        return z ^ z >>> 31;
    }

    // --- per-domain streams ----------------------------------------------
    //
    // Measured 2026-09-11: with worldgen, lighting and Util.backgroundExecutor()
    // all forced onto the server thread, two fresh servers still reached the
    // shared counter at different positions. The seed trace showed run A doing
    // NoiseBasedChunkGenerator.applyCarvers where run B did
    // ChunkGenerator.applyBiomeDecoration, both on the Server thread. The cause
    // is not a thread: MinecraftServer.prepareLevels drains tasks under a
    // wall-clock budget (haveTime() compares Util.getNanos() to
    // nextTickTimeNanos), so how many chunk generation tasks are batched per
    // pass depends on how fast the host is at that instant.
    //
    // That drift is harmless for world *content*: every worldgen consumer of
    // generateUniqueSeed() throws the value away immediately
    // (applyCarvers -> setLargeFeatureSeed, applyBiomeDecoration and
    // spawnOriginalMobs -> setDecorationSeed, all re-keyed from the world seed
    // and the chunk coordinates). Measured: chunk contents, entity types,
    // entity positions and ENTITY_COUNTER ids were already identical.
    //
    // It is not harmless for entities, whose UUID is drawn from Entity.random.
    // So entities get their own stream, and their seed is a pure function of an
    // ordinal rather than a position in the shared sequence. Nothing any other
    // subsystem does can shift it.

    private static final long DOMAIN_ENTITY = 0x656E74697479L; // "entity"
    /** Used only by {@code /detmc reseed} when it re-keys an already-constructed entity. */
    public static final long DOMAIN_ENTITY_LIVE = 0x656E746C697665L; // "entlive"
    /** {@code Level.random}. */
    public static final long DOMAIN_LEVEL = 0x6C6576656CL; // "level"
    /** {@code Level.randValue}, the int LCG behind {@code getBlockRandomPos}. */
    public static final long DOMAIN_RANDTICK = 0x72616E64746BL; // "randtk"
    private static long entityOrdinal;

    /** Seed for the n-th entity constructed, independent of the shared counter. */
    public static long entitySeed() {
        synchronized (LOCK) {
            return derive(currentMaster, DOMAIN_ENTITY, entityOrdinal++);
        }
    }

    /**
     * Order-independent seed for a domain and an ordinal, keyed on the live master.
     * Used to re-key already-constructed {@code RandomSource}s at reseed time, where
     * iteration order is a hash order and so cannot be used as the ordinal.
     */
    public static long derivedSeed(long domain, long ordinal) {
        synchronized (LOCK) {
            return derive(currentMaster, domain, ordinal);
        }
    }

    private static long derive(long master, long domain, long ordinal) {
        long key = (master ^ domain) + ordinal * GOLDEN_RATIO_64;
        return mixStafford13(mixStafford13(key));
    }

    /**
     * Re-key the master stream from {@code newSeed} at this instant. The new master is
     * {@code newSeed ^ worldSeed}, i.e. exactly the master a server launched with
     * {@code -Ddetmc.seed=newSeed} on this world would have had, so a branch is named by
     * one number and nothing else.
     *
     * <p>Resets the shared xoroshiro state and {@code issued} (both are "since the last
     * re-key" by definition). Deliberately does <em>not</em> reset {@code entityOrdinal},
     * the world seed binding, the gametime or anything in the world.
     *
     * <p>Re-keying only changes seeds handed out <em>after</em> this call. Every
     * {@code RandomSource} that already exists keeps drawing from where it was, so
     * {@code DetCommands} re-keys the live ones ({@code Level.random}, {@code Level.randValue},
     * every loaded {@code Entity.random}) right after calling this.
     *
     * @return the new master seed
     */
    public static long reseed(long newSeed) {
        synchronized (LOCK) {
            long before = currentMaster;
            rekey(newSeed ^ worldSeed);
            reseedArg = newSeed;
            reseeded = true;
            LOGGER.info("[detmc] reseed {}: master {} -> {} (world seed {}); issued reset to 0, entityOrdinal kept at {}",
                    newSeed, before, currentMaster, worldSeed, entityOrdinal);
            return currentMaster;
        }
    }

    public static long entitiesSeeded() {
        synchronized (LOCK) {
            return entityOrdinal;
        }
    }

    /** xoroshiro128++ */
    public static long nextSeed() {
        synchronized (LOCK) {
            long lo = stateLo;
            long hi = stateHi;
            long result = Long.rotateLeft(lo + hi, 17) + lo;
            hi ^= lo;
            stateLo = Long.rotateLeft(lo, 49) ^ hi ^ (hi << 21);
            stateHi = Long.rotateLeft(hi, 28);
            issued++;
            if (TRACE_SEEDS) {
                LOGGER.info("[detmc-seed] {} thread={} at={}", issued, Thread.currentThread().getName(), caller());
            }
            return result;
        }
    }

    /** First three frames outside the mod, for {@code -Ddetmc.traceSeeds}. */
    private static String caller() {
        return StackWalker.getInstance()
                .walk(frames -> frames
                        .filter(f -> !f.getClassName().startsWith("dev.detmc"))
                        .limit(3)
                        .map(f -> f.getClassName() + "." + f.getMethodName() + ":" + f.getLineNumber())
                        .reduce((a, b) -> a + "<" + b)
                        .orElse("?"));
    }

    // --- restart persistence ---------------------------------------------
    //
    // What is and is not carried across a restart, measured rather than assumed:
    //
    //   carried here      the shared xoroshiro state (stateLo/stateHi), the number of
    //                     seeds issued, and entityOrdinal. Every future allocation
    //                     therefore continues the uninterrupted timeline's sequence.
    //   NOT carried       the draw position of any RandomSource that already exists:
    //                     Level.random (random ticks), every Entity.random, every
    //                     mob's goal/brain randoms. Vanilla NBT does not serialise
    //                     RandomSource state at all, so a reloaded mob starts a fresh
    //                     stream from its seed whatever we do here.
    //
    // So this makes two resumed runs agree with each other, and makes the seeds they
    // hand out agree with the uninterrupted run; it cannot by itself make a resumed
    // run agree with an uninterrupted one.

    public static boolean persistEnabled() {
        return PERSIST;
    }

    /** Write the stream position into {@code <worldDir>/detmc-rng.properties}. */
    public static void persistState(Path worldDir) {
        if (!PERSIST || worldDir == null) {
            return;
        }
        Properties props = new Properties();
        synchronized (LOCK) {
            // masterSeed is the LIVE key, which after a /detmc reseed is not
            // PROP_SEED_VALUE ^ worldSeed any more. baseMaster carries the launch-time
            // value so restoreState can still check the file belongs to this world.
            props.setProperty("masterSeed", Long.toString(currentMaster));
            props.setProperty("baseMaster", Long.toString(baseMaster));
            props.setProperty("reseeded", Boolean.toString(reseeded));
            props.setProperty("reseedArg", Long.toString(reseedArg));
            props.setProperty("stateLo", Long.toString(stateLo));
            props.setProperty("stateHi", Long.toString(stateHi));
            props.setProperty("issued", Long.toString(issued));
            props.setProperty("entityOrdinal", Long.toString(entityOrdinal));
        }
        Path target = worldDir.resolve(STATE_FILE);
        Path tmp = worldDir.resolve(STATE_FILE + ".tmp");
        try {
            Files.createDirectories(worldDir);
            try (var out = Files.newOutputStream(tmp)) {
                props.store(out, "detmc RNG stream position; delete to restart the stream");
            }
            Files.move(tmp, target, StandardCopyOption.REPLACE_EXISTING);
        } catch (IOException e) {
            LOGGER.warn("[detmc] could not write {}: {}", target, e.toString());
        }
    }

    /**
     * Restore the stream position from the sidecar, if one is there. Must run after
     * {@code bindWorldSeed} (which rekeys) and before any entity is constructed.
     */
    public static void restoreState(Path worldDir) {
        if (!PERSIST || worldDir == null) {
            return;
        }
        Path source = worldDir.resolve(STATE_FILE);
        if (!Files.isRegularFile(source)) {
            LOGGER.info("[detmc] no {} in {}; the stream starts from the master seed", STATE_FILE, worldDir);
            return;
        }
        Properties props = new Properties();
        try (var in = Files.newInputStream(source)) {
            props.load(in);
        } catch (IOException e) {
            LOGGER.warn("[detmc] could not read {}: {}", source, e.toString());
            return;
        }
        try {
            long savedMaster = Long.parseLong(props.getProperty("masterSeed"));
            // Files written before the reseed command have no baseMaster; there masterSeed
            // was the launch-time value, so it doubles as one.
            long savedBase = Long.parseLong(props.getProperty("baseMaster", Long.toString(savedMaster)));
            boolean savedReseeded = Boolean.parseBoolean(props.getProperty("reseeded", "false"));
            long savedReseedArg = Long.parseLong(props.getProperty("reseedArg", "0"));
            long lo = Long.parseLong(props.getProperty("stateLo"));
            long hi = Long.parseLong(props.getProperty("stateHi"));
            long iss = Long.parseLong(props.getProperty("issued"));
            long ord = Long.parseLong(props.getProperty("entityOrdinal"));
            synchronized (LOCK) {
                long expected = PROP_SEED_VALUE ^ worldSeed;
                if (savedBase != expected) {
                    LOGGER.warn("[detmc] {} was written by master seed {} but this server is {}; ignoring it",
                            STATE_FILE, savedBase, expected);
                    return;
                }
                if (lo == 0L && hi == 0L) {
                    LOGGER.warn("[detmc] {} holds an all-zero xoroshiro state; ignoring it", STATE_FILE);
                    return;
                }
                // Adopt the saved master, not the launch-time one: a save taken after a
                // /detmc reseed has to come back up on the reseeded branch.
                currentMaster = savedMaster;
                reseeded = savedReseeded;
                reseedArg = savedReseedArg;
                stateLo = lo;
                stateHi = hi;
                issued = iss;
                entityOrdinal = ord;
            }
            LOGGER.info("[detmc] restored RNG stream from {}: master={} (reseeded={} arg={}) issued={} entityOrdinal={}",
                    source, savedMaster, savedReseeded, savedReseedArg, iss, ord);
        } catch (NumberFormatException | NullPointerException e) {
            LOGGER.warn("[detmc] {} is malformed ({}); ignoring it", source, e.toString());
        }
    }

    // --- drop-in replacements for the vanilla factories -------------------

    /** Replaces {@code RandomSource.create()}. */
    public static RandomSource create() {
        return RandomSource.create(nextSeed());
    }

    /** Replaces {@code RandomSource.createThreadSafe()}. */
    public static RandomSource createThreadSafe() {
        return new ThreadSafeLegacyRandomSource(nextSeed());
    }

    /** Replaces {@code RandomSource.create()} for {@code Entity.random} only. */
    public static RandomSource createEntity() {
        return RandomSource.create(entitySeed());
    }

    /** Replaces {@code RandomSource.createThreadLocalInstance()}. */
    public static RandomSource createThreadLocalInstance() {
        return RandomSource.createThreadLocalInstance(nextSeed());
    }
}
