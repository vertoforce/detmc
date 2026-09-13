package dev.detmc.mixin;

import dev.detmc.DetRng;
import net.minecraft.world.level.chunk.status.ChunkStatus;
import net.minecraft.world.level.chunk.status.ChunkStep;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Phase 3, step 1 — make the SPAWN step depend on neighbour light.
 *
 * <p>Read from the 26.2 source, not guessed. {@code ChunkPyramid.GENERATION_PYRAMID}
 * declares {@code SPAWN} with a single neighbour requirement, {@code BIOMES} at
 * radius 1. {@code ChunkStatusTasks.generateSpawn} then builds a
 * {@code WorldGenRegion} whose {@code getLightEngine()} is the level's live
 * {@code ThreadedLevelLightEngine}, and {@code Animal.checkAnimalSpawnRules}
 * reads {@code getRawBrightness(pos, 0) > 8} from it. Sky light within six blocks
 * of a chunk edge comes from the neighbour's columns, and the neighbour's
 * {@code LIGHT} step ({@code propagateLightSources}) is not ordered against the
 * centre chunk's {@code SPAWN}. Whichever lands first decides the fox.
 *
 * <p>Adding {@code LIGHT} at radius 1 to the SPAWN step makes the read wait for
 * every neighbour's {@code lightChunk} future, which completes only after
 * {@code runLightUpdates()} has propagated that neighbour's sources. The pyramid
 * builder merges requirements with {@code ChunkStatus.max}, so the existing
 * {@code BIOMES} entry is subsumed, not duplicated. LIGHT precedes SPAWN in the
 * status list, so the builder's ordering check passes and no dependency cycle
 * is introduced. Cost: one more ring at INITIALIZE_LIGHT around every FULL chunk.
 *
 * <p>Applies to both pyramids; on LOADING_PYRAMID the SPAWN step is a
 * pass-through and the neighbour {@code lightChunk(chunk, true)} call is cheap.
 */
@Mixin(ChunkStep.Builder.class)
public abstract class ChunkStepBuilderMixin {
    @Shadow
    @Final
    private ChunkStatus status;

    @Shadow
    public abstract ChunkStep.Builder addRequirement(ChunkStatus status, int radius);

    @Inject(method = "build()Lnet/minecraft/world/level/chunk/status/ChunkStep;", at = @At("HEAD"))
    private void detmc$spawnNeedsNeighbourLight(CallbackInfoReturnable<ChunkStep> cir) {
        if (this.status == ChunkStatus.SPAWN) {
            this.addRequirement(ChunkStatus.LIGHT, 1);
            DetRng.LOGGER.info("[detmc] SPAWN step: neighbour LIGHT radius 1 requirement added");
        }
    }
}
