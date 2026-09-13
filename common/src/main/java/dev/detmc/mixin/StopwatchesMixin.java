package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetTrace;
import net.minecraft.util.Util;
import net.minecraft.world.Stopwatches;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 8 sweep — the only wall clock in 26.2 that commands can branch on and that is
 * written to the save.
 *
 * <p>{@code Stopwatches} is a {@code SavedData} of named elapsed times;
 * {@code Stopwatches.currentTime()} is {@code Util.getMillis()}. {@code /stopwatch} starts
 * and reads them and {@code ExecuteCommand} compares against them, so a datapack can make
 * world state depend on how many real milliseconds the host took, and {@code pack()}
 * writes the elapsed value into {@code data/stopwatches.dat}. Two replicas therefore
 * differ both in behaviour and on disk.
 *
 * <p>Replaced with the world clock ({@code gameTime * 50}), which makes a stopwatch count
 * ticks: exactly right for a replica, and it keeps the wall-clock unit so a datapack's
 * millisecond thresholds still mean what they say on a server that is keeping up.
 *
 * <p>Not on the harness's path (no stopwatch is ever started), so <b>not covered by the
 * 24000-tick pair</b>.
 */
@Mixin(Stopwatches.class)
public class StopwatchesMixin {
    @Redirect(
            method = "currentTime()J",
            at = @At(value = "INVOKE", target = "Lnet/minecraft/util/Util;getMillis()J")
    )
    private static long detmc$stopwatchGameTime() {
        return DetExecutors.syncRandom() ? DetTrace.gameTimeMillis() : Util.getMillis();
    }
}
