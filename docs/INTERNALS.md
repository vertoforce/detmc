# detmc — deterministic RNG for headless Minecraft 26.2 servers

Goal: the same world seed + the same `-Ddetmc.seed` + the same scripted commands
replay identically on a headless server, so a long search (e.g. enderman block
placement) can be run across many parallel servers and any hit replayed exactly.

**Phase 1 closed seeding.** Vanilla seeds most `RandomSource` instances from
`System.nanoTime()` or `ThreadLocalRandom`, which is the first and largest source
of divergence.

**Phase 2 closes ordering.** One shared seed counter is only reproducible while
the order of allocation is. Worldgen, lighting and chunk IO all ran on
background threads. Two servers with identical world contents therefore reached
the counter in a different number of steps. Phase 2 forces that work onto the server thread and
sorts the two collections whose iteration order was a hash order.

## What it actually does

A single server-wide xoroshiro128++ stream in `DetRng`, keyed on
`-Ddetmc.seed` XOR the world seed, hands out every seed the game would otherwise
have taken from the clock. The `RandomSource` objects handed back are still
vanilla `LegacyRandomSource` / `SingleThreadedRandomSource`, so per-object draw
sequences are bit-identical to vanilla for a given seed — only the *seed*
becomes reproducible.

Mixins (all in `common/`, all targets verified against the decompiled 26.2
source in a local decompile of the 26.2 server jar):

| Mixin | Target | What it replaces |
|---|---|---|
| `RandomSupportMixin` | `RandomSupport.generateUniqueSeed()J` (`@Overwrite`) | `SEED_UNIQUIFIER ^ System.nanoTime()` → counter. Safety net: this feeds *every* `RandomSource.create()` / `createThreadSafe()` site, including the ones not redirected below. |
| `EntityMixin` | `Entity.<init>(EntityType;Level;)V` | `Entity.random`. Entity UUIDs derive from it, so they follow. |
| `LevelMixin` | `Level.<init>(WritableLevelData;ResourceKey;RegistryAccess;Holder;ZZJI)V` | all three variants: `createThreadLocalInstance()` (`randValue`, used by `getBlockRandomPos` → every random tick), `create()` (`random`), `createThreadSafe()` (`soundSeedGenerator`). |
| `ShufflingListMixin` | `ShufflingList.<init>` (both ctors, neither chains) | `random` |
| `RaidMixin` | `Raid.<init>` (both ctors, neither chains) | `random` |
| `WanderingTraderSpawnerMixin` | `WanderingTraderSpawner.<init>(SavedDataStorage;)V` | `random` |
| `MinecraftServerMixin` | `MinecraftServer.<init>` | `random` |
| `MinecraftServerMixin` | `MinecraftServer.createLevels()V` → `WorldOptions.seed()J` | binds the world seed into `DetRng` at the earliest point it exists, before the overworld `ServerLevel` is built |
| `MinecraftServerMixin` | `MinecraftServer.tickServer(BooleanSupplier)V` `@At("HEAD")` | one-shot: logs the master seed and, if `-Ddetmc.freezeOnStart=true`, calls `tickRateManager().setFrozen(true)` before tick 1 |
| `AngerManagementMixin` | `AngerManagement.<init>(Predicate;List;)V` | `createThreadLocalInstance()` for `conversionDelay` |
| `CommandsMixin` | `Commands.<init>` (`@Inject` RETURN) | registers `/detmc` into the dispatcher the constructor just filled, if a loader entrypoint armed `DetCommands`. Not a seeding or ordering fix; see [Commands](#commands). |
| `LevelRandValueAccessor` | `Level.randValue` (`@Accessor`) | lets `/detmc reseed` write the int LCG behind `getBlockRandomPos`. There is no `RandomSource` left to call `setSeed` on. |

Phase 2 adds three more, all ordering rather than seeding:

| Mixin | Target | What it changes |
|---|---|---|
| `UtilMixin` | `Util.backgroundExecutor()` (`@Inject` HEAD, cancellable) | returns a `TracingExecutor` over the same trampoline. Chunk generation steps hop onto this pool directly (`wgen_fill_noise`, `init_biomes`, `parseChunk`, `structureRings`) and their `thenApply` continuations then run on whichever thread completed the future. `ioPool()` is left async on purpose. |
| `ServerLevelMixin` | `ServerLevel.<init>` → `@ModifyArg` index 4 of `new ServerChunkCache(...)` | swaps the background `Executor` for a same-thread one. That one argument is the root of every async chunk path: both `ConsecutiveExecutor`s in `ChunkMap.<init>` (`"worldgen"` and `"light"`), the `PriorityConsecutiveExecutor` inside each of the two `ChunkTaskDispatcher`s, and `ChunkMap.DistanceManager`. The `"light"` executor is also what `ThreadedLevelLightEngine` runs on, so lighting becomes synchronous for free. |
| `PersistentEntitySectionManagerMixin` | `processPendingLoads()V` (`@Overwrite`), plus `@Inject` on `addWorldGenChunkEntities` and `addEntity` | `loadingInbox` is a `ConcurrentLinkedQueue` fed by entity-storage IO futures, so chunks arrive in completion order. The overwrite drains the whole batch, sorts it by packed `ChunkPos`, then adds. Entity ids (`ServerLevel.ENTITY_COUNTER`) and therefore tick order follow. The injects are the `-Ddetmc.traceEntities` diagnostic. |
| `DistanceManagerMixin` | `forEachEntityTickingChunk` → `@Redirect` on `Long2ByteMaps.fastIterable`, and `getSpawnCandidateChunks` → `@Redirect` on `LongSet.iterator` | returns the chunk entries sorted by packed `ChunkPos` instead of hash order. This is what `ChunkMap.forEachBlockTickingChunk` walks. Redirecting `fastIterable` avoids naming `ChunkMap$DistanceManager`, which is package-private and so unreachable from `dev.detmc.mixin`. |
| `ChunkTaskPriorityQueueMixin` | `pop()` (`@Overwrite`) | pops the lowest packed `ChunkPos` in the top priority level instead of `firstLongKey()`, so chunk generation order stops depending on submission order. Priority levels are untouched. Measured: **no effect on the remaining divergence**, kept as an invariant. |
| `ChunkStepBuilderMixin` | `ChunkStep$Builder.build()` (`@Inject` HEAD) | adds `addRequirement(LIGHT, 1)` when the step is `SPAWN`. `ChunkPyramid.GENERATION_PYRAMID` requires only `BIOMES` r1 there, while `ChunkStatusTasks.generateSpawn` reads live sky light through `WorldGenRegion.getLightEngine()`. Verified to fire (2 hits, one per pyramid). On its own it did **not** fix the entity count; kept as an ordering invariant. |
| `ChunkStatusTasksMixin` + `ThreadedLevelLightEngineAccessor` | `generateSpawn` (`@Inject` HEAD) | loops `runUpdate()` until `lightTasks` is empty and `hasLightWork()` is false, cap 64. Measured: fires for about 4 chunks in a whole forceload, so the light queue was almost never the problem. |
| `ChunkMapMixin` | `ChunkMap.tick` → `@ModifyArg` index 0 of `processUnloads(BooleanSupplier)` | passes a constant `true`. `processUnloads` drains `unloadQueue` for whatever is left of the 50 ms tick, and each unload calls `lightEngine.updateChunkStatus`, which drops that chunk's light sections. That is what made a worldgen spawn check read sky light 14 in one run and 9 in the other at the same block. |
| `MinecraftServerMixin` | `tickServer(BooleanSupplier)` `@At("TAIL")` | polls every level's chunk source to a fixpoint, so no chunk task straddles a tick boundary and tick-level work stops being interleaved at a clock-dependent point. |
| `DistanceManagerMixin` | `runAllUpdates(ChunkMap)` → `@Redirect` on both `Set.iterator()` calls | `chunksToUpdateFutures` is a `ReferenceOpenHashSet<ChunkHolder>`, i.e. `System.identityHashCode` order, and those two loops are what submit chunk generation steps. Sorted by `ChunkPos.pack()`. This is the fix that made entity UUIDs reproducible. |
| `AnimalMixin` | `Animal.isBrightEnoughToSpawn` (`@Inject` RETURN) | diagnostic only, off unless `-Ddetmc.traceSpawnLight=true`. |
| `ChunkMapMixin` | `saveChunksEagerly` and `saveAllChunks(Z)` → `@Redirect` on `Util.getMillis()` | returns `level.getGameTime() * 50`. `saveChunkIfNeeded` gates on `now < nextChunkSaveTime`, where `nextChunkSaveTime` was set to `now + 10000` ms at the last save, so the tick a chunk was saved on was a function of elapsed milliseconds. Measured: at gametime 206 of one 3000-tick segment, A ran `save [4, -7]` and B ran nothing; 1119 eager saves against 1124 over the segment. On the world clock the 10 s interval is exactly 200 ticks everywhere. |
| `ChunkMapMixin` | `saveChunksEagerly` → `@Redirect` on `AtomicInteger.get()` | the same loop also gates on `activeChunkWrites.get() < 128`, and a background write completion is what decrements it. Reports 0; the 20-saves-per-call cap still bounds the loop. |
| `SectionStorageMixin` | `SectionStorage.tick` → `@Redirect` on `BooleanSupplier.getAsBoolean()` | constant `true`, so `PoiManager` writes its whole dirty set instead of as much of it as fits in the rest of the 50 ms tick. Same shape as the `processUnloads` fix. |
| `MinecraftServerMixin` | `tickServer` TAIL, `@Shadow` on `ticksUntilAutosave` | vanilla counts autosave down in *server* ticks, and `tickServer` runs while the tick rate manager is frozen too. Two replicas sit frozen for a different number of ticks during the scripted setup, so the autosave landed at a different gametime on each. The countdown is pinned and the save is fired from the tick tail on a fixed 6000-**gametime** grid. |
| `MinecraftServerMixin` | `saveEverything(ZZZ)Z` `@At("TAIL")`, and the `createLevels` seed redirect | phase 5 restart persistence. Writes `DetRng`'s stream position into `<world>/detmc-rng.properties` on every save, and restores it right after `bindWorldSeed` — after the rekey, before `prepareLevels` constructs the first entity. Without it a restart put `issued` and `entityOrdinal` back to 0. |
| `EntityStorageMixin`, `SectionStorageMixin`, `ChunkMapMixin`, `ServerLevelMixin` | various, `@Inject` | phase 4 probe, off unless `-Ddetmc.traceIo=true`. One `[detmc-io]` line at each point where a background result enters main-thread game state, plus one `[detmc-dg]` digest line per tick over every entity's UUID, position and motion. |
| `IOWorkerMixin` | `IOWorker.loadAsync(ChunkPos)` (`@Inject` RETURN, cancellable) | phase 7. Every region read (chunk via `ChunkMap extends SimpleRegionStorage`, entity via `EntityStorage`, POI via `SectionStorage`) is joined on the calling thread before it is returned, so its continuation runs at the request point rather than at IO-completion time. Measured: `EntityStorage.loadEntities` deserialises on the `MinecraftServer` queue when the read lands, and two runs of one jar (test/phase7/A1 vs A2) constructed the loaded spawn-area animals one or two `ENTITY_COUNTER` ids apart, which also moves their `Entity.random` ordinal and one worldgen goat's UUID. Writes stay asynchronous. `-Ddetmc.syncReads=false` opts out. |
| `LongJumpToRandomPosMixin` | `calculateOptimalJumpVector` → `@Redirect` on `Collections.shuffle(List)` | phase 7. The goat long jump shuffled its launch angles with `new java.util.Random()` (seeded from `System.nanoTime()`) and used the first angle that worked. Measured: runs identical for gametimes 0..2198 diverged at 2199 on exactly one entity, goat id 17, motion y 0.888 against 0.729. Now `Util.shuffle(list, body.getRandom())`. Not covered: the villager-only `InsideBrownianWalk` (same call inside a synthetic lambda) and `@e[sort=random]`. |
| `LegacyRandomSourceMixin` + `DetDrawCounter` | `LegacyRandomSource.next(I)I` (`@Inject` HEAD) | phase 7 diagnostic, always on, a counter increment per draw. The `[detmc-dg]` line gains `levelRng=<draws>,<state>` and each `[detmc-ew]` row gains the entity RNG's `<draws>,<state>`, so a diff shows whether an entity consumed its RNG differently or merely computed differently. |
| `EntityStorageMixin` | `loadEntities` → `@Redirect` on `thenApplyAsync(Function, Executor)` | phase 7. With reads joined, vanilla still queued the deserialisation as a `TickTask` on the `MinecraftServer` queue, while worldgen entities are constructed inline in chunk tasks, so which ran first was decided by the between-tick wall-clock loop. Measured (test/phase7/A10k vs B10k, syncReads jar): one run split the entity inbox across two ticks and the loaded animals' ids and ordinals moved by one or two. Deserialises inline when the read is already complete. |
| `PersistentEntitySectionManagerMixin` | `processPendingLoads()V` (`@Inject` HEAD; the phase 2 `@Overwrite` is gone) | phase 7. The overwrite dropped vanilla's `chunkLoadStatuses.put(pos, LOADED)`, so every chunk stayed `PENDING`, `storeChunkSections` always returned false and the mod wrote **0 bytes of `entities/*.mca`** on every save (one-variable control: unmodded Fabric wrote 25 KB). Vanilla's body now runs; the inject only reorders the inbox in place, and only sorts it on the async path (`-Ddetmc.syncReads=false`), because with synchronous reads the inbox fills in request order and a per-batch sort would make the add order depend on where the batches split. |
| `MinecraftServerMixin` | `saveEverything(ZZZ)Z` TAIL | phase 7: logs `[detmc] saveEverything done: flush=.. force=.. gt=..`. `SaveAllCommand` is silent and `save-all flush` over rcon times out on a busy server (measured in every phase 7 run), so this line is the only evidence a save finished; `run-det.sh` waits for it before hashing region files. |
| `EntityMixin` | `Entity.<init>` (`@Inject` RETURN) | phase 7 probe, on with `-Ddetmc.traceEntities=true`: one `[detmc-ec]` line per construction with id, `DetRng` ordinal, type, thread and caller. Construction order is what assigns ids and `Entity.random` seeds; `test/phase7/E*/ec-*.txt` are the reference traces. |
| `MinecraftServerMixin` | `pollTaskInternal()Z` (`@Inject` HEAD, cancellable) | phase 7. Polls every level's chunk source before the server's own task queue, so an rcon command (or any other-thread task) runs only when the chunk pipeline is quiescent. Measured: with vanilla order, `forceload add` landed at an arbitrary point inside the spawn-chunk generation and flipped the construction order of two worldgen mobs (their ids and RNG seeds) between runs. Also gates on `-Ddetmc.syncTasks`. |
| `ChunkTaskPriorityQueueMixin`, `MinecraftServerMixin` | `submit`/`pop`, `tickServer` HEAD/TAIL, `waitUntilNextTick` HEAD | phase 7 probes, on with `-Ddetmc.traceIo=true`: `[detmc-io] cqSubmit/cqPop <queue> <pos> p<level>` and `[detmc-io] phase tickStart|drainStart|drainEnd|wait`. |
| `AttributeMapMixin` | `AttributeMap.pack()` (`@Inject` RETURN) | phase 7. The saved `attributes` list is walked out of an `Object2ObjectOpenHashMap<Holder<Attribute>, ...>` and `Holder.Reference` hashes by identity, so two runs with identical entity state wrote `entities/*.mca` that differed only in that list's order (test/phase7/G2 vs G3: 4 of 37 chunks; `test/nbt-canon.py` shows them identical once the lists are sorted). Sorted by registered name on save. |
| `ChunkTaskPriorityQueueMixin` | `pop()` | phase 7 adds `-Ddetmc.sortedChunkPop=false`, which restores vanilla insertion-order pop; see STATUS for the measurement. |

Phase 8 is a sweep rather than a bug hunt: every remaining use of a JDK random class,
of the wall clock, and of an identity-hashed collection was classified against the
decompiled server source, and the game-visible ones were fixed. All thirteen are gated on
`-Ddetmc.syncRandom` (default on) and **none of them is on the harness world's path**, so
the 24000-tick pair does not test them; the full classification, including everything
deliberately left alone, is in STATUS.md.

| Mixin | Target | What it changes |
|---|---|---|
| `InsideBrownianWalkMixin` | `lambda$create$2` → `@Redirect` on `Collections.shuffle(List)` | the villager indoor walk shuffled its 27 candidate positions with `new java.util.Random()` (`System.nanoTime()`), the same defect as the phase 7 goat jump. Now `Util.shuffle(poses, body.getRandom())`. |
| `EntitySelectorParserMixin` | `lambda$static$6` (the body of `ORDER_RANDOM`) → `@Redirect` on `Collections.shuffle(List)` | `@e[sort=random]`, the last `Collections.shuffle` in the jar. Now shuffled with the level's `RandomSource`, taken from the first selected entity. |
| `PlayerSpawnFinderMixin` | `<init>(ServerLevel;BlockPos;I)` → `@Redirect` on `RandomSource.createThreadLocalInstance()` | the spawn search's starting offset, i.e. the block a player lands on, came from `ThreadLocalRandom`. `createThreadLocalInstance()` is the one factory `RandomSupportMixin` does not cover. |
| `SpreadPlayersCommandMixin` | `spreadPlayers(...)` → same redirect | `/spreadplayers` drew every destination from `ThreadLocalRandom`. |
| `StructureBlockEntityMixin` | `createRandom(J)` → `@Redirect` on `Util.getMillis()` | a structure block with the default seed 0 seeded its integrity RNG from the wall clock. Now `gameTime * 50`. |
| `StructurePlaceSettingsMixin` | `getRandom(BlockPos)` → same redirect | the `pos == null` fallback of template placement did the same. |
| `StopwatchesMixin` | `currentTime()J` → same redirect | `/stopwatch` measures real milliseconds, commands branch on it and `pack()` writes it to `data/stopwatches.dat`. A stopwatch now counts ticks in millisecond units. |
| `ItemEnchantmentsMixin` | `entrySet()` (`@Inject` RETURN) | `Object2IntOpenHashMap<Holder<Enchantment>>`, identity-hashed, and both `EnchantmentHelper.runIterationOnItem` overloads walk it — so every enchantment effect ran in per-JVM order, and two of those call sites thread a `RandomSource` through the loop. Sorted by registered name. |
| `AbstractFurnaceBlockEntityMixin` | `getRecipesToAwardAndPopExperience` → `@Redirect` on `FastEntrySet.iterator()` | `recipesUsed` is keyed by `ResourceKey`, which has no `hashCode`, and each entry draws `level.getRandom().nextFloat()` for its XP fraction. Sorted by recipe identifier. |
| `PoiSectionMixin` | `getRecords` → `@Redirect` on `Map.entrySet()` | `byType` is a `HashMap<Holder<PoiType>, …>`; the distance sort downstream is *stable*, so identity order decided which job site or portal a mob claimed. Sorted by registered name. |
| `StackedContentsMixin` | `getUniqueAvailableIngredientItems` (`@Inject` RETURN) | `Reference2IntOpenHashMap<Holder<Item>>`; the recipe book takes the first viable assignment, so identity order picked which stack was spent. Sorted by registered name. |
| `ScoreboardMixin` | `packPlayerScores()` (`@Inject` RETURN) | `Reference2ObjectOpenHashMap<Objective, Score>` decided the order of `scoreboard.dat`. Save-order only. |
| `ServerRecipeBookMixin` | `pack()` → `@Redirect` on `List.copyOf(Collection)` | `known` and `highlight` are `Sets.newIdentityHashSet()`, and both are codec'd into the player `.dat`. Save-order only. |

Mixin applies a configuration when its target class is first loaded, so a wrong descriptor
in any of the above would not fail at startup — it would fail hours later, the first time
something enchanted was swung. `DetMcBootstrap.verifyLazyMixinTargets` therefore loads all
fourteen lazy targets (the thirteen above plus `LongJumpToRandomPos`) at the first server
tick and logs `[detmc] 14 lazy mixin targets loaded and applied`.
`-Ddetmc.verifyMixins=false` skips it.


### Is the saved world the same?

Three checks, in increasing strength, all in `test/`:

| script | what it compares | blind to |
|---|---|---|
| `region-md5.py` | md5 of each `.mca` with the timestamp table blanked | nothing, but it *fails* on differences that are not world state: which partial neighbour chunks got saved, and where in the file a chunk sits |
| `nbt-canon.py` | one canonical line per chunk, compound keys sorted | NBT key order (which is the point) |
| `mca-payloads.py` | sha1 of each chunk's decompressed payload bytes, plus its sector offset | nothing at all, so this is the authority |

Measured 2026-09-12 over both a 24000-tick pair and a resume pair: **zero chunks differed in
payload bytes in any file**, while raw md5 differed in up to 4 of 13 files. Use md5 as a
screening test and `mca-payloads.py` to decide.

### Throughput

Measured 2026-09-12, `test/tps-bench3.sh 20000`, six interleaved sprints from a fresh world
each, one container at a time, `MEMORY=2G`: **detmc 212 TPS / 4.70 ms per tick (median of
188, 212, 224) against 239 TPS / 4.18 ms (median of 237, 239, 282)** for the same image with
`./mods` emptied, i.e. **1.13x slower**, with java %CPU 152-154 against 140-142. The two
sets do not overlap. Earlier sections of STATUS quote a 1.2-2.3x range; that came from
re-sprinting one container and is superseded.

### Flags required for a deterministic run

All four are needed. Dropping any one of them reintroduces divergence.

```
-Ddetmc.seed=<long>         # same value on every replica
-Ddetmc.freezeOnStart=true  # no ticks until the scripted setup has run
-Dmax.bg.threads=1          # shrink anything detmc has not captured to one thread
-Ddetmc.traceIo=true        # optional: per-tick entity digest + async hand-off trace
-Ddetmc.traceEntityWindow=0:3,2190:2210   # optional: one [detmc-ew] row per entity per tick inside each lo:hi window
```

`detmc.syncReads` (phase 7, region reads joined at the request point) is on by default
and must stay on; it is what makes entity ids and `Entity.random` seeds of *loaded*
entities reproducible.

`detmc.syncRandom` (phase 8, the JDK-`Random` and identity-order sweep) is also on by
default. Nothing on the harness world reaches any of its thirteen sites, so turning it off
does not change any measurement in this file; it is the switch for a world that has
players, villagers, enchantments, furnaces, scoreboards or `@e[sort=random]`.

plus the same world `SEED` and the same mod set on every replica. `detmc.syncChunks`
is on by default and must stay on.

**Replay from save (phase 8, measured 2026-09-12):** two servers restarted from the same
save agree for the next 12000 ticks, at every one of 12001 gametimes. A resumed run does
**not** match the uninterrupted timeline, and cannot: vanilla serialises no `RandomSource`
state, so `Level.random` and every `Entity.random` restart from a fresh draw position.
`detmc-rng.properties` carries the seed stream across the restart, not the draw positions.

**Verified lengths, one uninterrupted segment, stacked mobs, symmetric harness
(`test/phase7-run.sh`, one container at a time, fresh world each run, `tick step` only).**
Measured 2026-09-12 with the phase 7 fixes (commit 61d213b); "first divergent gametime"
is over the per-tick entity digest at every gametime:

| Segment | Pos / UUID / Motion / carriedBlockState | Region, timestamp-blind | entities/*.mca | First divergent gametime |
|---|---|---|---|---|
| 100 (x3) | MATCH | MATCH 12 files (2 of 3 pairs; one `region/r.-1.-1.mca` differs with equal state, unexplained) | identical | none |
| 10000 | MATCH | MATCH (12 files) | 330,637 B both, canonically identical | none |
| 24000 | MATCH | MATCH (12 files) | 330,476 B both, canonically identical | none |
| 24000 repeat (fresh worlds) | MATCH; identical to the first pair at every gametime | 11 of 12; `region/r.-1.-1.mca` holds 35 extra never-loaded partial chunks on one side (IO-timed unload), all shared chunks identical | identical | none |

Entity ids are not in this table because `data get entity @s id` returns nothing; they
are checked through the `[detmc-ew]` rows and the `[detmc-ec]` construction trace, which
were identical in every passing pair above.

Cost: chunk work is now serial. A fresh `SEED=12345` world reached
`Done` in 11.6s against 4.0s for the unmodified build, measured on the same host.

### System properties

| Property | Default | Meaning |
|---|---|---|
| `-Ddetmc.seed=<long>` | `0` | XORed with the world seed to key the stream |
| `-Ddetmc.freezeOnStart=true` | `false` | freeze ticks before tick 1, so the scripted setup runs against a still world |
| `-Ddetmc.syncChunks=false` | on | opt **out** of the phase-2 same-thread executors. Only useful for bisecting. A deterministic run wants it on. |
| `-Ddetmc.traceEntities=true` | `false` | log every `addEntity` in arrival order (`[detmc-ea]`: source `worldgen` / `other`, id, type, UUID, position) and every `Entity` construction (`[detmc-ec]`: id, `DetRng` ordinal, type, thread, caller). Construction order assigns ids and `Entity.random` seeds, so `ec` is the order that has to match. |
| `-Ddetmc.traceEntityWindow=lo:hi[,lo:hi]` | off | one `[detmc-ew]` row per entity per tick inside each window: uuid, type, id, pos, motion, rot, tickCount, entity RNG draws and state. `test/analyze-ew.py` diffs two runs. |
| `-Ddetmc.syncReads=false` | on | opt **out** of joining region reads (chunk, entity, POI) at the request point and of inline entity deserialisation. Off, loaded entities take their ids and seeds at IO-completion time. |
| `-Ddetmc.syncTasks=false` | on | opt **out** of the pinned `shouldRun(TickTask)` and of polling chunk work before the server task queue. Off, an rcon command runs at a wall-clock point inside in-flight chunk generation. |
| `-Ddetmc.sortedChunkPop=false` | on | vanilla insertion-order pop in `ChunkTaskPriorityQueue` instead of the lowest `ChunkPos`. Measured: makes no difference to the startup flip; kept as a bisect switch. |
| `-Ddetmc.ioBarrier=true` | off | wait for in-flight chunk writes at every tick end. Measured: no effect on any divergence. |
| `-Ddetmc.traceSpawnLight=true` | `false` | log every `Animal.isBrightEnoughToSpawn` call in order: the chunk whose SPAWN step is running, the block position, the raw brightness read and the verdict. This is the trace that split the worldgen defect into "the light value is wrong" and "the chunk order is wrong". |
| `-Ddetmc.persistRng=false` | on | opt **out** of writing `<world>/detmc-rng.properties` at every save and restoring it at load. On, the shared xoroshiro state, the seeds-issued count and the entity ordinal survive a restart, so a run resumed from a save keeps handing out the seeds the uninterrupted timeline would have. Note the limit: vanilla serialises no `RandomSource` draw position, so `Level.random` and every `Entity.random` still restart from their seed on load. |
| `-Ddetmc.traceSeeds=true` | `false` | log every `DetRng` allocation in order, with the thread name and three caller frames. This is what found the `Worker-Main-1` divergence. Thousands of lines per run. |

Startup logs `[detmc] seed source armed: …`, then `[detmc] world seed N bound;
master seed = …`, then `[detmc] first server tick: master seed N (K seeds issued
during startup)`.

### Commands

Two, both permission level 2 (`Commands.LEVEL_GAMEMASTERS`). The rcon console source is
`LevelBasedPermissionSet.OWNER`, so `rcon-cli` can call them; a non-op player cannot.

| Command | Effect |
|---|---|
| `/detmc rng` | prints one line: `masterSeed`, `baseMaster`, `reseeded`, `reseedArg`, `issued`, `entityOrdinal`, `gametime`. Regex-parseable, for the scenario runner. |
| `/detmc reseed <long>` | branches the run onto a new RNG schedule at this instant. |

`reseed N` sets the master to `N ^ worldSeed`, i.e. exactly the master a server launched
with `-Ddetmc.seed=N` on this world would have had, so a branch is named by one number.
It re-keys the shared xoroshiro stream (and therefore resets `issued`, which is "seeds
since the last re-key" by definition). It changes nothing else about the world: not the
gametime, not the world seed binding, not `entityOrdinal`, not a single block.

Re-keying the master on its own would not change mob behaviour for a long time, because
every `RandomSource` that already exists keeps drawing from where it was. So the command
also re-keys the live ones:

| Stream | How |
|---|---|
| `Level.random`, every `ServerLevel` | `setSeed(derive(newMaster, LEVEL, hash(dimension id)))` |
| `Level.randValue`, every `ServerLevel` | written through `LevelRandValueAccessor`. This is the LCG that picks random-tick block positions. |
| `Entity.random`, every loaded entity in every level | `setSeed(derive(newMaster, ENTITY_LIVE, entity.getId()))` |

The ordinal is the entity id or the dimension name's hash, never the iteration position:
`ServerLevel.getAllEntities()` walks a hash order, so an order-dependent derivation would
not be reproducible.

**Not re-keyed, and known.** `ShufflingList.random` (villager and piglin brains),
`Raid.random`, `WanderingTraderSpawner.random`, `MinecraftServer.random`, and the draw
position of any other `RandomSource` handed out before the reseed that is not reachable
from a level or an entity. Those were seeded from the pre-reseed master and keep their old
stream until they are reconstructed. Entities created *after* the reseed are unaffected by
this list: they draw from `DetRng.entitySeed()`, which is keyed on the live master.

**Persistence.** `<world>/detmc-rng.properties` gained `baseMaster`, `reseeded` and
`reseedArg`; `masterSeed` now holds the live key rather than `-Ddetmc.seed ^ worldSeed`.
`restoreState` validates the file against the world using `baseMaster` and then adopts the
saved `masterSeed`, so a save taken after a reseed comes back up on the reseeded branch.
Files written before this change have no `baseMaster` and fall back to `masterSeed`, which
is what they meant. The pre-existing limitation is unchanged: vanilla serialises no
`RandomSource` draw position, so a reloaded entity restarts its stream from
`entitySeed(ordinal)` and not from whatever `/detmc reseed` wrote into it.

Caveat measured 2026-09-12: on a busy detmc server `save-all flush` over rcon returns
`i/o timeout` and the save does not land for at least 20 minutes, so do not read the
sidecar straight after it. `SaveAllCommand` passes `silent=true`, so there is no log line
to wait on either. STATUS.md has the numbers.

Registration is in `CommandsMixin`, not in a Fabric API `CommandRegistrationCallback`:
these servers run a bare Fabric Loader install with no Fabric API jar, so that class is
not on the runtime classpath. The Fabric entrypoint (`DetMcFabric.onInitialize`) arms
`DetCommands`; the mixin registers only if armed, so a loader that does not opt in gets a
vanilla dispatcher. Startup logs `[detmc] /detmc registered (rng, reseed; permission
level 2)`.

### Known limitation (phase 1 by design)

Seeds come off one counter, so reproducibility holds only while the *order* of
allocation is reproducible. That is exactly what phase 2 fixes. Two consequences
worth knowing:

- `Level` seeds are stream-ordinal, not dimension-derived. The scope doc proposed
  deriving them from world seed + dimension; that is not reachable from a
  `@Redirect` inside `Level.<init>` because the field initialisers run before
  `this.dimension` is assigned. Stream-ordinal is equivalent as long as level
  construction order is stable, which it is on a fresh server.
- `MinecraftServer.random` is seeded before the world seed is known, so it comes
  off the property-only stream. Reproducible either way; just note that changing
  the world seed does not change that one object.

## Build

No host JDK, no host Gradle, no host Minecraft. Everything runs in
`eclipse-temurin:25` pinned by digest.

```bash
./fetch-deps.sh              # once: server jar + bundled libs into ./libs
./docker-gradle.sh :fabric:build
# -> fabric/build/libs/detmc-0.1.0.jar
```

`docker-gradle.sh` mounts `./.gradle-home` at `/root/.gradle`, so the Gradle
distribution and the dependency cache survive between runs.

## Toolchain — and why there is no Fabric Loom

Verified 2026-09-11 against `meta.fabricmc.net`, `api.modrinth.com` and
`maven.neoforged.net`:

| Component | Version |
|---|---|
| Minecraft | `26.2` (release; `javaVersion` 25, class major 69) |
| Fabric Loader | `0.19.5` |
| Fabric API | `0.160.0+26.2` (declared `compileOnly`, not used yet) |
| NeoForge | `26.2.0.87` (stub only this phase) |
| ModDevGradle | `2.0.146` (stub only this phase) |
| Gradle | `9.7.1` |

The scope doc's numbers were all still current. What *is* stale is the plan to
use Loom:

> **Fabric Loom cannot configure Minecraft 26.2.** Both `1.17.20` (current
> stable) and `1.18.0-alpha.21` fail at configuration time with
> `Failed to setup Minecraft, java.lang.RuntimeException: Failed to find official
> mojang mappings for 26.2`. Measured cause: the 26.2 entry in
> `piston-meta.mojang.com/mc/game/version_manifest_v2.json` has only `client` and
> `server` under `downloads` — no `client_mappings` / `server_mappings`. Mojang
> now ships the server jar already deobfuscated: `server-26.2.jar` contains 7753
> `net/minecraft/**` entries including `net/minecraft/world/entity/Entity.class`,
> and zero single-letter obfuscated class names.

Consequences, all verified rather than assumed:

- There is nothing to remap. A production Fabric 26.2 install
  (`fabric-server-mc.26.2-loader.0.19.5-launcher.1.1.2.jar`) carries **no
  intermediary jar** under `libraries/` at all, so build namespace and runtime
  namespace are both "official"/Mojang.
- So this build skips Loom entirely and compiles straight against the shipped
  `server-26.2.jar` plus the libraries bundled in Mojang's server bundler
  (`fetch-deps.sh` extracts both). `jar` *is* the mod jar; there is no `remapJar`.
- When Loom gains 26.x support, only `common/build.gradle` and
  `fabric/build.gradle` need to change. Nothing in `src/` assumes anything about
  mappings.

Mixin comes from `net.fabricmc:sponge-mixin:0.17.4+mixin.0.8.7` — the exact
version Fabric Loader 0.19.5 ships — and `detmc.mixins.json` declares
`compatibilityLevel: JAVA_25` (that constant exists in this Mixin build; checked).
There is no refmap, which is correct for a single-namespace build.

## Layout

```
common/     loader-agnostic: DetRng, DetMcBootstrap, all 8 mixins, detmc.mixins.json
fabric/     fabric.mod.json + a ModInitializer that only logs; bundles common/'s classes
neoforge/   PHASE 1 STUB. settings-included, no plugins, produces nothing.
            NeoForge patches Entity.<init> but not the RandomSource.create() call
            sites, so common/ is expected to apply unchanged. Wiring notes are in
            neoforge/build.gradle.
test/       two-server determinism harness
libs/       gitignored; populated by fetch-deps.sh
```

## Test

```bash
test/run-tests.sh --list                     # the cases and what each expects
test/run-tests.sh --case early-1000          # fastest match case, 6.6 min measured
test/run-tests.sh --case vanilla-control     # the control, which must diverge, 5.1 min
test/run-tests.sh --all --junit out.xml      # everything, with a JUnit report
test/run-tests.sh --case X --compare-only    # re-print a verdict, start no servers
```

`test/README.md` lists every case and what it costs. `test/cases/README.md` is the
`case.yaml` schema. To bisect a divergence, add checkpoints to a case: `early-1000`
already dumps at ticks 0, 1, 5, 20, 100 and 300.

Tick 0 is the fastest signal there is. The phase-1 divergence was already present
before a single tick ran, so the sprint told us nothing the tick-0 capture did not.

Every case **settles before dumping**. Chunk loading keeps running while ticks
are frozen, so a dump taken straight after `forceload` catches a different number
of entities on each server. That looked like a determinism bug in phase 1 (sheep
on one server, pigs on the other) and was not one. They poll until the entity
list stops changing.

One inherited bug is worth knowing about, because it invalidates part of the
phase 1 table. `sprint()` waited for the string `Sprint completed` to appear in
the container log at all, not for a new one, so the second call returned off the
first sprint's line and the next `tick freeze` aborted the sprint. Measured:
`time query gametime` said **126** after a nominal 23900-tick sprint. Every
phase 1 row labelled `t24000` was really about tick 126. Fixed by counting
occurrences.

Both also retry a container once. The `itzg` image downloads the Fabric server
jar on first start and that download fails here often enough to matter
(`java.net.SocketException: Network is unreachable`).

`test/compose-det.yml` runs two `itzg/minecraft-server` containers pinned by
digest, `TYPE=FABRIC`, `VERSION=26.2`, `SEED=12345`, `./mods` bind-mounted at
`/data/mods`, and `JVM_OPTS=-Ddetmc.seed=1 -Ddetmc.freezeOnStart=true
-Dmax.bg.threads=1 ${DETMC_EXTRA_OPTS}`. Export `DETMC_EXTRA_OPTS` to turn the
traces on, e.g. `DETMC_EXTRA_OPTS="-Ddetmc.traceSeeds=true"`.

`test/run-det.sh` is an earlier `run-det.sh` bench script plus two additions: a **100-tick checkpoint** before the full 24000-tick sprint, and
**entity UUID dumps**. The checkpoint is what tells phase 2 whether a divergence
is immediate (seeding still leaking) or accumulates (ordering).

`test/diff-det.sh` compares section by section and prints, per section,
MATCH or DIFFER with a line count, then the first three positions from each run.

## Correction that applies to every result below (measured 2026-09-11)

Every `[t100]` and `[t24000]` row recorded before phase 4 compared **different
numbers of ticks**. `run-det.sh`'s `sprint()` did `tick unfreeze` / `tick sprint N` /
poll the container log for `Sprint completed` / `tick freeze`, and two windows in that
sequence run the server free at 20 tps: between the unfreeze and the sprint starting,
and between the sprint finishing and the freeze landing, the second bounded only by
the 0.1 s poll. Measured at nominal t1, with identical UUIDs and identical post-summon
`Motion`: endermen holding `Motion` y `-0.4475` in one run and `-0.6517` in the other,
which is gravity after different tick counts, not a different random draw.

So the phase 1 and phase 2 sprint rows below record a real divergence plus a harness
artefact, and cannot be used to size either. The tick-0 and worldgen rows are
unaffected. `goto_tick` fixed it; see phase 4.

## Phase 1 result (measured 2026-09-11)

Load test: `TYPE=FABRIC`, `VERSION=26.2`, `FABRIC_LOADER_VERSION=0.19.5`,
`./mods` mounted at `/data/mods`. Server log:
`Loading 5 mods:` → `- detmc 0.1.0`. All 8 mixins applied, no Mixin errors,
`Done (3.991s)`, ticks frozen before tick 1 as requested.

Determinism run: two fresh servers, `SEED=12345`, `-Ddetmc.seed=1`,
60 summoned mobs, 100-tick checkpoint then a 24000-tick sprint.

| Section | Result |
|---|---|
| `[t100]` / `[t24000]` enderman Pos, zombie Pos, UUIDs, carriedBlockState | **DIFFER** |
| pre-existing entities (tick 0, before any sprint) | **DIFFER** |
| region + entities `.mca` md5 (8 files) | **DIFFER** |
| seeds issued by `DetRng` at first tick | **47 on both runs — MATCH** |

### Divergence read for phase 2: it is ordering, not RNG

Three measurements, all from this run:

1. `DetRng` had issued exactly **47** seeds at the head of the first
   `tickServer` on both servers (and again on a third, separate single-server
   run). The number and therefore the value sequence of seed allocations is
   stable. The seeding funnel is closed.
2. The divergence is already present at **tick 0**, before the harness unfreezes
   anything. So no amount of tick-path work would have caught it; it happens
   during world load.
3. The tick-0 entity dump is the smoking gun. Both runs report the same four
   minecart-with-chest positions — `[-85.5, 41.5, -20.5]`, `[25.5, -46.5, 34.5]`,
   `[-28.5, 37.5, 84.5]`, `[92.5, -48.5, -16.5]` — but A lists the middle two in
   the opposite order to B. Identical worldgen output, different iteration order.
   In the same dump A has sheep near `[145, 138, 16.9]` where B has pigs near
   `[159, 141, 28]`: consistent with `Level.random` / entity `random` being
   handed different positions of the same 47-seed sequence because worldgen
   allocates them from background threads.

That read was right about the cause and wrong about one detail: the sheep and
the pigs were not a seed-stream artefact, they were two runs dumped at different
points of the same chunk-loading sequence. The harness now settles before it
dumps. See the phase 2 section below for what the measurements actually said.

## Phase 2 result (measured 2026-09-11)

Partial. Ordering is fixed and the seed stream is fixed. Worldgen mob population
still differs between runs for chunks generated late in the forceload, so the
24000-tick sprint still fails.

| Signal | Phase 1 | Phase 2 |
|---|---|---|
| entity iteration order at tick 0 | DIFFER | **MATCH** |
| entity `UUID` values | DIFFER | **MATCH** |
| entity `Pos` values | DIFFER | **MATCH** |
| first 19 worldgen entities, type + id + UUID + pos in arrival order | DIFFER | **identical, line for line** |
| full worldgen entity set over both forceloaded regions | DIFFER | **DIFFER: 19 vs 22** |
| `[t24000]` enderman `Pos`, `carriedBlockState`, region md5 | DIFFER | DIFFER |
| startup | `Done (4.0s)` | `Done (12.5s)`, no deadlock |

Read the first four rows and the fifth together. The mechanisms phase 2 set out
to fix are fixed:

- The four minecarts come back in the same order on both servers.
- Entity UUIDs are now identical for every entity both runs agree exists,
  including ones generated minutes apart.
- Nothing spawns before tick 1. Every tick-0 entity is tagged `src=worldgen` by
  the trace, so natural spawning can be struck off the scope doc's hypothesis
  list.

What is left is narrower and different in kind.

### Still open: worldgen mob count, not order

Measured on the full run, first divergence at the tenth entity added:

```
A  10 src=worldgen id=28 chicken uuid=0fb1c240-... pos=187,156,33
B  10 src=worldgen id=31 chicken uuid=0fb1c240-... pos=187,156,33
```

Same entity, same position, **same UUID**, different `id`. `Entity.ENTITY_COUNTER`
is three further along in B, so B constructed three more entities than A before
this point. Over both forceloaded regions A ends with 19 worldgen animals and B
with 22: B has two extra foxes at `200,125,-130` and `200,125,-135` that A never
produced, and the cow at `210,152,13` has a different UUID in each run.

So `NaturalSpawner.spawnMobsForChunkGeneration` ran the same random sequence in
both (positions and types line up) but its **spawn checks** came out differently.
Those checks are `SpawnPlacements.isSpawnPositionOk` and `checkSpawnRules`, and
for animals the latter reaches `isBrightEnoughToSpawn`. A chunk whose sky light
has not propagated yet fails it.

The concrete next lead, unverified: `ServerChunkCache.MainThreadExecutor.pollTask`
calls `lightEngine.tryScheduleUpdate()` on every poll, and the poll loop runs
under `MinecraftServer.haveTime()`, which reads `Util.getNanos()`. The light
batch size itself is a fixed count (`taskPerBatch = 1000`, not a time budget), so
the suspect is the *cadence* of light flushes relative to chunk generation, not
the batching inside the light engine. Forcing a light flush to completion before
`ChunkStatusTasks.spawnOriginalMobs` runs would test it.

`ChunkTaskPriorityQueueMixin` was added for this and **did not change the
measured outcome**. It is kept because it removes a real dependency on insertion
order, not because it fixed anything.

### Correction to an earlier claim in this file

An intermediate commit recorded tick-0 as fully matching. That was measured over
19 entities, which is all `pair-tick0.sh` had loaded when its settle loop gave
up. The full run shows B reaching 22. The fast harness under-settles, so treat
its green as necessary and not sufficient.

### What each step actually found

Four measurements, in the order they were taken. Each one changed the plan.

1. **Same-thread chunk executor does not deadlock.** The scope doc flagged this
   as the top unknown. It is not one. `Preparing spawn area: 100%` and
   `Done (11.6s)` on the first try. Startup is 3x slower and that is the whole
   cost.

2. **One executor was not enough.** Redirecting only the `ServerChunkCache`
   executor left 929 of 1310 seed allocations on `Worker-Main-1`. Chunk
   generation reaches `Util.backgroundExecutor()` directly, and every
   `thenApply` continuation then runs on whichever thread completed the future.

3. **A trampolining executor deadlocks `IOWorker`.** It has to be direct.
   `IOWorker.createOldDataForRegion` does
   `supplyAsync(..., Util.backgroundExecutor())` and `isOldChunkAround` joins the
   result on the server thread. A trampoline enqueues instead of running when the
   thread is already draining, so the join never returned. Symptom: the server
   parks at `Selecting global world spawn...` forever. Thread dump:
   `Server thread` WAITING in `CompletableFuture.waitingGet`, `IOWorker.java:92`.
   The `ChunkMap` executor still needs the trampoline, because
   `AbstractConsecutiveExecutor.run()` re-registers itself and a direct executor
   recurses once per queued task there.

4. **One thread is still not enough, and the cause is a clock.** With every seed
   draw on `Server thread`, the runs still parted at seed 65, same signature: one
   run in `applyCarvers` where the other was in `applyBiomeDecoration`.
   `MinecraftServer.prepareLevels` drains tasks under a wall-clock budget
   (`haveTime()` reads `Util.getNanos()`), so how many chunk generation tasks get
   batched per pass depends on how fast the host is at that instant.

   That drift cannot change world contents. Every worldgen consumer of
   `generateUniqueSeed()` throws the value away on the next line:
   `applyCarvers` calls `setLargeFeatureSeed`, `applyBiomeDecoration` and
   `spawnOriginalMobs` call `setDecorationSeed`, all re-keyed from the world seed
   and the chunk coordinates. Measured: chunk contents, entity types, entity
   positions and `ENTITY_COUNTER` ids were already identical at that point.

   It does change entity UUIDs, which are drawn from `Entity.random`. So entities
   got their own stream. `DetRng.entitySeed()` is a pure function of the master
   seed and an ordinal, not a position in a shared sequence, so no other
   subsystem's allocation can shift it.

The general lesson for any later divergence: a shared counter only needs to be
order-stable for the consumers that keep the value. Give those consumers a
per-domain stream and the rest of the drift stops mattering.

## Phase 4 result (measured 2026-09-11)

Two fresh servers, `SEED=12345`, `-Ddetmc.seed=1`, 22 worldgen entities plus 60
summoned mobs, checkpoints placed on exact absolute gametime.

| Signal | Phase 2 | Phase 4 |
|---|---|---|
| full worldgen entity set over both forceloaded regions | DIFFER (19 vs 22) | **MATCH (22 vs 22)** |
| spawn-light trace (position, brightness, verdict, in call order) | n/a | **IDENTICAL** |
| `Pos` / `UUID` / `Motion`, ticks 1 … 89 | DIFFER | **MATCH** |
| `Pos` / `UUID` / `Motion` at t1000, t2000, t3000, stepping only | DIFFER | **MATCH** |
| `[t100]` enderman `Pos`, zombie `Pos`, `UUID`, `id` | DIFFER | **MATCH** |
| the same spans advanced with `/tick sprint` | DIFFER | DIFFER |

### The "19 vs 22" was two defects and a measurement error

Worth reading as a method note, because each step changed the diagnosis.

1. **The count itself was partly a settle artefact.** `pair-tick0.sh` broke its settle
   loop after 10 stable polls (5 s) and `run-det.sh` after 6 (3 s). Serial chunk
   generation has 3 s lulls, so the loops gave up mid-forceload and the two runs were
   dumped at different points of the same sequence. 30 stable polls (15 s) makes both
   runs report 22.

2. **A new trace split the rest in two.** `-Ddetmc.traceSpawnLight=true` logs every
   `Animal.isBrightEnoughToSpawn` call with the chunk whose SPAWN step is running, the
   block position, the raw brightness and the verdict. That turned an entity count into
   two separate statements: the light *values* disagreed, and the chunk *order*
   disagreed.

3. **The light values were wall-clock dependent.** `chunk=[11, 2] pos=187,156,33
   raw=14` against `raw=9`, same seed, same block. Cause: `ChunkMap.processUnloads`
   drains `unloadQueue` while `haveTime()` holds, and each unload calls
   `lightEngine.updateChunkStatus`, dropping that chunk's light sections. Fixed by
   `ChunkMapMixin` plus the tick-end chunk-task fixpoint in `MinecraftServerMixin`.
   After that every value in the trace matched.

4. **The chunk order was identity-hash dependent.** With the values fixed, A still ran
   SPAWN for `[12, -8]` before `[12, -9]` and B the reverse.
   `DistanceManager.chunksToUpdateFutures` is a `ReferenceOpenHashSet<ChunkHolder>`,
   so `runAllUpdates` submitted generation steps in `System.identityHashCode` order.
   That swap alone moved `ENTITY_COUNTER`: 8 of 46 entities came out with different
   UUIDs at identical positions. Sorting the two iterations by `ChunkPos.pack()` fixed
   it, and worldgen has matched since.

5. **The phase 3 fixes were not the lever, and that is now measured rather than
   guessed.** Both fire — the SPAWN step really does gain a neighbour `LIGHT` r1
   requirement, and the light drain really does run — but the drain fired for about 4
   chunks in an entire forceload. They are kept as invariants.

### Still open: long uninterrupted runs drift

Worldgen, entity identity and the first few thousand ticks are reproducible. What is
not is a long run without a pause in it.

| How time was advanced | Result |
|---|---|
| `tick step`, 20-tick segments, t0 → t2200 | **MATCH** at all 11 checkpoints |
| `tick step`, 1000-tick segments, t0 → t3000 | **MATCH** at t1000, t2000, t3000 |
| `tick step`, one 3000-tick segment | **DIFFER** at t3000 |
| `tick step`, t0 → t100 then t100 → t24000 | MATCH at t100, **DIFFER** at t24000 |
| sprint, 100-tick segments then 200 | MATCH to t2000, **DIFFER** from t2200 |

An intermediate reading of these numbers — that `/tick sprint` was the cause — was
wrong, and the step-only run to exactly gametime 24000 on both servers is what refuted
it. The variable is how many ticks run between frozen checkpoints; sprint ticks make
it worse because at about 2.5 ms each, more of them pass while the same background
work is in flight.

`UUID` and `id` match at every checkpoint in every one of these runs, t24000 included.
Only `Pos`, `Motion`, `carriedBlockState` and the block regions drift, and at t3000
only 4 of 51 entities were involved.

Hypothesis, **unverified**: a frozen checkpoint is a barrier — several seconds of rcon
dumps during which any pending asynchronous completion lands, so both servers start the
next segment identical. Without one, a completion submitted by a background thread is
picked up by `MinecraftServer.shouldRun`
(`task.getTick() + 3 < tickCount || haveTime()`) in whichever tick the host finished it.
`Util.ioPool()` is deliberately still asynchronous, so `IOWorker` completions are the
obvious candidate. STATUS.md has the probe to run before writing any code.
