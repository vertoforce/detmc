package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.LinkedHashSet;
import java.util.stream.Collector;
import java.util.stream.Collectors;
import net.minecraft.world.entity.ai.behavior.AcquirePoi;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 9 — which point of interest a villager retries next depends on JVM allocation order.
 *
 * <p>Measured 2026-09-13 on two containers resumed from the same checkpoint of the natural
 * village world (parent {@code g181n0}, reseed 548001651, 1200 ticks,
 * {@code -Ddetmc.traceIo -Ddetmc.traceDraws}): the arms are bit-identical until gametime
 * 8736205, where one takes exactly one more draw from the level {@code RandomSource} than
 * the other. The draw trace names the site:
 *
 * <pre>
 *   v0  BitRandomSource.nextInt &lt; Behavior.tryStart &lt; Brain.startEachNonRunningBehavior
 *   v1  BitRandomSource.nextInt &lt; AcquirePoi$JitteredLinearRetry.markAttempt &lt;
 *       AcquirePoi.lambda$create$5 &lt; PoiManager.lambda$findAllWithType$0
 * </pre>
 *
 * <p>Cause. {@code AcquirePoi} collects its five closest candidates with
 * {@code Collectors.toSet()}, i.e. a {@code HashSet<Pair<Holder<PoiType>, BlockPos>>}.
 * {@code Pair.hashCode()} is {@code Objects.hashCode(first, second)} and
 * {@code Holder.Reference} declares neither {@code hashCode} nor {@code equals} (checked with
 * javap on the 26.2 server jar), so the set iterates in <em>identity-hash</em> order. When
 * the batch fails to path, the behaviour walks that set and gives each POI a
 * {@code JitteredLinearRetry}, whose constructor draws {@code random.nextInt(40)} from the
 * <b>level</b> random. The number of draws is the same in both arms; which POI gets which
 * delay is not. Ticks later {@code shouldRetry} then fires for a different subset, so one arm
 * calls {@code markAttempt} once more than the other and the shared level stream is off by
 * one draw from there on. Villagers and the village iron golem read that stream for their
 * walk targets, which is why they, and nothing else, end up standing somewhere else.
 *
 * <p>Fix: collect into a {@code LinkedHashSet}, so the set keeps the encounter order of
 * {@code findAllClosestFirstWithType} — already a stable sort by squared distance over a
 * deterministic stream ({@code PoiSectionMixin} fixes the type grouping above it, and
 * {@code PoiRecord} does override {@code hashCode}). Vanilla's order is arbitrary, so
 * pinning it to closest-first changes no documented behaviour.
 *
 * <p>Reachable on any world with villagers and a POI, which is why neither
 * {@code replay-from-save} nor {@code sweep} could see it: their worlds have no village.
 */
@Mixin(AcquirePoi.class)
public class AcquirePoiMixin {
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Redirect(
            method = "lambda$create$3",
            at = @At(value = "INVOKE", target = "Ljava/util/stream/Collectors;toSet()Ljava/util/stream/Collector;")
    )
    private static Collector detmc$orderedBatch() {
        return DetExecutors.syncRandom()
                ? Collectors.toCollection(LinkedHashSet::new)
                : Collectors.toSet();
    }
}
