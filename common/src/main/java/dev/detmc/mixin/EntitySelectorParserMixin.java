package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.Collections;
import java.util.List;
import net.minecraft.commands.arguments.selector.EntitySelectorParser;
import net.minecraft.util.Util;
import net.minecraft.world.entity.Entity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — {@code @e[sort=random]}, the third and last
 * {@code Collections.shuffle(List)} in the server jar.
 *
 * <p>{@code EntitySelectorParser.ORDER_RANDOM} is
 * {@code (pos, entities) -> Collections.shuffle(entities)}, i.e. {@code new
 * java.util.Random()} seeded from {@code System.nanoTime()}. Any command that selects
 * with {@code sort=random} — the usual shape is {@code @e[sort=random,limit=1]} — picks a
 * different entity in two runs of the same world, and everything that command then does
 * is off. This is the one site in the sweep a datapack is most likely to hit.
 *
 * <p>Routed through the level's {@code RandomSource} (taken from the first selected
 * entity, the only level handle the lambda has), which detmc seeds in {@code LevelMixin}.
 * That consumes draws from the level stream, so a world that uses {@code sort=random} is
 * deterministic but not vanilla-identical afterwards.
 *
 * <p>Limit worth stating: this fixes the <em>shuffle</em>. The list handed to it comes
 * from the level's entity iteration, so if that order were itself unstable, a
 * deterministic shuffle of an unstable list would still be unstable. The per-tick entity
 * digest is what tests that, and it has been identical across 24000-tick pairs.
 *
 * <p>Target is the synthetic {@code lambda$static$6(Vec3, List)}, confirmed by
 * {@code javap -c} on the 26.2 server jar as the only method in the class that calls
 * {@code Collections.shuffle}.
 */
@Mixin(EntitySelectorParser.class)
public class EntitySelectorParserMixin {
    @SuppressWarnings({"unchecked", "rawtypes"})
    @Redirect(
            method = "lambda$static$6(Lnet/minecraft/world/phys/Vec3;Ljava/util/List;)V",
            at = @At(value = "INVOKE", target = "Ljava/util/Collections;shuffle(Ljava/util/List;)V")
    )
    private static void detmc$shuffleWithLevelRandom(List<?> entities) {
        if (!DetExecutors.syncRandom() || entities.size() < 2 || !(entities.get(0) instanceof Entity first)) {
            Collections.shuffle(entities);
            return;
        }
        Util.shuffle((List) entities, first.level().getRandom());
    }
}
