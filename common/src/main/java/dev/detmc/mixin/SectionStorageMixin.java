package dev.detmc.mixin;

import dev.detmc.DetTrace;
import it.unimi.dsi.fastutil.longs.Long2ObjectMap;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.chunk.storage.SectionStorage;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import dev.detmc.DetExecutors;
import java.util.function.BooleanSupplier;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Phase 4 probe only — no behaviour change.
 *
 * <p>{@code unpackPendingLoads} walks {@code pendingLoads} and calls
 * {@code future.getNow(null)}: a section chunk is unpacked into {@code storage} on the
 * first tick on which its IO happens to have finished, and skipped otherwise. That is a
 * pure wall-clock race, and {@code PoiManager} is a {@code SectionStorage}. Logging the
 * pending count either side of the call dates every landing to a tick.
 */
@Mixin(SectionStorage.class)
public class SectionStorageMixin {
    @Shadow
    @Final
    private Long2ObjectMap<?> pendingLoads;

    @Inject(method = "unpackPendingLoads()V", at = @At("HEAD"))
    private void detmc$pendingHead(CallbackInfo ci) {
        if (DetTrace.tracingIo() && !this.pendingLoads.isEmpty()) {
            DetTrace.io("secPendingHead", this.getClass().getSimpleName() + " n=" + this.pendingLoads.size());
        }
    }

    @Inject(method = "unpackPendingLoads()V", at = @At("TAIL"))
    private void detmc$pendingTail(CallbackInfo ci) {
        if (DetTrace.tracingIo() && !this.pendingLoads.isEmpty()) {
            DetTrace.io("secPendingTail", this.getClass().getSimpleName() + " n=" + this.pendingLoads.size());
        }
    }

    @Inject(method = "writeChunk(Lnet/minecraft/world/level/ChunkPos;)V", at = @At("HEAD"))
    private void detmc$writeChunk(ChunkPos pos, CallbackInfo ci) {
        DetTrace.io("secWrite", this.getClass().getSimpleName() + " " + pos);
    }

    /**
     * Phase 4 fix, same shape as {@code ChunkMap.processUnloads}: {@code tick} writes
     * dirty section chunks only while {@code haveTime} holds, i.e. for whatever is left
     * of the 50 ms tick, so the number written per tick is a property of the host.
     * Writing clears {@code dirtyChunks}, which feeds {@code hasWork()} and so the
     * server's own idea of whether there is outstanding work. Drain it fully instead.
     * {@code PoiManager} is the instance that matters here.
     */
    @Redirect(
            method = "tick(Ljava/util/function/BooleanSupplier;)V",
            at = @At(value = "INVOKE", target = "Ljava/util/function/BooleanSupplier;getAsBoolean()Z")
    )
    private boolean detmc$writeWithoutAClock(BooleanSupplier haveTime) {
        return DetExecutors.syncChunks() || haveTime.getAsBoolean();
    }
}
