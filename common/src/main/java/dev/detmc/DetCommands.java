package dev.detmc;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.LongArgumentType;
import dev.detmc.mixin.LevelRandValueAccessor;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.entity.Entity;

/**
 * {@code /detmc} — the scenario runner's handle on the RNG.
 *
 * <pre>
 *   /detmc rng             print masterSeed, baseMaster, issued, entityOrdinal, gametime
 *   /detmc reseed &lt;long&gt;   branch the run onto a new RNG schedule, here and now
 * </pre>
 *
 * <p>Both need permission level 2 ({@code Commands.LEVEL_GAMEMASTERS}). The rcon console
 * source is {@code LevelBasedPermissionSet.OWNER}, so rcon passes.
 *
 * <p><b>What reseed touches.</b> {@link DetRng#reseed(long)} re-keys the master stream, which
 * only changes seeds handed out from that point on. Everything already constructed holds a
 * {@code RandomSource} of its own and would keep drawing from its old position, so two runs
 * that reseed to different values would still move their mobs identically for a long time.
 * So this command also re-keys the live ones:
 *
 * <ul>
 *   <li>{@code Level.random} for every {@code ServerLevel} — the level-wide draws.</li>
 *   <li>{@code Level.randValue} for every {@code ServerLevel} — the LCG that picks random-tick
 *       block positions, via {@link LevelRandValueAccessor}.</li>
 *   <li>{@code Entity.random} for every loaded entity in every level — mob goals, brains and
 *       movement all draw from it.</li>
 * </ul>
 *
 * <p>Each of those is keyed from {@code (new master, domain, ordinal)} where the ordinal is the
 * entity id or the dimension name's hash, never the iteration position: entity iteration is a
 * hash order, so an order-dependent derivation would not be reproducible.
 *
 * <p>Not re-keyed, and known: a mob's {@code ShufflingList} randoms, {@code Raid.random},
 * {@code WanderingTraderSpawner.random}, {@code MinecraftServer.random}, and the draw position
 * of any {@code RandomSource} handed out before the reseed that is not reachable from a level
 * or an entity. All of those were seeded from the pre-reseed master and keep their old stream.
 */
public final class DetCommands {
    /**
     * Set by the loader entrypoint. Command registration itself happens in
     * {@code CommandsMixin}, because Fabric API — and therefore
     * {@code CommandRegistrationCallback} — is not on these servers' runtime classpath.
     */
    private static volatile boolean armed;

    private DetCommands() {
    }

    /** Called from the loader entrypoint ({@code DetMcFabric.onInitialize}). */
    public static void arm() {
        armed = true;
    }

    public static boolean isArmed() {
        return armed;
    }

    public static void register(CommandDispatcher<CommandSourceStack> dispatcher) {
        dispatcher.register(
                Commands.literal("detmc")
                        .requires(Commands.hasPermission(Commands.LEVEL_GAMEMASTERS))
                        .then(Commands.literal("rng")
                                .executes(ctx -> {
                                    String line = describe(ctx.getSource().getServer());
                                    ctx.getSource().sendSuccess(() -> Component.literal(line), false);
                                    DetRng.LOGGER.info("[detmc] {}", line);
                                    return 1;
                                }))
                        .then(Commands.literal("reseed")
                                .then(Commands.argument("seed", LongArgumentType.longArg())
                                        .executes(ctx -> {
                                            long seed = LongArgumentType.getLong(ctx, "seed");
                                            String line = reseed(ctx.getSource().getServer(), seed);
                                            ctx.getSource().sendSuccess(() -> Component.literal(line), true);
                                            return 1;
                                        })))
        );
        DetRng.LOGGER.info("[detmc] /detmc registered (rng, reseed; permission level 2)");
    }

    /** One line, machine-readable enough for the scenario runner to parse with a regex. */
    public static String describe(MinecraftServer server) {
        long gametime = server.overworld().getGameTime();
        return "detmc rng: masterSeed=" + DetRng.masterSeed()
                + " baseMaster=" + DetRng.baseMasterSeed()
                + " reseeded=" + DetRng.isReseeded()
                + " reseedArg=" + DetRng.reseedArgument()
                + " issued=" + DetRng.seedsIssued()
                + " entityOrdinal=" + DetRng.entitiesSeeded()
                + " gametime=" + gametime;
    }

    /**
     * Re-key the master stream and every live stream derived from it. Runs on the server
     * thread (rcon commands go through {@code MinecraftServer.executeBlocking}), so no
     * tick is in flight while the seeds are swapped.
     */
    public static String reseed(MinecraftServer server, long newSeed) {
        long master = DetRng.reseed(newSeed);
        int levelCount = 0;
        int entityCount = 0;
        for (ServerLevel level : server.getAllLevels()) {
            long dimension = level.dimension().identifier().toString().hashCode();
            level.getRandom().setSeed(DetRng.derivedSeed(DetRng.DOMAIN_LEVEL, dimension));
            ((LevelRandValueAccessor) (Object) level)
                    .detmc$setRandValue((int) DetRng.derivedSeed(DetRng.DOMAIN_RANDTICK, dimension));
            levelCount++;
            for (Entity entity : level.getAllEntities()) {
                entity.getRandom().setSeed(DetRng.derivedSeed(DetRng.DOMAIN_ENTITY_LIVE, entity.getId()));
                entityCount++;
            }
        }
        String line = "detmc reseed: arg=" + newSeed
                + " masterSeed=" + master
                + " levels=" + levelCount
                + " entities=" + entityCount
                + " gametime=" + server.overworld().getGameTime();
        DetRng.LOGGER.info("[detmc] {}", line);
        return line;
    }
}
