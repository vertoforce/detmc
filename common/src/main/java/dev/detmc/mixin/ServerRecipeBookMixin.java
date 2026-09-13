package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.List;
import net.minecraft.resources.ResourceKey;
import net.minecraft.stats.ServerRecipeBook;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — the player's recipe book is written in identity order.
 *
 * <p>{@code ServerRecipeBook.known} and {@code .highlight} are
 * {@code Sets.newIdentityHashSet()} of {@code ResourceKey<Recipe<?>>}, and {@code pack()}
 * turns each into a {@code List.copyOf(...)} that is codec'd into the player's {@code .dat}.
 * The list order, and so the file's bytes, is {@code System.identityHashCode} order.
 *
 * <p>Both {@code List.copyOf} calls in {@code pack()} are redirected, so both lists come
 * out sorted by recipe key. Behaviour is untouched — the sets are only ever queried with
 * {@code contains} — and only the on-disk order changes. Not exercised by the harness (no
 * players), so <b>not covered by the 24000-tick pair</b>.
 */
@Mixin(ServerRecipeBook.class)
public class ServerRecipeBookMixin {
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Redirect(
            method = "pack()Lnet/minecraft/stats/ServerRecipeBook$Packed;",
            at = @At(value = "INVOKE", target = "Ljava/util/List;copyOf(Ljava/util/Collection;)Ljava/util/List;")
    )
    private List detmc$copyInRecipeKeyOrder(Collection keys) {
        if (!DetExecutors.syncRandom() || keys.size() < 2) {
            return List.copyOf(keys);
        }
        List sorted = new ArrayList(keys);
        sorted.sort(Comparator.comparing(k -> ((ResourceKey<?>) k).identifier().toString()));
        return List.copyOf(sorted);
    }
}
