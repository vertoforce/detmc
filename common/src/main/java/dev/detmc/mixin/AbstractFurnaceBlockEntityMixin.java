package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import it.unimi.dsi.fastutil.objects.ObjectIterator;
import it.unimi.dsi.fastutil.objects.ObjectIterators;
import it.unimi.dsi.fastutil.objects.Reference2IntMap;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — a furnace pays out its experience in identity order, drawing the level
 * RNG once per recipe.
 *
 * <p>{@code recipesUsed} is a {@code Reference2IntOpenHashMap<ResourceKey<Recipe<?>>>} and
 * {@code ResourceKey} does not override {@code hashCode}, so
 * {@code getRecipesToAwardAndPopExperience} walks it in {@code System.identityHashCode}
 * order. The loop body calls {@code createExperience}, which does
 * {@code level.getRandom().nextFloat() < xpFraction} — one draw from the <em>level</em>
 * stream per recipe. With two or more distinct recipes in one furnace the number of orbs
 * and the level RNG's stream position both depend on JVM allocation order, and the level
 * stream is what drives random ticks for the whole world.
 *
 * <p>Sorted by recipe key. Not reachable on the harness world (no furnace is ever used),
 * so this is reasoned from the source and is <b>not covered by the 24000-tick pair</b>.
 */
@Mixin(AbstractFurnaceBlockEntity.class)
public class AbstractFurnaceBlockEntityMixin {
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Redirect(
            method = "getRecipesToAwardAndPopExperience(Lnet/minecraft/server/level/ServerLevel;Lnet/minecraft/world/phys/Vec3;)Ljava/util/List;",
            at = @At(value = "INVOKE", target = "Lit/unimi/dsi/fastutil/objects/Reference2IntMap$FastEntrySet;iterator()Lit/unimi/dsi/fastutil/objects/ObjectIterator;")
    )
    private ObjectIterator detmc$awardInRecipeKeyOrder(Reference2IntMap.FastEntrySet entries) {
        if (!DetExecutors.syncRandom() || entries.size() < 2) {
            return entries.iterator();
        }
        List<Reference2IntMap.Entry> sorted = new ArrayList<>(entries);
        sorted.sort(Comparator.comparing(e -> ((ResourceKey<?>) e.getKey()).identifier().toString()));
        return ObjectIterators.asObjectIterator(sorted.iterator());
    }
}
