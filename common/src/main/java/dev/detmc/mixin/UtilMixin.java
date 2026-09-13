package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetRng;
import net.minecraft.TracingExecutor;
import net.minecraft.util.Util;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 2, step 1b — measured, not guessed.
 *
 * <p>With only the {@code ServerChunkCache} executor forced same-thread, a seed
 * trace of two fresh servers showed 929 of 1310 {@code DetRng} allocations still
 * coming off {@code Worker-Main-1}, and the two runs parting company at seed 19:
 * run A was in {@code NoiseBasedChunkGenerator.applyCarvers}, run B in
 * {@code ChunkGenerator.applyBiomeDecoration}. Chunk generation steps hop onto
 * {@code Util.backgroundExecutor()} directly ({@code wgen_fill_noise},
 * {@code init_biomes}, {@code parseChunk}, {@code structureRings}), and every
 * {@code thenApply} continuation then runs on whichever thread completed the
 * future. One shared seed counter plus two threads equals a different stream
 * position per run, even though the chunk <em>contents</em> were identical.
 *
 * <p>So {@code backgroundExecutor()} is replaced wholesale. {@code TracingExecutor}
 * is a record over an {@code ExecutorService}, and its {@code forName()} returns
 * that service unchanged when Tracy is not available, so wrapping the trampoline
 * covers every caller.
 *
 * <p>{@code ioPool()} is deliberately left alone: no seed in the trace came off
 * an {@code IO-Worker} thread, and entity load order is already made stable by
 * {@code PersistentEntitySectionManagerMixin}.
 */
@Mixin(Util.class)
public class UtilMixin {
    private static TracingExecutor detmc$sameThreadBackground;

    @Inject(method = "backgroundExecutor()Lnet/minecraft/TracingExecutor;", at = @At("HEAD"), cancellable = true)
    private static void detmc$syncBackgroundExecutor(CallbackInfoReturnable<TracingExecutor> cir) {
        if (!DetExecutors.syncChunks()) {
            return;
        }
        if (detmc$sameThreadBackground == null) {
            detmc$sameThreadBackground = new TracingExecutor(DetExecutors.sameThreadService());
            DetRng.LOGGER.info("[detmc] Util.backgroundExecutor() forced same-thread");
        }
        cir.setReturnValue(detmc$sameThreadBackground);
    }
}
