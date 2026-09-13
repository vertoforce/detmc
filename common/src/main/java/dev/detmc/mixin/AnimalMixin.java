package dev.detmc.mixin;

import dev.detmc.DetTrace;
import net.minecraft.core.BlockPos;
import net.minecraft.world.entity.animal.Animal;
import net.minecraft.world.level.BlockAndLightGetter;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 3 diagnostic, off unless {@code -Ddetmc.traceSpawnLight=true}.
 *
 * <p>{@code Animal.isBrightEnoughToSpawn} is {@code getRawBrightness(pos, 0) > 8}
 * and is the only light-dependent term in {@code Animal.checkAnimalSpawnRules}, so
 * it is the single read that decides whether a worldgen animal survives its spawn
 * check. Logging it in call order, tagged with the chunk whose SPAWN step is on the
 * stack, turns "19 animals here, 22 there" into "this position read 9 in one run
 * and 4 in the other".
 *
 * <p>The brightness is re-read rather than captured from the frame. Same thread,
 * no block or light mutation between the original read and this one.
 */
@Mixin(Animal.class)
public abstract class AnimalMixin {
    @Inject(
            method = "isBrightEnoughToSpawn(Lnet/minecraft/world/level/BlockAndLightGetter;Lnet/minecraft/core/BlockPos;)Z",
            at = @At("RETURN")
    )
    private static void detmc$traceSpawnLight(
            BlockAndLightGetter level, BlockPos pos, CallbackInfoReturnable<Boolean> cir) {
        if (!DetTrace.tracingSpawnLight()) {
            return;
        }
        DetTrace.spawnLightCheck(pos.getX(), pos.getY(), pos.getZ(),
                level.getRawBrightness(pos, 0), cir.getReturnValueZ());
    }
}
