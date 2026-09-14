package dev.detmc.mixin;

import dev.detmc.DetDrawCounter;
import dev.detmc.DetTrace;
import java.util.concurrent.atomic.AtomicLong;
import net.minecraft.world.level.levelgen.LegacyRandomSource;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.Unique;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/** Phase 7 diagnostic: count draws and expose the state of every vanilla legacy RNG. */
@Mixin(LegacyRandomSource.class)
public class LegacyRandomSourceMixin implements DetDrawCounter {
    @Shadow
    @Final
    private AtomicLong seed;

    @Unique
    private long detmc$draws;

    @Inject(method = "next(I)I", at = @At("HEAD"))
    private void detmc$countDraw(int bits, CallbackInfoReturnable<Integer> cir) {
        this.detmc$draws++;
        DetTrace.drawSite(this, this.detmc$draws);
    }

    @Override
    public long detmc$draws() {
        return this.detmc$draws;
    }

    @Override
    public long detmc$state() {
        return this.seed.get();
    }
}
