package dev.detmc.mixin;

import dev.detmc.DetExecutors;
import dev.detmc.DetTrace;
import net.minecraft.world.level.ChunkPos;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;
import it.unimi.dsi.fastutil.longs.Long2ObjectLinkedOpenHashMap;
import it.unimi.dsi.fastutil.longs.LongIterator;
import java.util.List;
import net.minecraft.server.level.ChunkTaskPriorityQueue;
import org.jspecify.annotations.Nullable;
import org.spongepowered.asm.mixin.Final;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Overwrite;
import org.spongepowered.asm.mixin.Shadow;

/**
 * Phase 2, step 5 — deterministic chunk generation order.
 *
 * <p>Measured after the executors were forced same-thread and entities got their
 * own seed stream: entity UUIDs matched exactly, but run A populated the fox
 * chunk at {@code 198,-115} where run B populated the rabbit chunk at
 * {@code 202,-134}, and {@code ENTITY_COUNTER} was one apart from the tenth
 * entity on. Same chunks, same contents, different completion order.
 *
 * <p>Vanilla {@code pop()} takes {@code queue.firstLongKey()} from a
 * {@code Long2ObjectLinkedOpenHashMap}, so within one priority level the order is
 * the insertion order. Insertion happens as tickets are processed, and how many
 * of those land in a batch depends on {@code MinecraftServer}'s wall-clock task
 * budget. Taking the lowest packed {@code ChunkPos} instead makes the order a
 * function of the set of queued chunks alone.
 *
 * <p>Priority levels are untouched, so the "nearest chunks first" behaviour that
 * actually matters is preserved. Only the arbitrary tie-break within a level
 * changes.
 */
@Mixin(ChunkTaskPriorityQueue.class)
public abstract class ChunkTaskPriorityQueueMixin {
    @Shadow
    @Final
    private List<Long2ObjectLinkedOpenHashMap<List<Runnable>>> queuesPerPriority;

    @Shadow
    private volatile int topPriorityQueueIndex;

    @Shadow
    public abstract boolean hasWork();

    @Shadow
    @org.spongepowered.asm.mixin.Final
    private String name;

    /** Phase 7 probe: every submission, with the queue name and priority level. */
    @Inject(method = "submit(Ljava/lang/Runnable;JI)V", at = @At("HEAD"))
    private void detmc$traceSubmit(Runnable task, long chunkPos, int level, CallbackInfo ci) {
        if (DetTrace.tracingIo()) {
            DetTrace.io("cqSubmit", this.name + " " + ChunkPos.unpack(chunkPos) + " p" + level);
        }
    }

    /**
     * @author detmc
     * @reason Phase 2: pop the lowest chunk pos in the top priority level, not
     *         whichever was submitted first.
     */
    @Overwrite
    public ChunkTaskPriorityQueue.@Nullable TasksForChunk pop() {
        if (!this.hasWork()) {
            return null;
        }

        Long2ObjectLinkedOpenHashMap<List<Runnable>> queue = this.queuesPerPriority.get(this.topPriorityQueueIndex);
        long chunkPos = Long.MAX_VALUE;
        if (DetExecutors.sortedChunkPop()) {
            for (LongIterator it = queue.keySet().iterator(); it.hasNext(); ) {
                long key = it.nextLong();
                if (key < chunkPos) {
                    chunkPos = key;
                }
            }
        } else {
            // Phase 7: vanilla order. "Lowest ChunkPos among what is queued" depends on how
            // many tasks are queued at pop time, i.e. on where the tick boundary fell in the
            // task stream; insertion order does not.
            chunkPos = queue.firstLongKey();
        }

        List<Runnable> tasks = queue.remove(chunkPos);
        if (DetTrace.tracingIo()) {
            DetTrace.io("cqPop", this.name + " " + ChunkPos.unpack(chunkPos) + " p" + this.topPriorityQueueIndex + " n" + tasks.size());
        }

        while (this.hasWork() && this.queuesPerPriority.get(this.topPriorityQueueIndex).isEmpty()) {
            this.topPriorityQueueIndex++;
        }

        return new ChunkTaskPriorityQueue.TasksForChunk(chunkPos, tasks);
    }
}
