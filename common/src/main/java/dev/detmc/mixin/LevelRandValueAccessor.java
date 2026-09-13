package dev.detmc.mixin;

import net.minecraft.world.level.Level;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

/**
 * {@code Level.randValue} is a plain {@code int} holding the state of the LCG behind
 * {@code getBlockRandomPos}, i.e. the block position of every random tick. It is seeded
 * once in the {@code Level} constructor from {@code DetRng.createThreadLocalInstance()},
 * so {@code /detmc reseed} has to write it directly; there is no {@code RandomSource}
 * left to call {@code setSeed} on.
 */
@Mixin(Level.class)
public interface LevelRandValueAccessor {
    @Accessor("randValue")
    void detmc$setRandValue(int randValue);
}
