package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.Comparator;
import java.util.List;
import net.minecraft.world.entity.ai.attributes.AttributeInstance;
import net.minecraft.world.entity.ai.attributes.AttributeMap;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 7 — the saved {@code attributes} list came out in identity-hash order.
 *
 * <p>Measured 2026-09-12 (test/phase7/G2 vs G3, digests identical at every gametime, RNG
 * states identical): `entities/*.mca` still differed in 4 of 37 chunks, and a canonical NBT
 * diff (`test/nbt-canon.py`) put every difference inside {@code attributes:[...]}, the same
 * entries in a different order; with the lists sorted the two files were identical.
 * {@code AttributeMap.attributes} is an {@code Object2ObjectOpenHashMap<Holder<Attribute>, ...>}
 * and {@code Holder.Reference} does not override {@code hashCode}, so {@code pack()} walks
 * it in per-JVM identity-hash order. Sorting the packed list by registered name makes the
 * save format a function of the state alone. Gameplay never reads this order.
 */
@Mixin(AttributeMap.class)
public class AttributeMapMixin {
    @Inject(method = "pack()Ljava/util/List;", at = @At("RETURN"))
    private void detmc$packInNameOrder(CallbackInfoReturnable<List<AttributeInstance.Packed>> cir) {
        if (!DetExecutors.syncChunks()) {
            return;
        }
        cir.getReturnValue().sort(Comparator.comparing(packed -> packed.attribute().getRegisteredName()));
    }
}
