package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetTrace;
import net.minecraft.util.Util;
import net.minecraft.world.level.block.entity.StructureBlockEntity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — a structure block with seed 0 seeds its RNG from the wall clock.
 *
 * <p>{@code StructureBlockEntity.createRandom(long)} is
 * {@code seed == 0 ? RandomSource.create(Util.getMillis()) : RandomSource.create(seed)},
 * and 0 is the default seed of a freshly placed structure block. That random decides which
 * blocks survive the {@code integrity} setting on every load, so two replicas placing the
 * same structure get different blocks. {@code RandomSource.create(long)} is the seeded
 * factory, so {@code RandomSupportMixin} does not cover this either.
 *
 * <p>Replaced with the world clock ({@code gameTime * 50}), the same substitution
 * {@code ChunkMapMixin} makes for the eager-save schedule. Server-reachable through
 * {@code /setblock} plus a redstone pulse; not on the harness's path, so <b>not covered by
 * the 24000-tick pair</b>.
 */
@Mixin(StructureBlockEntity.class)
public class StructureBlockEntityMixin {
    @Redirect(
            method = "createRandom(J)Lnet/minecraft/util/RandomSource;",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/Util;getMillis()J")
    )
    private static long detmc$structureSeedFromGameTime() {
        return DetExecutors.syncRandom() ? DetTrace.gameTimeMillis() : Util.getMillis();
    }
}
