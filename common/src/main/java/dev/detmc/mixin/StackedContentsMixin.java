package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.Comparator;
import java.util.List;
import net.minecraft.core.Holder;
import net.minecraft.world.entity.player.StackedContents;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 8 sweep — which stack the recipe book spends is identity-ordered.
 *
 * <p>{@code StackedContents.amounts} is a {@code Reference2IntOpenHashMap<Holder<Item>>}
 * (no {@code hashCode} on {@code Holder.Reference}), and
 * {@code getUniqueAvailableIngredientItems} walks it to build the candidate list that
 * {@code RecipePicker.tryAssigningNewItem} then scans by index, taking the <em>first</em>
 * viable assignment. When two different stacks both satisfy an ingredient, the winner is
 * decided by JVM allocation order, and {@code ServerPlaceRecipe} physically moves that
 * item into the crafting grid.
 *
 * <p>Sorted by registered item name. Reachable whenever a player clicks a recipe in the
 * book; the harness has no players, so this is <b>not covered by the 24000-tick pair</b>.
 */
@Mixin(StackedContents.class)
public class StackedContentsMixin {
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Inject(method = "getUniqueAvailableIngredientItems(Ljava/lang/Iterable;)Ljava/util/List;", at = @At("RETURN"))
    private void detmc$candidatesInNameOrder(Iterable ingredients, CallbackInfoReturnable<List> cir) {
        List result = cir.getReturnValue();
        if (!DetExecutors.syncRandom() || result == null || result.size() < 2) {
            return;
        }
        // The list vanilla builds here is a fresh ArrayList, so sorting it in place is safe.
        result.sort(Comparator.comparing(
                o -> o instanceof Holder<?> h ? h.getRegisteredName() : String.valueOf(o)));
    }
}
