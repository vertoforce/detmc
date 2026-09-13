package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import net.minecraft.world.scores.Scoreboard;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 8 sweep — {@code scoreboard.dat} comes out in identity order.
 *
 * <p>{@code PlayerScores.scores} is a {@code Reference2ObjectOpenHashMap<Objective, Score>}
 * and {@code Objective} has no {@code hashCode} override, so the list
 * {@code packPlayerScores} produces — and therefore the bytes written to
 * {@code data/scoreboard.dat} — is ordered by {@code System.identityHashCode}. Two replicas
 * that played identically still write different save files. (The per-owner outer map is
 * {@code String}-keyed and was already fine.)
 *
 * <p>Same class of defect, and the same fix, as the phase 7 {@code AttributeMapMixin}:
 * sort on the way out, here by owner and then objective name. Behaviour is untouched;
 * this only makes the save comparable. Not exercised by the harness (no scoreboard), so
 * <b>not covered by the 24000-tick pair</b>.
 */
@Mixin(Scoreboard.class)
public class ScoreboardMixin {
    @Inject(method = "packPlayerScores()Ljava/util/List;", at = @At("RETURN"), cancellable = true)
    private void detmc$packScoresInNameOrder(CallbackInfoReturnable<List<Scoreboard.PackedScore>> cir) {
        List<Scoreboard.PackedScore> packed = cir.getReturnValue();
        if (!DetExecutors.syncRandom() || packed == null || packed.size() < 2) {
            return;
        }
        List<Scoreboard.PackedScore> sorted = new ArrayList<>(packed);
        sorted.sort(Comparator.comparing(Scoreboard.PackedScore::owner)
                .thenComparing(Scoreboard.PackedScore::objective));
        cir.setReturnValue(List.copyOf(sorted));
    }
}
