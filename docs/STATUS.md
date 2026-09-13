# detmc status (2026-09-11, phase 5)

## Correction first: the cause-2 fix did not close the divergence

The previous section of this file recorded a green 3000-tick stacked pair and inferred
that sorting `navigatingMobs` and `chunkHoldersToBroadcast` had fixed the residual.
**That inference was wrong.** Measured this session with the same jar:

| Segment | Entity data | Region (timestamp-blind) | First divergent gametime |
|---|---|---|---|
| 3000 stacked | MATCH | MATCH (13 files) | none |
| 10000 stacked | **DIFFER** (Pos, Motion, carriedBlockState; UUID and id MATCH) | **DIFFER** (4 of 13) | **2009** |

Gametime 2009 is the same tick the two pre-fix runs diverged at. So the sorting fixes
removed *some* order dependence — UUIDs and entity ids are now stable, which they were
not — but the divergence at 2009 survives them. A 3000-tick pair passing is not
evidence that 2009 is clean; it is a coin flip that landed heads.

## What the probe says about gametime 2009 now

From the 10000-tick pair (`dg-10000-*.txt`, `io-10000-*.txt`):

```
gt=2007  A h=e7cf719841fa341b  B h=e7cf719841fa341b   n=97 both
gt=2008  A h=dd37a7afd191ce33  B h=dd37a7afd191ce33   n=97 both
gt=2009  A h=c6f718edace7dba4  B h=577306037ddcc17c   <== first differing tick
```

Every traced main-thread event at 2009 is identical, including both `setBlock` lines
(`kelp age=24` and `kelp_plant` at `-48,56,2` / `-48,55,2` in both runs). The first
`setBlock` difference is at **2026**, i.e. 17 ticks *after* the entity divergence, so a
block change is a consequence here, not the cause.

The one traced difference anywhere near it is at **gametime 2008**, and it is a
wall-clock quantity:

```
gt=2008  A: eagerHead activeWrites=0
         B: eagerHead activeWrites=1
```

i.e. B had a background chunk write in flight where A had none. That counter is
`@Redirect`ed to 0 for the save gate, so it cannot change the save decision; what it
records is that the two hosts were in a different IO state one tick before the
divergence. That is consistent with suspect 3 below (`shouldRun(TickTask)`'s 4-tick
wall-clock window) and inconsistent with a pure iteration-order bug, which would not
care what the IO pool was doing.

**Not yet identified: which entity and which field diverge at 2009.** The digest is a
single hash over 97 entities, so it dates the divergence but does not localise it. The
next probe should be a windowed per-entity dump (`uuid|pos|motion|rot|age` for a
gametime range), which localises it to one entity in one short run.

## Phase 5 code: the RNG stream position now survives a restart

`DetRng` did not persist anything. A restart re-ran the static initialiser and
`bindWorldSeed`, both of which call `rekey`, so `issued` and `entityOrdinal` both went
back to 0 and every seed handed out after a restart differed from the one the
uninterrupted timeline would have used.

Added (`-Ddetmc.persistRng`, default on, inert on a fresh world):

* `DetRng.persistState(Path)` writes `<worldDir>/detmc-rng.properties` with
  `masterSeed`, `stateLo`, `stateHi`, `issued`, `entityOrdinal`, via a tmp file + move.
* `DetRng.restoreState(Path)` reads it back, refusing a file written under a different
  master seed or holding an all-zero xoroshiro state.
* `MinecraftServerMixin`: persist at `saveEverything(ZZZ)Z` TAIL (covers the world-clock
  autosave and `save-all flush`); restore in the `createLevels` redirect immediately
  after `bindWorldSeed`, which is after the rekey and before `prepareLevels` builds the
  first entity.

**What this cannot do, and it matters for the branching goal.** Vanilla serialises no
`RandomSource` state at all. `Level.random` (which drives random ticks), every
`Entity.random`, and every mob's goal/brain random are all reconstructed from a seed on
load with a *fresh* draw position. A run resumed from a save therefore cannot reproduce
the uninterrupted timeline even with a perfect seed stream; at best two resumed runs can
agree with each other. Making resume equal the uninterrupted run needs those draw
positions persisted too, which is a much larger change (a per-entity NBT tag plus a
level-data tag, and `LegacyRandomSource` exposes no state getter).

## Phase 4 record (kept; read the correction above first)


## Where it stands

Worldgen and tick 0 are bit-identical, and short segments are clean. The open defect
is long uninterrupted runs, and this session moved it from a hypothesis to a
**dated, reproducible** divergence with two named causes, one fixed and measured,
one fixed but not yet confirmed.

Everything below was measured this session unless it says otherwise.

## The probe (this is now the tool of record)

`-Ddetmc.traceIo=true` turns on two traces:

* `[detmc-dg]` — one line per tick: `t=<serverTick> gt=<gameTime> n=<entities> h=<hash>`,
  an FNV-1a over every entity's UUID, position, motion, rotation and age, sorted by
  UUID. **This is what dates a divergence to the exact tick**; no checkpoint dump can.
* `[detmc-io]` — one line at each point where a background result enters main-thread
  game state (chunk read/load, `ChunkMap.save`, the eager-save decision,
  `EntityStorage.loadEntities`, the `PersistentEntitySectionManager` inbox batch,
  `SectionStorage` pending loads and writes, chunk unload, autosave) **and every
  `Level.setBlock`**.

`test/analyze-probe.py` diffs two runs. It re-keys everything on **gametime**, because
`tickCount` is not comparable between replicas: `tickServer` also runs while the tick
rate manager is frozen, and two runs sit frozen for a different number of ticks during
the settle and the 60 summons.

## Cause 1 — the chunk save schedule ran on the wall clock (FIXED, measured)

First differing event, one uninterrupted 3000-tick segment, spread mobs:

```
gametime 206   A: save [4, -7] / eagerSave [4, -7]
               B: (nothing)
```

Over gametimes 1..3000, A performed 1119 eager saves and B 1124. No chunk load,
entity load or unload happened inside the segment at all — the eager save was the
only main-thread work whose tick placement differed.

`ChunkMap.saveChunksEagerly` reads `Util.getMillis()`, and `saveChunkIfNeeded` gates on
`now < nextChunkSaveTime` where `nextChunkSaveTime = now + 10000` ms. It is not inert:
`scheduleUnload` waits on `ChunkHolder.getSaveSyncFuture()`, so save timing moves unload
timing, and every unload calls `lightEngine.updateChunkStatus`.

Fixed by four changes, all gated on `detmc.syncChunks` (see README table):
`Util.getMillis()` in `saveChunksEagerly` and `saveAllChunks(Z)` → `gameTime * 50`;
`activeChunkWrites.get()` in `saveChunksEagerly` → 0; `SectionStorage.tick`'s
`haveTime` → constant true; and `ticksUntilAutosave`, which counted **server** ticks
including frozen ones, replaced by a save fired from the tick tail on a fixed
6000-gametime grid.

Measured after: a 400-tick segment had **zero** per-gametime event differences and
121 eager saves on both sides. In a later 10000-tick run the first save difference had
moved from gametime 206 to **3080**.

## Cause 2 — identity-hash iteration order (FIXED, measured)

**The earlier "async IO landing in a wall-clock-dependent tick" hypothesis does not
explain the residual.** With cause 1 fixed, the entity digest still diverges, and it
diverges at **gametime 2009 in two independent runs** — a 10000-tick one and a
3000-tick one. A fixed tick is the signature of a per-JVM iteration order, not a
timing race. The `[detmc-io]` events at gametimes 2008 and 2009 are identical in both
runs, and the first `setBlock` difference is at 2026, i.e. **after** the divergence.

Two collections in the tick path hash on `System.identityHashCode`:

| Collection | Where it is walked | Why it matters here |
|---|---|---|
| `ServerLevel.navigatingMobs` — `ObjectOpenHashSet<Mob>`, and `Mob` does not override `hashCode` | `sendBlockUpdated`, on **every block change**, to build `navigationsToUpdate` and then call `recomputePath()` in that order | this world random-ticks kelp and cave vines: about **43,000 block changes per 3000 ticks**, with 60 stacked mobs navigating. Recomputation reads the level-shared `pathTypesByPosCache`. |
| `ServerChunkCache.chunkHoldersToBroadcast` — `ReferenceOpenHashSet<ChunkHolder>` | `broadcastChangedChunks`, every tick, draining each chunk's pending block-change sections | same shape as `DistanceManager.chunksToUpdateFutures`, the phase 4 fix that made entity UUIDs reproducible |

`ServerLevelMixin` now redirects `Set.iterator()` in `sendBlockUpdated` to a copy sorted
by entity id, and the new `ServerChunkCacheMixin` redirects the one in
`broadcastChangedChunks` to a copy sorted by packed `ChunkPos`.

Measured after, **the same stacked 3000-tick segment that diverged at gametime 2009
twice**: digest identical at all 3001 gametimes, and Pos, UUID, id, Motion,
`carriedBlockState` and the timestamp-blind region md5 (13 files) all MATCH.

## Results, phase 4, fixed harness, one uninterrupted segment each

| Segment | Mobs | Entity data | Region content | First divergent gametime |
|---|---|---|---|---|
| 3000 | spread | MATCH | (raw md5 only) | none |
| 400 | spread | MATCH | — | none |
| 3000 (cause 1 fixed) | stacked | MATCH | raw md5 differs — see note | none in the digest |
| 10000 (cause 1 fixed) | stacked | **DIFFER** (Pos, Motion, carriedBlockState) | differs | **2009** |
| 3000 (cause 1 fixed) | stacked | **DIFFER** (Pos, Motion) | differs (timestamp-blind) | **2009** |
| 3000 (both fixed) | stacked | **MATCH** | **MATCH** (timestamp-blind, 13 files) | none |
| 10000 (both fixed) | stacked | **DIFFER** | **DIFFER** (4 of 13) | **2009** |

Stacked mobs (`run-det.sh`) are far more sensitive than spread ones (`SPREAD=1` in
`run-early.sh`): 30 endermen inside one another drive cramming and mutual push, and it
is the stacked configuration that reproduces at gametime 2009.

## Raw region md5 is not a valid check

`RegionFile.getTimestamp()` is `(int)(Util.getEpochMillis() / 1000L)` and it is written
into bytes 4096..8191 of every region file on every save (`RegionFile.java:278,309`).
**Two runs taken minutes apart can never have equal raw region md5s, whatever the world
contains.** Every "region md5 DIFFER" this project has recorded is contaminated by that.
`test/region-md5.py` blanks the timestamp table and `run-det.sh` now emits a
`region md5 no timestamps` section, which is the real check. The 3000-tick stacked run
differs in 4 of 13 files under the blind comparison too, so that one is a genuine
content divergence.

## Next steps

1. **Localise gametime 2009.** Add a windowed per-entity trace
   (`-Ddetmc.traceEntityWindow=lo:hi` → one line per entity per tick with pos, motion,
   rotation, age) and run a short pair to ~2100. One entity and one field will name the
   subsystem; the whole-world digest cannot.
2. `MinecraftServer.shouldRun(TickTask)` is `task.getTick() + 3 < tickCount || haveTime()`,
   so a queued main-thread task runs on a wall-clock-chosen tick inside a 4-tick window.
   This is now the leading suspect, not a parked one: the only traced difference before
   2009 is that B had a chunk write in flight at 2008. Probe it by tracing
   (queuedTick → ranTick) for every `TickTask`, then, if they differ, make the predicate
   tick-only and re-run 10000.
3. Untested, in order, once 10000 is green: 24000, a repeat 24000 from fresh worlds,
   then replay-from-save. Running them before 10000 passes only re-measures this defect.
4. Replay-from-save is scripted but **not yet run**: `test/replay-pair.sh` (pair to 12000,
   `save-all flush`, `docker stop`/`start`, resume to 24000) plus `test/run-det-resume.sh`
   (the resume half; issues no setup commands, because forceload, gamerules, time and
   weather are all in the save and re-issuing them would be work the uninterrupted
   timeline never did). It compares resume-A vs resume-B and resumed vs uninterrupted.

## Phase 5 measured: throughput, and the sidecar round-trip

`test/tps-bench.sh` / `test/tps-rest.sh`, `tick sprint 20000` on the stacked config
(both forceloads, 60 persistent mobs, `spawn_mobs true`), same image, same seed, same
host, minutes apart. TPS is the server's own "Sprint completed" line.

| Build | Sprint TPS | ms/tick | java %CPU | wall |
|---|---|---|---|---|
| detmc (all flags) | 231 | 4.32 | 155 | 87.1 s |
| same image + Fabric loader, `./mods` emptied, run 1 | 269 | 3.72 | 129 | 75.2 s |
| same, run 2 | **526** | 1.90 | 110 | 38.3 s |

**Do not quote a single slowdown factor from this.** Two consecutive sprints of the same
unmodified server came out 269 and 526 TPS, a 1.96x spread, so the run-to-run noise is
larger than the effect being measured. What can be said: detmc runs at 231 TPS where the
unmodified build runs at 269-526, i.e. somewhere between 1.2x and 2.3x slower, and it
burns more CPU while being slower (155 % against 110-129 %), which is what a same-thread
executor plus per-event sorting looks like. A clean number needs several interleaved
runs; an earlier on-disk vanilla bench for this script (`spawn_mobs false`) is 343-374 TPS. The heavy cost is still worldgen, already measured:
11.6 s to `Done` against 4.0 s.

The restart sidecar works end to end:

```
first boot  [detmc] no detmc-rng.properties in ./world/.; the stream starts from the master seed
after save  dataA/world/detmc-rng.properties -> masterSeed=12344 issued=1448 entityOrdinal=135
                                                stateLo=2803792396716601645 stateHi=3866189303321584236
restart     [detmc] restored RNG stream from ./world/./detmc-rng.properties: issued=1448 entityOrdinal=135
            gametime after restart: 18000  (the sprint's world state came back)
```

masterSeed 12344 is `-Ddetmc.seed=1 ^ 12345`, as expected. What is *not* yet measured is
whether two resumed replicas agree with each other; `test/replay-pair.sh` does that and
has not been run.

## Harness

Every determinism check is a case under `test/cases/`, driven by one script.

```
test/run-tests.sh --list                     # the cases and what each expects
test/run-tests.sh --case early-1000          # one case
test/run-tests.sh --all --junit out.xml      # everything, with a JUnit report
test/run-tests.sh --case X --compare-only    # re-print a verdict, start no servers
```

`test/README.md` lists the cases, the memory budget and which old script each case
replaced. `test/cases/README.md` is the `case.yaml` schema. Artifacts land in
`test/.runs/<case>/`, one directory per case, so two cases cannot overwrite each other.

Still standalone, because they answer questions no case asks:

```
test/analyze-probe.py   # digest + io diff, keyed on gametime
test/region-md5.py      # timestamp-blind region md5
test/nbt-canon.py       # canonical NBT dump
```

Per-case knobs that used to be environment variables (`TARGETS`, `SPRINT_MARGIN`,
`DETMC_EXTRA_OPTS`, `SPREAD`) are now fields in `case.yaml` and lines in the case's
`setup.commands`. Every case settles on 30 stable polls (15 s) before dumping.

**Do not kill a run and start another immediately.** A surviving per-label process keeps
appending to the artifacts the new run truncates; one run was invalidated that way.

## 2026-09-11 late: the gametime-2009 divergence, next step

A windowed per-entity trace and a `MinecraftServer.shouldRun` mixin were written but not
tested, because the fix is a hypothesis rather than a known change. Lead suspect:
the `shouldRun(TickTask)` wall-clock gate (at gametime 2008, `activeWrites` was 0 in one
run and 1 in the other).

## 2026-09-11 later: the shouldRun mixin compiles, and is UNTESTED

This section records a build result and nothing about determinism. **The gametime-2009
hunch is neither confirmed nor refuted. No run was made with this code.**

What is now in the tree (reviewed and compiled, not run):

- `MinecraftServerMixin.detmc$shouldRunWithoutAClock` — `@Inject` at HEAD of
  `shouldRun(TickTask)`, returns `true` unconditionally under `-Ddetmc.syncTasks`
  (default on). Vanilla is `task.getTick() + 3 < tickCount || haveTime()`, so the verdict
  normally comes from `Util.getNanos()`.
- The tick-end drain now also drains the main-thread queue (`this.pollTask()`), so no
  `TickTask` straddles a tick boundary.
- `detmc$ioBarrier` + `ChunkMapAccessor` — waits on `ChunkMap.activeChunkWrites` and
  `synchronize(false)` at tick end, under `-Ddetmc.ioBarrier` (default on). This is a
  *second*, separate change; `-Ddetmc.ioBarrier=false` isolates the `shouldRun` hunch.
- `DetTrace`: `-Ddetmc.traceEntityWindow=lo:hi` windowed per-entity dump, entity type and
  id added to each digest row, and a `clockTasks=` count of the `shouldRun` decisions
  vanilla would have handed to the clock (only counted when `-Ddetmc.traceIo=true`).

Verified this session, from the 26.2 decompile, not assumed:

- `MinecraftServer.java:880` `shouldRun` is `task.getTick() + 3 < this.tickCount || this.haveTime()`.
- `pollTaskInternal` (`:891`) reaches `haveTime()` only via
  `isSprinting() || shouldRunAllTasks() || haveTime()`, and `isSprinting()` is true for the
  whole of a `tick sprint`, so it short-circuits there. `tickServer` gets a constant
  `() -> false` while sprinting (`:1007`).
- `ServerChunkCache.pollTask` (`:278`) and its `MainThreadExecutor.pollTask` (`:628`) read
  no clock; `ChunkMap.tick`'s `haveTime` (`:505`) is already forced by `ChunkMapMixin`.
- `waitUntilNextTick`/`managedBlock(() -> !haveTime())` still exit on the clock, but that
  is a wait loop, not a task-ordering decision; forcing `shouldRun` true cannot deadlock it.
- `ChunkMap.activeChunkWrites` (`:153`) exists and `synchronize(boolean)` is public on
  `SimpleRegionStorage` (`:92`), so both accessors are sound.

Result: `./docker-gradle.sh :fabric:build` BUILD SUCCESSFUL, `fabric/build/libs/detmc-0.1.0.jar`.

### Next session, exactly this

```
cd test
TARGETS=10000 SPRINT_MARGIN=100000 \
  DETMC_EXTRA_OPTS="-Ddetmc.traceIo=true -Ddetmc.ioBarrier=false" \
  ./run-pair.sh            # ~20 min for the pair; background it, wait with an until-loop
```

`ioBarrier=false` on purpose: it tests the `shouldRun` hunch alone. Read the first
divergent gametime from `diff-det.sh` and the `region md5 no timestamps` section. If 10000
matches, run 24000. If it still diverges at 2009, re-run with the barrier on (drop
`-Ddetmc.ioBarrier=false`) before concluding, because gt=2008 showed `activeWrites` 0 vs 1
and that is the other half of the same suspicion. If both fail, the hunch is refuted and
the `shouldRun` mixin should stay only if it is cost-free, which is itself unmeasured.

## 2026-09-11 night: the shouldRun pin moves the divergence 2009 -> 2199, and does not fix it

Run: `TARGETS=10000 SPRINT_MARGIN=100000 DETMC_EXTRA_OPTS="-Ddetmc.traceIo=true -Ddetmc.ioBarrier=false" ./run-pair.sh`,
i.e. the `shouldRun(TickTask)` pin alone, IO barrier off. Artifacts in `test/phase6-barrier-off/`.

**Verdict on the hunch: partially confirmed, not sufficient.** The wall-clock task gate was
a genuine cause — pinning it moved the first divergent gametime from 2009 to 2199, and
gametimes 0..2198 are now bit-identical in the digest. It is not the whole cause: the pair
still diverges.

| | before (bf927b7) | after (`shouldRun` pinned) |
|---|---|---|
| first divergent gametime | 2009 | **2199** |
| divergent gametimes of 10001 | — | 7802 |
| entity count at the divergence | — | equal, n=99 both |
| entities at gt=10000 | — | **n=90 (A) vs n=89 (B)** |

Digest evidence, `test/phase6-barrier-off/dgh-{A,B}.txt` (last line per gametime, probe
counter stripped):

```
A: gt=2198 n=99 h=9daf35f910c65a12     B: gt=2198 n=99 h=9daf35f910c65a12   MATCH
A: gt=2199 n=99 h=13c467ff39f1cddb     B: gt=2199 n=99 h=49db1f9cf9c1e283   DIFFER
```

Same under either dedupe rule (first or last digest line per gametime), so it is not a
frozen-tick artifact. Harness verdict at t10000: Pos, Motion and `carriedBlockState`
DIFFER, UUID and id MATCH, timestamp-blind region md5 differs in 4 of 13 files.

Run B was healthy, not a harness failure: `RUN_FINISHED`, `settled after 34 polls`, no
`WARNING: wanted gametime`, and it reached gt=10000. (`test/phase6-10000.log` is a *stale*
artifact from 19:54 that predates this run; the log for this one is
`test/phase6-barrier-off/pair.log`.)

### The probe says task arrival still differs

`clockTasks` counts the `shouldRun` decisions vanilla would have handed to `haveTime()`.
Over the ticking part of the run: **A 157, B 125**. The gate no longer *defers* anything,
so this is not deferral variance — it means main-thread tasks are still being *queued* at
different ticks in the two runs. That is the IO-completion path
(`ChunkMap.save` -> `Util.ioPool()` -> `scheduleUnload` -> `unloadQueue`), which is exactly
what `-Ddetmc.ioBarrier` targets, and it is why the barrier run is the right next test
rather than a fishing expedition.

## 2026-09-12: the IO barrier changes nothing; 2199 is a second, separate cause

Run: same pair at 10000 ticks with the barrier left on (`DETMC_EXTRA_OPTS="-Ddetmc.traceIo=true"`,
`TARGETS=10000 SPRINT_MARGIN=100000`). Artifacts in `test/phase6-barrier-on/`.

| | first divergent gametime | divergent gametimes of 10001 | entities at gt=10000 |
|---|---|---|---|
| before this work (bf927b7) | 2009 | — | — |
| `shouldRun` pinned, barrier off | **2199** | 7802 | 90 vs 89 |
| `shouldRun` pinned, barrier on | **2199** | 7802 | 90 vs 91 |

**The IO barrier buys nothing measurable.** Same divergence gametime, same count of
divergent gametimes. Its default is therefore flipped to off (`-Ddetmc.ioBarrier=true`
opts in); it costs a `synchronize(false).join()` plus a spin at every tick end and has no
measured benefit. The code is kept because the next investigator will want to re-arm it
cheaply.

### Run A is reproducible; run B is not

This is the sharpest thing the run produced, and it reframes the problem.

```
gt=2198  A(barrier off) 9daf35f910c65a12   B(off) 9daf35f910c65a12   B(on) 9daf35f910c65a12
gt=2199  A(barrier off) 13c467ff39f1cddb
         A(barrier on)  13c467ff39f1cddb   <- A gives the SAME hash in both runs
         B(barrier off) 49db1f9cf9c1e283
         B(barrier on)  82ae2d681367ea8e   <- B gives a DIFFERENT hash every time
```

Every gametime 0..2198 is identical across all four runs, entity count included (n=99).
At 2199 run A lands on one value twice, and run B lands on a third and a fourth. So the
residual nondeterminism is not "A differs from B"; it is that some runs are stable and
others are not, with the instability entering at a fixed gametime. A fix has to explain
why 2198 is reproducible across four runs and 2199 is not.

`clockTasks` (the `shouldRun` decisions vanilla would have given the clock) during the
ticking part: barrier off A 157 / B 125, barrier on A 170 / B 151. Task *arrival* still
differs, and the barrier did not close that gap either.

### Verdict on the gametime-2009 hunch

**Partially confirmed, and superseded.** `MinecraftServer.shouldRun(TickTask)`'s wall-clock
gate was a real cause: pinning it made gametimes 0..2198 bit-identical and pushed the first
divergence 190 ticks later, reproducibly. It was not the *only* cause, so the pair still
fails at 10000. The `shouldRun` mixin stays on by default — its benefit is measured. 24000
was not run, because 10000 has to pass first.

### Not measured, and worth saying plainly

- Why gametime 2199 specifically. Nothing in this session localises it. The windowed trace
  (`-Ddetmc.traceEntityWindow=2190:2210`) exists, compiles, and has **never been run**;
  that is the cheapest next step and it names the entity and field rather than guessing.
- The throughput cost of the `shouldRun` pin. Every sprint benchmark in this file predates it.

### Harness traps hit this session, both mine

1. A B-only rerun without `TARGETS`/`SPRINT_MARGIN` silently used the defaults, so B ran
   to 24000 under `tick sprint` while A had run to 10000 under `tick step`. `isSprinting()`
   changes the task-poll path itself, so that pair was invalid and was discarded and rerun.
   **A partial rerun must carry the same env as the run it is compared against.**
2. `mc-det-B` failed to start twice with `Network is unreachable` fetching `server.jar`,
   because `run-pair.sh` wipes `dataB` and the image re-downloads. It was transient (a
   later fetch returned HTTP 200), not a mod fault — but a wiped data dir makes the harness
   depend on Mojang being reachable.

## 2026-09-12 phase 7: two causes named and fixed; three prior claims corrected

### Corrections first

1. **"Run A is reproducible; run B is not" was wrong.** With a symmetric harness (one
   container at a time, fresh data dir, same jar, `test/phase7-run.sh`), run A did not
   reproduce itself: A2 differed from A1 from **gametime 0**, and a third A with a 5-minute
   extra frozen delay (Adelay) matched A1 through 2198 and differed at **2199**. The three
   earlier A values that agreed at 2199 were three lucky draws of the same launch angle
   (see cause 2). Nothing about the label, the container, or B waiting while A ran was the
   cause; B1 alone diverged at 2199 exactly like Adelay.
2. **Every "UUID and id MATCH" verdict was half vacuous.** `data get entity @s id` returns
   nothing for entities, so the harness's "all entities id" section has been empty in every
   run and "id MATCH" compared zero lines with zero lines. Entity ids are only visible in
   the `[detmc-ew]` rows, and there they *did* differ between A1 and A2.
3. **"This is the fix that made entity UUIDs reproducible" (phase 2, `DistanceManagerMixin`)
   proved less than it claimed.** A loaded entity's UUID comes from NBT, so equal UUIDs say
   nothing about its construction order, and it is construction order that assigns
   `ENTITY_COUNTER` ids and the `DetRng` entity ordinal (i.e. the `Entity.random` seed).

4. Four of my own claims from earlier in this section were wrong and are corrected below:
   "phase 7b passed" (A3 vs B2) was one lucky pair of the same startup class; the first
   "construction order IDENTICAL" reading came from a log the daemon had rotated (it covered
   only the summons); the sorted chunk-pop hunch was refuted by the H series; and joining
   the region read alone did not fix cause 1, it took inline deserialisation *and* the
   command-queue ordering fix.

### Harness used (test/phase7-run.sh, test/phase7-chain.sh)

One container at a time, `rm -rf data<label>` before each, same `mods/detmc-0.1.0.jar`
(md5 recorded in `phase7/<tag>/params.txt`), `TARGETS=2300 SPRINT_MARGIN=100000`
(zero sprints, `tick step` only), `-Ddetmc.traceIo=true -Ddetmc.traceEntityWindow=2190:2210`.
`PRE_DELAY=<s>` in `run-det.sh` sleeps that long after `Done` while the server is frozen.
Three unrelated runner containers (`mc-det-R1..R3`) were up on the same host
throughout; the host has 56 cores, load ~15.

### Experiment 1: symmetric harness, jar 02c2af5e (the tree at fdfb290)

| run | label | frozen ticks before gt=1 | digest gt=0 (n=82) | gt=2198 | gt=2199 | first divergent gt vs A1 |
|---|---|---|---|---|---|---|
| A1 | A | 916 | b4a55ed6dccdda57 | 9daf35f910c65a12 | 13c467ff39f1cddb | — (identical to both phase-6 A runs at every gametime 0..2300) |
| A2 | A | 909 | **1f1a421d2366bb23** | 45f76ebb1322d434 | 77a9b8b9e39637e5 | **0** |
| Adelay | A, `PRE_DELAY=300` | 6918 | b4a55ed6dccdda57 | 9daf35f910c65a12 | **82ae2d681367ea8e** | **2199** |
| B1 | B | 888 | b4a55ed6dccdda57 | 9daf35f910c65a12 | **49db1f9cf9c1e283** | **2199** |

Adelay's 2199 value is the phase-6 barrier-on B value and B1's is the phase-6 barrier-off B
value, so all seven runs of this jar fall into: one startup class that differs from gt=0
(A2), and, within the common startup class, four distinct values at 2199.

### Cause 1 (A2, gametime 0): loaded entities are constructed at IO-completion time

`[detmc-ew]` rows at gt=2190, A1 against A2, same UUID, different id: chicken 14→16,
goat 17→18, rabbit 35→36, seven foxes 36..42→37..43. The 30 endermen (45..74), 30 zombies
(75..104), minecarts and items kept their ids. One worldgen goat changed UUID (18 in A1,
17 in A2, different UUIDs), i.e. it was constructed at a different `DetRng` entity ordinal.
The pre-existing-entity `Pos` dump was identical in both runs; only ids and seeds moved.

Source (26.2): `EntityStorage.loadEntities` is `read(pos).thenApplyAsync(deserialise,
entityDeserializerQueue::schedule)` and `ServerLevel` builds that queue on the
`MinecraftServer` itself, so `EntityType.loadEntitiesRecursive` runs on the main queue in
whichever tick the IO worker finished the read, interleaved with worldgen constructions
from the SPAWN step. Sorting the inbox (`PersistentEntitySectionManagerMixin`) fixes the
*add* order but the id and ordinal were already taken at construction.
`ChunkMap.scheduleChunkLoad` (`ChunkMap extends SimpleRegionStorage`) and
`SectionStorage.tryRead` (POI) have the same shape, and all three go through
`IOWorker.loadAsync`.

Fix: `IOWorkerMixin` joins the read before `loadAsync` returns (`-Ddetmc.syncReads`, default
on). The IO worker still does the read; the caller waits; every continuation runs at the
request point. Writes are untouched.

### Cause 2 (gametime 2199): the goat long jump shuffled its angles with `new java.util.Random()`

`[detmc-ew]` at gt=2199, A1 against Adelay and against B1: **exactly one entity differs,
goat id 17 (UUID 16daf86c…), fields pos and motion only**; every other entity's row is
byte-identical. Motion y is 0.888 (A1), 0.729 (Adelay), 0.771 (B1): three launch vectors
for the same jump.

Source: `LongJumpToRandomPos.calculateOptimalJumpVector` does
`Collections.shuffle(allowedAngles)` and returns the first angle whose vector is valid.
`Collections.shuffle(List)` uses `new java.util.Random()`, seeded from
`System.nanoTime()`; nothing detmc seeds reaches it. Runs agree only when the first valid
angle happens to coincide, which is why one value could show up three times.

Fix: `LongJumpToRandomPosMixin` redirects that call to `Util.shuffle(list, body.getRandom())`.
The mob's stream now takes `allowedAngles.size() - 1` extra draws per long jump, so goat AI
is deterministic but not vanilla-identical after a jump. Not covered: `InsideBrownianWalk`
(villagers only, same call inside a synthetic lambda) and `@e[sort=random]`.

### Diagnostic added

`LegacyRandomSourceMixin` counts draws and exposes the state of every vanilla legacy RNG;
`[detmc-dg]` now carries `levelRng=<draws>,<state>` and each `[detmc-ew]` row ends with the
entity RNG's `<draws>,<state>`. `-Ddetmc.traceEntityWindow` takes several `lo:hi` windows.
`test/analyze-ew.py` diffs two window traces and names the entity and fields.

### Second pass (same day): the sync-read fix alone was not enough, and two more defects

**Correction: "phase 7b passed" (A3 vs B2 identical at all 2301 gametimes, region md5 match)
was a lucky pair.** The 10000-tick pair with the same jar (`phase7/A10k`, `phase7/B10k`)
diverged at **gametime 0** with the cause-1 signature: chicken 14→16, goat 17→18, rabbit
35→36, foxes 36..42→37..43, one worldgen goat with a different UUID, and B10k's entity
inbox split across two ticks (`[0,-7]` row at t=18, `[0,-9]` row at t=19) where every
other run delivered it in one batch. Region md5 (12 files, timestamp-blind) still matched
and `levelRng` draws/state were equal at gt=10000 (`6008251,b0b2bba88a8c` both), so the
world's block state and the level RNG were not what moved; entity ids and seeds were.

Why joining the read was not enough: `EntityStorage.loadEntities` still handed the
deserialisation to `entityDeserializerQueue`, a `TickTask` on the `MinecraftServer` queue,
while worldgen entities are constructed inline inside chunk tasks (the same-thread
executor runs a freshly submitted task immediately unless it is already draining). The
between-tick loop (`pollTaskInternal`: server queue first, then one chunk task, while
`haveTime()`) decides whether that `TickTask` runs before or after the next tick's inline
chunk work, and that is the wall clock again. The construction probe (`[detmc-ec]`,
`phase7/E1/ec-A.txt`) shows the shape: the spawn-area animals are constructed **twice**,
once at the SPAWN step (ids 13..30, UUID drawn from the ordinal, then stored as NBT in the
proto-chunk) and again from NBT at load (ids 31..44, UUID from NBT, fresh ordinal). The
second set is exactly the set whose ids moved.

Fix 1b: `EntityStorageMixin` deserialises inline when the read is already complete
(`thenApply` instead of `thenApplyAsync`), so construction happens at the request point,
inside the same ordered chunk-task stream as worldgen. The inbox is kept FIFO under
`syncReads` (a per-batch sort would make the *add* order depend on where a batch splits;
B10k added the -7 row before the -9 row for that reason).

**Defect 3 (found while running scenarios, cause found here): the mod never saved entities.**
The phase 2 `@Overwrite` of `PersistentEntitySectionManager.processPendingLoads` dropped
vanilla's `chunkLoadStatuses.put(pos, LOADED)`. Every chunk stayed `PENDING`,
`storeChunkSections` returned false for all of them, and every save wrote 0 bytes of
`entities/*.mca` (runner's one-variable control: unmodded Fabric wrote 25 KB; the value did
not change with `-Ddetmc.syncReads=false`, which is consistent with a status bug rather
than an IO pin). The overwrite is replaced by an inject that only reorders the inbox in
place; vanilla's loop, including the status update, runs. Consequence for earlier
results: every "region md5 no timestamps" MATCH that included `entities/*.mca` compared
empty files, so those rows never checked entity persistence. `run-det.sh` now prints
`### entity region bytes` so the next run measures it.

**Defect 4 (harness): `save-all flush` over rcon times out and the md5 ran 3 s later.**
Every phase 7 result file has `Failed to read command: ... i/o timeout` after
`>>> save-all flush`. The command does run (666 `ChunkMap.save` events at gt=10000 in both
10000-tick runs), but nothing showed it had *finished* before the md5. `saveEverything`
TAIL now logs `[detmc] saveEverything done`, and `run-det.sh` waits for that line.

**Defect 5 (harness): the container log rotated the startup traces away.** The daemon
default for these containers is `json-file, max-size 10m, max-file 3`; with
`-Ddetmc.traceIo` the log passes 30 MB and `docker logs` no longer holds the startup, so
`ec-*.txt`/`trace-*.txt` in `phase7/F1..F3` begin at the summons (id 45) and the first
"construction order IDENTICAL" reading covered only the summons. `compose-det.yml` now
pins `max-size 2g, max-file 1`. Any trace file whose first entity id is not 1 came from a
rotated log.

Note for the next reader: `ServerLevel.getNextEntityId` skips ids that are still in use
(`while (id == 0 || chunkSource.hasEntityWithId(id)) id = ENTITY_COUNTER.incrementAndGet()`),
so an entity id is *not* a pure construction count; the `DetRng` ordinal is. The two can
drift apart when a construction happens while an earlier entity with the next id is still
registered, which is why `[detmc-ec]` prints both.

Cross-check from the branching driver (runner/runs/hunt-br1 vs hunt-br1-replay,
commit 1c57d9f): with the enderman_hunt arena (spawn_mobs false, random_tick_speed 0, no
natural mobs, 20 summoned endermen) a from-scratch replay matched byte for byte through
gametime 6001 and never showed the 2199 divergence. Consistent with cause 2: the entity
that diverged at 2199 is a worldgen goat, and the arena has no natural mobs.

### Third pass: the startup construction flip, localised and fixed

Runs F1..F3, G1..G3, H1..H4, J1..J3, K1..K3 (all `TARGETS=100`, one container at a time,
same jar within a series; `phase7/<tag>/`) fall into exactly two startup classes, visible in
the gt=0 digest (`54d60ddbbfa1b46a` against `3e08de0820903e9`) and in `[detmc-ec]`: the
14th and 16th constructions swap between a chicken (a FULL-step `addWorldGenChunkEntities`
load of chunk [9,3]) and a goat (the SPAWN step of chunk [10,-3]). Everything before and
after is identical, every run is on the Server thread, ids and ordinals stay in lockstep.

| series | jar / flag | class per run |
|---|---|---|
| F1..F3 | 33cf60a2 (sync reads, inline deserialise) | A, B, A |
| G1..G3 | same, log cap raised | B, A, A |
| H1..H4 | 8535510 + `-Ddetmc.sortedChunkPop=false` | B, A, A, A |
| J1..J3 | 7a5fc0d7 (queue trace) | A, A, B |
| K1..K3 | 7a13f7ec (phase markers) | B, A, B |

So `ChunkTaskPriorityQueueMixin`'s sorted pop is **not** the cause (H flips with vanilla
FIFO pop), and neither is tick placement: K1 and K3 are the same class although in K1 the
whole spawn-chunk burst ran in tick 17's between-tick wait and in K3 one tick later.

The fork itself (`[detmc-io] cqSubmit/cqPop/phase`, K1 vs K2, same tick, inside the tick-end
drain): `ServerChunkCache.pollTask` either finds new distance-manager work (K2: worldgen
submit `[-1,-10]`, chunk reads `[-12,-21]`...) or runs the queued full-status-change tasks
(K1: a burst of `entityLoadReq`). New tickets in that window come from the harness's rcon
commands (`forceload add`), which land on the `MinecraftServer` queue whenever the network
delivers them, and vanilla `pollTaskInternal` polls that queue *before* chunk work. A
command therefore ran at an arbitrary point inside in-flight chunk generation, and the
generation of the newly forced region interleaved with the spawn-chunk pipeline at that
point.

Fix: `MinecraftServerMixin.detmc$chunkWorkBeforeCommands` (`@Inject` HEAD of
`pollTaskInternal`, under `syncTasks`) polls every level's chunk source first, so a queued
command runs only when the chunk pipeline is quiescent. Measured: see the L series below.

**L series (jar 2e4512cb, the fix above), three 100-tick runs, one container at a time,
frozen-tick counts 972 / 921 / 669:** `[detmc-ec]` identical (298 constructions), add order
identical, gt=0 and gt=100 digests identical (`d2d3fbf8c619112f` / `adbaae598ec46055`),
level RNG identical, entity files 165,539 bytes on all three and canonically identical,
region md5 all 12 files equal for L1 vs L2. L1 vs L3 differ in one file,
`region/r.-1.-1.mca`, with every state measure equal; the frozen-tick count differed by
300 there and eager saves run 20 chunks per frozen tick, so the working hypothesis
(unverified) is a chunk saved at a different generation stage and not re-saved because it
was never dirtied again. From M10kB on, `phase7-run.sh` keeps `region/*.mca` copies so
this can be compared canonically.

### Verification with the final jar (2e4512cb; commit 61d213b)

Symmetric harness, one container at a time, fresh world each run, `SPRINT_MARGIN=100000`
(`tick step` only, zero sprints), `-Ddetmc.traceIo=true -Ddetmc.traceEntityWindow=0:3,2190:2210`.
"First divergent gametime" is over the per-tick digest (uuid, type, id, pos, motion, rot,
tickCount, entity RNG draws+state) at every gametime of the run.

| pair | ticks | first divergent gametime | Pos / Motion / carriedBlockState / UUID | region md5 (timestamp-blind) | entities/*.mca |
|---|---|---|---|---|---|
| L1 vs L2 | 100 | none (0..100) | MATCH | MATCH, 12 files | 165,539 B both, canonical NBT identical |
| L1 vs L3 | 100 | none (0..100) | MATCH | 11 of 12; `region/r.-1.-1.mca` differs, state equal (see above) | identical |
| M10kA vs M10kB | 10000 | **none (0..10000)** | MATCH | **MATCH, 12 files** | 330,637 B both, canonical NBT identical (42 chunks) |
| M24kA vs M24kB | 24000 | **none (0..24000)** | MATCH | **MATCH, 12 files** | 330,476 B both, canonical NBT identical (42 chunks) |
| M24kA2 vs M24kB2 | 24000 | **none (0..24000)** | MATCH | 11 of 12; `region/r.-1.-1.mca` differs, see note | 330,476 B both, canonical NBT identical |
| M24kA vs M24kA2 | 24000 | **none (0..24000)** | (same digest `ece955b3ba726416`, n=239, levelRng `14377975,bc4507945148` at 24000 on all four runs) | | |

For the record, every earlier pair this session and its first divergent gametime:
A1/A2 0 (loaded-entity construction race); A1/Adelay 2199 (goat); A1/B1 2199 (goat);
A3/B2 none over 0..2300 (lucky class); A10k/B10k 0; F1/F2 0, F1/F3 none; G1/G2 0, G2/G3
none; H1/H2..H4 0; J1/J3 0; K1/K2 0, K1/K3 none; G24kA/G24kB 0 (yet the 60 summoned mobs
matched at 24000 in Pos/Motion/carriedBlockState; only the natural animals' UUID set and
count differed). F10kA/F10kB none over 3..10000 (that jar still had the command-queue
flip; the pair happened to share a class).

**The `region/r.-1.-1.mca` difference (L1 vs L3, M24kA2 vs M24kB2) is not world state.**
With region copies kept from M10kB on, `test/nbt-canon.py` on the two 24000-repeat files:
all 352 chunks present in both are canonically identical; M24kA2 holds 35 extra chunks
(region-local 15..31, 13..31, i.e. outside every forceload) that M24kB2 never wrote. A
chunk that is unloaded while still `EMPTY` is not saved (`ChunkMap.save`); one that had
reached a later status is. Chunk unload is queued from the IO thread when the chunk's
save-sync future completes, so which of those neighbour chunks got as far as a saveable
status before their unload landed is IO-timed. The loaded world, every entity, and every
digest were identical, so this is an on-disk cache of partial neighbours, not a divergence;
worldgen is seed-keyed, so regenerating them from scratch gives the same blocks. Candidate
fix, **unmeasured**: `-Ddetmc.ioBarrier=true` (pins unloads to the tick that issued the
save). Not run this session.

**Still unverified after phase 7:** replay-from-save (`test/replay-pair.sh`, never run);
the throughput cost of the phase 7 changes (synchronous reads, chunk-first polling);
`InsideBrownianWalk` and `@e[sort=random]` (JDK `Random`, same class of bug as the goat,
not on this world's path); worlds with villagers, raids, or players.

## 2026-09-12: `/detmc reseed` and `/detmc rng`

New and additive; unrelated to the gametime 2199 defect above. The scenario runner can now
branch a run at a chosen gametime onto a new RNG schedule without restarting the server.

| Command | Effect |
|---|---|
| `/detmc rng` | one line: `masterSeed`, `baseMaster`, `reseeded`, `reseedArg`, `issued`, `entityOrdinal`, `gametime` |
| `/detmc reseed <long>` | re-keys the master stream to `<long> ^ worldSeed` and re-keys every live stream derived from it |

Both are permission level 2 (`Commands.LEVEL_GAMEMASTERS`); the rcon console source is
`LevelBasedPermissionSet.OWNER`, so `rcon-cli` passes. Registration is `CommandsMixin` on
`Commands.<init>` RETURN, gated on `DetCommands.isArmed()`, which `DetMcFabric.onInitialize`
sets. It is not `CommandRegistrationCallback`, because Fabric API is not on these servers'
runtime classpath.

### What reseed re-keys

`DetRng.reseed` on its own would change nothing visible for a long time: every
`RandomSource` that already exists keeps drawing from where it was. So `DetCommands.reseed`
also writes into the live ones.

| Stream | Derivation |
|---|---|
| `Level.random`, per `ServerLevel` | `setSeed(derive(newMaster, LEVEL, hash(dimension id)))` |
| `Level.randValue`, per `ServerLevel` | written through `LevelRandValueAccessor`; this is the LCG behind `getBlockRandomPos`, i.e. random-tick block choice |
| `Entity.random`, every loaded entity | `setSeed(derive(newMaster, ENTITY_LIVE, entity.getId()))` |

Ordinals are entity ids and dimension-name hashes, never iteration positions:
`getAllEntities()` walks a hash order. Not re-keyed: `ShufflingList.random`, `Raid.random`,
`WanderingTraderSpawner.random`, `MinecraftServer.random`. Entities constructed after the
reseed are unaffected by that list, because `DetRng.entitySeed()` is keyed on the live
master.

Pre-reseed behaviour is bit-identical to before this change: `entitySeed()` now derives from
`currentMaster`, which equals `PROP_SEED_VALUE ^ worldSeed` until a reseed happens.

### Measured 2026-09-12

Three replicas, fresh `SEED=12345` worlds, `-Ddetmc.seed=1`, the standard forceload plus 60
summoned mobs, all frozen. `goto_tick 1000`, dump, `/detmc reseed N`, then checkpoints out
to gametime 2000. 2000 is short of the 2199 defect on purpose, so the comparison is about
the reseed and nothing else. `test/pair-reseed.sh`; artifacts in `test/reseed-run1/`.

| Pair | Reseed args | Verdict |
|---|---|---|
| R1 vs R2 | 777 / 777 | **MATCH at every checkpoint**: Pos, Motion, UUID and `carriedBlockState` at t1000, t1001, t1005, t1020, t1100, t1300, t1600, t2000, plus timestamp-blind region md5, 13 of 13 files |
| R1 vs R3 | 777 / 778 | **DIFFER**. Identical at t1000 (pre-reseed) and t1001; Pos and Motion differ from **gametime 1002**, two ticks after the reseed. `carriedBlockState` differs by t1300. UUID matches throughout, which is expected: a reseed creates and destroys no entity. Region md5 differs in 4 of 13 files at t2000. |

The exact first divergent gametime is from a second run, `test/pair-reseed-narrow.sh`, which
puts a checkpoint on every tick from 1001 to 1005: MATCH at 1001, DIFFER at 1002.

`entities=95` on the reseed line of both R1 and R2 at gametime 1000, so the entity re-key
ran over the whole loaded set.

### Persistence

`<world>/detmc-rng.properties` gained `baseMaster`, `reseeded` and `reseedArg`, and
`masterSeed` now holds the live key. `restoreState` validates the file against the world
using `baseMaster`, then adopts the saved `masterSeed`, so a save taken after a reseed comes
back up on the reseeded branch. Old files without `baseMaster` fall back to `masterSeed`,
which is what that key meant before.

Measured round trip, `test/reseed-persist-check.sh` on a freshly generated world with no
forceload, `-Ddetmc.seed=1`, `SEED=12345`:

```
before:        detmc rng: masterSeed=12344 baseMaster=12344 reseeded=false reseedArg=0 issued=86 entityOrdinal=1
reseed:        detmc reseed: arg=4242 masterSeed=8363 levels=3 entities=0 gametime=0
sidecar rewritten after 1 polls
               baseMaster=12344 masterSeed=8363 reseeded=true reseedArg=4242 issued=0
after restart: detmc rng: masterSeed=8363 baseMaster=12344 reseeded=true reseedArg=4242 issued=11 entityOrdinal=1
[detmc] restored RNG stream from ./world/./detmc-rng.properties: master=8363 (reseeded=true arg=4242) issued=0 entityOrdinal=1
```

`8363 == 4242 ^ 12345`, so the sidecar came back on the reseeded branch and not on the
launch-time one.

The pre-existing limitation is unchanged: vanilla serialises no `RandomSource` draw
position, so a reloaded entity restarts its stream from `entitySeed(ordinal)` and not from
whatever the reseed wrote into it.

### Two harness facts found in passing

1. **`save-all flush` over rcon did not land in either harness run.** In the three-replica
   run it returned `Failed to read command: ... i/o timeout` and the sidecar was still the
   startup save's when `docker compose down` came. In a separate single-server check on a
   world that was still generating its forceload, the command was accepted at 08:00 and
   20 minutes later the sidecar's mtime was still the 07:57:30 autosave, with one core at
   99.9%. `SaveAllCommand` passes `silent=true`, so there is no log line to wait on either.
   The reseed itself is not the cause: in the three-replica run the server answered every
   rcon dump for 1000 ticks after the reseed and only the flush timed out.
   Consequence: the `detmc-rng.properties` row in the verdict tables above reports startup
   state on every replica and must be ignored, and the persistence evidence above is from a
   small world where the flush does return. `run-det.sh` ends with the same
   `save-all flush` + `sleep 3`, so its region md5 sections are read from the previous save
   as well. Root cause not investigated; it is orthogonal to this change.
2. `docker compose down`'s 10 s stop timeout SIGKILLs one of these servers before its
   shutdown save finishes, so the on-disk world after a harness run is whatever the last
   completed autosave wrote.

## 2026-09-12 phase 8: the JDK-Random and identity-order sweep

Thirteen fixes, none of them on this harness's path. The sweep was exhaustive over the
decompiled 26.2 **server** jar (4849 files, no client
package), so the value here is the classification as much as the code: what is left, and
why each surviving hit is safe.

Everything in this section is read from the source or measured this session; where a fix is
reasoned rather than measured, the row says so.

### What the sweep searched, and the raw counts

| Pattern | Hits in the server jar | Verdict |
|---|---|---|
| `new Random(` / `new java.util.Random` | **0** | the JDK class is never constructed directly |
| `Collections.shuffle(` | 3 | all three use the JDK-internal `new Random()`; one fixed in phase 7, two fixed here |
| `ThreadLocalRandom` | 2 | one is `RandomSource.createThreadLocalInstance()` itself; its 6 callers are classified below |
| `Math.random(` | 1 | network latency simulator |
| `UUID.randomUUID(` | 11 | none reaches world state |
| `SecureRandom` | 4 | crypto only |
| `System.nanoTime` | 3 | one is `RandomSupport` (already overwritten), two are the sprint timer |
| `System.currentTimeMillis` | 2 | metrics and the exception collector |
| `Util.getMillis/getNanos/getEpochMillis` | 59 | the real wall-clock surface; classified below |

`RandomSupportMixin` already covers `RandomSource.create()` and `createThreadSafe()`,
because both go through `generateUniqueSeed()`. It does **not** cover
`createThreadLocalInstance()` (it reads `ThreadLocalRandom` directly) or
`RandomSource.create(long)` (the caller supplies the seed). Those two gaps are where four
of this phase's fixes are.

### Classification: JDK random classes and the wall clock

| Site | Reachable on the server? | Game-visible? | Verdict |
|---|---|---|---|
| `LongJumpToRandomPos.calculateOptimalJumpVector` `Collections.shuffle` | yes, goats | yes — measured as the gametime-2199 divergence | **FIXED in phase 7** |
| `InsideBrownianWalk` (`lambda$create$2`) `Collections.shuffle` | yes, villagers indoors | yes, the walk target | **FIXED**, `InsideBrownianWalkMixin` → `body.getRandom()` |
| `EntitySelectorParser.ORDER_RANDOM` (`lambda$static$6`) `Collections.shuffle` | yes, `@e[sort=random]` | yes, which entity a command acts on | **FIXED**, `EntitySelectorParserMixin` → the level's `RandomSource` |
| `PlayerSpawnFinder.<init>` `createThreadLocalInstance()` | yes, every join and bedless respawn | yes, the block a player spawns on | **FIXED**, `PlayerSpawnFinderMixin` → `level.getRandom()` |
| `SpreadPlayersCommand.spreadPlayers` `createThreadLocalInstance()` | yes, `/spreadplayers` | yes, it teleports entities | **FIXED**, `SpreadPlayersCommandMixin` → `level.getRandom()` |
| `StructureBlockEntity.createRandom(0)` `Util.getMillis()` | yes, a structure block at its default seed | yes, `integrity` block removal | **FIXED**, `StructureBlockEntityMixin` → `gameTime * 50` |
| `StructurePlaceSettings.getRandom(null)` `Util.getMillis()` | yes, template placement without a position | yes, same integrity path | **FIXED**, `StructurePlaceSettingsMixin` → `gameTime * 50` |
| `Stopwatches.currentTime()` `Util.getMillis()` | yes, `/stopwatch` and `execute if` | yes — and `pack()` writes it into `data/stopwatches.dat` | **FIXED**, `StopwatchesMixin` → `gameTime * 50` |
| `Level.randValue` `createThreadLocalInstance()` | yes | yes | already fixed, `LevelMixin` (phase 1) |
| `AngerManagement.conversionDelay` `createThreadLocalInstance()` | yes, wardens | yes | already fixed, `AngerManagementMixin` (phase 1) |
| `EntityZombieVillagerTypeFix` `createThreadLocalInstance()` | only when upgrading a pre-1.11 world | yes, but once, at conversion | **left**: a DataFix lambda over a `Dynamic`, and the result is then stored in NBT, so a re-upgrade of the *same* save is not repeated. Worth revisiting for anyone replaying legacy worlds. |
| `QueryThreadGs4` challenge token | query protocol | no | left |
| `ServerConnectionListener.getSessionId` `UUID.randomUUID` | yes | no — a telemetry id, never saved | left |
| `ServerConnectionListener$LatencySimulator` `Math.random()` | only with the latency-simulation flag | no — delays packets | left |
| `CustomBossEvents.load` `UUID.randomUUID` | yes | no — the boss bar's network id; the save keys on the `ResourceLocation` | left |
| `DetectedVersion.BUILT_IN`, `FileFixerUpper` temp names, `GameTestHelper` mock players, `LocalChatSession` | mixed | no | left |
| `Crypt.secureRandom`, `jsonrpc SecurityConfig` `SecureRandom` | yes | no — key material | left |
| `ServerTickRateManager.sprintTickStartTime/endTickWork` `System.nanoTime` | yes | no — accumulates the number printed in `Sprint completed`; nothing reads it back | left (and it is the source of every TPS figure below) |
| `ServerMetricsSamplersProvider`, `SuppressedExceptionCollector` `currentTimeMillis` | yes | no — logging | left |
| `PrimaryLevelData` `LastPlayed` `Util.getEpochMillis()` | yes | on disk only, in `level.dat` | left: the harness hashes `region/` and `entities/`, not `level.dat`. Anyone hashing a whole world directory has to blank it, the same way `region-md5.py` blanks the region timestamp table. |
| `RegionFile.getTimestamp()` `Util.getEpochMillis()` | yes | on disk only | known since phase 4; `test/region-md5.py` blanks it |
| `ServerPlayer.lastActionTime` / `playerIdleTimeout` `Util.getMillis()` | yes, with players | yes — the idle kick fires on wall-clock minutes | **left, and it is a real gap for player worlds.** Not fixed because an idle timeout of 0 (the default) disables it entirely, so no replica in this project can hit it. |
| the `MinecraftServer` tick loop's own `Util.getNanos()` (`haveTime`, `nextTickTimeNanos`, `waitUntilNextTick`) | yes | yes, indirectly | handled in phases 4-7 by pinning the *decisions* (`shouldRun`, `processUnloads`, `SectionStorage.tick`, the eager-save schedule, chunk-work-before-commands) rather than the clock itself |

### Classification: identity-hashed iteration

`Entity.hashCode()` is `getId()` (`Entity.java:431`), so every `Set<ServerPlayer>` and
`Map<Entity, …>` in the tick path is already deterministic — that rules out most of the
candidates. `PoiRecord` hashes on its position and `Activity` on its name, which rules out
the inner POI sets and all of `Brain`'s activity maps. What is left is the collections keyed
by `Holder.Reference`, `ResourceKey`, `Objective`, `Structure` or `AttributeInstance`, none
of which override `hashCode`.

| Collection | Walked at | Verdict |
|---|---|---|
| `ServerLevel.navigatingMobs` | `sendBlockUpdated` | already fixed, phase 4 |
| `ServerChunkCache.chunkHoldersToBroadcast` | `broadcastChangedChunks` | already fixed, phase 4 |
| `DistanceManager.chunksToUpdateFutures` | `runAllUpdates` | already fixed, phase 4 |
| `DistanceManager` entity-ticking / spawn-candidate chunks | `forEachEntityTickingChunk`, `getSpawnCandidateChunks` | already fixed, phase 2 |
| `AttributeMap.attributes` | `pack()` | already fixed, phase 7 |
| `ItemEnchantments.enchantments` (`Object2IntOpenHashMap<Holder<Enchantment>>`) | `entrySet()`, walked by both `EnchantmentHelper.runIterationOnItem` overloads | **FIXED**, `ItemEnchantmentsMixin`. The widest one left: every enchantment effect on every item ran in identity order, and two call sites thread a `RandomSource` through the loop (`modifyTridentSpinAttackStrength`, and Mending's `Util.getRandomSafe(items, source.getRandom())`), so the order moved the RNG stream and not just one outcome. |
| `AbstractFurnaceBlockEntity.recipesUsed` (`Reference2IntOpenHashMap<ResourceKey<Recipe<?>>>`) | `getRecipesToAwardAndPopExperience` | **FIXED**, `AbstractFurnaceBlockEntityMixin`. Each entry draws `level.getRandom().nextFloat()` for its XP fraction, so with two recipes in one furnace the *level* stream position depended on allocation order. |
| `PoiSection.byType` (`HashMap<Holder<PoiType>, Set<PoiRecord>>`) | `getRecords` | **FIXED**, `PoiSectionMixin`. `PoiManager.findAllClosestFirstWithType` sorts *stably*, so ties keep encounter order and `AcquirePoi`'s `limit(5)` saw a different candidate set. |
| `StackedContents.amounts` (`Reference2IntOpenHashMap<Holder<Item>>`) | `getUniqueAvailableIngredientItems` → `RecipePicker.tryAssigningNewItem` | **FIXED**, `StackedContentsMixin`. The picker takes the first viable assignment, so identity order chose which stack the recipe book spent. |
| `PlayerScores.scores` (`Reference2ObjectOpenHashMap<Objective, Score>`) | `Scoreboard.packPlayerScores` | **FIXED**, `ScoreboardMixin`. Save-order only: `scoreboard.dat` bytes. |
| `ServerRecipeBook.known` / `.highlight` (`Sets.newIdentityHashSet`) | `pack()` | **FIXED**, `ServerRecipeBookMixin`. Save-order only: the player `.dat`. |
| `Brain.memories`, `.availableBehaviorsByPriority`, `.sensors` | tick | benign: the behaviour map is a `TreeMap` of `LinkedHashSet`, the sensors a `LinkedHashMap`, and the memory map is only cleared, per-slot ticked, or serialised through string keys |
| `GoalSelector.availableGoals` | tick | benign: `ObjectLinkedOpenHashSet`, insertion-ordered |
| `AttributeMap.attributesToSync` / `.attributesToUpdate` | `ServerEntity`, `LivingEntity` | benign: one packet payload, and four idempotent clamps |
| `ChunkAccess.structureStarts` / `.structuresRefences` | `SerializableChunkData.packStructureData` | benign: only `put` into a string-keyed `CompoundTag` |
| `ChunkMap$TrackedEntity.seenBy`, `PlayerList` duplicate-login set, `AdvancementNode.children`, `MappedRegistry` identity maps, `GossipContainer` transfer set, `DataComponent*` array maps, every `Long2Object*` / `Int2Object*` / `BlockPos`-keyed map in the chunk and tick storage | various | benign: independent per-element work, lookup-only, insertion-ordered array maps, or a stable key |

### Why none of this is covered by the pair

Every one of the thirteen fixes is off the harness world's path: it has no players, no
villagers, no enchanted items, no furnace, no scoreboard, no structure block and issues no
`@e[sort=random]`. The 24000-tick pair therefore shows only that the sweep does not
*regress* the world that is measured. The evidence that each fix is real is the source
reading in the tables above plus `javap -c` on `libs/minecraft-server-26.2.jar`, which was
used to confirm every target method descriptor and every redirected call site before the
mixins were written (two of the targets are synthetic lambdas, `lambda$create$2` and
`lambda$static$6`, and those names came from the disassembly, not from a guess).

Mixin applies a config when the target class is first loaded, so a wrong descriptor in an
untouched class fails *hours* later rather than at boot. `DetMcBootstrap.verifyLazyMixinTargets`
closes that: it force-loads all fourteen lazy targets at the first server tick.
Measured this session: `[detmc] 14 lazy mixin targets loaded and applied`, no Mixin error,
so every phase 8 injection point is confirmed to bind — which is a weaker claim than "the
fix works" and a stronger one than "it compiles".

### Verification of the phase 8 jar (sha256 a20b86a0…, md5 bf387966)

Symmetric harness, `test/phase7-run.sh`, one container at a time, fresh world each run,
natural spawning on (`gamerule spawn_mobs true`), `TARGETS=24000 SPRINT_MARGIN=100000`
(`tick step` only, zero sprints), `-Ddetmc.traceIo=true -Ddetmc.traceEntityWindow=0:3,2190:2210`.
Artifacts in `test/phase7/P8A24k/` and `test/phase7/P8B24k/`; harness verdict in
`test/phase8-diff.txt`. Host load average was 30-40 throughout (eight unrelated
`mc-run-hunt-*` containers).

| pair | ticks | first divergent gametime | divergent gametimes | Pos / Motion / UUID / carriedBlockState | entities/*.mca | region md5 (timestamp-blind) |
|---|---|---|---|---|---|---|
| P8A24k vs P8B24k | 24000 | **none** | **0 of 24001** | MATCH | 330,476 B both, all 42 chunks canonically identical | 11 of 12; `region/r.-1.-1.mca` differs, see below |
| P8A24k vs phase 7's M24kA (different jar) | 24000 | **none** | **0 of 24001** | — | — | — |

The digest at gametime 24000 is `h=ece955b3ba726416 n=239 levelRng=14377975,bc4507945148` in
**all three** runs, which is the same value phase 7 recorded for M24kA/M24kB/M24kA2/M24kB2.
The three runs reached it after a different number of frozen startup ticks (t=25249, 24945
and 25233), so that is the harness varying and the world not varying.

**The phase 8 sweep is measurably inert on this world**, which is what the classification
above predicts: the phase 8 jar reproduces the phase 7 jar's timeline at every one of 24001
gametimes. That is the only claim the pair supports about the sweep. It is not evidence that
any of the thirteen fixes works.

`region/r.-1.-1.mca` again, third time now: `test/nbt-canon.py` on the two copies shows
**366 chunks present in both and all 366 canonically identical**, with B holding 21 extra
chunks (region-local 16..30, 13..31, all outside both forceloads) that A never wrote. Same
cause as phase 7: a chunk unloaded while still `EMPTY` is not saved, one that got further is,
and the unload is queued from the IO thread when the save-sync future completes. The loaded
world, every entity and every digest are identical, so this is an on-disk cache of partial
neighbours. Candidate fix `-Ddetmc.ioBarrier=true` is still **unmeasured**.

### Replay-from-save: half measured, half void (host OOM)

`test/replay-pair.sh` ran for the first time ever this session. The A half finished; the B
half was killed by a **host-wide OOM at 15:27** (the coordinator stopped every `mc-*`
container; `mc-det-B` exited 255 mid-run). So:

| comparison | result |
|---|---|
| fresh A to 12000 (`dg-pre12000-A`) vs the uninterrupted 24000 run (`P8A24k`) over 0..12000 | **identical, 0 of 12001 gametimes divergent** — a third independent fresh run of this jar landing on the same timeline |
| resumed A vs resumed B (the actual replay question) | **NOT MEASURED.** B never ran. Must be re-run. |
| resumed A vs uninterrupted A over 12000..24000 | **differs at every one of 12001 gametimes, first divergent gametime 12000**, i.e. immediately |

The resumed-vs-uninterrupted divergence is expected, and this is the first time it has been
measured rather than argued. Phase 5 predicted it from the source: vanilla serialises no
`RandomSource` state at all, so `Level.random` and every `Entity.random` come back from a
fresh seed. The digest shows exactly that:

```
gt=12000  resumed        h=46c7e257b8b15190  levelRng=0,f57dd75b5010
gt=12000  uninterrupted  h=bbeb624c07fa740d  levelRng=7210083,2d7b9f7199f4
gt=24000  resumed        h=8a51c419545c0e58  n=236  levelRng=7171748,30553f6f29b4
gt=24000  uninterrupted  h=ece955b3ba726416  n=239  levelRng=14377975,bc4507945148
```

The level RNG restarts at draw 0 with a different state the moment the world is reloaded,
and by gametime 24000 the two worlds hold a different number of entities. `detmc-rng.properties`
did its job (`restored RNG stream ... issued=1448 entityOrdinal=487`), which is the *seed
stream* position; the *draw* positions of the live `RandomSource` objects are what is missing,
and closing that needs a per-entity NBT tag plus a level-data tag.

**So the open question is unchanged: do two resumed replicas agree with each other?**
That is the pair that has still never been run.

### Harness traps hit this session

1. **A Bash background task was killed at ~70 minutes, taking `replay-pair.sh` with it.**
   Run A had already finished its resume; B's first half died mid-flight. Long chains must be
   launched with `setsid nohup ... < /dev/null` so they are in their own session and survive
   the launcher, and even then the driver has to be restartable.
2. **`replay-pair.sh` brought up both servers at once.** Each is `MEMORY=4G` plus about 1 GB
   of JVM overhead, and the script only ever drives one of them; the idle one was pure
   waste and it was half of a host-wide OOM. The script now starts and removes one service
   at a time, which caps it at ~5 GB.

### Throughput of the phase 8 jar: 1.13x slower than no mod at all

`test/tps-bench3.sh 20000`, new this session and the first bench here that is worth
quoting a ratio from. Six sprints, **interleaved** (detmc, vanilla, detmc, vanilla, …) so a
drift in host load cannot land on one variant, each from a **fresh data dir** with the same
scripted setup (both forceloads, 60 stacked persistent mobs, `spawn_mobs true`, frozen),
one container at a time, `MEMORY=2G`. "vanilla" is the same image and the same Fabric
loader with `./mods` emptied. TPS is the server's own `Sprint completed` line.

| variant | TPS per run | median TPS | median ms/tick | java %CPU | wall |
|---|---|---|---|---|---|
| detmc (all flags, no probe) | 188, 212, 224 | **212** | **4.70** | 152-154 | 90-107 s |
| same image, `./mods` empty | 237, 239, 282 | **239** | **4.18** | 140-142 | 72-85 s |

**detmc costs 1.13x on ms/tick** (4.70 against 4.18) and about 11 points of CPU. The two
sets do not overlap — the slowest vanilla run (237) is faster than the fastest detmc run
(224) — so on this config the cost is real and it is small. Host load average was 19-26
throughout (the wave-2 hunt fleet), which is why these are medians of three and not one
number.

This supersedes the phase 5 figure (231 detmc against 269-526 vanilla, a 1.96x spread
*within* the vanilla runs). The spread there came from re-sprinting the same container
twice; a fresh world per sprint cuts the within-variant spread to 1.19x on both sides.
Phase 5 ran at `MEMORY=4G`, this at 2G, so the absolute numbers are not comparable across
sections — the ratio is the number to carry forward. The `-Ddetmc.traceIo` probe is off in
all six runs; it is not free.

### Replay-from-save, measured at last: two resumed replicas DO agree

`test/replay-pair.sh`, run to completion for the first time (2026-09-12, `MEMORY=2G`, one
container at a time). Each label: fresh world to gametime 12000, `save-all flush`,
`docker stop`, `docker start`, resume to 24000. Artifacts `test/resume-{A,B}.txt`,
`test/dg-resume-{A,B}.txt`, `test/phase8-replay.log`.

| comparison | gametimes | first divergent gametime | verdict |
|---|---|---|---|
| **resumed A vs resumed B**, 12000..24000 | 12001 | **none** | `h=8a51c419545c0e58 n=236 levelRng=7171748,30553f6f29b4` at gametime 24000 in both, and every entity section MATCH |
| the two pre-restart halves, 0..11999 | 12000 | **none** | the fresh halves agree too |
| resumed vs uninterrupted, 12000..24000 | 12001 | **12000**, i.e. immediately | expected, see below |

**The branching goal is met for resumed replicas.** Two servers restarted from the same
save produce the same world for the next 12000 ticks, entity for entity. The resumed
timeline also reproduced across three separate runs and a heap change: the voided
15:20 run and both of these landed on `8a51c419545c0e58` at gametime 24000, at 4 GB and at
2 GB of heap.

Resumed does **not** equal uninterrupted, and this is now measured rather than argued.
Phase 5 predicted it from the source: vanilla serialises no `RandomSource` state, so
`Level.random` and every `Entity.random` restart from a fresh seed.

```
gt=12000  resumed        h=46c7e257b8b15190  levelRng=0,f57dd75b5010
gt=12000  uninterrupted  h=bbeb624c07fa740d  levelRng=7210083,2d7b9f7199f4
gt=24000  resumed        h=8a51c419545c0e58  n=236  levelRng=7171748,30553f6f29b4
gt=24000  uninterrupted  h=ece955b3ba726416  n=239  levelRng=14377975,bc4507945148
```

`detmc-rng.properties` restored the *seed stream* exactly (`issued=1448 entityOrdinal=487`,
the same numbers in both labels and in the voided run). What is missing is the *draw*
position of each live `RandomSource`, and closing that needs a per-entity NBT tag plus a
level-data tag.

### Correction to every "region md5 differs" row in this file

**No chunk has ever differed in content.** `test/mca-payloads.py` (new) hashes each chunk's
decompressed payload bytes with no NBT parsing at all, so it is blind to nothing: if two
saves hold different blocks, it says so. Run over both of this session's pairs:

| pair | file | chunks A / B | chunks whose payload bytes differ | same chunk at a different sector | chunks only in B |
|---|---|---|---|---|---|
| uninterrupted | region/r.-1.-1 | 366 / 387 | **0** | 14 | 21 |
| uninterrupted | region/r.-1.0 | 354 / 380 | **0** | 0 | 26 |
| uninterrupted | region/r.0.-1 | 512 / 546 | **0** | 0 | 34 |
| uninterrupted | region/r.0.0 | 482 / 508 | **0** | 0 | 26 |
| uninterrupted | entities/*(4 files) | equal | **0** | all of them | 0 |
| resumed | region/*(4 files) | equal | **0** | 38-129 per file | 0 |
| resumed | entities/*(4 files) | equal | **0** | 0 | 0 |

So `region-md5.py`, even with the timestamp table blanked, is sensitive to two things that
are not world state:

1. **Chunk presence.** A chunk unloaded while still `EMPTY` is not saved. One that got
   further is. This is the phase 7 `r.-1.-1` finding, and it is now visible in all four
   region files of the *post-removal copies*, because `compose rm -sf` gives the server a
   10 s stop and one replica gets further through its shutdown save than the other. The
   in-run md5 section, taken before removal, matched in 11 of 12 files.
2. **Sector placement.** Byte-identical chunks land at different offsets inside the `.mca`,
   because free-sector allocation depends on the order the chunks were written in. The
   resumed pair writes each region twice (the 12000 flush, then 24000) and 38 to 129 chunks
   per file moved, with zero content differences.

The tool of record for "is the saved world the same" is therefore
`test/mca-payloads.py` plus `test/nbt-canon.py`. A raw or timestamp-blind md5 is a
screening test only, and a failing one means "look closer", not "the world diverged".

## 2026-09-12 sweep test: the thirteen phase-8 fixes, walked at last

The phase-8 section above says it plainly: the sweep was measurably inert on the
compose-det.yml world, and the 24000-tick pair was evidence that nothing regressed, not
that any of the thirteen fixes works. `test/cases/sweep` closes that gap. It builds a
flat world by command that does walk the paths, runs the same A/B pair on it, and runs
`test/cases/sweep-control` with `-Ddetmc.syncRandom=false` so that a PASS means
something.

Everything here was measured this session, one 2 GB pair at a time, `memgate --need
5000` before every launch, with the hunt fleet (eleven `mc-run-hunt-*` containers)
loading the host throughout.

### Result

| case | flags | ticks | first divergent gametime | saved chunks (nbt-canon) | verdict |
|---|---|---|---|---|---|
| `sweep` | fixes on (default) | 12000 | **none, 0 of 12001** | all 6 files canonically identical (247 chunks) | PASS |
| `sweep-control` | `-Ddetmc.syncRandom=false` | 12000 | **4** | every region and entity file differs | PASS (expect diverge) |

Digest at gametime 12000, both halves of the fixed pair:
`h=78f917e628a4a1f7 n=94 levelRng=6249962,c7bfab2ed655`, and the entity digest and the
level RNG position agree at every one of the 12001 gametimes. Reproduced on three
independent pairs of the same case, all six halves landing on that same digest. The
control's halves part at gametime 4 in one pair and gametime 2 in the other, which is
about as early as a villager brain can move them.

Run time: 25.4 min for `sweep`, 25.0 min for `sweep-control`. Almost all of it is
`tick step` at 20 tps, by choice; see the trap below.

One raw `.mca` md5 still differs in the passing pair (`region/r.0.0.mca`) while
`nbt-canon.py` finds all 81 of its chunks canonically identical: free-sector placement
inside the file depends on the order the chunks were written, which moves bytes without
moving state. That is the phase-7 finding, and it is why the canonical comparison and
not the md5 is what fails this case.

### Two single-run oracles, sharper than the pair

These compare the world against arithmetic rather than against another run, so one
server answers them:

| oracle | fixes on | fixes off |
|---|---|---|
| `/stopwatch query sw_setup` at gametime 12000 | **600.0s**, exactly 12000 x 50 ms | 628.372s, the wall clock |
| the same template placed twice at integrity 0.5 seed 0 at one gametime | **Test passed**, 3375 blocks block-for-block equal | Test failed |

### Which of the thirteen the world actually reaches

Eleven are exercised. Two cannot be, and neither is a defect in the fix:

- **06 StructurePlaceSettings.getRandom(null)** is dead code. No caller in the 26.2
  server jar passes null, so no command reaches it.
- **03 PlayerSpawnFinder** is out of reach of the only player this harness can get.
  Measured: `/player Sweeper spawn` (Carpet 26.2) puts the fake player at exactly
  `0.0, -60.0, 0.0` on three joins in a row, and still exactly there with
  `respawn_radius` raised from 10 to 200; killing a fake player removes it instead of
  respawning it. Carpet places the player itself, so the vanilla spawn search never
  runs. A real client would reach it.

| # | path | what walks it | control diverges? |
|---|---|---|---|
| 01 | InsideBrownianWalk | 6 villagers in a closed room at midnight, 2 beds between them | yes, by t3000 |
| 02 | EntitySelectorParser | `@e[tag=sel,sort=random,limit=1]` every 20 ticks; `#picks` = 600 at t12000 | yes, by t3000 |
| 03 | PlayerSpawnFinder | not exercised, see above | n/a |
| 04 | SpreadPlayersCommand | `/spreadplayers` over 6 armour stands every 200 ticks; `#spreads` = 60 | yes, by t3000 |
| 05 | StructureBlockEntity | `/place template ... 0.5 0` and a redstone-fired LOAD structure block, every 400 ticks | yes, by t3000 |
| 06 | StructurePlaceSettings | unreachable, dead code | n/a |
| 07 | Stopwatches | two stopwatches, queried at every checkpoint | yes, by t3000 |
| 08 | ItemEnchantments | `/damage ... minecraft:magic` every 200 ticks on a zombie in four enchanted pieces, eleven enchantments | **no** |
| 09 | AbstractFurnaceBlockEntity | a furnace holding three `RecipesUsed` entries destroyed with `/setblock ... air destroy`, which pops the XP with no player; 30 destructions, 40 orbs alive at t12000 | yes, by t3000 |
| 10 | PoiSection | seven POI types in one section; six villagers take six job sites and two beds | yes, by t6000 |
| 11 | StackedContents | a crafter pulsed every 400 ticks on the shapeless book recipe; 30 crafts, 4 book items alive at t12000 | yes, by t3000 |
| 12 | Scoreboard | two objectives over four named holders, plus a per-stand `picked` objective | one direction only, below |
| 13 | ServerRecipeBook | `/recipe give @a *`, 1561 recipes in `known` | **no** |

Three qualifications, none of them cosmetic:

1. **08 ItemEnchantments runs, but nothing here can see its order.** The damage lands
   and every piece carries at least two enchantments, so the mixin's `size() >= 2` guard
   engages, but the surviving health is the same number with the fixes on and off
   (`#health` = 185600006 both ways). This probe does not show the iteration order
   mattering. The phase-8 argument for that fix is that the order moves the RNG stream at
   two call sites (trident spin attack, and Mending's `Util.getRandomSafe`); reaching
   those needs a player holding damaged Mending gear and XP orbs to pick up.
2. **12 Scoreboard is clean in one direction only.** With the fixes on, `scoreboard.dat`
   is byte-identical across the pair. With them off it differs, but so do the scores it
   contains, because the control world parted at gametime 4 and the per-stand `picked`
   counts moved with it. The control half of that comparison is not attributable to
   `packPlayerScores`.
3. **13 ServerRecipeBook is walked and does not move.** The player `.dat` is
   byte-identical in the fixed pair *and* in the control pair. So the run shows the path
   being reached with 1561 entries in an identity hash set, and shows nothing about the
   order. Hypothesis, unverified: two runs with the same allocation sequence get the same
   identity hash codes, so the set iterates the same way in both.

### A correction to the phase-8 classification: `Brain.memories` can serialise unstably

The phase-8 table lists `Brain.memories` as benign, "only cleared, per-slot ticked, or
serialised through string keys". The serialised half of that is not safe. In the first
12000-tick fixed pair, at all four checkpoints, one villager's memories printed with the
same keys and the same values in a different order in A than in B:

```
A: {"minecraft:home": {...}, "minecraft:last_slept": {value: 9480L}, "minecraft:last_woken": {value: 9380L}, "minecraft:potential_job_site": {...}}
B: {"minecraft:home": {...}, "minecraft:last_slept": {value: 9480L}, "minecraft:potential_job_site": {...}, "minecraft:last_woken": {value: 9380L}}
```

The entity digest was identical at all 12001 gametimes of that same pair, so the world
state agreed and only the NBT key order did not. **It did not reproduce in the next pair
of the same case**, so it is intermittent, and the artifacts of the first pair were
overwritten by the re-run. It is the same class of defect as the `Scoreboard` and
`ServerRecipeBook` fixes in phase 8 -- save order only, invisible to the tick, visible to
anyone diffing a save -- and it is not fixed. `sweep-diff.py` therefore carries that one
section as a named `known` exemption rather than silently ignoring it, and the exemption
is guarded: `nbt-canon.py` runs over both halves' saved `entities/` and `region/` files
on every run and fails the case if any chunk's *content* differs.

### Two harness traps this case paid for

1. **`tick sprint` cost a whole pair.** The first full run had A land on gametime 6025
   and B on 6000 for the same checkpoint, because the server free-runs between the
   `tick unfreeze` rcon call and the `tick sprint` one. The checkpoint probes place
   structures whose integrity seed is `gameTime * 50`, so the halves then placed
   different blocks and the pair diverged at gametime 6077 -- the harness, not the mod.
   `sweep-pair.sh` now defaults to `SPRINT_MARGIN=100000`, i.e. `tick step` only, the
   setting phase 8 used, and labels a missed checkpoint `VOID` rather than `WARNING`.
2. **A `sleep 1` after the fake player's join failed the case.** One half's `list`
   reported "0 of a max of 20 players online" and the other "1", purely because the join
   landed on the other side of the sleep; the player was in both worlds (identical `Pos`,
   1561 recipes, byte-identical `.dat`). The script now polls `list` until the player is
   there. It matters beyond the transcript: `recipe give @a *` with nobody online unlocks
   nothing, and path 13 would not be exercised at all in that half.

### Running it

```bash
test/run-tests.sh --case sweep --case sweep-control --no-build
SWEEP_COMPARE_ONLY=1 test/sweep-pair.sh sweep      # re-verdict, no servers
```

`--no-build` matters while a hunt is running: the runner otherwise rebuilds
`fabric/build/libs/detmc-0.1.0.jar`, and the `mc-run-hunt-*` fleet launches new
containers against that file between generations.

Both cases are self-driven (`test/cases/README.md`, "Self-driven cases"). Three things
keep them out of the generic runner and would each have to become a schema field and
runner code first: a flat level type, a second mod jar in `mods/` (Carpet, for the fake
player), and a datapack with a `minecraft:tick` function tag rather than one `setup`
function.

## 2026-09-13: the fake player was lost on every resume

`branch.py` only calls `setup()` for generation-0 nodes, and `Replica.resume` never
put the Carpet fake players back.  Carpet logs them out on shutdown and writes nothing
that restores them, so every node after generation 0 booted its parent's world with
`list` reading 0 players -- and natural spawning only runs in chunks with a
non-spectator player within 128 blocks
(`ChunkMap.playerIsCloseEnoughForSpawning`).  `hunt-villager-wave1` ran
entirely that way: its `endermen` metric read **0 in 548 of its 549 node-generations**,
and it ticked 1.42x faster than the scenario it was supposed to be running.

### The fix

`Replica.resume` now re-spawns every `setup.fake_players` entry, and a node whose
player does not show up in `list` FAILS instead of ticking an empty village.
`run.spawn_fake_players` is the old `setup` block plus the two guards
`bench_tps.py` had to learn: `gamemode <mode> <name>` is retried until the server
stops answering "No player was found" (Carpet's `player ... spawn` returns before
the join), and the mode is then read back from `playerGameType`. `branch.py`
records what it spawned in the node's lineage record and in the checkpoint's
`meta.json`, so "was there a player" is answerable per node without the driver log.

**Order: after the re-summon and the material restore, BEFORE the reseed.**
Everything a resume does to the world has to be identical across a parent's
children, so that the `detmc reseed` argument stays the one thing that makes a
sibling different. Nothing steps the tick loop, so the gametime at the reseed is
still exactly the parent's checkpoint gametime.

**Verified on one node**, resuming `hunt-villager-wave1`'s `g181n0` checkpoint
(gametime 8,736,001, reseed 548001651 -- one this run actually issued) for a
6,000-tick segment, one container at a time through `memgate --need 3000`
(three runs under `runner/runs/`):

| reading at the end of the segment | control: no player (the old path) | with the fix |
|---|---|---|
| `list` | `There are 0 of a max of 20 players online:` | `There are 1 of a max of 20 players online: villagewatch` |
| `playerGameType` | -- | **1** (creative), after **1** retry of `gamemode creative` |
| monsters in the village box | -- | **32** (30 at the resume): 9 zombies, 1 skeleton, 22 other |
| `endermen` | 0 | 0 -- 6,000 ticks is a quarter of one night |
| ticks/s | 254.5, 251.9 | **195.2, 190.9** |

**1.31x on the rate**, same world copy and same segment. The throughput A/B measured
the same effect from the other end at 1.42x on a quieter host.

The retry is load-bearing, and only on the resume path: the first `gamemode creative`
answered "No player was found" in **5 of 5 resumed nodes** and in **0 of the 2
generation-0 setups** (which joined on the first try -- a fresh world boots with
nothing to load). Without the retry a resumed node has its player online but in
SURVIVAL, and mobs target a survival player.

**The name the server lists is not always the name the scenario typed.** Caught in
production the same day: `player stormwatch spawn` produced a player `list` reports as
**`Stormwatch`** on 4 of 5 lightning nodes and as `stormwatch` on the fifth, while
`villagewatch` came back verbatim on every villager node. Carpet resolves a
`GameProfile` for the name and the canonical casing comes back with it, per node, as a
race against that lookup. Commands accept either casing; only the display differs. The
first version of this check compared case-sensitively and failed **8 of 8**
generation-0 lightning nodes that had the player standing in them, so the check is
case-insensitive and the `playerGameType` read asks about the name `list` gave back.

**The retry runs longer than one round trip.** Retries needed for
`gamemode creative` on the 5 nodes of that wave: 1, 1, 4, 0, 5. At one second apiece
that is up to 5 s after Carpet's spawn command has already returned.

### Two resumed containers do not agree on mob state, and never did

Measured while checking the fix could not have broken determinism: two containers,
same parent checkpoint, same reseed, same 6,000 ticks.

| arm | final gametime | score metrics | detector hits | block region chunks | entity chunks | `entityOrdinal` |
|---|---|---|---|---|---|---|
| no player (the pre-fix resume path) | MATCH | MATCH, all 12 | MATCH | **MATCH, every file** | DIFFER, **7 of 715** | MATCH (42090) |
| with the fix | MATCH | MATCH, all 12 | MATCH | **MATCH, every file** | DIFFER, **18 of 721** | DIFFER (42131 / 42159) |

The control is the finding: **mob state was already not bit-reproducible across two
resumed containers**, before this change and with no player in the world. That is the
gap phase 5 named and this file has carried since ("Resumed does not equal
uninterrupted"): vanilla serialises no `RandomSource` state, so every entity read back
out of `entities/*.mca` starts from a fresh draw position. It was invisible until now
because the hunt jar wrote 0-byte `entities/*.mca` and no comparison could see a mob.
What the fix adds is more mobs for that gap to act on.

Everything a search actually reads -- the final gametime, all 12 score metrics, the
detector hits and **every block in every region file** -- matches in both arms.

**A settle after the join does not close it, and costs 22 s a node.** Tried, because
chunk loading continues while the ticks are frozen and a joining player brings its own
tickets: `settle()` (31 polls, 22 s) left `entityOrdinal` at 42118 against 42128 and
diverged the score metrics as well (`endermen` 0/1, `villagers` 9/7). The wait is not
in the code; the comment in `Replica.resume` carries this measurement.

