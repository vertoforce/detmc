package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetTrace;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Queue;
import java.util.stream.Stream;
import net.minecraft.world.level.entity.ChunkEntities;
import net.minecraft.world.level.entity.EntityAccess;
import net.minecraft.world.level.entity.PersistentEntitySectionManager;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 2, step 3 — deterministic entity insertion order.
 *
 * <p>{@code loadingInbox} is a {@code ConcurrentLinkedQueue} filled by IO
 * completions, so the order in which chunks' entity lists reach
 * {@code processPendingLoads} is whatever order the storage futures happened to
 * finish in. That order decides {@code ServerLevel.ENTITY_COUNTER} ids, which in
 * turn decide tick order. Draining the batch sorted by packed {@code ChunkPos}
 * removes the dependency on IO completion timing.
 */
@Mixin(PersistentEntitySectionManager.class)
public class PersistentEntitySectionManagerMixin {
    @Shadow
    @Final
    private Queue<ChunkEntities<EntityAccess>> loadingInbox;

    /**
     * Phase 7 — the phase 2 {@code @Overwrite} of this method re-implemented the add loop
     * and dropped vanilla's {@code chunkLoadStatuses.put(pos, LOADED)}. Every chunk therefore
     * stayed {@code PENDING}, {@code storeChunkSections} returned false for all of them, and
     * the mod wrote 0 bytes of {@code entities/*.mca} on every save (measured with a
     * one-variable control: unmodded Fabric wrote 25 KB). Vanilla's
     * body runs untouched now; this only reorders the inbox in place, and only on the async
     * path: with {@code -Ddetmc.syncReads} the inbox fills in request order, which is
     * deterministic, and a batch is "whatever arrived since the last tick", which is not,
     * so sorting a batch would make the add order depend on where the batches split
     * (measured: test/phase7/B10k split one batch in two and added the -7 row before the -9 row).
     */
    @Inject(method = "processPendingLoads()V", at = @At("HEAD"))
    private void detmc$orderInbox(CallbackInfo ci) {
        if (this.loadingInbox.isEmpty()) {
            return;
        }
        List<ChunkEntities<EntityAccess>> batch = new ArrayList<>();
        ChunkEntities<EntityAccess> chunk;
        while ((chunk = this.loadingInbox.poll()) != null) {
            batch.add(chunk);
        }
        if (!DetExecutors.syncReads()) {
            batch.sort(Comparator.comparingLong(c -> c.getPos().pack()));
        }
        if (DetTrace.tracingIo()) {
            DetTrace.io("entityInbox", batch.stream().map(c -> c.getPos().toString()).toList());
        }
        this.loadingInbox.addAll(batch);
    }

    @Inject(method = "addWorldGenChunkEntities(Ljava/util/stream/Stream;)V", at = @At("HEAD"))
    private void detmc$worldgenAddHead(Stream<EntityAccess> entities, CallbackInfo ci) {
        if (DetTrace.tracing()) {
            DetTrace.pushSource("worldgen");
        }
    }

    @Inject(method = "addWorldGenChunkEntities(Ljava/util/stream/Stream;)V", at = @At("RETURN"))
    private void detmc$worldgenAddReturn(Stream<EntityAccess> entities, CallbackInfo ci) {
        if (DetTrace.tracing()) {
            DetTrace.popSource();
        }
    }

    @Inject(method = "addEntity(Lnet/minecraft/world/level/entity/EntityAccess;Z)Z", at = @At("HEAD"))
    private void detmc$traceAdd(EntityAccess entity, boolean loaded, CallbackInfoReturnable<Boolean> cir) {
        DetTrace.entityAdded(entity, loaded);
    }
}
