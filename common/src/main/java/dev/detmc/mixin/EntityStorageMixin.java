package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetTrace;
import java.util.concurrent.Executor;
import java.util.function.Function;
import org.spongepowered.asm.mixin.injection.Redirect;
import java.util.concurrent.CompletableFuture;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.chunk.storage.EntityStorage;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 4 probe only — no behaviour change.
 *
 * <p>{@code loadEntities} chains
 * {@code simpleRegionStorage.read(pos).thenApplyAsync(..., entityDeserializerQueue::schedule)}.
 * The read runs on {@code Util.ioPool()}, so the deserialisation, and therefore the
 * {@code loadingInbox} entry that {@code PersistentEntitySectionManager.processPendingLoads}
 * picks up, is scheduled onto the main thread on whichever tick the IO finished.
 * Logging the request tick here and the delivery tick in
 * {@code PersistentEntitySectionManagerMixin} shows the gap directly.
 */
@Mixin(EntityStorage.class)
public class EntityStorageMixin {
    /**
     * Phase 7 fix. With {@code IOWorkerMixin} the read is already complete here, but vanilla
     * still hands the deserialisation to {@code entityDeserializerQueue}, i.e. a
     * {@code TickTask} on the {@code MinecraftServer} queue. Worldgen entities are
     * constructed inline inside chunk tasks, so whether that task runs before or after the
     * next chunk task is decided by the between-tick wall-clock loop. Measured 2026-09-12
     * (test/phase7/B10k against A10k, same jar): the inbox batch split across two ticks and
     * the loaded animals' ids and RNG ordinals shifted by one or two. Deserialising inline
     * puts the construction at the request point, in the same ordered stream as worldgen.
     */
    @Redirect(
            method = "loadEntities(Lnet/minecraft/world/level/ChunkPos;)Ljava/util/concurrent/CompletableFuture;",
            at = @At(value = "INVOKE", target = "Ljava/util/concurrent/CompletableFuture;thenApplyAsync(Ljava/util/function/Function;Ljava/util/concurrent/Executor;)Ljava/util/concurrent/CompletableFuture;")
    )
    private <T, U> CompletableFuture<U> detmc$deserialiseInline(CompletableFuture<T> read, Function<? super T, ? extends U> fn, Executor executor) {
        if (!DetExecutors.syncReads() || !read.isDone()) {
            return read.thenApplyAsync(fn, executor);
        }
        return read.thenApply(fn);
    }

    @Inject(
            method = "loadEntities(Lnet/minecraft/world/level/ChunkPos;)Ljava/util/concurrent/CompletableFuture;",
            at = @At("HEAD")
    )
    private void detmc$traceLoadEntities(ChunkPos pos, CallbackInfoReturnable<CompletableFuture<?>> cir) {
        DetTrace.io("entityLoadReq", pos);
    }
}
