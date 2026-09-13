package dev.detmc.mixin;

import it.unimi.dsi.fastutil.objects.ObjectList;
import net.minecraft.server.level.ThreadedLevelLightEngine;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;
import org.spongepowered.asm.mixin.gen.Invoker;

@Mixin(ThreadedLevelLightEngine.class)
public interface ThreadedLevelLightEngineAccessor {
    @Accessor("lightTasks")
    ObjectList<?> detmc$lightTasks();

    @Invoker("runUpdate")
    void detmc$runUpdate();
}
