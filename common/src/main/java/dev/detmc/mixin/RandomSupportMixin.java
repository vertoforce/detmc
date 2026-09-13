package dev.detmc.mixin;

import dev.detmc.DetRng;
import net.minecraft.world.level.levelgen.RandomSupport;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Overwrite;

/**
 * Safety net. Vanilla {@code generateUniqueSeed()} is
 * {@code SEED_UNIQUIFIER.updateAndGet(...) ^ System.nanoTime()} and feeds every
 * {@code RandomSource.create()} / {@code createThreadSafe()} call site (24 of
 * them). Replacing it with the deterministic counter covers the sites this mod
 * does not redirect explicitly.
 */
@Mixin(RandomSupport.class)
public class RandomSupportMixin {
    /**
     * @author detmc
     * @reason Remove the System.nanoTime() term; determinism is the whole point
     *         of this mod and there is no sensible way to express this as an
     *         injection.
     */
    @Overwrite
    public static long generateUniqueSeed() {
        return DetRng.nextSeed();
    }
}
