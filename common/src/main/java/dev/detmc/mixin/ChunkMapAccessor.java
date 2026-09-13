package dev.detmc.mixin;

import java.util.concurrent.atomic.AtomicInteger;
import net.minecraft.server.level.ChunkMap;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

/**
 * Phase 6 — read {@code ChunkMap.activeChunkWrites} from outside.
 *
 * <p>It is the count of chunk saves whose write future has not completed yet. The
 * tick-end IO barrier in {@code MinecraftServerMixin} waits on it, because a save
 * completing after the tick boundary is what puts {@code scheduleUnload}'s body into
 * {@code unloadQueue} one tick later than the run next door.
 */
@Mixin(ChunkMap.class)
public interface ChunkMapAccessor {
    @Accessor("activeChunkWrites")
    AtomicInteger detmc$activeChunkWrites();
}
