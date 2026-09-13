package dev.detmc.mixin;

import dev.detmc.DetRng;
import dev.detmc.DetTrace;
import net.minecraft.util.RandomSource;
import net.minecraft.world.level.Level;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.block.state.BlockState;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * All three seeding variants in the single {@code Level} constructor
 * {@code (WritableLevelData;ResourceKey;RegistryAccess;Holder;ZZJI)V}:
 * <ul>
 *   <li>{@code randValue} - {@code createThreadLocalInstance().nextInt()}, used by
 *       {@code getBlockRandomPos} and therefore by every random tick.</li>
 *   <li>{@code random} - {@code create()}</li>
 *   <li>{@code soundSeedGenerator} - {@code createThreadSafe()}</li>
 * </ul>
 */
@Mixin(Level.class)
public class LevelMixin {
    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;createThreadLocalInstance()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$randValue() {
        return DetRng.createThreadLocalInstance();
    }

    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;create()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$random() {
        return DetRng.create();
    }

    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;createThreadSafe()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$soundSeedGenerator() {
        return DetRng.createThreadSafe();
    }

    /**
     * Phase 4 probe, off unless {@code -Ddetmc.traceIo=true}. Dates every block change to
     * a tick. The entity digest is blind to these: an enderman that moves a different
     * block leaves every entity field identical, so the two runs stay bit-identical until
     * a mob later interacts with the difference.
     */
    @Inject(
            method = "setBlock(Lnet/minecraft/core/BlockPos;Lnet/minecraft/world/level/block/state/BlockState;II)Z",
            at = @At("HEAD")
    )
    private void detmc$traceSetBlock(BlockPos pos, BlockState blockState, int updateFlags, int updateLimit,
                                     CallbackInfoReturnable<Boolean> cir) {
        DetTrace.blockSet(pos, "old", blockState);
    }
}
