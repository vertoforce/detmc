package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetRng;
import dev.detmc.DetTrace;
import java.util.concurrent.Executor;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.Iterator;
import java.util.List;
import java.util.Set;
import net.minecraft.world.entity.Mob;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.level.chunk.LevelChunk;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.ModifyArg;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Phase 2, step 1 — force all chunk work onto the calling (main) thread.
 *
 * <p>{@code ServerLevel.<init>} takes an {@code Executor} and uses it in exactly
 * one place: the {@code new ServerChunkCache(...)} call. That executor is the
 * single root of every background chunk path:
 *
 * <ul>
 *   <li>{@code new ConsecutiveExecutor(executor, "worldgen")}</li>
 *   <li>{@code new ConsecutiveExecutor(executor, "light")} — also handed to
 *       {@code ThreadedLevelLightEngine}, so this makes lighting synchronous too</li>
 *   <li>both {@code ChunkTaskDispatcher}s, whose own
 *       {@code PriorityConsecutiveExecutor("dispatcher")} takes the same executor</li>
 *   <li>{@code ChunkMap.DistanceManager}</li>
 * </ul>
 *
 * <p>Swapping one argument therefore covers all of them, and avoids having to
 * redirect four separate constructor calls in {@code ChunkMap}/{@code ChunkTaskDispatcher}
 * (two of which sit behind the package-private {@code ChunkMap$DistanceManager}).
 */
@Mixin(ServerLevel.class)
public class ServerLevelMixin {
    @ModifyArg(
            method = "<init>(Lnet/minecraft/server/MinecraftServer;Ljava/util/concurrent/Executor;Lnet/minecraft/world/level/storage/LevelStorageSource$LevelStorageAccess;Lnet/minecraft/world/level/storage/ServerLevelData;Lnet/minecraft/resources/ResourceKey;Lnet/minecraft/world/level/dimension/LevelStem;ZJLjava/util/List;Z)V",
            at = @At(
                    value = "INVOKE",
                    target = "Lnet/minecraft/server/level/ServerChunkCache;<init>(Lnet/minecraft/server/level/ServerLevel;Lnet/minecraft/world/level/storage/LevelStorageSource$LevelStorageAccess;Lcom/mojang/datafixers/DataFixer;Lnet/minecraft/world/level/levelgen/structure/templatesystem/StructureTemplateManager;Ljava/util/concurrent/Executor;Lnet/minecraft/world/level/chunk/ChunkGenerator;IIZLnet/minecraft/world/level/entity/ChunkStatusUpdateListener;Ljava/util/function/Supplier;)V"
            ),
            index = 4
    )
    private static Executor detmc$syncChunkExecutor(Executor original) {
        if (!DetExecutors.syncChunks()) {
            return original;
        }
        DetRng.LOGGER.info("[detmc] chunk executor forced same-thread (worldgen, light, dispatchers)");
        return DetExecutors.sameThread();
    }

    /** Phase 4 probe: a chunk leaving the live level, stamped with the tick. */
    @Inject(method = "unload(Lnet/minecraft/world/level/chunk/LevelChunk;)V", at = @At("HEAD"))
    private void detmc$traceUnload(LevelChunk chunk, CallbackInfo ci) {
        DetTrace.io("unload", chunk.getPos());
    }

    /**
     * Phase 4 — {@code navigatingMobs} is an {@code ObjectOpenHashSet<Mob>} and
     * {@code Mob} does not override {@code hashCode}, so it iterates in
     * {@code System.identityHashCode} order: stable inside one JVM, different between
     * two. {@code sendBlockUpdated} walks it on every block change to build
     * {@code navigationsToUpdate}, then calls {@code recomputePath()} in that order,
     * and the recomputation reads through the level-shared {@code pathTypesByPosCache}.
     *
     * <p>Measured: the same pair diverged at gametime <b>2009</b> in two independent runs,
     * a 3000-tick one and a 10000-tick one — a fixed tick, not a timing race, which is the
     * signature of a per-JVM iteration order rather than an IO race. In this world 60
     * stacked mobs are navigating while kelp and cave vines random-tick, which is about
     * 43,000 block changes per 3000 ticks, so this loop runs constantly.
     *
     * <p>Same fix as {@code DistanceManagerMixin}: iterate a copy in entity-id order.
     * The second loop in the method walks a {@code List}, so redirecting
     * {@code Set.iterator()} cannot hit it.
     */
    @Redirect(
            method = "sendBlockUpdated(Lnet/minecraft/core/BlockPos;Lnet/minecraft/world/level/block/state/BlockState;Lnet/minecraft/world/level/block/state/BlockState;I)V",
            at = @At(value = "INVOKE", target = "Ljava/util/Set;iterator()Ljava/util/Iterator;")
    )
    private Iterator<Mob> detmc$navigatingMobsInIdOrder(Set<Mob> navigatingMobs) {
        if (!DetExecutors.syncChunks() || navigatingMobs.size() < 2) {
            return navigatingMobs.iterator();
        }
        List<Mob> sorted = new ArrayList<>(navigatingMobs);
        sorted.sort(Comparator.comparingInt(Mob::getId));
        return sorted.iterator();
    }
}
