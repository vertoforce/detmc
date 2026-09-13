package dev.detmc.mixin;

import dev.detmc.DetRng;
import dev.detmc.DetTrace;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.level.Level;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import net.minecraft.util.RandomSource;
import net.minecraft.world.entity.Entity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * {@code Entity.random} (field init, single ctor {@code (EntityType;Level;)V}).
 * Entity UUIDs derive from it, so they follow.
 *
 * <p>Phase 2: this comes off its own stream, not the shared counter. Entities are
 * the one consumer whose seed value survives, and the shared counter's position
 * still drifts between runs because chunk generation is batched under a
 * wall-clock budget. See the per-domain stream note in {@code DetRng}.
 */
@Mixin(Entity.class)
public class EntityMixin {
    @Redirect(
            method = "<init>",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;create()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$seed() {
        return DetRng.createEntity();
    }

    /** Phase 7 probe: construction order, which is what assigns the id and the RNG ordinal. */
    @Inject(method = "<init>", at = @At("RETURN"))
    private void detmc$traceConstruction(EntityType<?> type, Level level, CallbackInfo ci) {
        DetTrace.entityConstructed((Entity) (Object) this);
    }
}
