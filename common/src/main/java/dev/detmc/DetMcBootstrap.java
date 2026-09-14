package dev.detmc;

import net.minecraft.server.MinecraftServer;

/** Loader-agnostic hooks driven from the mixins. */
public final class DetMcBootstrap {
    private static boolean freezeApplied;

    private DetMcBootstrap() {
    }

    /**
     * Invoked from the head of the very first {@code MinecraftServer.tickServer}
     * call, i.e. before any level has ticked. Freezing here rather than from a
     * loader lifecycle event keeps the common module free of Fabric API.
     */
    public static void onFirstServerTick(MinecraftServer server) {
        if (freezeApplied) {
            return;
        }
        freezeApplied = true;
        DetRng.LOGGER.info("[detmc] first server tick: master seed {} ({} seeds issued during startup)",
                DetRng.masterSeed(), DetRng.seedsIssued());
        if (DetRng.freezeOnStart()) {
            server.tickRateManager().setFrozen(true);
            DetRng.LOGGER.info("[detmc] ticks frozen before tick 1 (-D{}=true)", DetRng.PROP_FREEZE);
        }
        verifyLazyMixinTargets();
    }

    /**
     * Phase 8 — load every mixin target that the harness world never touches.
     *
     * <p>Mixin applies a configuration when its target class is first loaded, so an
     * injection whose target method or call site is wrong does not fail at startup: it
     * fails hours later, the first time something enchanted is swung or a villager looks
     * for a job. Nine of the phase 8 sweep's targets are in that category. Touching the
     * classes here turns that into a startup crash, which is the failure mode a
     * determinism harness can act on.
     *
     * <p>{@code -Ddetmc.verifyMixins=false} skips it.
     */
    private static void verifyLazyMixinTargets() {
        if ("false".equalsIgnoreCase(System.getProperty("detmc.verifyMixins", "true"))) {
            return;
        }
        String[] targets = {
            "net.minecraft.world.entity.ai.behavior.InsideBrownianWalk",
            "net.minecraft.world.entity.ai.behavior.LongJumpToRandomPos",
            "net.minecraft.commands.arguments.selector.EntitySelectorParser",
            "net.minecraft.server.level.PlayerSpawnFinder",
            "net.minecraft.server.commands.SpreadPlayersCommand",
            "net.minecraft.world.level.block.entity.StructureBlockEntity",
            "net.minecraft.world.level.levelgen.structure.templatesystem.StructurePlaceSettings",
            "net.minecraft.world.Stopwatches",
            "net.minecraft.world.item.enchantment.ItemEnchantments",
            "net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity",
            "net.minecraft.world.entity.ai.behavior.AcquirePoi",
            "net.minecraft.world.entity.ai.village.poi.PoiSection",
            "net.minecraft.world.entity.player.StackedContents",
            "net.minecraft.world.scores.Scoreboard",
            "net.minecraft.stats.ServerRecipeBook",
        };
        ClassLoader loader = DetMcBootstrap.class.getClassLoader();
        for (String name : targets) {
            try {
                Class.forName(name, true, loader);
            } catch (ClassNotFoundException e) {
                throw new IllegalStateException("[detmc] mixin target missing: " + name, e);
            }
        }
        DetRng.LOGGER.info("[detmc] {} lazy mixin targets loaded and applied", targets.length);
    }
}
