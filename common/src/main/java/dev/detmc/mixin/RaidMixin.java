package dev.detmc.mixin;

import dev.detmc.DetRng;
import net.minecraft.util.RandomSource;
import net.minecraft.world.entity.raid.Raid;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/** Raid.random. Both ctors initialise the field; neither chains. */
@Mixin(Raid.class)
public class RaidMixin {
    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;create()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$seed() {
        return DetRng.create();
    }
}
