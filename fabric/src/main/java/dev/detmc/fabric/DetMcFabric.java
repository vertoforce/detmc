package dev.detmc.fabric;

import dev.detmc.DetCommands;
import dev.detmc.DetRng;
import net.fabricmc.api.ModInitializer;

/**
 * Announces the configuration and arms {@code /detmc}. All other behaviour lives in
 * the loader-agnostic mixins in {@code common/}. Deliberately does not touch Fabric
 * API, so the mod runs on a bare Fabric Loader install.
 *
 * <p>Arming rather than registering is why there is no {@code CommandRegistrationCallback}
 * here: that class ships in Fabric API, which is not installed on these servers. The
 * loader entrypoint opts the loader in, {@code CommandsMixin} does the registration
 * inside {@code Commands.<init>}, and the command itself requires permission level 2 so
 * rcon (which runs as OWNER) can call it. This entrypoint runs from
 * {@code Hooks.startServer} at the top of {@code Main.main}, well before the first
 * {@code Commands} is built.
 */
public class DetMcFabric implements ModInitializer {
    @Override
    public void onInitialize() {
        DetCommands.arm();
        DetRng.LOGGER.info("[detmc] loaded on Fabric: -D{}={}, -D{}={}; /detmc armed",
                DetRng.PROP_SEED, DetRng.propertySeed(),
                DetRng.PROP_FREEZE, DetRng.freezeOnStart());
    }
}
