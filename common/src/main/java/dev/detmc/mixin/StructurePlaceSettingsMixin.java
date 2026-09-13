package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetTrace;
import net.minecraft.util.Util;
import net.minecraft.world.level.levelgen.structure.templatesystem.StructurePlaceSettings;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — template placement falls back to the wall clock when it has no position.
 *
 * <p>{@code StructurePlaceSettings.getRandom(BlockPos)} returns the explicitly set random
 * if there is one, else {@code RandomSource.create(Mth.getSeed(pos))} — or, when
 * {@code pos} is null, {@code RandomSource.create(Util.getMillis())}. The position-keyed
 * branch is what worldgen uses and it is already deterministic; the null branch is the one
 * that leaks the clock, and it feeds the same block-integrity and loot-table decisions.
 *
 * <p>Replaced with the world clock ({@code gameTime * 50}). Not on the harness's path, so
 * <b>not covered by the 24000-tick pair</b>.
 */
@Mixin(StructurePlaceSettings.class)
public class StructurePlaceSettingsMixin {
    @Redirect(
            method = "getRandom(Lnet/minecraft/core/BlockPos;)Lnet/minecraft/util/RandomSource;",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/Util;getMillis()J")
    )
    private long detmc$templateSeedFromGameTime() {
        return DetExecutors.syncRandom() ? DetTrace.gameTimeMillis() : Util.getMillis();
    }
}
