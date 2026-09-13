package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import net.minecraft.core.Holder;
import net.minecraft.world.entity.ai.village.poi.PoiSection;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — which point of interest a mob claims depends on JVM allocation order.
 *
 * <p>{@code PoiSection.byType} is a {@code HashMap<Holder<PoiType>, Set<PoiRecord>>} and
 * {@code Holder.Reference} has no {@code hashCode} override, so {@code getRecords} streams
 * the types in identity order. The inner {@code Set<PoiRecord>} is safe ({@code PoiRecord}
 * hashes on its position), so only the *type* grouping moves — which is enough: for a
 * multi-type predicate such as {@code VillagerProfession.ALL_ACQUIRABLE_JOBS} or
 * {@code PoiTypeTags.VILLAGE}, {@code PoiManager.findAllClosestFirstWithType} sorts by
 * distance <em>stably</em>, so ties keep the encounter order, and {@code AcquirePoi} then
 * takes the first five candidates. {@code PoiManager.take} ({@code findFirst}) and
 * {@code getRandom} ({@code Util.toShuffledList} over this stream) inherit the same order.
 *
 * <p>Sorted by registered POI type name. Reachable on any world with villagers or nether
 * portals; the harness world has POIs but never queries them, so this is <b>not covered by
 * the 24000-tick pair</b>.
 */
@Mixin(PoiSection.class)
public class PoiSectionMixin {
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Redirect(
            method = "getRecords(Ljava/util/function/Predicate;Lnet/minecraft/world/entity/ai/village/poi/PoiManager$Occupancy;)Ljava/util/stream/Stream;",
            at = @At(value = "INVOKE", target = "Ljava/util/Map;entrySet()Ljava/util/Set;")
    )
    private Set detmc$typesInNameOrder(Map byType) {
        Set entries = byType.entrySet();
        if (!DetExecutors.syncRandom() || entries.size() < 2) {
            return entries;
        }
        List<Map.Entry> sorted = new ArrayList<>(entries);
        sorted.sort(Comparator.comparing(e -> ((Holder<?>) e.getKey()).getRegisteredName()));
        return new LinkedHashSet<>(sorted);
    }
}
