package dev.detmc.mixin;

import net.minecraft.world.level.entity.EntityPersistentStorage;
import net.minecraft.world.level.entity.PersistentEntitySectionManager;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

/**
 * Phase 9 — {@code permanentStorage} is the {@code EntityStorage} whose IO worker writes
 * {@code entities/*.mca}; the save barrier in {@code MinecraftServerMixin} flushes it.
 */
@Mixin(PersistentEntitySectionManager.class)
public interface PersistentEntitySectionManagerAccessor {
    @Accessor("permanentStorage")
    EntityPersistentStorage<?> detmc$permanentStorage();
}
