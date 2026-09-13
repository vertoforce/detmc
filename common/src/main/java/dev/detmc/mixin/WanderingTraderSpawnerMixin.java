package dev.detmc.mixin;

import dev.detmc.DetRng;
import net.minecraft.util.RandomSource;
import net.minecraft.world.entity.npc.wanderingtrader.WanderingTraderSpawner;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/** WanderingTraderSpawner.random, single ctor (SavedDataStorage;)V. */
@Mixin(WanderingTraderSpawner.class)
public class WanderingTraderSpawnerMixin {
    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;create()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$seed() {
        return DetRng.create();
    }
}
