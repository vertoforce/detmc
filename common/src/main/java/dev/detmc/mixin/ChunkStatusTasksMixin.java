package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetRng;
import dev.detmc.DetTrace;
import java.util.concurrent.CompletableFuture;
import net.minecraft.server.level.ThreadedLevelLightEngine;
import net.minecraft.world.level.chunk.ChunkAccess;
import net.minecraft.world.level.chunk.status.ChunkStatusTasks;
import net.minecraft.world.level.chunk.status.ChunkStep;
import net.minecraft.world.level.chunk.status.WorldGenContext;
import net.minecraft.server.level.GenerationChunkHolder;
import net.minecraft.util.StaticCache2D;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 3, step 2 — flush queued light before the SPAWN step reads it.
 *
 * <p>{@code ThreadedLevelLightEngine.runUpdate} processes at most 1000 queued
 * tasks per call and is otherwise only invoked from
 * {@code ServerChunkCache.MainThreadExecutor.pollTask}. Block writes made into
 * this chunk by a neighbour's FEATURES step after INITIALIZE_LIGHT arrive as
 * {@code checkBlock} tasks keyed by this chunk's position; the light dispatcher
 * pops by priority level then position, not FIFO, so those tasks can sit behind
 * a batch cut when {@code generateSpawn} runs. Draining the engine here makes
 * the value {@code isBrightEnoughToSpawn} reads a function of chunk contents,
 * not of where the last batch boundary fell.
 *
 * <p>Same-thread only. With real worker threads {@code runUpdate} belongs to the
 * light executor and calling it from the worldgen thread would race, so the
 * drain is skipped unless {@code DetExecutors.syncChunks()} is on. Under the
 * trampoline every {@code runUpdate} frame has returned before a worldgen task
 * runs, so this call is never re-entrant.
 */
@Mixin(ChunkStatusTasks.class)
public abstract class ChunkStatusTasksMixin {

    @Inject(
            method = "generateSpawn(Lnet/minecraft/world/level/chunk/status/WorldGenContext;Lnet/minecraft/world/level/chunk/status/ChunkStep;Lnet/minecraft/util/StaticCache2D;Lnet/minecraft/world/level/chunk/ChunkAccess;)Ljava/util/concurrent/CompletableFuture;",
            at = @At("HEAD")
    )
    private static void detmc$drainLightBeforeSpawn(
            WorldGenContext context, ChunkStep step, StaticCache2D<GenerationChunkHolder> chunks, ChunkAccess chunk,
            CallbackInfoReturnable<CompletableFuture<ChunkAccess>> cir) {
        if (DetTrace.tracingSpawnLight()) {
            DetTrace.pushSpawnChunk(chunk.getPos().toString());
        }
        if (!DetExecutors.syncChunks()) {
            return;
        }
        ThreadedLevelLightEngine engine = context.lightEngine();
        ThreadedLevelLightEngineAccessor access = (ThreadedLevelLightEngineAccessor) engine;
        int rounds = 0;
        while ((!access.detmc$lightTasks().isEmpty() || engine.hasLightWork()) && rounds < 64) {
            access.detmc$runUpdate();
            rounds++;
        }
        if (rounds > 0) {
            DetRng.LOGGER.info("[detmc-ld] drained light before spawn at {}: {} rounds", chunk.getPos(), rounds);
        }
        if (rounds == 64) {
            DetRng.LOGGER.warn("[detmc] light drain before spawn hit {} rounds at {}", rounds, chunk.getPos());
        }
    }

    @Inject(
            method = "generateSpawn(Lnet/minecraft/world/level/chunk/status/WorldGenContext;Lnet/minecraft/world/level/chunk/status/ChunkStep;Lnet/minecraft/util/StaticCache2D;Lnet/minecraft/world/level/chunk/ChunkAccess;)Ljava/util/concurrent/CompletableFuture;",
            at = @At("RETURN")
    )
    private static void detmc$clearSpawnChunkTag(
            WorldGenContext context, ChunkStep step, StaticCache2D<GenerationChunkHolder> chunks, ChunkAccess chunk,
            CallbackInfoReturnable<CompletableFuture<ChunkAccess>> cir) {
        if (DetTrace.tracingSpawnLight()) {
            DetTrace.popSpawnChunk();
        }
    }
}
