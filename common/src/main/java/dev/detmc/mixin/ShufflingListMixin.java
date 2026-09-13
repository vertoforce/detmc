package dev.detmc.mixin;

import dev.detmc.DetRng;
import net.minecraft.util.RandomSource;
import net.minecraft.world.entity.ai.behavior.ShufflingList;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/** ShufflingList.random. Both ctors ()V and (List;)V initialise the field; neither chains. */
@Mixin(ShufflingList.class)
public class ShufflingListMixin {
    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;create()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$seed() {
        return DetRng.create();
    }
}
