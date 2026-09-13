package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetRng;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import net.minecraft.nbt.CompoundTag;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.chunk.storage.IOWorker;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 7 — region reads complete before the caller continues.
 *
 * <p>Measured 2026-09-12 (test/phase7/A1 vs A2, same jar, same harness, one container at a
 * time): the loaded animals in the spawn area (chicken, goat, rabbit, seven foxes) came
 * back with entity ids shifted by one or two against the worldgen-constructed ones, and
 * one worldgen goat got a different UUID, i.e. a different {@code DetRng} entity ordinal.
 * Cause, from the 26.2 source: {@code EntityStorage.loadEntities} is
 * {@code read(pos).thenApplyAsync(deserialise, entityDeserializerQueue)} whose executor is
 * the {@code MinecraftServer} main queue, so the entities are constructed -- and take
 * their {@code ENTITY_COUNTER} id and their {@code Entity.random} ordinal -- in whichever
 * tick the IO worker finished the read, interleaved with worldgen constructions.
 * {@code ChunkMap.scheduleChunkLoad} and {@code SectionStorage} (POI) reads have the same
 * shape. All three funnel through {@code IOWorker.loadAsync}, so this is the one place
 * to pin: the IO worker still does the read, the calling thread waits for it, and every
 * continuation therefore runs at the request point instead of at the completion point.
 * Writes are left asynchronous. {@code -Ddetmc.syncReads=false} opts out.
 */
@Mixin(IOWorker.class)
public class IOWorkerMixin {
    @Inject(method = "loadAsync(Lnet/minecraft/world/level/ChunkPos;)Ljava/util/concurrent/CompletableFuture;",
            at = @At("RETURN"), cancellable = true)
    private void detmc$joinRead(ChunkPos pos, CallbackInfoReturnable<CompletableFuture<Optional<CompoundTag>>> cir) {
        if (!DetExecutors.syncReads()) {
            return;
        }
        // The IO worker is a single consecutive executor: a join issued from one of its own
        // tasks would wait on a task queued behind itself. Never seen from vanilla, guarded anyway.
        if (Thread.currentThread().getName().startsWith("IO-Worker")) {
            return;
        }
        CompletableFuture<Optional<CompoundTag>> pending = cir.getReturnValue();
        try {
            cir.setReturnValue(CompletableFuture.completedFuture(pending.join()));
        } catch (CompletionException e) {
            DetRng.LOGGER.warn("[detmc] synchronous region read of {} failed: {}", pos, e.getCause() == null ? e : e.getCause());
            cir.setReturnValue(pending);
        }
    }
}
