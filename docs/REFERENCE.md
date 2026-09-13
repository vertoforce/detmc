# detmc reference

Every JVM flag and every in-game command the detmc mod understands. The README
covers setup. This file is the full list.

Flags are Java system properties, passed before `-jar`:

```
java -Ddetmc.seed=8 -Ddetmc.traceIo=true -jar server.jar nogui
```

Booleans take `true` or `false`. Defaults come from the source.

## Core

Leave these alone for a normal deterministic server.

| Flag | Type | Default | What it does and when to set it |
|---|---|---|---|
| `detmc.seed` | long | `0` | Base RNG seed, XORed with the world seed to key every stream the mod hands out. Change it for a different, still repeatable, run of the same world. |
| `detmc.persistRng` | boolean | `true` | Saves the stream position to `detmc-rng.properties` in the world folder, so a restart continues the same timeline. |
| `detmc.syncRandom` | boolean | `true` | Routes the last seven `java.util.Random` and wall-clock seeded sites through the seeded stream. Set `false` to restore vanilla at all seven. |
| `detmc.syncChunks` | boolean | `true` | Runs chunk generation and loading on the server thread, fixing their order. The main ordering fix. Set `false` to measure its cost. |
| `detmc.syncTasks` | boolean | `true` | Runs a queued main-thread task in the tick that queued it, not a wall-clock-chosen tick up to three later. |
| `detmc.syncReads` | boolean | `true` | Region reads for chunks, entities, POI return before the caller continues, so entity construction lands at the request point. |

## Harness and debug

| Flag | Type | Default | What it does and when to set it |
|---|---|---|---|
| `detmc.freezeOnStart` | boolean | `false` | Freezes ticks just before tick 1. Set `true` when a harness attaches before the world moves. |
| `detmc.ioBarrier` | boolean | `false` | Waits for in-flight chunk writes at every tick end. Measured 2026-09-12: same first divergence, matching digests, so the join is off by default. |
| `detmc.verifyMixins` | boolean | `true` | Loads every lazy mixin target at the first tick, so a broken injection crashes at startup rather than hours in. |
| `detmc.sortedChunkPop` | boolean | `true` | Pops the lowest chunk position from the top priority level. Set `false` for vanilla insertion order. |

## Tracing

All off by default, all write to the server log, all cost speed. Turn one on to find
where two runs stopped matching.

| Flag | Type | Default | What it does and when to set it |
|---|---|---|---|
| `detmc.traceSeeds` | boolean | `false` | One line per seed handed out, in order, naming who took it. Finds the allocation where two runs part. |
| `detmc.traceEntities` | boolean | `false` | One line per entity added, in arrival order with its source. Shows which entity appeared where. |
| `detmc.traceSpawnLight` | boolean | `false` | One line per worldgen mob brightness check: chunk, position, raw brightness, verdict. Dates a mob divergence to one light read. |
| `detmc.traceIo` | boolean | `false` | One hash per tick over every entity's UUID, position, motion, plus a label wherever a background result enters game state. Dates a divergence to the tick. |
| `detmc.traceEntityWindow` | `lo:hi` list | empty, off | Inside the given gametime ranges, inclusive, dumps one line per entity per tick, naming the entity and the field that moved. Several ranges allowed: `-Ddetmc.traceEntityWindow=2100:2300,5000:5100`. |

## Commands

Both need permission level 2. An rcon console has it, so the scenario runner can call them.


| Command | Argument | What it does |
|---|---|---|
| `/detmc rng` | none | Prints one line: master seed, base seed, reseed state and argument, seeds issued, entity ordinal, gametime. Confirms two runs share a stream. |
| `/detmc reseed <seed>` | `<seed>`, a long | Re-keys the master stream and every live level and entity random, from this tick on. Branches one run into several sharing a prefix. |

`reseed` leaves a mob's shuffling lists, raid randoms, the wandering trader spawner
and the server random on their pre-reseed stream.
