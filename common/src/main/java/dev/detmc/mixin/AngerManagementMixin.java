package dev.detmc.mixin;

import dev.detmc.DetRng;
import net.minecraft.util.RandomSource;
import net.minecraft.world.entity.monster.warden.AngerManagement;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/** AngerManagement.conversionDelay = Mth.randomBetweenInclusive(RandomSource.createThreadLocalInstance(), 0, 2). */
@Mixin(AngerManagement.class)
public class AngerManagementMixin {
    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;createThreadLocalInstance()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$seed() {
        return DetRng.createThreadLocalInstance();
    }
}
