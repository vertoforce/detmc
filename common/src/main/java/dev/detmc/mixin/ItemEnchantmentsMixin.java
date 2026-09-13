package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import it.unimi.dsi.fastutil.objects.Object2IntMap;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import net.minecraft.core.Holder;
import net.minecraft.world.item.enchantment.ItemEnchantments;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 8 sweep — the widest identity-ordered iteration left on the server.
 *
 * <p>{@code ItemEnchantments.enchantments} is an
 * {@code Object2IntOpenHashMap<Holder<Enchantment>>} and {@code Holder.Reference} does not
 * override {@code hashCode}, so {@code entrySet()} hands out its entries in
 * {@code System.identityHashCode} order. Both {@code EnchantmentHelper.runIterationOnItem}
 * overloads walk exactly that set, which puts every enchantment effect on an item — damage
 * bonuses, durability, projectile behaviour, Mending — behind a per-JVM order. Two of those
 * call sites thread a {@code RandomSource} through the loop
 * ({@code modifyTridentSpinAttackStrength}, and the Mending target chosen by
 * {@code Util.getRandomSafe(items, source.getRandom())}), so the order does not just decide
 * an outcome, it moves the RNG stream position.
 *
 * <p>Sorted by registered enchantment name, which is a property of the world's registries
 * and identical in every replica. Sorting happens in {@code entrySet()}, the single method
 * both iteration sites go through; {@code getLevel}, {@code size} and the codecs are
 * untouched, and the returned set is still unmodifiable.
 *
 * <p>Not reachable on the harness world (nothing there is enchanted), so this is reasoned
 * from the source and is <b>not covered by the 24000-tick pair</b>.
 */
@Mixin(ItemEnchantments.class)
public class ItemEnchantmentsMixin {
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Inject(method = "entrySet()Ljava/util/Set;", at = @At("RETURN"), cancellable = true)
    private void detmc$entrySetInNameOrder(CallbackInfoReturnable<Set<Object2IntMap.Entry<Holder<?>>>> cir) {
        Set<Object2IntMap.Entry<Holder<?>>> entries = cir.getReturnValue();
        if (!DetExecutors.syncRandom() || entries == null || entries.size() < 2) {
            return;
        }
        List<Object2IntMap.Entry<Holder<?>>> sorted = new ArrayList<>(entries);
        sorted.sort(Comparator.comparing(e -> ((Holder) e.getKey()).getRegisteredName()));
        cir.setReturnValue(Collections.unmodifiableSet(new LinkedHashSet<>(sorted)));
    }
}
