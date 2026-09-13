package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import net.minecraft.core.BlockPos;
import net.minecraft.server.level.PlayerSpawnFinder;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.util.RandomSource;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — where a player spawns is drawn from {@code ThreadLocalRandom}.
 *
 * <p>{@code PlayerSpawnFinder} scans the {@code respawn_radius} square as a coprime walk
 * and starts it at {@code RandomSource.createThreadLocalInstance().nextInt(candidateCount)}.
 * {@code createThreadLocalInstance()} is {@code new SingleThreadedRandomSource(
 * ThreadLocalRandom.current().nextLong())} — the one {@code RandomSource} factory that
 * does not go through {@code RandomSupport.generateUniqueSeed()}, so
 * {@code RandomSupportMixin} (the mod's safety net for every other factory) does not cover
 * it. The starting offset, and therefore the spawn block a player lands on, is a property
 * of the JVM.
 *
 * <p>Game-visible and server-reachable: it runs for every initial join and every respawn
 * without a bed. The harness has no players, so this is reasoned from the source and is
 * <b>not covered by the 24000-tick pair</b>.
 *
 * <p>Routed through the level's {@code RandomSource}, which {@code LevelMixin} seeds.
 */
@Mixin(PlayerSpawnFinder.class)
public class PlayerSpawnFinderMixin {
    @Redirect(
            method = "<init>(Lnet/minecraft/server/level/ServerLevel;Lnet/minecraft/core/BlockPos;I)V",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;createThreadLocalInstance()Lnet/minecraft/util/RandomSource;")
    )
    private RandomSource detmc$spawnOffsetFromLevel(ServerLevel level, BlockPos spawnSuggestion, int radius) {
        if (!DetExecutors.syncRandom()) {
            return RandomSource.createThreadLocalInstance();
        }
        return level.getRandom();
    }
}
