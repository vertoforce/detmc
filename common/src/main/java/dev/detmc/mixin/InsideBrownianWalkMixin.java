package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.Collections;
import java.util.List;
import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.util.Util;
import net.minecraft.world.entity.PathfinderMob;
import net.minecraft.world.entity.ai.behavior.InsideBrownianWalk;
import net.minecraft.world.entity.ai.behavior.declarative.MemoryAccessor;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — the second of the three {@code Collections.shuffle(List)} calls in the
 * server jar, and the same defect as the goat long jump that phase 7 measured.
 *
 * <p>{@code InsideBrownianWalk} builds the 27 block positions around the mob, shuffles
 * them and walks to the first one that is sheltered and standable.
 * {@code Collections.shuffle(List)} uses {@code new java.util.Random()}, seeded from
 * {@code System.nanoTime()}, so which of several valid neighbours a mob picks is a
 * property of the JVM's clock. Nothing detmc seeds reaches it.
 *
 * <p>Reachable on the server whenever a villager (the only vanilla user of this behaviour)
 * is indoors, i.e. every villager night. It is <em>not</em> reachable on this project's
 * test world, which has no villagers, so this fix is reasoned from the source and is
 * <b>not covered by the 24000-tick pair</b>; the pair only shows it does not regress.
 *
 * <p>The shuffle lives in a synthetic lambda ({@code lambda$create$2}, confirmed by
 * {@code javap -c} on the 26.2 server jar: the {@code Collections.shuffle} call is at
 * offset 63 of that method and nowhere else in the class), so the target is named by its
 * full descriptor and the mob is taken from the lambda's captured arguments.
 */
@Mixin(InsideBrownianWalk.class)
public class InsideBrownianWalkMixin {
    @Redirect(
            method = "lambda$create$2(Lnet/minecraft/world/entity/ai/behavior/declarative/MemoryAccessor;FLnet/minecraft/server/level/ServerLevel;Lnet/minecraft/world/entity/PathfinderMob;J)Z",
            at = @At(value = "INVOKE", target = "Ljava/util/Collections;shuffle(Ljava/util/List;)V")
    )
    private static void detmc$shuffleWithMobRandom(
            List<BlockPos> poses,
            MemoryAccessor<?, ?> walkTarget,
            float speedModifier,
            ServerLevel level,
            PathfinderMob body,
            long timestamp) {
        if (!DetExecutors.syncRandom()) {
            Collections.shuffle(poses);
            return;
        }
        Util.shuffle(poses, body.getRandom());
    }
}
