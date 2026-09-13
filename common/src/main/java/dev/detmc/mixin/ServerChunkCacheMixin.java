package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Iterator;
import java.util.List;
import java.util.Set;
import net.minecraft.server.level.ChunkHolder;
import net.minecraft.server.level.ServerChunkCache;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 4 — the other identity-ordered set in the tick path.
 *
 * <p>{@code ServerChunkCache.chunkHoldersToBroadcast} is a
 * {@code ReferenceOpenHashSet<ChunkHolder>}, i.e. {@code System.identityHashCode} order,
 * and {@code broadcastChangedChunks} walks it every tick calling
 * {@code ChunkHolder.broadcastChanges}, which drains that chunk's pending block-change
 * sections. Sort by packed {@code ChunkPos} so the drain order is a property of the world
 * rather than of JVM allocation, the same treatment
 * {@code DistanceManager.chunksToUpdateFutures} already needed.
 */
@Mixin(ServerChunkCache.class)
public class ServerChunkCacheMixin {
    @Redirect(
            method = "broadcastChangedChunks(Lnet/minecraft/util/profiling/ProfilerFiller;)V",
            at = @At(value = "INVOKE", target = "Ljava/util/Set;iterator()Ljava/util/Iterator;")
    )
    private Iterator<ChunkHolder> detmc$broadcastInChunkOrder(Set<ChunkHolder> holders) {
        if (!DetExecutors.syncChunks() || holders.size() < 2) {
            return holders.iterator();
        }
        List<ChunkHolder> sorted = new ArrayList<>(holders);
        sorted.sort(Comparator.comparingLong(h -> h.getPos().pack()));
        return sorted.iterator();
    }
}
