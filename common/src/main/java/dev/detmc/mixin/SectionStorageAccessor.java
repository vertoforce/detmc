package dev.detmc.mixin;

import net.minecraft.world.level.chunk.storage.SectionStorage;
import net.minecraft.world.level.chunk.storage.SimpleRegionStorage;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

/** Phase 9 — the POI manager's region storage, so the save barrier can wait on its writes. */
@Mixin(SectionStorage.class)
public interface SectionStorageAccessor {
    @Accessor("simpleRegionStorage")
    SimpleRegionStorage detmc$simpleRegionStorage();
}
