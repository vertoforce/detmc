package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetTrace;
import java.util.function.BooleanSupplier;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import net.minecraft.server.level.ChunkHolder;
import net.minecraft.server.level.ChunkMap;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.chunk.ChunkAccess;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.ModifyArg;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 4 — take the wall clock out of chunk unloading.
 *
 * <p>Measured, not assumed. With {@code -Ddetmc.traceSpawnLight=true} two runs of
 * the same seed read different sky-light values at the same block during the SPAWN
 * step: {@code chunk=[11, 2] pos=187,156,33 raw=14} against {@code raw=9}, and they
 * ran the SPAWN steps of {@code [12, -9]} and {@code [12, -8]} in opposite orders.
 * That is downstream of this method. {@code ChunkMap.processUnloads} drains
 * {@code unloadQueue} while {@code haveTime.getAsBoolean()} is true, i.e. for
 * whatever is left of the 50 ms tick, and each unload task calls
 * {@code lightEngine.updateChunkStatus}, which drops that chunk's light sections.
 * So the set of chunks holding light at any point in the forceload is a function of
 * how fast the host was, and the neighbouring sky light a spawn check reads follows.
 *
 * <p>Replacing the supplier with a constant {@code true} drains the queue fully every
 * tick. The work is the same work, just not spread across an unpredictable number of
 * ticks. Vanilla already force-drains everything above 2000 queued
 * ({@code minimalNumberOfChunksToProcess}), so a full drain is a widening of an
 * existing path rather than a new one. {@code saveChunksEagerly} inherits the same
 * supplier and is separately bounded at 20 chunks per call, so it stays bounded.
 *
 * <p>{@code poiManager.tick(haveTime)} in the same method is left alone: it does not
 * touch the light engine.
 */
@Mixin(ChunkMap.class)
public abstract class ChunkMapMixin {
    private static final BooleanSupplier DETMC_ALWAYS = () -> true;

    @ModifyArg(
            method = "tick(Ljava/util/function/BooleanSupplier;)V",
            at = @At(
                    value = "INVOKE",
                    target = "Lnet/minecraft/server/level/ChunkMap;processUnloads(Ljava/util/function/BooleanSupplier;)V"
            ),
            index = 0
    )
    private BooleanSupplier detmc$unloadWithoutAClock(BooleanSupplier haveTime) {
        return DetExecutors.syncChunks() ? DETMC_ALWAYS : haveTime;
    }

    // ---- phase 4 probe -----------------------------------------------------
    // Every point at which chunk IO touches main-thread game state, stamped with
    // the tick it landed on. saveChunksEagerly is the interesting one: its gate is
    // Util.getMillis() (nextChunkSaveTime = now + 10000 ms) AND activeChunkWrites,
    // which a background write decrements, so both operands are wall-clock.

    @Shadow
    @Final
    private AtomicInteger activeChunkWrites;

    @Inject(method = "saveChunksEagerly(Ljava/util/function/BooleanSupplier;)V", at = @At("HEAD"))
    private void detmc$traceEagerHead(java.util.function.BooleanSupplier haveTime, CallbackInfo ci) {
        if (DetTrace.tracingIo()) {
            DetTrace.io("eagerHead", "activeWrites=" + this.activeChunkWrites.get());
        }
    }

    @Inject(method = "saveChunkIfNeeded(Lnet/minecraft/server/level/ChunkHolder;J)Z", at = @At("RETURN"))
    private void detmc$traceEager(ChunkHolder chunk, long now, CallbackInfoReturnable<Boolean> cir) {
        if (DetTrace.tracingIo() && cir.getReturnValueZ()) {
            DetTrace.io("eagerSave", chunk.getPos());
        }
    }

    @Inject(method = "save(Lnet/minecraft/world/level/chunk/ChunkAccess;)Z", at = @At("HEAD"))
    private void detmc$traceSave(ChunkAccess chunk, CallbackInfoReturnable<Boolean> cir) {
        DetTrace.io("save", chunk.getPos());
    }

    @Inject(method = "readChunk(Lnet/minecraft/world/level/ChunkPos;)Ljava/util/concurrent/CompletableFuture;", at = @At("HEAD"))
    private void detmc$traceRead(ChunkPos pos, CallbackInfoReturnable<CompletableFuture<?>> cir) {
        DetTrace.io("readReq", pos);
    }

    @Inject(method = "scheduleChunkLoad(Lnet/minecraft/world/level/ChunkPos;)Ljava/util/concurrent/CompletableFuture;", at = @At("HEAD"))
    private void detmc$traceChunkLoad(ChunkPos pos, CallbackInfoReturnable<CompletableFuture<?>> cir) {
        DetTrace.io("chunkLoad", pos);
    }

    // ---- phase 4 fix: the chunk save schedule ran on the wall clock -----------
    //
    // Measured with the probe above, same seed, one uninterrupted 3000-tick step
    // segment, first differing event at gametime 206:
    //
    //   A: save [4, -7] / eagerSave [4, -7]
    //   B: (nothing)
    //
    // and over the segment A performed 1119 eager saves against B's 1124.
    //
    // Cause: saveChunksEagerly reads Util.getMillis() and saveChunkIfNeeded gates on
    // "now < nextChunkSaveTime", where nextChunkSaveTime was set to now + 10000 ms the
    // last time that chunk was saved. So the tick a chunk is saved on is a function of
    // elapsed milliseconds, not of elapsed ticks, and two hosts running at different
    // speeds put the same save on different ticks. A frozen checkpoint hides it because
    // both servers flush and settle there.
    //
    // A save is not inert: ChunkMap.scheduleUnload waits on ChunkHolder.getSaveSyncFuture(),
    // so save timing moves unload timing, and every unload calls
    // lightEngine.updateChunkStatus -- the same light-section drop that phase 4 already
    // had to take the wall clock out of in processUnloads.
    //
    // Fix: drive the schedule off the world clock instead. gameTime * 50 ms is the same
    // number vanilla would see on a server holding exactly 20 tps, so the 10 s
    // re-save interval becomes exactly 200 ticks, identically on every replica.

    @Shadow
    @Final
    private ServerLevel level;

    @Redirect(
            method = "saveChunksEagerly(Ljava/util/function/BooleanSupplier;)V",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/Util;getMillis()J")
    )
    private long detmc$eagerSaveClock() {
        return detmc$clock();
    }

    @Redirect(
            method = "saveAllChunks(Z)V",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/Util;getMillis()J")
    )
    private long detmc$saveAllClock() {
        return detmc$clock();
    }

    private long detmc$clock() {
        return DetExecutors.syncChunks() ? this.level.getGameTime() * 50L : net.minecraft.util.Util.getMillis();
    }

    /**
     * The other half of the same gate. {@code activeChunkWrites} is decremented by a
     * background write completion, so {@code activeChunkWrites.get() < 128} is a second
     * wall-clock operand. The 20-saves-per-call cap already bounds the loop, so reporting
     * zero here only removes the timing dependency.
     */
    @Redirect(
            method = "saveChunksEagerly(Ljava/util/function/BooleanSupplier;)V",
            at = @At(value = "INVOKE", target = "Ljava/util/concurrent/atomic/AtomicInteger;get()I")
    )
    private int detmc$ignoreActiveWrites(AtomicInteger instance) {
        return DetExecutors.syncChunks() ? 0 : instance.get();
    }
}
