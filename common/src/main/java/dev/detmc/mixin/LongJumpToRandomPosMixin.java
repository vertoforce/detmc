package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import java.util.Collections;
import java.util.List;
import net.minecraft.util.Util;
import net.minecraft.world.entity.Mob;
import net.minecraft.world.entity.ai.behavior.LongJumpToRandomPos;
import net.minecraft.world.phys.Vec3;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Redirect;

/**
 * Phase 7 — the goat long jump picked its launch angle with a wall-clock JDK RNG.
 *
 * <p>Measured 2026-09-12 (test/phase7): three runs bit-identical for gametimes 0..2198,
 * then at 2199 exactly one entity differs, goat id 17, and only in position and motion
 * (motion y 0.888 against 0.729, i.e. a different launch vector for the same jump). The
 * digest at 2199 took one of four values across seven runs of the same jar.
 *
 * <p>Cause: {@code calculateOptimalJumpVector} does {@code Collections.shuffle(allowedAngles)}
 * and returns the vector of the first angle that works. {@code Collections.shuffle(List)}
 * uses {@code new java.util.Random()}, seeded from {@code System.nanoTime()}, so the angle
 * order is a property of the JVM's start time and nothing detmc seeds reaches it. The
 * same jar matching itself on some runs is just several angles producing the same first
 * valid vector.
 *
 * <p>Fix: shuffle with the mob's own {@code RandomSource}, which detmc already seeds.
 * This draws {@code allowedAngles.size() - 1} extra ints from the mob's stream on every
 * long jump, so goat AI after a jump is deterministic but not vanilla-identical; nothing in
 * this project needs vanilla-identical draws. {@code -Ddetmc.syncChunks=false} opts out.
 *
 * <p>Not covered: {@code InsideBrownianWalk} (villagers only) has the same call inside a
 * synthetic lambda, and {@code @e[sort=random]} shuffles with the same JDK RNG.
 */
@Mixin(LongJumpToRandomPos.class)
public class LongJumpToRandomPosMixin {
    @Redirect(
            method = "calculateOptimalJumpVector(Lnet/minecraft/world/entity/Mob;Lnet/minecraft/world/phys/Vec3;)Lnet/minecraft/world/phys/Vec3;",
            at = @At(value = "INVOKE", target = "Ljava/util/Collections;shuffle(Ljava/util/List;)V")
    )
    private void detmc$shuffleWithMobRandom(List<Integer> allowedAngles, Mob body, Vec3 targetPos) {
        if (!DetExecutors.syncChunks()) {
            Collections.shuffle(allowedAngles);
            return;
        }
        Util.shuffle(allowedAngles, body.getRandom());
    }
}
