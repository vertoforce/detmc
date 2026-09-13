package dev.detmc.mixin;

import dev.detmc.DetCommands;
import dev.detmc.DetRng;
import net.minecraft.commands.CommandBuildContext;
import net.minecraft.commands.Commands;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Adds {@code /detmc} to the dispatcher the {@code Commands} constructor just filled.
 *
 * <p>This is the delivery mechanism, not the policy. Fabric API is not on the runtime
 * classpath of these servers (the mod is loaded by a bare Fabric Loader install, so
 * {@code CommandRegistrationCallback} would be a {@code NoClassDefFoundError}), so the
 * loader entrypoint arms {@link DetCommands} and this mixin does the registration.
 * A loader that never arms it gets a vanilla dispatcher.
 *
 * <p>A new {@code Commands} is built per datapack reload, so this fires once per
 * dispatcher rather than once per server.
 */
@Mixin(Commands.class)
public class CommandsMixin {
    @Inject(method = "<init>", at = @At("RETURN"))
    private void detmc$registerDetmcCommand(Commands.CommandSelection selection, CommandBuildContext context,
                                            CallbackInfo ci) {
        if (!DetCommands.isArmed()) {
            DetRng.LOGGER.info("[detmc] /detmc not armed by any loader entrypoint; dispatcher left vanilla");
            return;
        }
        DetCommands.register(((Commands) (Object) this).getDispatcher());
    }
}
