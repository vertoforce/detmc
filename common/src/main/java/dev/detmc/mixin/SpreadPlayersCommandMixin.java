package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.Collection;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.server.commands.SpreadPlayersCommand;
import net.minecraft.util.RandomSource;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.phys.Vec2;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — {@code /spreadplayers} teleports from {@code ThreadLocalRandom}.
 *
 * <p>{@code spreadPlayers} takes one {@code RandomSource.createThreadLocalInstance()} and
 * uses it for both {@code createInitialPositions} and the repulsion passes in
 * {@code spreadPositions}, so every destination the command produces comes from
 * {@code ThreadLocalRandom.current().nextLong()}. Same factory, same gap in
 * {@code RandomSupportMixin}'s cover, as {@code PlayerSpawnFinder}.
 *
 * <p>Game-visible and server-reachable: it moves entities, and a datapack calling it is
 * enough. The harness never issues it, so this is reasoned from the source and is
 * <b>not covered by the 24000-tick pair</b>.
 */
@Mixin(SpreadPlayersCommand.class)
public class SpreadPlayersCommandMixin {
    @Redirect(
            method = "spreadPlayers(Lnet/minecraft/commands/CommandSourceStack;Lnet/minecraft/world/phys/Vec2;FFIZLjava/util/Collection;)I",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/RandomSource;createThreadLocalInstance()Lnet/minecraft/util/RandomSource;")
    )
    private static RandomSource detmc$spreadFromLevel(
            CommandSourceStack source,
            Vec2 center,
            float spreadDistance,
            float maxRange,
            int maxHeight,
            boolean respectTeams,
            Collection<? extends Entity> entities) {
        if (!DetExecutors.syncRandom()) {
            return RandomSource.createThreadLocalInstance();
        }
        return source.getLevel().getRandom();
    }
}
