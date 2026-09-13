package dev.detmc.mixin;

import it.unimi.dsi.fastutil.longs.AbstractLong2ByteMap;
import it.unimi.dsi.fastutil.longs.Long2ByteMap;
import it.unimi.dsi.fastutil.longs.Long2ByteMaps;
import it.unimi.dsi.fastutil.longs.LongArrayList;
import it.unimi.dsi.fastutil.longs.LongIterator;
import it.unimi.dsi.fastutil.longs.LongSet;
import it.unimi.dsi.fastutil.objects.ObjectArrayList;
import it.unimi.dsi.fastutil.objects.ObjectIterable;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Iterator;
import java.util.List;
import java.util.Set;
import net.minecraft.server.level.ChunkHolder;
import net.minecraft.server.level.DistanceManager;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 2, step 4 — deterministic block-ticking chunk order.
 *
 * <p>{@code ChunkMap.forEachBlockTickingChunk} delegates straight to
 * {@code DistanceManager.forEachEntityTickingChunk}, which walks
 * {@code Long2ByteMaps.fastIterable(simulationChunkTracker.chunks)} — hash order,
 * i.e. a function of the whole insert/remove history of that map. Sorting by the
 * packed {@code ChunkPos} makes the order depend only on the <em>set</em> of
 * ticking chunks.
 *
 * <p>Redirecting {@code fastIterable} rather than overwriting
 * {@code ChunkMap.forEachBlockTickingChunk} keeps this to public fastutil types:
 * the receiver there is the package-private {@code ChunkMap$DistanceManager},
 * which a mixin in another package cannot name.
 *
 * <p>{@code fastIterable} recycles one {@code Entry} instance, so the entries are
 * copied into {@code BasicEntry} before sorting.
 */
@Mixin(DistanceManager.class)
public class DistanceManagerMixin {
    @Redirect(
            method = "forEachEntityTickingChunk(Lit/unimi/dsi/fastutil/longs/LongConsumer;)V",
            at = @At(
                    value = "INVOKE",
                    target = "Lit/unimi/dsi/fastutil/longs/Long2ByteMaps;fastIterable(Lit/unimi/dsi/fastutil/longs/Long2ByteMap;)Lit/unimi/dsi/fastutil/objects/ObjectIterable;"
            )
    )
    private ObjectIterable<Long2ByteMap.Entry> detmc$sortedChunks(Long2ByteMap chunks) {
        ObjectArrayList<Long2ByteMap.Entry> copy = new ObjectArrayList<>(chunks.size());
        for (Long2ByteMap.Entry entry : Long2ByteMaps.fastIterable(chunks)) {
            copy.add(new AbstractLong2ByteMap.BasicEntry(entry.getLongKey(), entry.getByteValue()));
        }
        copy.sort(Comparator.comparingLong(Long2ByteMap.Entry::getLongKey));
        return copy;
    }

    /**
     * Same hazard, second collection. {@code getSpawnCandidateChunks} hands
     * {@code ChunkMap.collectSpawningChunks} a raw {@code LongSet} iterator, so
     * natural mob spawning walks chunks in hash order. Sorted by packed
     * {@code ChunkPos} it depends only on which chunks are candidates.
     */
    @Redirect(
            method = "getSpawnCandidateChunks()Lit/unimi/dsi/fastutil/longs/LongIterator;",
            at = @At(
                    value = "INVOKE",
                    target = "Lit/unimi/dsi/fastutil/longs/LongSet;iterator()Lit/unimi/dsi/fastutil/longs/LongIterator;"
            )
    )
    private LongIterator detmc$sortedSpawnCandidates(LongSet chunks) {
        LongArrayList sorted = new LongArrayList(chunks);
        sorted.sort(null);
        return sorted.iterator();
    }

    /**
     * Phase 4 — the last order-carrying collection on the chunk path.
     *
     * <p>{@code chunksToUpdateFutures} is a {@code ReferenceOpenHashSet<ChunkHolder>}.
     * A reference set hashes on {@code System.identityHashCode}, so its iteration order
     * is a property of where the JVM happened to allocate those holders, not of the
     * world. {@code runAllUpdates} iterates it twice, calling
     * {@code updateHighestAllowedStatus} and then {@code updateFutures}, which is what
     * submits chunk generation steps. Two servers on the same seed therefore schedule
     * the same set of steps in a different order.
     *
     * <p>Measured before this fix, with {@code -Ddetmc.traceSpawnLight=true}: the two
     * runs read identical sky-light values at identical positions, but A ran the SPAWN
     * step of chunk {@code [12, -8]} before {@code [12, -9]} and B ran them the other
     * way round. That swap alone moved {@code ENTITY_COUNTER}, so 8 of 46 worldgen
     * entities came out with different UUIDs while every position matched.
     *
     * <p>Both loops are redirected by the same handler, so both passes see the same
     * order. Sorted by packed {@code ChunkPos} ({@code ChunkPos.pack()}), which is total over the set.
     */
    @Redirect(
            method = "runAllUpdates(Lnet/minecraft/server/level/ChunkMap;)Z",
            at = @At(value = "INVOKE", target = "Ljava/util/Set;iterator()Ljava/util/Iterator;")
    )
    private Iterator<ChunkHolder> detmc$sortedChunkHolderUpdates(Set<ChunkHolder> holders) {
        List<ChunkHolder> sorted = new ArrayList<>(holders);
        sorted.sort(Comparator.comparingLong((ChunkHolder h) -> h.getPos().pack()));
        return sorted.iterator();
    }
}
