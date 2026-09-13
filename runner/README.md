# runner: declarative scenario runs for detmc

One YAML file describes a long search: what the world must contain, how the server
is set up, what counts as a hit, how long to run and how many replicas. `run.py`
resolves a world seed, generates a compose file per run, starts N replica servers
(same world seed, different `-Ddetmc.seed`), applies the setup by rcon in a fixed
order while ticks are frozen, advances time in exact segments, polls a generated
detector datapack and appends hits to `results.jsonl`.

`branch.py` drives the same scenarios as a branching (go-explore) search over
saved worlds instead of N independent replicas; see its section below.

Nothing here modifies the mod or `test/`. Containers are named
`mc-run-<run_id>-r<N>` (`run.py`) and `mc-run-<run_id>-<node>` (`branch.py`).

## Quick start

```bash
../docker-gradle.sh :fabric:build        # produces fabric/build/libs/detmc-0.1.0.jar
./fetch-carpet.sh                        # only needed by scenarios with fake players
python3 -m venv .venv && .venv/bin/pip install pyyaml
.venv/bin/python run.py scenarios/zombie_siege.yaml
```

Useful flags: `--replicas N`, `--ticks N`, `--segment-ticks N`, `--snapshot-every N`,
`--no-render`, `--seeds 12345,999` (skip the prefilter), `--run-id NAME`, `--keep`
(leave the containers up), `--reseed auto|on|off`, `--dry-run` (write `compose.yml` +
`manifest.json` and stop).

```bash
.venv/bin/python test_patterns.py               # pattern matcher + generated mcfunction
.venv/bin/python run.py scenarios/enderman_hunt.yaml   # the 30-replica hunt
.venv/bin/python branch.py search scenarios/enderman_hunt.yaml \
    --replicas 10 --generations 8 --keep-top 3 --children 3   # the branching search
```

Output lands in `runs/<run_id>/`: `compose.yml`, `.env` (per-run rcon password),
`mods/` (the exact jars used), `manifest.json` (everything needed to replay),
`data-r<N>/` (the worlds), `r<N>.log` (every rcon command and reply),
`results.jsonl`, `snaps/` if snapshots are on.

## Scenario format

| Key | Meaning |
|---|---|
| `extends` | another scenario file, relative to this one. It is loaded first and this file is deep-merged over it: dicts merge key by key, **lists are replaced whole**. `scenarios/enderman_hunt_smiley.yaml` is 40 lines of diff over `enderman_hunt.yaml` rather than a 250-line copy that would drift the moment the arena changed. Cycles raise. |
| `world.seeds` | explicit world seeds. Empty means call `prefilter.py`. |
| `world.prefilter` | `count` (seeds wanted), `start_seed`, `max_seeds` |
| `world.requirements.spawn_biome` | biome at the world spawn must be one of these |
| `world.requirements.spawn_biome_radius` | tolerance in blocks |
| `world.requirements.structures` | list of `{id, within}`: structure id, max blocks from spawn |
| `server` | `difficulty`, `simulation_distance`, `view_distance`, `spawn_monsters`, `memory` |
| `anchor` | where the scenario is built: `from: spawn\|structure\|fixed`, `structure`, `pos`, `offset`, `y: surface\|<int>` |
| `setup.gamerules` | map of gamerule to value, applied in file order |
| `setup.time` | `{set: midnight\|17000\|...}` |
| `setup.weather` | `{policy: clear\|rain\|thunder\|natural, duration: N}` |
| `setup.forceload` | list of `{from: [x,z], to: [x,z]}` in block coords |
| `setup.fake_players` | list of `{name, at: [x,y,z], facing: [yaw,pitch]}`, needs Carpet |
| `setup.prep` | ordered command strings, or `{scatter: {area,y,block,spacing}}` |
| `setup.summons` | list of `{type, count, nbt, tag, at: {mode: point\|grid\|ring, origin, spacing, cols, radius}}` |
| `setup.tag_entities` | list of `{selector, tag}`, run after the summons |
| `anchor.surface_skip` | block predicates `y: surface` looks through (default below) |
| `detector` | see below, plus `interval_ticks`, `check_every_ticks` and `stop_on_hit` |
| `run` | `ticks`, `segment_ticks`, `replicas`, `rng_seeds`, `reseed` |
| `score` | `metrics` (progress metrics, see branch.py) and `rank`, the expression they are ranked by |
| `resume` | `resummon`, `restore_material`: how a saved world is picked back up (branch.py only) |
| `snapshots` | `enabled`, `every_ticks`, `camera`, `render`, `image`, `threads` |

Any string may contain `${...}` with integer arithmetic over the resolved variables
`spawn_x`, `spawn_z`, `anchor_x`, `anchor_y`, `anchor_z`, for example
`"fill ${anchor_x-9} ${anchor_y} ${anchor_z-9} ..."`.

Gamerule names are the 26.2 snake_case ones (`advance_time`, `spawn_mobs`,
`mob_griefing`, `random_tick_speed`, `advance_weather`). Read out of the 26.2
server jar: `assets/minecraft/lang/deprecated.json` maps `gamerule.doDaylightCycle`
to `gamerule.minecraft.advance_time` and so on, because game rules became a
registry (`BuiltInRegistries.GAME_RULE`) in this version.

### Detectors

`run.py` generates the datapack, writes it into `data-r<N>/world/datapacks/`,
`/reload`s, enables it and checks it is listed. The pack keeps its state in one
scoreboard objective `detmc`: `#hit` (0/1), `#hitgt` (gametime of the hit),
`#val` (last measured value), `#thresh` (the threshold, set by `run.py` after
setup so it can be relative to a measured baseline). The runner reads those scores
after every segment, which is why the hit tick is exact rather than segment-rounded.

| `detector.kind` | Keys | What the tick function does |
|---|---|---|
| `entity_count` | `selector`, `op` (`<`,`<=`,`>`,`>=`), `threshold` (int or an expression in `baseline`) | `execute store result score #val_<name> detmc if entity <selector>`, compares with `#th_<name>` |
| `column_stack` | `area: [x1,z1,x2,z2]`, `base_y`, `height`, `air_block` | one line per column: hit when all `height` blocks above the floor are non-air |
| `pattern` | `pattern` (name in `patterns.py`, or an inline spec), `area`, `base_y`, `levels`, `air_block` | one line per `size x size` window per level: the pattern's `required` cells must all have their column top at the scan level, then a shared counter checks `min_any` of `any_of` |
| `multi` | `detectors: [...]` | every sub-detector runs in the same pack with its own hit flag |
| `custom_datapack` | `path` | your pack is copied in; it must set `#hit` in objective `detmc` |
| `custom_scarpet` | `path` | `.sc` copied to `world/scripts` and `/script load`ed; same contract |
| `none` | | run without a detector (snapshots or manual inspection) |

Every detector has a `name` and its own scores in objective `detmc`: `#hit_<name>`,
`#gt_<name>` (gametime of that detector's first hit), `#val_<name>`, `#th_<name>`.
The globals `#hit` / `#hitgt` record whichever detector fired first.
`stop_on_hit: true` stops a replica at the first hit of any detector, `all` keeps it
running until every detector has fired, `false` runs to the tick budget.

`check_every_ticks: N` runs the checks on every Nth tick instead of every tick. The
generated pack is one `execute if block` chain per column (`column_stack`) or per
window per level (`pattern`), so a 25x25 area with both detectors is 1546 commands;
at `N = 20` that is ~77 commands per tick amortised.

### The relaxed smiley

`patterns.py` transcribes `montecarlo/sim.py:277-288` cell for cell, with the
citation on every line: eyes `SM_EYES = [(1,1),(3,1)]` (sim.py:227), mouth
`SM_MOUTH` = the ten cells of rows `dz = 3` and `dz = 4` (sim.py:228), scan height
`Y` = the top y of the first eye's column (sim.py:279-281), and a hit when both eyes
and **at least 3** of the ten mouth cells have their column top at exactly `Y`
(sim.py:288). A cell "has its column top at Y" is `unless block <cell> y air if
block <cell> y+1 air`, which is what sim.py's `top_placed & (topy == Y)` means.

The model knows which blocks an enderman placed and the datapack does not, so
in-game `top_placed` is just "non-air". The scenario keeps that honest: the pen
floor is one layer below the scanned levels, and the seeded loose blocks sit on a
spacing-3 grid, which can never produce the eye pair (the eyes are 2 apart in x at
the same z and `2 % 3 != 0`).

### Setup order

Fixed, and identical on every replica, because the mod's determinism depends on it:

1. wait for `Done (` in the container log (ticks are already frozen by
   `-Ddetmc.freezeOnStart=true`), `tick freeze` again for safety
2. `difficulty`, then the gamerules in file order
3. `time set`, `weather`
4. resolve `anchor_x` / `anchor_z` (`locate structure` works on ungenerated chunks)
5. `forceload add` for every region
6. settle: poll the entity list until it is unchanged for 30 polls (15 s). Chunk
   loading keeps running while ticks are frozen, so a dump taken straight after a
   forceload catches a different number of entities on each replica. `test/` learned
   this the hard way.
7. resolve `anchor_y` by binary search with `execute if block` (needs the chunk
   loaded, hence after the forceload)
8. `setup.prep` commands in order
9. fake players (`/player <name> spawn at x y z`, Carpet), then `gamemode`
   **retried** until the server stops saying "No player was found" and `list`
   checked: Carpet's spawn returns before the join, so an unretried gamemode
   silently leaves the player in survival. A player that never joins is a hard
   error. The same code runs on every resume -- see "What a resumed world keeps"
10. summons -- every summoned entity gets `Tags:["detmc_summoned", "<summon.tag>"]`
    merged into its NBT, then `setup.tag_entities` tags anything the scenario needs
    to count that was not summoned
11. probe for `/detmc reseed` (see `run.reseed` below)
12. install the detector datapack, enable it, `/reload`, **wait for the load
    function to actually run**, create the objective, measure the baseline, set
    `#th_<name>`, then `#armed 1` last

Then the run advances in segments of `min(detector.interval_ticks,
run.segment_ticks)` using the `goto_tick` method from `test/run-det.sh`: sprint to
60 ticks short of the target, freeze, close the gap with `tick step`, verify
`time query gametime` landed exactly. Replicas are advanced in parallel threads.

## Branching search: `branch.py`

`run.py` gives every replica the same tick budget whatever it does with it. For a
1-in-50-years event that is the wrong shape: most replicas are going nowhere and
keep their servers anyway. `branch.py` runs the same scenario as a beam search
over saved worlds instead.

```
generation 0   N replicas from the base world, one -Ddetmc.seed each
checkpoint     freeze, `function detmc:score`, read the progress metrics
select         keep the top K worlds, as full world-save copies
branch         M children per kept world: resume the copy, `/detmc reseed <s>`
prune          delete the rest
repeat         G generations, or until a detector fires
```

```bash
.venv/bin/python branch.py search scenarios/enderman_hunt.yaml \
    --replicas 4 --generations 2 --segment-ticks 3000 \
    --keep-top 2 --children 2 --concurrency 10
.venv/bin/python branch.py replay runs/<run>/lineage.jsonl g1n0 --run-id <run>-replay
.venv/bin/python branch.py replay-leg runs/<run>/lineage.jsonl g1n0 --repeat 3 \
    --column 89,-21,135,139
.venv/bin/python branch.py verify runs/<a>/checkpoints/g1n0 runs/<b>/checkpoints/g1n0
.venv/bin/python branch.py compare runs/<a>/dumps/g1n0.txt runs/<b>/dumps/g1n0-r1.txt
```

`--generations` counts generation 0, so `2` is one branching round. `--concurrency`
caps servers alive at once and defaults to 10: a replica holds ~1.5 GiB of RSS at
`MEMORY=1G` (measured, see "RAM is the binding constraint" below) and this box has
had 18-31 GB free. `branch.py` prints the arithmetic at startup and warns if the
cap does not fit. Other flags: `--keep-top K`, `--children M`, `--segment-ticks`,
`--memory`, `--jar` (use a jar from another checkout without merging it),
`--dump-at`, `--locate-at`, `--frames-at`, `--no-stop-on-hit`, `--keep`,
`--resume <run-id>` and `--resume-from <run-id>` (both below).

The four modes answer four different questions:

| mode | claim it can support | cost |
|---|---|---|
| `search` | this world reached this score | the whole run |
| `replay <node>` | the node is a function of (world seed, base rng seed, schedule) | one leg per resume point |
| `replay-leg <node>` | the node's own segment is a function of (parent world, resume gametime, reseed).  Nothing about how the parent arose | one leg |
| `verify <a> <b>` | two checkpoints on disk are the same world | no server at all |

Output lands in `runs/<run_id>/`: `lineage.jsonl` (one line per node, appended as
it finishes), `lineage.json` (the whole tree plus the best node), `manifest.json`,
`params.json` (every argument, for `--resume`), `checkpoints/<node>/world` +
`meta.json` (the kept world saves, ~2 MB each), `dumps/<node>.txt` (entity state
at every stop), `logs/<node>.log`, `compose/`. `work/<node>` is the live `/data`
and is deleted after each wave unless the node failed.

### `--resume <run-id>`: pick a dead search back up

```bash
.venv/bin/python branch.py search --resume hunt-br-wave1 --generations 183 \
    --jar /path/to/detmc-0.1.0.jar
```

The scenario argument is optional here; everything the run was launched with comes
back from `runs/<run-id>/params.json`, and a flag typed on the command line still
wins (that is how `--generations` gets raised). A run that predates `params.json`
falls back to `manifest.json`, which does **not** carry `--jar` or `--memory`, so
pass those again -- the log prints exactly what it restored and what the command
line kept.

It continues from the last generation that has at least 2 usable checkpoints left
on disk, which after a clean generation is exactly the `--keep-top` worlds, and:

* appends to the same `lineage.jsonl` (records past the resume point are left in
  place and logged as superseded);
* continues the ordinal counter, recovered from the lineage itself
  (`child_seed(ordinal) = ordinal * 1000003 + 7`, so a child's reseed *is* its
  ordinal), so resumed children never collide with reseeds already in the file;
* moves the old `DONE` aside to `DONE.before-resume-<timestamp>`, because a
  `until [ -f DONE ]` waiter would otherwise return the instant the run restarts;
* writes `manifest-resume-<timestamp>.json` rather than overwriting the original;
* **re-ranks the inherited parents with the resuming run's own `score.rank`.**
  Every other node's metrics are re-measured, but the generation read off disk is
  not, and its recorded `rank` was computed under whatever scenario wrote it. The
  first thing a resumed search does is `keep-top` over exactly those parents, so
  a stale number would pick the new beam by the old objective. The log prints
  every change (`g48n0: rank 2050447 -> 520447 under this run's score.rank`).

### `--resume-from <run-id>`: continue a run without writing to it

```bash
.venv/bin/python branch.py search scenarios/enderman_hunt_smiley.yaml \
    --resume-from hunt-br-wave1 --run-id hunt-br-wave2 --generations 183
```

`--resume` appends to the run it continues, which is wrong when that run is a
**result**: hunt-br-wave1 holds the only two observations of the 3-high pillar,
and 130 more generations of a different experiment in the same `lineage.jsonl`
would bury them. `--resume-from` seeds a **new** run directory from the old one
and then resumes that, so the source run is never opened for writing again:

| copied into `runs/<new>/` | why |
|---|---|
| `lineage.jsonl` | the schedules. A wave-2 node still replays from tick 0 through every one of its ancestors' resume points |
| `checkpoints/<node>/` for the last usable generation | the new beam's parents. Nothing else is copied: 8 dirs at 2.5 MB, not the 200 that were on disk |
| `template/` | the server install, hard-linked into every node's `/data` by `prepare_dir`. Without it all 8 containers re-download Fabric every generation. Copied with `cp -a`, **not** `cp -al`: a hard link would let a new container write through into the run this is supposed to leave alone |
| `manifest.json` -> `manifest-source.json` | provenance. The new run writes its own `manifest.json`, which is what `branch.py replay` reads the scenario file out of |

`RESUMED_FROM.json` records the source run, the generation continued from, the
checkpoints copied and when. `--run-id` is required (the point is a separate
run), the destination must be empty apart from a `*.log` (so the documented
`mkdir -p runs/<id>` + `> runs/<id>/driver.log` launch line still works), and
`--resume` and `--resume-from` are mutually exclusive.

Everything else is the `--resume` path unchanged, including "a typed flag wins":
the scenario is passed positionally above, so the continuation runs the **new**
scenario while `--replicas`, `--keep-top`, `--children`, `--segment-ticks` and
the world seed come back from the source run's `params.json`.

### One node failing does not end the search

Measured the hard way: hunt-br-wave1 died at generation 45 with 44 clean
generations behind it because 2 of 8 nodes failed to resume. A failed node is now
dropped, and a generation only aborts the search when **fewer than 2 nodes
survive** -- a beam of one has nothing to branch. Each failure gets one bounded
retry first (fresh copy of the parent checkpoint into a fresh container), the
failure goes in that node's lineage record, and `spawn_children` gives the spare
children to the best surviving parents so the wave width does not shrink for the
rest of the run.

### A lineage is a base seed and a list of resume points

```json
{"node": "g1n0", "parent": "g0n1", "generation": 1,
 "base_rng_seed": 2, "base_gametime": 1,
 "schedule": [{"gametime": 3001, "reseed": 5000022}],
 "resume_at_gametime": 3001, "final_gametime": 6001,
 "rank": 102004,
 "scores": {"stack_1": 48, "stack_2": 0, "stack_max": 1, "stack_blocks": 48,
            "smiley": 4, "smiley_gated": 2},
 "hits": {}, "checkpoint": "checkpoints/g1n0", "dump": "dumps/g1n0.txt",
 "timings": {"boot_seconds": 54.8, "setup_seconds": 13.8, "tick_seconds": 10.9,
             "ticks_per_second": 274.4, "score_seconds": 0.19}}
```

`schedule` is the whole branch: world seed and `-Ddetmc.seed` from the manifest,
then one `(gametime, reseed)` pair per generation. **Every gametime in it is a
resume point, not a mid-run reseed, and that distinction is the reason the format
works.** Vanilla serialises no `RandomSource` draw position, so a world that was
saved and reloaded can never be bit-identical to one that ran straight through
(docs/STATUS.md, "Persistence"). A schedule that said "reseed at tick 3001 of one
continuous run" would therefore not replay. Written as resume points it does,
because `branch.py replay` performs the same save, restart and reseed at the same
ticks. Replay is one process: leg 0 is a fresh world with the base seed, leg i
resumes leg i-1's checkpoint and issues `schedule[i-1].reseed`.

### Progress metrics

A detector answers yes/no. A search needs "how close is this world". `scenario.score`
declares metrics that are computed **on demand** by one `function detmc:score` over
rcon at a checkpoint, while ticks are frozen. They are in no function tag, so a
`run.py` run pays nothing for them.

| `metrics[].kind` | Keys | Scores it sets |
|---|---|---|
| `column_stack_profile` | `area`, `base_y`, `max_height`, `air_block` | `#s_<name>_<h>` = columns with >= h stacked blocks, plus the derived `<name>_max` (tallest column) and `<name>_blocks` (total blocks in the area) |
| `pattern_best` | `pattern`, `area`, `base_y`, `levels`, `air_block` | `#s_<name>` = most pattern cells at one scan level in any window; `#s_<name>_gated` = most `any_of` cells in a window whose `required` cells all match, and -1 when no window has them |

`score.rank` is an expression over those names (`max`, `min` and `abs` are
available). `enderman_hunt.yaml` ranks on
`100000*stack_max + 1000*max(smiley_gated, 0) + 10*stack_2 + smiley`: tallest
stack first, then how close any window is to the relaxed smiley, then the dense
2-stack count so the ranking is not all ties.

Measured on `mc-run-probe2`, the generated pack for `enderman_hunt.yaml` is 3389
lines in `detmc:score` plus a 19-line window function called 882 times, so about
19,000 commands per call: **0.14-0.19 s per checkpoint**, including the rcon round
trip. The cost is per checkpoint, not per tick.

### What a resumed world keeps, and what it loses

Measured 2026-09-12 on `mc-run-probe1`/`mc-run-probe2`: a world copied out from
under a live frozen server after a plain `save-all`, then started in a new
container.

| | after the restart |
|---|---|
| gametime | kept (609 -> 609) |
| forceload | kept (9 chunks, same list) |
| gamerules, time, weather | kept |
| blocks | kept (`execute if block 96 137 -30 dirt` passes) |
| the detector datapack | kept and auto-enabled, but **`detmc:load` has NOT run yet**: `#loaded` 1 and every holder come back from the save, and the `minecraft:load` tag only fires on the first *ticked* tick (measured 2026-09-12, see "The load tag runs on the first ticked tick") |
| `detmc-rng.properties` | kept, validated against `baseMaster` and adopted |
| `/detmc reseed` | works on the resumed world |
| **entities** | **all gone**: 5 endermen in, 0 out (on the merged master jar they come back; the fake players never do) |
| **fake players** | **all gone**: Carpet logs them out on shutdown and writes nothing that brings them back. `list` on a freshly booted copy of a set-up world reads 0 players (measured 2026-09-13, all 6 cells of the throughput A/B). `Replica.resume` re-spawns them |

The entity loss is the known detmc defect (`entities/*.mca` is 0 bytes on every
save, see "detmc never writes entity region files"), and it decides the design:

- `Replica.resume` re-summons `setup.summons` at the scenario's own positions. The
  mobs' positions, carried blocks and goal state do not survive a generation. The
  **block art does**, and the block art is what the detectors and the score read,
  so the search is a search over block configurations.
- An enderman that vanishes holding a block takes that block out of the arena, and
  the loose-block count is the arena's whole material budget: the mob never creates
  material, it only recycles it (montecarlo/README.md). Measured in `hunt-br1`, the
  25x25 pen held 45-53 of its 64 blocks at the 3000-tick checkpoint, so a few
  generations of this would empty it. `resume.restore_material` counts what is
  actually there with the same score function the ranking uses and refills the
  deficit into the first free cells of a fixed grid, in a fixed order, so a replay
  puts them back in the same places. It put 11 and 17 blocks back in `hunt-br1`.
- Nothing in the resume path steps the tick loop. No `/reload`, no `tick step`. So
  the gametime at the reseed is exactly the parent's checkpoint gametime, which is
  what makes `(gametime, reseed)` a complete description.

- The **fake players** do not come back either, and that one is worse than a lost
  mob: natural spawning only runs in chunks with a non-spectator player within 128
  blocks, so a resumed node with no player is a world where the scenario cannot
  happen. `Replica.resume` re-spawns every `setup.fake_players` entry and raises if
  `list` does not then show it, which drops the node instead of ticking an empty
  village. Until 2026-09-13 it did not, and `hunt-villager-wave1` ran 182
  generations that way -- `endermen` 0 in 548 of 549 node-generations.
  See "The fake player is re-spawned on every resume" below.

Order inside a resume is fixed and matters: re-summon, restore blocks, re-spawn the
fake players, **then** reseed. Siblings are identical up to the reseed, so the
reseed is the only thing that makes them different -- which is exactly why the spawn
goes before it and not after: a player spawned after the reseed would draw from the
new stream and every sibling would enter its segment at a different offset into its
own seed.

Two saving rules, both from measurements already in this repo:

- `save-all`, never `save-all flush`. The flush does not return on a detmc server.
  `Replica.save_world` issues the plain one and waits on the mtime of
  `<world>/detmc-rng.properties`, which the mod rewrites on every save. Measured in
  `hunt-br1`: 0.1-0.2 s.
- The checkpoint is copied out from under the still-running, still-frozen server.
  Not after `docker compose down`: compose's 10 s stop timeout SIGKILLs one of these
  servers before its shutdown save finishes (test/, 2026-09-12), so the on-disk
  world after a teardown is the previous autosave.

### The fake player is re-spawned on every resume (2026-09-13)

`run.Replica.spawn_fake_players` is one method called from both `setup` and `resume`.
It issues Carpet's `player <name> spawn at x y z [facing]`, **retries**
`gamemode <mode> <name>` until the server stops answering "No player was found",
reads `playerGameType` back, and raises unless `list` holds the name -- which fails
the node in `branch.py` (dropped, one bounded retry) rather than ticking an empty
village at a rate that reads like progress. `branch.py` writes what it spawned into
the node's lineage record and into the checkpoint's `meta.json`.

The retry is not defensive coding, and it is the resume path that needs it: the first
`gamemode creative` answered "No player was found" in **5 of 5 resumed nodes** and in
**0 of the 2 generation-0 setups** measured here. An unretried spawn leaves the player
online but in **survival**, which is a different world (mobs target a survival player;
a creative one is invulnerable and therefore invisible to targeting).

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

Verified on one node, resuming `hunt-villager-wave1`'s `g181n0` checkpoint
(gametime 8,736,001, reseed 548001651) for a 6,000-tick segment, one container at a
time through `memgate --need 3000`:

| at the end of the segment | control, no player (the old path) | with the fix |
|---|---|---|
| `list` | `There are 0 of a max of 20 players online:` | `There are 1 ...: villagewatch` |
| `playerGameType` | -- | 1 (creative), after 1 retry |
| monsters in the village box | -- | **32** (30 at the resume): 9 zombies, 1 skeleton |
| ticks/s | 254.5, 251.9 | **195.2, 190.9** |

**Two resumed containers still do not agree on mob state, and did not before this
either.** Same parent, same reseed, 6,000 ticks, two containers: final gametime, all
12 score metrics, the detector hits and **every block in every region file** match in
both arms; the `entities/*.mca` payloads differ in both (7 of 715 chunks with no
player, 18 of 721 with one). The control is the finding -- that is the per-entity
`RandomSource` draw position vanilla never serialises, not anything this fix did.
A `settle()` after the join was tried and does not close it (22 s a node,
`entityOrdinal` still 42118 against 42128), so it is not in the code. docs/STATUS.md,
"Two resumed containers do not agree on mob state".

### Verified end to end, and the replay is exact (2026-09-12)

`branch.py search scenarios/enderman_hunt.yaml --run-id hunt-br1 --replicas 4
--generations 2 --segment-ticks 3000 --keep-top 2 --children 2 --concurrency 10
--dump-at 1000,2000,2150,2200,2400`, world seed 12345, the `/detmc reseed` jar from the
feature branch that added it (commit 510a85b, sha256 `fec838e19afa7a3e...`).

8 nodes, 242 s wall. Generation 0 ranked 102004 / 101003 / 101003 / 100003, kept
`g0n1` and `g0n0`, and gave each two children. Every child resumed at gametime 3001
with 20 mobs re-summoned and its own reseed; the reseed reported `entities=20`, so
the re-key reached the whole loaded set.

Then `branch.py replay runs/hunt-br1/lineage.jsonl g1n0` re-ran that one leaf from
scratch: fresh world, `-Ddetmc.seed=2`, run to gametime 3001, save, restart,
`/detmc reseed 5000022`, run to gametime 6001. 217.6 s.

| compared | verdict |
|---|---|
| leaf entity dump at gametime 6001, Pos / Motion / UUID / carriedBlockState | **MATCH**, byte for byte (1845 chars of full-precision doubles for 20 endermen) |
| generation-0 leg, dumps at gametimes 1000, 2000, 2150, 2200, 2400, 3001 | **MATCH at every one** |
| leaf checkpoint, timestamp-blind region md5 (`test/region-md5.py`) | **MATCH**, all files |
| generation-0 checkpoint, same md5 | **MATCH**, all files |
| leaf `detmc-rng.properties` | **MATCH**, including `masterSeed=5012335`, `stateHi`, `stateLo` |
| progress scores at both checkpoints | identical (`stack_1` 53 then 48, `smiley_gated` 2, rank 102004) |

Negative controls on the same comparison, so the MATCHes are not an insensitive
diff:

| compared | verdict |
|---|---|
| siblings `g1n0` (reseed 5000022) vs `g1n1` (reseed 6000025), same parent | Pos, Motion and `carriedBlockState` **DIFFER**; UUID matches. That is the signature docs/STATUS.md records for a reseed: it creates and destroys no entity |
| `g0n1` (`-Ddetmc.seed=2`) vs `g0n0` (`-Ddetmc.seed=1`) | **DIFFER** from the first checkpoint, gametime 1000 |
| sibling checkpoints, region md5 | `region/r.0.-1.mca` differs |

Two honest caveats on that table:

- **The known gametime-2199 divergence did not reproduce here.** docs/STATUS.md dates
  the engine's remaining drift between two supposedly identical runs to gametime
  2199 with the `shouldRun` pin on, which is the default and is the code this jar
  was built from. This run dumped at 2150, 2200 and 2400 on purpose and matched at
  all three. The configurations differ in several ways at once (this arena has
  `spawn_mobs false`, `random_tick_speed 0`, no natural mobs and one forceload,
  and the replay leg ran alone while docs/STATUS.md's pair ran concurrently), so this
  is **not** evidence that 2199 is fixed. It is evidence that this scenario, on
  this path, replayed exactly. Nothing here isolates why.
- `data get entity @s id` returns nothing on 26.2, so that dump section is empty in
  every file and its MATCH carries no information. The section is kept because
  `test/run-reseed.sh` writes it too.

### Measured cost of branching

| item | generation 0 | generation 1 (resumed) |
|---|---|---|
| container boot, 4 in parallel | 48.6 s | 54.8 s |
| setup / resume | 49.9 s | **13.8 s** |
| 3000 ticks | 38.2 s (78.6 ticks/s, 6 barriers + 6 entity dumps) | 10.9-11.4 s (**264-274 ticks/s**, 1 barrier) |
| `function detmc:score` | 0.14-0.15 s | 0.17-0.19 s |
| `save-all` + world copy | 0.2 s + 0.3 s, 1.9 MB | 0.1-0.2 s + 0.2 s, 2.0 MB |

The 264-274 ticks/s at `--segment-ticks 3000` agrees with the 260 measured for
`run.py` at the same segment length, so the branching machinery costs nothing per
tick. What it costs is **~65 s of fixed overhead per generation per wave** (boot
plus resume), which is paid however long the segment is. At 3000-tick segments that
is 86% overhead. At the scenario's own 24000-tick segments it would be about 68%,
and the ticking rate goes up as well (728 ticks/s, measured for `run.py`). Use long
segments. The overhead is per wave, not per node, so widening `--concurrency` up to
what RAM allows is free.

Disk: a checkpoint is ~2 MB. A live `/data` is ~100 MB (822 MB for 8 nodes before
the cleanup was added), which is why `work/<node>` is deleted after each wave.

### Limits

- Mob state does not cross a generation, only blocks do. A scenario whose event
  depends on where a mob is standing cannot be branched this way until detmc writes
  entity region files.
- A generation advances straight to its target and reads the detector there.
  `detector.interval_ticks` is not subdivided, which costs nothing in hit
  resolution (the hit gametime comes from the pack's own `#gt_<name>`) but does
  mean `stop_on_hit` takes effect at the end of a generation.
- `restore_material` needs a `column_stack_profile` metric covering the whole
  arena; it reads `<name>_blocks` from it.
- Snapshots and renders are off in `branch.py`. The checkpoint is already a world
  copy; point `camera/snap.sh` at `checkpoints/<node>/world` to render one.

## Seed prefilter

`prefilter.py` turns `world.requirements` into concrete world seeds. See
`prefilter/README.md` for the Cubiomes image, the version it supports, and the
`/locate` fallback. `run.py` calls it as

```
prefilter.py --scenario <file> --count K --start-seed N --max-seeds N
```

and reads one JSON object from stdout: `{"method", "mc_version", "version_mismatch",
"checked", "seeds": [{"world_seed", "spawn", "structures"}]}`. Structure positions
from the prefilter are used as the anchor when present; otherwise the anchor comes
from `/locate structure` on the live server.

## Example scenarios

- `scenarios/zombie_siege.yaml`: plains village within 200 blocks of spawn, one
  Carpet fake player in the village, normal difficulty (easy zombies cannot break
  doors or zombify villagers), day-time clock running so the 18000 siege roll
  happens, detector = villager count below the count measured at setup.
- `scenarios/enderman_pillar3.yaml`: flat dirt pen with a stone-brick wall,
  20 persistent endermen, loose dirt blocks every 3 blocks **at foot level**
  (montecarlo finding: the take goal only samples cells at or above the enderman's
  own feet, so a solid floor is never picked up), permanent midnight so nothing
  teleports away in daylight, no fake player (a watched enderman turns hostile),
  detector = any column of 3 stacked blocks in the pen.
- `scenarios/enderman_hunt.yaml`: **the first real hunt.** Same idea, sized for it:
  a 25x25 dirt slab inside a 4-high stone-brick pen, L = 64 loose dirt blocks on a
  spacing-3 grid at foot level, 20 persistent endermen, easy difficulty, permanent
  midnight, no player at all, one in-game year per replica
  (365 x 24000 = 8,760,000 ticks) in 24000-tick segments, 10 replicas per wave and
  three waves for the full 30 (see the sizing section), and a `multi` detector
  running `pillar3` (`column_stack`, height 3) and `smiley_strict` (`pattern`,
  `smiley_strict`) together with `stop_on_hit: all`.  The relaxed smiley was the
  second detector until `hunt-wave1` measured it firing on 3 of 8 replicas in the
  first 48,000 ticks; it is a score metric now. Snapshots every 720000 ticks
  (30 in-game days, 12 frames per replica) with a camera resolved against that
  replica's anchor, rendered by `camera/snap.sh` with
  `mc-camera-chunky:2.5.0-478-mobs1`.

- `scenarios/enderman_hunt_smiley.yaml`: **wave 2.** `extends: enderman_hunt.yaml`
  plus two keys: `smiley_strict` is the only detector, and the rank leads with
  strict-smiley progress instead of `stack_max`. Same arena, same score metrics.
  `pillar3` fired twice in hunt-br-wave1, so leaving it in would stop every later
  wave on a 3-stack before the smiley ever had a chance; it stays visible as
  `stack_3` / `stack_max`, which are the same 625 clauses. See "hunt-br-wave2".

  Why one in-game year: montecarlo measures 0.0207 pillar-h>=3 and 0.0191
  relaxed-smiley per enderman-year, so 20 endermen for a year is an expected 0.41
  and 0.38 **first** hits per replica, ~12 and ~11 across 30 replicas. The arena is
  denser than montecarlo's (L = 64 on 625 cells, p ~ 0.10, against 64 on 4096,
  p = 0.0154), and rates go roughly as `p^(k-1)`, so hits should come sooner than
  the table says. This is a hunt for occurrences, not a rate measurement.

## Sizing the hunt: measured throughput, memory and cost

Everything below is from this box (56 cores, 72 GB, ~44 GB already held by other
services), `scenarios/enderman_hunt.yaml` unchanged except for the flag under test.

### Segment length is the throughput knob, not the core count

Each segment boundary is a freeze, a sprint-completion poll and a detector read, and
that fixed cost is paid per segment however long the segment is:

| `--segment-ticks` | replicas in parallel | heap | ticks/s per replica |
|---|---|---|---|
| 3000 | 8 | 2G | 260 |
| 12000 | 8 | 1G | 480 |
| 24000 | 10 | 1G | **728** |

The scenario ships `segment_ticks: 24000`, so 728 ticks/s per replica is the number
to extrapolate from. Going from 8 to 10 replicas did not cost throughput -- ticking
10 replicas is 15-20 of the 56 cores (measured 150-205% CPU each while sprinting,
under 1% while frozen), so the box is nowhere near CPU-bound.

### RAM is the binding constraint, and 30 replicas do not fit

Container RSS tracks the heap setting almost exactly, measured with `docker stats`
during a live run:

| `server.memory` | steady-state RSS per replica | OOM in any `r*.log` |
|---|---|---|
| 2G | 2.10 GiB | none |
| 1G | 1.46-1.61 GiB | none |

`free -g` reported **18-27 GB available** across the session. At 1.5 GiB each:

| replicas at once | RAM | verdict |
|---|---|---|
| 10 | ~15 GB | measured, 18 GB still free afterwards |
| 16 | ~24 GB | the ceiling, no headroom |
| 30 | ~45 GB | **does not fit** |

So the scenario is set to `replicas: 10` and the 30-replica hunt runs as three waves
with `--rng-seed-offset 0 / 10 / 20`, which gives rng seeds 1-10, 11-20, 21-30 and
three `results.jsonl` files to concatenate. This is a property of *this host*, not of
the scenario: re-check `free -g` and raise `replicas` if the box is quieter.

### Memory budget: every launch goes through `memgate.py`

This box (73.6 GB, no swap) once took a global OOM. Several launchers had started
detmc work at the same time with no shared budget: 8 search nodes at ~1.6 GB RSS,
3 parallel Chunky renders at ~1.9 GB, a 4G-heap verification pair at ~3.5 GB each
and one more run, on top of a ~37 GB baseline of unrelated services already on the
box. The kernel killed `mc-det-B` and several system daemons.

`runner/memgate.py` is the fix. Every launcher asks it first, and it blocks until
`MemAvailable` covers the job plus a reserve that is never spent:

| caller | need it asks for |
|---|---|
| `run.py` (`docker compose up`) | replica count x (heap + 1024 MB) |
| `branch.py` (wave `up`) | wave size x (heap + 1024 MB) |
| `branch.py` (single-node retry `up`) | heap + 1024 MB |
| `camera/snap.sh` (each Chunky pass) | 2500 MB |
| `camera/timelapse.sh` (render stage) | 2500 MB x `CHUNKY_JOBS` |
| `test/run-pair.sh` | 2 x (heap + 1024 MB), heap read from `compose-det.yml` |

- **Reserve: 12288 MB**, left to the host. Override with `DETMC_MEM_RESERVE_MB`,
  or `--reserve` on the CLI.
- **`CHUNKY_JOBS` defaults to 1.** Concurrent renders were part of the OOM.
  Raise it deliberately: `CHUNKY_JOBS=3 camera/timelapse.sh ...`, or `--jobs 3`.
- `branch.py` also caps wave width by what fits right now. A generation still
  runs every node, in more waves, and logs `memory budget: capping wave width`
  when it reduces the count. `--concurrency` is the ceiling, not a promise.
- A waiting gate prints one line per minute to stderr and never spins. It waits
  forever by default; pass `--timeout` if a caller must fail instead.
- Use it from a shell: `python3 runner/memgate.py --need 2500`. Exit 0 means
  acquired, 1 means the timeout expired. Success prints nothing.
- `down` and `rm` are never gated. Freeing memory must not have to wait for it.

### What the 30-replica hunt costs

| item | measured | source |
|---|---|---|
| boot + setup | 120-155 s for a whole wave, in parallel | `setup done in ...` |
| ticking | 728 ticks/s per replica | 48000 ticks, 10 replicas |
| one replica, full length | 8,760,000 / 728 = **3.34 h** | extrapolated |
| snapshot copy | inside the tick budget (~1.15 MB of `region/*.mca`) | `snapshot t...` |
| one render | 24.6 s at 56 threads, 37.6 s at 2 concurrent | `camera/snap.sh` |
| renders per wave | 12 rounds x 10 concurrent, ~12-18 min total | extrapolated |
| **one wave** | **~3.6 h** | |
| **all three waves** | **~11 h** | |

The two extrapolations are flagged as such: 728 ticks/s was sustained over 48,000
ticks, not 8,760,000, and the render figure assumes 10 concurrent Chunky runs scale
like the 2-concurrent measurement. Neither has been run to length. Set
`snapshots.threads` to bound Chunky if the renders start starving the servers.

## hunt-wave1

The first real wave, 2026-09-12. It ran flat (`run.py`), reached gametime 48,001 on
all 8 replicas with 3 detector hits, and was then stopped on purpose when the plan
changed to a branching search. `branch.py` **cannot** take over on the master jar;
the last subsection is the measurement that says why.

### Launch

```bash
cd runner
mkdir -p runs/hunt-wave1
setsid nohup .venv/bin/python run.py scenarios/enderman_hunt.yaml \
    --run-id hunt-wave1 --replicas 8 > runs/hunt-wave1/driver.log 2>&1 < /dev/null &
```

`setsid` plus the `< /dev/null` is what survives the launching shell. Everything
else comes from the scenario as committed: 8,760,000 ticks in 24,000-tick segments,
`stop_on_hit: all`, snapshots every 720,000 ticks with renders on. `--replicas 8`
is the only override, and it is a host measurement rather than a preference:
`free -g` reported 34 GiB available before the launch and 20 GiB while the eight
servers were generating their worlds. The rng seeds are the scenario's own first
eight, 1-8, because `rng_seeds[i % len]` with no `--rng-seed-offset`.

### The completion marker: `runs/<run_id>/DONE`

A detached driver is otherwise only observable by tailing its log, and "the log
stopped moving" does not distinguish a finished run from a killed one. `run.py`
writes `runs/<run_id>/DONE` from `Run.execute`'s `finally` block, so the file
appears exactly once whichever way the run ends.

| field | meaning |
|---|---|
| `status` | 0 = the tick loop completed, 1 = it raised |
| `error` | `"<ExceptionType>: <message>"`, null on a clean finish |
| `hits_total` | detector hits summed over every replica |
| `replica_summary` | per replica: `rng_seed`, `gametime_reached`, `hits` (detector name -> hit gametime) |
| `wall_seconds`, `setup_seconds`, `run_seconds` | the same clocks `results.jsonl` carries |

Poll for the file, not for the log:

```bash
until [ -f runs/hunt-wave1/DONE ]; do sleep 60; done; cat runs/hunt-wave1/DONE
```

Verified on the interrupt path in this run: `SIGINT` to the driver produced
`"status": 1, "error": "KeyboardInterrupt: "` with all 8 replicas summarised.

### Reading hits

Three places, in increasing order of detail.

```bash
# 1. the driver log, one line per hit as it happens
grep HIT runs/hunt-wave1/driver.log
#    r1: HIT smiley at gametime 9239 (value 0)

# 2. the marker, once the run ends
jq '.hits_total, .replica_summary' runs/hunt-wave1/DONE

# 3. results.jsonl, one JSON object per replica, written at the end.
#    `where` carries the actual matched columns / windows.
jq -c '{replica, rng_seed, hits, where}' runs/hunt-wave1/results.jsonl
```

Live, against a running replica, the scoreboard is the source of truth. Objective
`detmc`, holders `#hit_<name>` (0/1), `#gt_<name>` (gametime of that detector's
first hit), `#val_<name>`, `#th_<name>`, plus the globals `#hit` / `#hitgt` for
whichever detector fired first:

```bash
for d in pillar3 smiley; do
  docker exec mc-run-hunt-wave1-r0 rcon-cli "scoreboard players get #hit_$d detmc"
  docker exec mc-run-hunt-wave1-r0 rcon-cli "scoreboard players get #gt_$d detmc"
done
```

`runs/hunt-wave1/r<N>.log` has every rcon command and reply for that replica,
including the detector reads at each segment boundary.

### Measured on this wave

| | measured |
|---|---|
| containers booted | 8 of 8, 55 s (launch 09:49:26, last `started` 09:50:21) |
| boot + setup, whole wave | **110.5 s** (`setup done in 110.5s`), tick base 1 on every replica |
| detector armed | after **1 `tick step`** on all 8 (`detector pack loaded after 1 tick steps`), `#armed 1` set last |
| anchor | `96,135,-32` on all 8, same as `hunt-dry2` |
| segment 1, 24,000 ticks | 32 s -> **750 ticks/s** per replica |
| segment 2, 24,000 ticks | 29 s -> **828 ticks/s** per replica |
| container RSS, 8 up | **1.25-1.35 GiB** each, `docker stats --no-stream` |
| host RAM | 34 GiB available before, 20 GiB during worldgen |

Two honest notes on that table. The RSS figure was taken while the servers were
generating their worlds, not in steady-state ticking, so it is not comparable to
the 1.46-1.61 GiB in "RAM is the binding constraint" above; it is a floor. And
750-828 ticks/s beats the 728 measured at 10 replicas, which is consistent with 8
replicas leaving more of the 56 cores free, but two segments is not a sustained
measurement.

At 790 ticks/s (the mean of the two segments) a full 8,760,000-tick replica is
**3.1 h** of ticking plus 110 s of setup plus the renders. That extrapolates 375x
beyond what was measured.

### The smiley fires in hours, not years

3 of 8 replicas hit `smiley` inside the first 48,000 ticks, which is 2 in-game days:

| replica | rng seed | `smiley` hit gametime |
|---|---|---|
| r1 | 2 | 9,239 |
| r0 | 1 | 33,539 |
| r4 | 5 | 33,119 |

No replica hit `pillar3`. This is not a false positive of the kind the `/reload`
finding describes: the detector was armed after the load function ran, and test A
in "The detectors fire, and only on the right thing" showed the seeded spacing-3
layout cannot produce the eye pair on its own.

It is also not a surprise, though the scenario header underestimates it.
Hypothesis (arithmetic, not measured): the relaxed smiley needs 2 eyes plus 3 of 10
mouth cells, so k = 5 marked cells, and this arena runs at mark density p ~ 0.10
against montecarlo's 0.0154. Scaling as `p^(k-1)` is `(0.10/0.0154)^4` ~ 1800x
montecarlo's 0.0191 per enderman-year, which for 20 endermen lands near one hit per
in-game day. The observed 9,239 / 33,119 / 33,539 are in that range. Nothing here
isolates the exponent; the scenario header's "hits should come sooner than the
table says" is the claim, and this is the first data on how much sooner.

Consequence for the hunt: **`pillar3` is the rare event and `smiley` is not**. A
year-long wave spends almost all of its budget waiting for the 3-stack. This was
acted on rather than noted: the relaxed smiley is no longer a detector. The hits
are now `pillar3` and `smiley_strict`, and the relaxed count stays on as a score
metric, where "how close is this world" is exactly what it is good for. See "The
strict smiley" below.

### `branch.py` cannot run this wave on the master jar

**Measured, twice, on live servers this session.** Branching is save, restart,
`/detmc reseed <s>`: the reseed is the only thing that makes two children of the
same parent differ. The master build has no such command.

```
runs/hunt-wave1/r0.log:439
>>> detmc rng
Unknown or incomplete command. See below for errordetmc rng<--[HERE]
```

`branch.py search scenarios/enderman_hunt.yaml --run-id probe-branch-noreseed
--replicas 2 --generations 2 --segment-ticks 1000 --keep-top 1 --children 2
--concurrency 2` then died at generation 0, after a full boot and setup:

```
RuntimeError: g0n0: --reseed on, but the mod has no `detmc` command;
              g0n1: --reseed on, but the mod has no `detmc` command
```

`--reseed off` does not rescue it. `Replica.resume` is handed `node.reseed` for
every generation >= 1 node regardless of the flag, and raises
``no `detmc reseed` command on this server``. So the failure is at generation 0
with the default and at generation 1 without it.

The jar the "Verified end to end" section used is the one from that feature
branch, and it is still built and on disk:

| jar | sha256 | `detmc reseed` |
|---|---|---|
| `fabric/build/libs/detmc-0.1.0.jar` (master) | `64f5dd0c4533c91a...` | **no** |
| the feature branch's `fabric/build/libs/detmc-0.1.0.jar` (commit 510a85b) | `fec838e19afa7a3e...` | yes |

`branch.py --jar <path>` exists for exactly this and merges nothing. A branching
wave therefore needs one of: that `--jar`, or `detmc reseed` landing on master.

**Resolved: `--jar` it is.** `hunt-br-wave1` runs on that branch's jar and the probe
came back positive on every node, which is the same evidence in the other
direction:

```
g0n0: reseed probe -> detmc rng: masterSeed=12344 baseMaster=12344 reseeded=false
      reseedArg=0 issued=486 entityOrdinal=27 gametime=0
```

## The strict smiley

`smiley_relaxed` asks for two eyes and any 3 of 10 mouth cells. `smiley_strict` is
montecarlo's `pat('smiley', ...)` (sim.py:275) with `forbid_rest` at its default
`True` (sim.py:247), and it is a much narrower thing:

| | `smiley_relaxed` | `smiley_strict` |
|---|---|---|
| cells that must top out at Y | 2 (`SM_EYES`, sim.py:227) | **7** (`SMILEY[0]`, sim.py:226) |
| extra cells | >= 3 of the 10 `SM_MOUTH` cells (sim.py:288) | none, there is no `any_of` |
| the rest of the 5x5 window | unconstrained | **must be clear at Y** (sim.py:258-264) |
| measured rate here | ~1 per in-game day per replica | not yet observed |

The seven cells are `[(1,1), (3,1), (0,3), (1,4), (2,4), (3,4), (4,3)]`: two eyes
and a five-cell curved mouth. `Pattern.forbid_rest` derives the other 18 cells of
the window, and `count_function` emits a second counter `#f_<name>` for them,
gated on `matches ..0`. The relaxed pattern has no forbidden cells, so its
generated text is byte-identical to before the change (asserted against the
previous commit).

Cost is unchanged where it matters. The per-tick cost is still one command per
window per level, because the seven required cells fold into that one command's
condition chain; the 18 forbidden cells are only counted for a window that already
passed. The generated tick path is 625 `pillar3` + 882 `smiley_strict` + a 20-line
counter, against 1546 commands for the old pair.

### Verified on a live 26.2 server

`mc-run-detstrict-r0`, the exact pack `run.py` generates for the scenario, armed,
on the real arena with the endermen killed and the pen interior cleared between
cases. One 5x5 window at corner `90,-36`, scan level y=136. `tick step 40` between
the placement and the read, because `check_every_ticks` is 20.

| # | world state | `#hit_smiley_strict` | `#s_smiley_strict` | `#s_smiley_gated` (relaxed) |
|---|---|---|---|---|
| A | empty pen | 0 | 0 | -1 |
| B | eyes + 3 mouth cells: a **relaxed** smiley | **0** | 3 | **3** |
| C | the exact 7-cell shape | **1** @ gt 119 | 7 | 5 |
| D | C **plus one block at the window centre** | **0** | 7 | 5 |
| E | C again, after D | **1** @ gt 199 | 7 | 5 |

B is the case the whole change exists for: that arrangement satisfies the relaxed
detector (`#s_smiley_gated` 3 is exactly its threshold) and does not fire the
strict one. D is the sharper test, and it isolates one variable: `#s_smiley_strict`
stays at 7, so all seven shape cells are still there and still at the scan level,
and the only reason the detector does not fire is the one extra block inside the
window. That is `forbid_rest`, measured rather than argued. E shows it is
repeatable and that the flag reset works.

`test_patterns.py` covers the same ground offline, including a tiny interpreter
that runs the generated mcfunction lines against synthetic grids and cross-checks
them against the Python matcher: 25 tests, 0 failures, 9 of them strict-only.

### What the score metrics became

The relaxed smiley and the stack profile are score components now, never hits, and
one new metric rides along for free:

| key | 0..N | what it says |
|---|---|---|
| `stack_max` | 0..4 | tallest column in the pen. The rare signal |
| `smiley_strict` | 0..7 | most of the seven shape cells present at one level in any window. Distance to a hit, in cells |
| `smiley_gated` | -1..10 | most mouth cells in a window that already has both eyes. The relaxed count |
| `stack_blocks` | 0..64 | material actually left in the pen. The densest tiebreaker there is |

```
rank: 1000000*stack_max + 10000*smiley_strict + 100*max(smiley_gated, 0) + stack_blocks
```

The weights make it strictly lexicographic rather than approximately so: each
exceeds the largest possible sum of everything below it (64 < 100, 10*100+64 <
10000, 7*10000+1064 < 1000000). `smiley_strict` costs nothing extra to compute --
`pattern_best` already counts `required + any_of` cells, and the strict pattern
has no `any_of`, so the existing counter is the metric.


## hunt-br-wave1

The branching wave, launched 2026-09-12 10:07:26, finished 13:08:49 on a
`pillar3` hit at generation 48 (see "How wave 1 ended" below; it is continued
by hunt-br-wave2, which is a different experiment and a different run dir). It is the hunt
`hunt-wave1` was meant to be: same scenario, same arena, but a beam search over
saved worlds rather than 8 independent replicas, so a world that got somewhere is
branched instead of being left to its own luck.

### Launch

```bash
cd runner
mkdir -p runs/hunt-br-wave1
setsid nohup .venv/bin/python branch.py search scenarios/enderman_hunt.yaml \
    --run-id hunt-br-wave1 --replicas 8 --generations 183 --keep-top 4 \
    --children 2 --segment-ticks 48000 --concurrency 8 \
    --jar fabric/build/libs/detmc-0.1.0.jar \
    > runs/hunt-br-wave1/driver.log 2>&1 < /dev/null &
```

8 nodes per generation (generation 0 is 8 fresh replicas; every later generation
is the top 4 worlds with 2 children each), 48,000 ticks per generation,
183 generations. `183 x 48,000 = 8,784,000` ticks, which is 366 in-game days, so
one lineage covers the same in-game year a flat replica would.

48,000-tick segments rather than the scenario's 24,000: the per-generation
overhead is fixed, so doubling the segment halves how often it is paid. At
24,000 it would be 365 generations of ~145 s, about 14.7 h; at 48,000 it is 183
generations of ~187 s, about 9.5 h for the same in-game year.

`--jar` points at that branch's build because branching needs `/detmc reseed` and
master does not have it. See the previous section for the measurement.

### Marker and progress

| what | where |
|---|---|
| completion marker | `runs/hunt-br-wave1/DONE` |
| per-node records, appended live | `runs/hunt-br-wave1/lineage.jsonl` |
| whole tree plus the best node | `runs/hunt-br-wave1/lineage.json` (written at the end) |
| driver log | `runs/hunt-br-wave1/driver.log` |
| kept world saves | `runs/hunt-br-wave1/checkpoints/<node>/world`, ~2.3 MB each |

`branch.py` now writes the same marker `run.py` does, from `search`'s (and
`replay`'s) `finally` block, so it lands on a clean finish, a crash and a
`KeyboardInterrupt` alike. Beyond the `run.py` fields it carries `mode`,
`generations_requested` / `generations_run`, `nodes_completed`, the beam
parameters, `mod_sha256`, and `best` (`node`, `rank`, `final_gametime`).

```bash
until [ -f runs/hunt-br-wave1/DONE ]; do sleep 300; done; cat runs/hunt-br-wave1/DONE

# live: rank per node as each generation lands
grep ranking runs/hunt-br-wave1/driver.log | tail -3
jq -c '{node, rank, hits, scores}' runs/hunt-br-wave1/lineage.jsonl | tail -8
```

Hits read the same as in `hunt-wave1`: `grep HIT driver.log`, the `hits` field on
each lineage record, and the live scoreboard holders `#hit_pillar3` /
`#hit_smiley_strict` on `mc-run-hunt-br-wave1-<node>`. `branch.py` stops the whole
search at the first node that hits anything, which is a different rule from the
scenario's `stop_on_hit: all`.

### Measured, first two generations

| | generation 0 | generation 1 (resumed) |
|---|---|---|
| wall, whole generation | **239 s** (10:07:26 -> 10:11:25) | **187 s** (10:11:25 -> 10:14:32) |
| container boot, 8 in parallel | 71.4 s | 79.4 s |
| setup / resume | 60.3 s | **16.1 s** |
| 48,000 ticks | 89.1-92.4 s, **519-539 ticks/s** per node | ~90 s, same range |
| `function detmc:score` (3 metrics) | 0.20-0.29 s | 0.20-0.29 s |
| `save-all` + checkpoint copy | 0.1-0.2 s + 0.2 s, 2.3 MB | same |
| container RSS while ticking | **1.48-1.55 GiB** each, ~12.2 GiB for 8 | same |
| host RAM available | 17-19 GiB during a wave | |

The reseed probe came back positive on all 8, and each generation-1 node resumed
at gametime 48,001 with 20 mobs re-summoned and 17 blocks restored, which is the
material the vanished endermen were holding.

**"Every node armed correctly" was wrong, and this table's `detector pack loaded
after 1 tick steps` only ever applied to generation 0.** Measured 2026-09-12 (see
"The load tag runs on the first ticked tick"): every resumed node ticked its whole
segment with `#armed` 0, so 352 of the run's 360 nodes were watched by nothing.

**519-539 ticks/s is well below the 750-828 `hunt-wave1` measured** on the same
box with 8 replicas and the same 1G heap. The one thing that changed in the hot
path is the detector: `smiley_strict` folds seven required cells into each
window's check line, so that is 14 block conditions per window per level against
the relaxed pattern's 4. Hypothesis (unverified): that is the cost. Nothing here
isolates it -- the segment length and the driver also changed -- and it would take
an armed/disarmed pair on one container to say so properly, the way "What the
detector pack costs" does below.

### Projection

At 187 s per generation steady state:

```
239 + 182 x 187 = 34,273 s = 9.5 h
```

so the wave finishes around **19:40 local on 2026-09-12**. That is an **upper
bound**: `branch.py` stops the entire search at the first detector hit, and the
whole point of the beam is to make that arrive sooner than it would in a flat run.
It also extrapolates 91x from two generations.

Generation 0's best rank was 2,040,147 and generation 1's was 2,040,248. Decoded
against the rank expression: `stack_max` 2, `smiley_strict` 4 of 7 cells,
`smiley_gated` 1 then 2, `stack_blocks` 47 then 48. The beam is moving, slowly,
on the two lowest terms.

### Relaunched from generation 45 (2026-09-12 12:56)

```bash
cd runner
setsid nohup .venv/bin/python branch.py search --resume hunt-br-wave1 \
    --generations 183 --concurrency 8 \
    --jar fabric/build/libs/detmc-0.1.0.jar \
    >> runs/hunt-br-wave1/driver.log 2>&1 < /dev/null &
```

`>>`, not `>`: the crash is in that log and is worth keeping. The previous marker
moved itself to `DONE.before-resume-20260912-125646`.

Generation 45 landed at 12:59:42, **8 of 8 nodes**, 2 min 56 s after the launch:
boot 68.8 s, setup/resume 12.3 s, every node at gametime 2,208,001, no drops, and
the same ranks the verification run produced from the same checkpoints and the
same reseeds. `lineage.jsonl` went from 360 to 368 records.

At ~180 s per generation the remaining 137 generations are about **6.9 h**, so
around 20:00 local. That is still an upper bound, and this time the bound means
something: generations 1-44 ran with the detector disarmed, so this is the first
part of the wave where a hit can actually register.

### Generation 45 died on a half-written checkpoint

The wave crashed at 12:33 after 44 clean generations (360 nodes, 2.4 h):

```
RuntimeError: g45n0: detmc:load did not run at startup;
              g45n1: detmc:load did not run at startup
```

The message was true and pointed at the wrong thing. What the two nodes actually
hit, read off the artefacts the run left behind:

| where | what it says |
|---|---|
| `logs/g45n0.log` | `scoreboard players get #loaded detmc` -> **`Unknown scoreboard objective 'detmc'`** |
| `work/g45n0/logs/latest.log` | `Error loading saved data: SavedDataType[minecraft:scoreboard]` `java.io.EOFException: Unexpected end of ZLIB input stream` |
| `ls -l checkpoints/g44n4/world/data/minecraft/scoreboard.dat` | **10 bytes**, against 352 / 353 / 351 for the other three kept parents |
| the lineage | g45n0 and g45n1 are the only two children of **g44n4** |

So the whole `detmc` objective was gone, `Replica.resume`'s single
`score("#loaded") != 1` read could not tell a missing objective from a load
function that had not run, and it blamed the function. The checkpoint was
half-written: `checkpoint()` copies the world out from under a **running** server
and `save_world` waits on detmc's own `detmc-rng.properties`, which is not the
last file a save writes -- vanilla rewrites `data/**/*.dat` in place, with no
temp file and no rename, so a copy can catch one mid-write. 1 of the 180
checkpoints still on disk is corrupt this way (`run.bad_nbt_files` over all of
them), and it cost the run 138 generations.

Three changes, in the order they now catch it:

1. `branch.BranchRun.checkpoint` **verifies its own copy** with
   `run.bad_nbt_files` (gzip-decompress `level.dat` and every `data/**/*.dat`,
   and require `scoreboard.dat` to actually contain `detmc`) and, if it is
   half-written, saves again and re-copies, up to 3 attempts.
2. `prepare_dir` checks the parent checkpoint before it copies it, so the error
   names the parent instead of surfacing 90 s later as a missing objective. A bad
   `level.dat` is fatal for that node; a bad sidecar is a warning, because the
   server rebuilds those.
3. `Replica.await_startup_load` replaces the single `#loaded` read: it polls, and
   if it runs out it runs `function detmc:load` by hand -- the same function the
   tag runs, whose first line recreates the objective and every holder. It costs
   no tick, which matters here: a branch's resume gametime has to stay exactly the
   parent's checkpoint gametime, so `tick step` is not available the way it is in
   `await_reload`.

Replaying it on a copy of the run (`hunt-br-wave1-rtest`, the same corrupt
g44n4 checkpoint) with the fixes in: both children of g44n4 were flagged before
boot, hit the missing objective, were repaired by hand and resumed at gametime
2,160,001 like the other six.

```
WARNING g45n0: parent checkpoint g44n4 has corrupt saved data:
  data/minecraft/scoreboard.dat: EOFError: Compressed file ended before the end-of-stream marker
g45n0: WARNING detmc:load had not run 45s after boot (detmc objective MISSING);
  running `function detmc:load` by hand
g45n0: detmc:load repaired by hand (the detmc objective was missing: a corrupt scoreboard.dat)
g45n0: resumed at gametime 2160001, 20 mobs re-summoned, 11 blocks restored, reseed 361001090
```

### `#armed` is checked at the end of every segment

That same test run then failed all 8 nodes on a new invariant:

```
g45n0: FAILED while ticking: #armed was 0 at the end of the segment,
       so the detector was not armed throughout
```

`_advance_one` now reads `#armed` after the last tick of a segment and fails the
node if it is not 1, because a detector that was disarmed mid-segment reports "no
hit" for a reason that has nothing to do with the world. It cost one rcon read per
node per generation and it caught, on its first run, that **every resumed segment
of hunt-br-wave1 ticked with the detector disarmed**: `detmc:load` ran
`scoreboard players set #armed detmc 0` unconditionally, and on a resumed world
the `minecraft:load` tag fires on the first *ticked* tick, which is after
`Replica.resume` armed it. See "The load tag runs on the first ticked tick".

### The resume, verified

Third run of the same test, with all of it in (`#armed` initialised,
`refresh_detector_pack`, the copy verification, the objective repair, the
one-retry/drop rule):

| | |
|---|---|
| command | `branch.py search --resume hunt-br-wave1-rtest --generations 46 --concurrency 8 --jar <branch jar>` |
| restored from `manifest.json` | scenario, `--replicas 8`, `--keep-top 4`, `--children 2`, `--segment-ticks 48000`, seed 12345; command line kept `--generations`, `--concurrency`, `--jar` |
| resumed at | generation 45, from the 4 kept generation-44 checkpoints, next ordinal 361 (the same reseeds the crashed attempt had issued) |
| nodes completed | **8 of 8**, all at gametime 2,208,001 = 2,160,001 + 48,000 |
| the two corrupt-parent nodes | flagged before boot, objective rebuilt by hand in ~1 s, resumed normally |
| `#armed` at the end of the segment | 1 on all 8 (the invariant passed) |
| wall | boot 69.5 s, setup/resume 12.9 s, whole generation 176 s, `DONE` status 0 |

### How wave 1 ended: the 3-stack, a second time

`DONE` status 0 at 13:08:49 on 2026-09-12, generation 48, 392 lineage records.
`g48n5` hit `pillar3` and `branch.py` stopped the whole search, which is the rule
it runs under.

| | |
|---|---|
| node | `g48n5`, parent `g47n7` |
| lineage | world seed 12345, `-Ddetmc.seed=8`, 48 resume points |
| its segment | gametime 2,304,001 -> 2,352,001, resumed with `/detmc reseed 390001177` |
| hit | `pillar3` at gametime **2,350,060** |
| scores at the checkpoint, 1,941 ticks later | `stack_3` 0, `stack_max` **2** |
| ranking that generation | 2,050,447 best, every node `stack_max` 2 |

The last two rows are the pair that matters: **the pillar was gone by the end of
the segment.** The checkpoint was taken 1,941 ticks after the hit and holds a
2-stack where the 3-stack stood. An enderman takes blocks as readily as it places
them, and nothing in the arena holds a stack once it exists.

Its coordinates are **(84,136,-20), (84,137,-20), (84,138,-20)**. They are not in
the wave-1 record, which kept only `#gt_pillar3`; they were read back out of a
re-run of the leg with `--locate-at 2350059,2350060` (see "Replay verification").
The stack is still standing at the freeze at gametime 2,350,060 and is a 2-stack
at 2,352,001, so the block that made it a pillar survived at most 1,941 ticks.

So the 3-high pillar has now been observed twice: `g18n0` at gametime 912,020
(recovered after the fact by the armed/disarmed A/B, see "`pillar3` and the
`stack_3` score never disagreed") and `g48n5` at 2,350,060, live. The strict
smiley has been observed zero times. That is what wave 2 is for.

## Replay verification: both pillar finds reproduce exactly (2026-09-12)

The 3-high pillar has been observed twice, in `g18n0` and `g48n5`. Both were
re-run and compared with the checkpoint the hunt kept. **Every world-state
criterion matched on every run**, including `g48n5`'s hit gametime, which came
back as 2,350,060 three times out of three.

They are **the same lineage**. `g48n5`'s 48 ancestors start `g0n7 g1n5 ... g17n1
g18n0`, so the generation-18 pillar and the generation-48 pillar are 30
generations apart on one chain, and replaying `g18n0` from tick 0 also replays
the first 19 legs of `g48n5`.

Seven criteria, all computed by `branch.py verify <checkpoint-a> <checkpoint-b>`,
which needs no server:

| criterion | where it comes from |
|---|---|
| final gametime | `meta.json` |
| score metrics | `meta.json`: the 10 values `function detmc:score` sets |
| detector hits | `meta.json`: detector name -> hit gametime |
| region md5 | `test/region-md5.py` over all 10 `*.mca`, timestamp table blanked |
| region chunk payloads | md5 of each live chunk's decompressed NBT (see below) |
| `detmc-rng.properties` | the 8 keys, without the Properties file's date header |
| block column at the pillar | palette-decoded straight out of `region/*.mca` |

### Three runs each of the one leg that contains the event

`branch.py replay-leg` boots the node's **parent** checkpoint, applies the node's
reseed at the recorded resume gametime the way `search` does (re-summon, restore
material, reseed, arm), runs the node's segment and verifies. Each run is its own
fresh container and its own fresh copy of the parent world.

| criterion | g18n0 #1 | #2 | #3 | g48n5 #1 | #2 | #3 |
|---|---|---|---|---|---|---|
| final gametime | MATCH | MATCH | MATCH | MATCH | MATCH | MATCH |
| score metrics | MATCH | MATCH | MATCH | MATCH | MATCH | MATCH |
| region md5 | MATCH | MATCH | MATCH | MATCH | MATCH | MATCH |
| region chunk payloads | MATCH | MATCH | MATCH | MATCH | MATCH | MATCH |
| `detmc-rng.properties` | MATCH | MATCH | MATCH | MATCH | MATCH | MATCH |
| block column at the pillar | MATCH | MATCH | MATCH | MATCH | MATCH | MATCH |
| detector hits | **DIFFER** | **DIFFER** | **DIFFER** | MATCH | MATCH | MATCH |
| wall seconds | 181.7 | 195.0 | 185.6 | 196.8 | 201.2 | 195.5 |

| | g18n0 | g48n5 |
|---|---|---|
| leg | 864,001 -> 912,001 | 2,304,001 -> 2,352,001 |
| parent checkpoint | `g17n1` | `g47n7` |
| reseed at the resume | 145000442 | 390001177 |
| `masterSeed` / `stateHi` / `stateLo` at the end | 145012675 / 1374177551126747436 / -7250424717394844046 | 389988896 / -6032218923908443161 / 3739160749364270204 |
| pillar column compared | (89,-21), y 135-139 | (84,-20), y 135-139 |
| its blocks at the checkpoint | dirt, dirt, dirt, dirt, air | dirt, dirt, dirt, air, air |
| hits recorded by the three replays | `pillar3` @ 864,020 (x3) | `pillar3` @ **2,350,060** (x3) |
| boot / resume / 48,000 ticks | 61.8-69.8 s / 11.9-13.5 s / 90.6-98.5 s | 63.2-71.3 s / 14.6-19.6 s / 99.1-106.0 s |

**The one DIFFER is the arming bug, not a divergence.** hunt-br-wave1 recorded
`hits: {}` for `g18n0` because generation 18 ticked with `#armed` 0 (see "The
load tag runs on the first ticked tick"). The replay runs the current detector
pack, so it sees what was always there. The three replays agree with each other
exactly: `pillar3` at gametime 864,020, the first 20-tick gate after the resume,
which dates the pillar to **before** the segment. `g17n1` already scores
`stack_3` 1, so `g18n0` inherited the pillar and kept it for all 48,000 ticks.
The A/B in "`pillar3` and the `stack_3` score never disagreed" put the same
stack at 912,020 by arming the *checkpoint*, which is the other end of the same
standing pillar.

### A whole lineage from tick 0, 19 legs, compared leg by leg

`branch.py replay` re-derives `g18n0` from the world seed and the base rng seed
alone: leg 0 is a fresh world, and each later leg resumes the previous leg's
checkpoint and issues the recorded reseed. Every leg's checkpoint was then
compared with the hunt's own checkpoint for that generation, so a divergence
would be dated to a 48,000-tick leg rather than to the whole run.

`branch.py replay runs/hunt-br-wave1/lineage.jsonl g18n0`, 19 legs, **3,437 s**
(57.3 min), `DONE` status 0. All 19 ancestor checkpoints are still on disk, so
every leg was checked, not just the leaf:

| leg | resumes at | ends at | hunt's node | verdict |
|---|---|---|---|---|
| 0 | fresh world, `-Ddetmc.seed=8` | 48,001 | `g0n7` | all 7 MATCH |
| 1-8 | 48,001 .. 384,001 | 96,001 .. 432,001 | `g1n5` `g2n2` `g3n5` `g4n7` `g5n3` `g6n3` `g7n5` `g8n6` | all 7 MATCH |
| 9 | 432,001 | 480,001 | `g9n4` | 6 of 7: `region md5` differs in 12,153 bytes of **free sectors** |
| 10-15 | 480,001 .. 720,001 | 528,001 .. 768,001 | `g10n5` `g11n3` `g12n2` `g13n2` `g14n7` `g15n6` | all 7 MATCH |
| 16 | 768,001 | 816,001 | `g16n3` | 6 of 7: `pillar3` at **799,960**, hunt recorded none |
| 17 | 816,001 | 864,001 | `g17n1` | 6 of 7: `pillar3` at 816,020, hunt recorded none |
| 18 | 864,001 | 912,001 | `g18n0` | 6 of 7: `pillar3` at 864,020, hunt recorded none |

So there is **no divergent gametime**. The score metrics, the rng state, the live
chunk payloads and the pillar column agree at all 19 checkpoints, and the two
rows that are not all-MATCH are the two artefacts below rather than drift.

Per leg: boot 50.6-69.9 s, resume 10.1-18.3 s (62.6 s for leg 0's full setup),
48,000 ticks in 73.1-104.3 s at **460-657 ticks/s**. Ticking is 1,739 s of the
3,437 s, so half the wall clock of a replay is container boot.

**The three hit legs date the pillar.** It appears at gametime **799,960** inside
leg 16, and (89,136,-21) / (89,137,-21) / (89,138,-21) are still dirt in the
`g18n0` checkpoint at 912,001. So it stood for at least **112,041 ticks**, 4.7
in-game days, across three generations. `g16n3` is also the first ancestor whose
`stack_3` score is 1 (`g15n6` scores 0), so the detector and the score metric put
its birth in the same generation. Wave 1 saw none of this: generations 1-44 ticked
with `#armed` 0.

### `test/region-md5.py` over-reports: free sectors are not world state

Leg 9 is the one row that is not all-MATCH, and the world is identical anyway.

| | `g9n4` | `g18n0-r9` |
|---|---|---|
| `r.0.-1.mca` size | 868,451 bytes | 868,451 bytes |
| live chunks | 54 | 54 |
| chunks whose decompressed NBT differs | **0** | |
| chunks whose *compressed* bytes differ | **0** | |
| chunks at a different sector offset | **0** | |
| differing bytes after blanking the timestamp table | **12,153** | |
| sectors they are in | 197, 198, 199, 200 | |
| those sectors referenced by any chunk offset entry | **none** | |

A region file is a heap with a free list. When a chunk grows past its allocation
it is rewritten into new sectors and the old ones go on the free list with their
bytes still in them, so the dead space is a function of the save history, not of
the world. Leg 10 resumed from leg 9 and matched `g10n5` on all seven criteria,
which is the same statement from the other direction.

`region_chunks_md5` is the criterion that answers the question asked: md5 of
every live chunk's decompressed NBT, keyed by region file and chunk index. It is
reported next to the whole-file md5 rather than instead of it, because the
whole-file md5 is what every earlier determinism claim in this repo used.

### A mid-segment stop does not change the leg

`--dump-at`'s help says to use the same list in a search and its replay, and
hunt-br-wave1 ran with `dump_at ""`, so whether an extra freeze perturbs a run
was untested. Two more replays of the `g48n5` leg say it does not:

| extra stops inside the segment | rcon commands the whole leg issued | verdict against `g48n5` |
|---|---|---|
| none (x3) | 117 | all 7 criteria MATCH |
| gametime 2,350,060 | 767 | all 7 criteria MATCH |
| gametimes 2,350,059 and 2,350,060 | **4,529** | all 7 criteria MATCH |

The third run sat frozen for 12.3 minutes of wall clock in the middle of the
segment, across two stops one tick apart, and issued 4,412 rcon commands the
hunt never issued. Its checkpoint is still identical on all seven criteria. That
is one scenario and one leg, so it is evidence about this path rather than a
general rule.

### The merged master jar replays the event but not the whole world

Measured 2026-09-12, because a time-lapse needs mobs and only the merged jar
saves them. Same command as the three verified replays above, `--jar` pointed at
`fabric/build/libs/detmc-0.1.0.jar` (commit 44c6757, sha256 `a20b86a0...`, the
hunt's `/detmc reseed` plus everything in docs/STATUS.md phase 7) instead of the
hunt's own `fec838e1`, compared with hunt-br-wave1's `g48n5` checkpoint:

| criterion | verdict |
|---|---|
| final gametime | MATCH |
| score metrics | MATCH, all 10 (`stack_1` 47, `stack_2` 2, `stack_blocks` 49, `smiley_strict` 4 ...) |
| detector hits | MATCH, `pillar3` at **2,350,060** |
| block column (84,-20) y135-139 | MATCH: dirt, dirt, dirt, air, air |
| **region md5** | **DIFFER, 4 of 10 files** |
| **region chunk payloads** | **DIFFER, 50 of 81 chunks the two have in common** |
| **`detmc-rng.properties`** | **DIFFER in one key**: `entityOrdinal` 1131 vs 1138. `masterSeed`, `reseedArg`, `stateHi` and `stateLo` all match |

So the first differing criterion is `region md5`, and the event is not what
differs. What differs is the forest:

- 41 of the 50 differing chunks carry `block_ticks` in the hunt's checkpoint and
  **none** in the merged one, hundreds of entries per chunk, all of them
  `*_leaves` with tick times ~2.3 million ticks in the past. 18 carry the same
  shape of `fluid_ticks` for water and lava. The merged jar ran those scheduled
  ticks; the hunt jar left them queued.
- What that changes in the block data is leaf state: `Properties.distance` on
  `oak_leaves` / `dark_oak_leaves`, a handful of palette entries, water `level`,
  and the `Heightmaps` and `BlockLight` that follow from them.
- The four chunks the pen sits in, (5,-3) (5,-2) (6,-3) (6,-2), differ too, and
  differ in exactly that: `block_ticks` for leaves at the tree line, leaf
  `distance` values, and 8 to 18 of ~3,800 `block_states.data` longs per chunk.
  Nothing the pen is scored on moves, which is what the 10 matching score metrics
  and the matching pillar column say from the other direction.
- `entityOrdinal` 1131 vs 1138 is 7 extra entity constructions. Phase 7 makes
  entities persist and be read back, which is 7 constructions the hunt jar never
  did. The draw position itself (`stateHi` / `stateLo`) is identical.

**Consequence for the time-lapse.** The frames are the merged jar's replay. It is
the only jar that can put a mob in a frame at all (632 of 632 `entities/*.mca`
are 0 bytes on the hunt jar, measured below), and on this leg it lands the same
pillar at the same tick in the same column with the same score metrics. It is
**not** a bit-identical world: the trees around the pen have decayed differently.
Neither run is "right"; the hunt jar's queued leaf ticks are the artefact phase 7
fixed. Anything that needs bit-identity still has to use `fec838e1`.

### `--frames-at`: one renderable world per gametime

`--frames-at 2338000:2352000:350` turns a leg into time-lapse frames. At each
listed gametime it takes the same stop `--dump-at` takes, `save-all`s, and copies
`level.dat` plus every dimension's `region/` **and `entities/`** into
`runs/<run-id>/frames/<gametime>/`. That directory is a save root, so
`camera/snap.sh runs/<id>/frames/<gt> <cam>.json <out>.png` renders it as is.

Tokens are absolute gametimes or `START:STOP:STEP` (inclusive, so 40 evenly
spaced stops are one token instead of 300 characters of place for a typo to hide
in). A token equal to the leg's own resume gametime is taken before the first
tick, because `stops` is strictly after the start.

`entities/` is the reason this is not just `--dump-at`. Chunky reads living mobs
only from `entities/*.mca` (camera/README.md), the checkpoint path does not copy
it, and **the hunt's own jar never writes it**: 632 `entities/*.mca` written after
a `save-all` across hunt-br-wave1's 400 checkpoints and hunt-br-wave2's 232 are
**0 bytes, every one**, against 400 of 400 non-empty `region/*.mca` (measured
2026-09-12). A frame from that jar has no mob in it whatever the renderer does.

Measured 2026-09-12 on the g48n5 leg, 47 frames, the merged master jar
`a20b86a0` (the entity-save fix, docs/STATUS.md phase 7 defect 3):

| | measured |
|---|---|
| one frame | 1.4 MB, 0.1-0.4 s (66 MB for 47 frames) |
| `entities/*.mca` in a frame | 21,117 bytes at the resume, 38,942 bytes by the end |
| the leg, 48,000 ticks, 47 stops | 650.8 s |
| the same leg, same jar, 1 stop | 545.1 s |

**The 47 stops did not change the leg**, which is the same claim "A mid-segment
stop does not change the leg" makes for 1 and 2 stops, now at 47.
`branch.py verify` between the 47-stop run and the 1-stop run of the same leg on
the same jar:

| criterion | verdict |
|---|---|
| `detmc-rng.properties` | MATCH, all 8 keys including `stateHi` / `stateLo` |
| score metrics | MATCH, all 10 |
| detector hits | MATCH, `pillar3` at 2,350,060 |
| block column (84,-20) y135-139 | MATCH |
| region chunk payloads | 82 of 84 MATCH |
| the 2 that differ | one field each: `LastUpdate` 2304202 vs 2304203 in chunk (8,-2), 2304203 vs 2304204 in chunk (4,1). Both chunks are outside the pen, and that field is the save clock, not the world |

### Where the g48n5 pillar was, and how the hit gametime lines up

`--locate-at <gametime>` re-tests every detector's own predicate over the whole
arena at a stop, whether or not that detector has fired. It found the pillar:

| gametime, frozen | `#hit_pillar3` | `#gt_pillar3` | columns matching the 3-stack predicate |
|---|---|---|---|
| 2,350,059 | 0 | -1 | **(84,136,-20)** |
| 2,350,060 | 0 | -1 | **(84,136,-20)** |
| 2,352,001 | 1 | 2,350,060 | none |

The flag is not readable at the freeze at 2,350,060 even though the gametime it
eventually records is 2,350,060: the generated check runs inside the next ticked
tick and stores the gametime it read. Both stop runs read `#hit_pillar3` 0 there,
and both ended with `#gt_pillar3` 2,350,060, so the offset is reproducible.

A `column_stack` scan is one rcon round trip per column, 625 here, measured at
107 s. That is why the scan is per stop and off by default.

### Commands and cost

```bash
cd runner
J=runs/hunt-br-wave1/mods/detmc-0.1.0.jar   # sha256 fec838e19afa7a3e..., the hunt's own jar

# three replays of one leg, verified against the hunt's checkpoint
.venv/bin/python branch.py replay-leg runs/hunt-br-wave1/lineage.jsonl g48n5 \
    --run-id replay-leg-g48n5 --repeat 3 --column 84,-20,135,139 \
    --concurrency 1 --jar $J

# the same leg with the pillar located at the tick the detector saw it
.venv/bin/python branch.py replay-leg runs/hunt-br-wave1/lineage.jsonl g48n5 \
    --run-id replay-leg-g48n5-stop59 --locate-at 2350059,2350060 --jar $J

# the whole lineage from tick 0
.venv/bin/python branch.py replay runs/hunt-br-wave1/lineage.jsonl g18n0 \
    --run-id replay-g18n0-full --concurrency 1 --jar $J

# compare any two checkpoints, no server
.venv/bin/python branch.py verify runs/hunt-br-wave1/checkpoints/g9n4 \
    runs/replay-g18n0-full/checkpoints/g18n0-r9 --column 89,-21,135,139
```

| | measured |
|---|---|
| one leg, boot to verified | 182-201 s |
| three legs of one node | 563 s (g18n0), 594 s (g48n5) |
| one leg with a dump stop | 304 s |
| one leg with two `--locate-at` stops | 930 s |
| whole 19-leg lineage, 912,001 ticks | 3,437 s (57.3 min), half of it container boot |
| `branch.py verify` on two checkpoints | 0.4 s |
| container RSS | ~1.5 GiB per replay container, 3 concurrent, 15-23 GiB free throughout |

`--jar` points at `runs/hunt-br-wave1/mods/detmc-0.1.0.jar` rather than at the
build tree the hunt was launched from, because that tree no longer exists. `stage_mods`
copies the jar into every run directory, and that copy hashes to
`fec838e19afa7a3e668956f9efc3f7143bcbdc26badf2389cc85da21212a7456`, the
`mod_sha256` hunt-br-wave1's own `manifest.json` records. Every replay above
logged the same hash at startup.

### What this does and does not prove

- A leg **is** a function of (parent world, resume gametime, reseed), three times
  for each of two legs 1,440,000 ticks apart on the same lineage.
- A whole lineage **is** a function of (world seed, base rng seed, schedule), for
  one lineage of 19 legs and 912,001 ticks.
- The **negative control** runs through the same comparator. `g18n0` against its
  sibling `g18n2` (same parent, different reseed, same final gametime):

  | criterion | verdict |
  |---|---|
  | final gametime | MATCH |
  | score metrics | DIFFER: `stack_max` 3 vs 2, `stack_3` 1 vs 0, `stack_blocks` 49 vs 45, `stack_1` 46 vs 43, `smiley_strict` 5 vs 4 |
  | region md5 | DIFFER: 1 of 10 files |
  | region chunk payloads | DIFFER: 4 of 81 chunks |
  | `detmc-rng.properties` | DIFFER: `masterSeed`, `reseedArg`, `stateHi`, `stateLo` |
  | block column (89,-21) | DIFFER at y 138: dirt vs air |

  So the MATCHes above are not an insensitive diff, and the one block that makes
  `g18n0` a 3-stack is exactly the one the sibling does not have.
- Nothing here says a *continuous* run reproduces. Every comparison is between
  runs that took the same save/restart/reseed points, which is the only thing
  detmc claims (docs/STATUS.md, "Persistence").
- The 20 endermen are still not compared. `entities/*.mca` is 0 bytes on every
  detmc save, so the entity dumps `branch.py compare` reads carry no mob state
  across a restart.

## hunt-br-wave2: the strict smiley is the only hit

Launched 2026-09-12 13:16:51, continuing hunt-br-wave1's generation 48.

`pillar3` has fired twice now, and a detector that fires stops the search. Left
in, it would stop every future wave on another 3-stack long before the strict
smiley (observed: 0 times) ever had a chance. So wave 2 drops it from the
detector set and keeps hunting for the one thing that has never been seen.

```bash
cd runner
mkdir -p runs/hunt-br-wave2
setsid nohup .venv/bin/python branch.py search scenarios/enderman_hunt_smiley.yaml \
    --resume-from hunt-br-wave1 --run-id hunt-br-wave2 \
    --replicas 8 --generations 183 --keep-top 4 --children 2 \
    --segment-ticks 48000 --concurrency 8 \
    --jar fabric/build/libs/detmc-0.1.0.jar \
    > runs/hunt-br-wave2/driver.log 2>&1 < /dev/null &
```

Same beam, same jar, same 48,000-tick segments, same world. `--resume-from`
copied wave 1's lineage, its 8 generation-48 checkpoints and its server template
into `runs/hunt-br-wave2/` and left wave 1 alone; the first wave-2 generation is
**49**, resuming at gametime 2,352,001.

### What changed: one scenario diff

`scenarios/enderman_hunt_smiley.yaml` is `extends: enderman_hunt.yaml` plus two
keys. Everything else -- arena, gamerules, summons, score metrics, resume rules,
segment length -- is inherited, and asserted identical section by section.

| | `enderman_hunt.yaml` | `enderman_hunt_smiley.yaml` |
|---|---|---|
| detectors (a hit stops the search) | `pillar3` + `smiley_strict` | **`smiley_strict` only** |
| `stop_on_hit` | `all` | `true` (one detector, so the same thing) |
| generated tick path | 625 `pillar3` columns + 882 `smiley_strict` window-levels | **882 window-levels**, and the `check_smiley_strict` / `count_smiley_strict` / `gate` / `tick` files are **byte-identical** to wave 1's |
| score metrics | `stack` profile, relaxed `smiley`, `smiley_strict` | identical, all three |
| `rank` | `1000000*stack_max + 10000*smiley_strict + 100*max(smiley_gated,0) + stack_blocks` | `100000*smiley_strict + 10000*stack_max + 100*max(smiley_gated,0) + stack_blocks` |

**`pillar3` is demoted, not deleted, and it was already duplicated.** The `stack`
metric emits `stack_3` over the same 25x25 interior from the same `base_y` with
the same "3 non-air blocks above the floor" semantics, and this repo diffed the
two generated mcfunctions clause for clause: identical 625/625 (see "`pillar3`
and the `stack_3` score never disagreed"). So `stack_3 >= 1` **is** the old
detector, read at every checkpoint, and `stack_max` carries it into the ranking.
The pen is still measured for pillars every generation; a pillar just no longer
ends the run.

**The ranking still prefers taller stacks, second.** The new order is
`smiley_strict` (0..7 cells of the exact shape, distance to the only remaining
hit) first, then `stack_max` (0..4, the rarer event, and a world whose endermen
stack is upstream of both patterns), then the relaxed `smiley_gated` (0..10) to
break ties, then `stack_blocks` (0..64, material left in the pen). The weights
keep it strictly lexicographic rather than approximately so: 64 < 100,
10*100+64 = 1064 < 10000, 4*10000+1064 = 41064 < 100000.

### Verified on the first generation

Generation 49, 13:16:51 -> 13:19:56, **8 of 8 nodes**, all at gametime 2,400,001.

| check | how | result |
|---|---|---|
| the new pack reached the copied worlds | `docker exec mc-run-hunt-br-wave2-g49n<i> ls .../datapacks/detmc_detector/data/detmc/function/` on all 8 live containers | `check_pillar3.mcfunction` and `hit_pillar3.mcfunction` **gone**, `check_smiley_strict` present. `refresh_detector_pack` did its job |
| the hit set | same 8 containers, `check.mcfunction` | `execute if score #hit_smiley_strict detmc matches 0 run function detmc:check_smiley_strict` -- **one line, one detector** |
| `#armed` during the whole segment | `_advance_one` reads it after the last tick and fails the node otherwise | 8 of 8 completed, so **`#armed` was 1 on every node**. Confirmed again from the kept checkpoints' `scoreboard.dat`: `#armed` 1, `#tick` 0 (the gate had just reset it; a blind node reads 48000) |
| `pillar3` really is inert | `g49n7` descends from `g48n5`, so its saved scoreboard still carries `#hit_pillar3` **1** at gametime 2,350,060 | the search **did not stop**. The new `detmc:load` never mentions the holder, nothing reads it, and `read_detector` only knows `smiley_strict` |
| the parents were re-ranked | driver log | all 8 rewritten, e.g. `g48n0: rank 2050447 -> 520447`. Same order here (every node had `stack_max` 2), kept `g48n0 g48n3 g48n2 g48n5` |
| hits | driver log, generation 49 | none, which is the expected answer for one 48,000-tick generation |

### Cost and ETA

| | measured |
|---|---|
| generation 49, whole | **185 s** (13:16:51 -> 13:19:56) |
| boot, 8 in parallel | 82.7 s |
| resume | 12.4 s; 20 mobs re-summoned and 13-17 blocks restored per node |
| 48,000 ticks | **73.1-75.1 s, 639-657 ticks/s** per node |
| `function detmc:score` | 0.17-0.24 s |
| `save-all` + checkpoint copy | 0.1-0.2 s + 0.1-0.2 s, 2.5 MB |

639-657 ticks/s against hunt-br-wave1's **519-539** on the same box, same jar,
same 48,000-tick segments, same 8-node beam. The one thing that changed in the
hot path is that 625 `pillar3` column clauses left it. That is the cost wave 1's
table hypothesised for `smiley_strict` and never isolated, now measured from the
other direction -- but it is still **not** an isolation: the host's other load
was not controlled and a generation is not an A/B. Treat it as +20% throughput
that arrived with the detector change, not as a proven cause.

133 generations left (50..182) at 185 s is **6.8 h**, so it finishes around
**20:10 local on 2026-09-12**. That is an upper bound twice over: the search
stops at the first strict smiley, and the estimate extrapolates 133x from one
generation. `183 x 48,000` still means the lineage covers one in-game year.

```bash
until [ -f runs/hunt-br-wave2/DONE ]; do sleep 300; done; cat runs/hunt-br-wave2/DONE
grep -E "HIT |ranking" runs/hunt-br-wave2/driver.log | tail -3
```

## Measured while building the hunt (2026-09-12)

All of it on real 26.2 servers in this repo's own containers (`mc-run-lab`,
`mc-run-det`), not reasoned from source.

### `/reload` never finishes while the ticks are frozen, and it disarms the detector

The one that mattered most, because it fails **silently**. Sequence on `mc-run-det`,
frozen, with the detector pack installed:

```
scoreboard players set #loaded detmc 0
scoreboard players set #armed  detmc 1
reload                       -> "Reloading!"
... 20 s of polling ...      -> #loaded 0 / #armed 1     (reload has NOT completed)
tick step 1                  -> #loaded 1 / #armed 0     (load ran, and reset #armed)
```

`/reload` answers immediately and then does nothing until the server ticks. When it
finally completes, the `minecraft:load` tag fires and `detmc:load` resets `#armed` to
0 -- so a setup that issued `/reload` and set `#armed 1` afterwards had its detector
**silently disarmed by the first tick of the first segment**. Every score looked
right, `#tick` kept counting up, and no detector could ever fire. Anything run before
this fix with a generated detector should be treated as unproven, including the
`siege-e2e2` "hits: none" below.

The fix is `Replica.await_reload`: `detmc:load` ends with
`scoreboard players set #loaded detmc 1`, and `run.py` issues `tick step 1` in a loop
until it reads that back (2 steps in practice) before setting any score. **That
covers the setup path only.** The resume path cannot spend a tick at all, and the
same deferral bit it far harder -- see "The load tag runs on the first ticked tick"
below. Those ticks
run with `#armed` still 0, so they cannot cause a spurious hit, and the run's tick
base is read after setup, so they do not shift the timeline either.

### The load tag runs on the first ticked tick, and that blinded the whole hunt

The section above is about `/reload`. The same deferral applies to **startup**, and
it is worse there, because nothing in the resume path ever ticks: `Replica.resume`
must leave the gametime exactly at the parent's checkpoint value, so it cannot step
the tick loop the way `await_reload` does.

Measured on `mc-run-pillar3probe` 2026-09-12, booting the `hunt-br-wave1` `g18n0`
checkpoint with `-Ddetmc.freezeOnStart=true`:

| # | what | reading |
|---|---|---|
| 1 | the saved scoreboard, straight off the boot | `#tick` **48000**, the segment length -- `detmc:gate` had not reset it once in 48,000 ticks |
| 2 | 5 s of wall clock, still frozen | `#tick` still 48000: the `minecraft:tick` tag does not run while frozen |
| 3 | `tick step 25` | `#tick` **25**, not 48025 -- `detmc:load` ran *inside the step* |

So on a resumed world `#loaded` 1 is the **saved** value and proves nothing, and
the `minecraft:load` tag fires on the first ticked tick of the segment -- after
`Replica.resume` has set `#armed` 1. The generated `detmc:load` then ran
`scoreboard players set #armed detmc 0`, and the node ticked all 48,000 ticks with
its detector switched off.

Confirmed across the run from the saved `scoreboard.dat` of the checkpoints:
`g0n3` (generation 0, the setup path) has `#tick` 1 and `#armed` 1, while `g1n0`,
`g5n2`, `g18n0` and `g44n5` all have `#tick` 48000 and `#armed` 0. **352 of
hunt-br-wave1's 360 nodes, about 16.9M ticks, were watched by nothing**, which is
why ~30 nodes carried a standing 3-stack for 20+ generations and `hits` was empty
everywhere. Everything the run says about *rates* is void; its worlds and its
score metrics (`function detmc:score` runs on demand, frozen, and needs no tag)
are not.

Three changes:

* `run.detector_pack`: `#armed` is **initialised**, never reset --
  `execute unless score #armed detmc matches -2147483648.. run scoreboard players
  set #armed detmc 0`. A brand-new world still starts disarmed; a resumed one
  keeps the arming `Replica.resume` gave it.
* `branch.BranchRun.refresh_detector_pack` regenerates the datapack inside the
  copied world before it boots. A resumed world carries its parent's pack and is
  never reloaded, so without this no fix to the generator could ever reach a
  running lineage.
* `branch._advance_one` reads `#armed` after the last tick of every segment and
  fails the node if it is not 1. `#tick` at a checkpoint is the same test by hand:
  0-20 means armed, a full segment length means blind.

### `pillar3` and the `stack_3` score never disagreed

`g18n0` scored `stack_max` 3 with no `pillar3` hit, which looked like a detector
bug. It is not. The 625 condition clauses of that checkpoint's own
`check_pillar3.mcfunction` and the 625 `#s_stack_3` clauses of its
`score.mcfunction`, extracted and diffed, are **identical 625/625**: both test
`unless block <x> 136 <z> air unless block <x> 137 <z> air unless block <x> 138
<z> air` over `[84,-44]..[108,-20]`, same levels, same area, same semantics.

The stack is real and free-standing: dirt at **(89,136,-21), (89,137,-21),
(89,138,-21)** on the pen floor dirt at (89,135,-21), air above and on every
horizontal neighbour except one loose block at (88,136,-22); the pen walls are at
x = 83/109 and z = -45/-19, nowhere near it. It did not fire because of the arming
bug above, and the A/B says exactly that -- two containers, the same `g18n0` world,
each armed while frozen and then stepped 25 ticks:

| pack | `#armed` after | `#tick` | `#hit_pillar3` |
|---|---|---|---|
| as the run had it | 0 | 25 | 0 |
| `#armed` initialised, not set | **1** | 5 (the gate fired at 20) | **1 at gametime 912020** |

And with a hand-placed stack, after clearing the natural one: `setblock 90
136/137/138 -21 dirt` on a clean pen floor cell -> `#s_stack_3` 1 and
`#hit_pillar3` 1 at gametime 912060, the next 20-tick gate. The fresh-world path
still starts disarmed (objective removed, reload, 1 tick step -> `#armed` 0, and
40 armed-less ticks with a 3-stack standing produce no hit). `test_patterns.py`:
0 failures.

### 26.2 rejects a bare `pack_format` above 81

```
[Server thread/WARN]: Error reading pack metadata, attempting fallback type
com.google.gson.JsonParseException: Pack declares support for version newer than 81,
  but is missing mandatory fields min_format and max_format
```

The pack still loaded through the fallback path, which is not something to build on.
`pack.mcmeta` now carries `pack_format`, `min_format` and `max_format`, all 107, and
the warning is gone (0 occurrences after the change, same server, same reload).

### A forceload alone is enough for enderman AI

The question was whether the hunt needs a Carpet fake player for chunk activity.
It does not. On `mc-run-lab`, `list` reporting **0 of a max of 20 players online**,
one `forceload add`, 20 persistent endermen, 25 loose dirt blocks at foot level:

| after | loose blocks left at foot level | endermen carrying dirt | blocks at foot+1 |
|---|---|---|---|
| 0 ticks | 25 | 0 | 0 (wall only) |
| 6,000 ticks | 10 | 15 | 0 |
| 66,027 ticks | 12 | 13 | 0 |
| 966,036 ticks | 11 | - | **1** |

So the take goal fires within the first few thousand ticks with no player anywhere,
and by ~40 in-game days a block had been placed on top of another one. This is the
opposite of natural spawning and lightning, which do need a player within 128 blocks
(`docs/projects/minecraft-simulation-scenarios.md`) -- the hunt wants neither, so it
runs with no player at all, which also avoids `EndermanFreezeWhenLookedAt`.

### `anchor.y: surface` on water, leaves and logs

The old binary search took the highest **non-air** block, so it landed on a tree
canopy or a lake surface. `#minecraft:replaceable` exists in 26.2 and covers air,
water, short grass and snow layers; leaves and logs are not in it and need their own
tags. The predicate is now "not replaceable **and** not `#minecraft:leaves` **and**
not `#minecraft:logs`", still binary-searched (it is monotone in y on normal
terrain). Measured on two hand-built columns:

| column, bottom to top | old result | new result |
|---|---|---|
| stone, dirt, grass_block, short_grass, snow | snow layer | grass_block |
| dirt, oak_log, oak_log, oak_leaves | oak_leaves | dirt |

### The detectors fire, and only on the right thing

`mc-run-det`, the exact pack `run.py` generates for `enderman_hunt.yaml`
(1546 commands: 625 `pillar3` columns, 882 `smiley` window-levels, a 12-line shared
counter), armed, on the real seeded arena:

| # | world state | `#hit_pillar3` | `#hit_smiley` | `#c_smiley` |
|---|---|---|---|---|
| A | seeded arena only (64 loose blocks, spacing 3) | 0 | 0 | never set |
| B | + a hand-placed 3-stack at `90,136..138,-30` | **1** @ gt 139 | 0 | - |
| C | + a hand-placed relaxed smiley | 1 | **1** @ gt 164 | 4 |
| D | cleared, then 2 eyes + **2** mouth cells | 0 | 0 | 2 |
| E | + the third mouth cell | 0 | **1** @ gt 214 | 3 |
| G | a block placed on top of one eye | 0 | 0 | **0** |

A is the important negative: `#c_smiley` was never even created, so no window ever
got past the two-eye pre-check -- the spacing-3 seed layout cannot produce the eye
pair, exactly as `test_patterns.py::test_seeded_scatter_grid_never_matches` claims.
D/E bracket the `>= 3` threshold from both sides. G confirms the "column **top** at
Y" semantics: burying an eye one block deep takes the whole window out.

### What the detector pack costs

Same arena, 20 endermen, bare floor (montecarlo: a flat slab produces nothing, and
indeed `#hit` stayed 0 across all 80,000 ticks), `tick sprint 20000`, alternating:

| `#armed` | TPS | ms/tick |
|---|---|---|
| 0 | 1235 | 0.81 |
| 1 | 1055 | 0.95 |
| 0 | 1525 | 0.66 |
| 1 | 1154 | 0.87 |

Roughly +0.15 to +0.25 ms/tick for 1546 commands at `check_every_ticks: 20`
(~77 commands/tick amortised). Run-to-run noise on the disarmed runs is 1235 vs 1525,
so treat this as "the detector costs order 20-30%", not as a precise factor.

## Measured while building this (2026-09-11)

Four things that changed the design, all from this session's runs on real 26.2
servers:

- **A datapack `minecraft:tick` function runs while the tick rate manager is
  frozen.** First end-to-end run: both replicas reported a hit at gametime 0 with
  `#val` 5 and `#thresh` still at its load-time maximum, i.e. the check ran during
  the scripted setup. Every generated detector now sits behind
  `execute if score #armed detmc matches 1`, and `run.py` sets `#armed` to 1 as the
  last setup command. This is the same class of surprise as the mod's autosave
  countdown: `tickServer` keeps running while frozen.
- **`save-all flush` does not return on a frozen detmc server.** Timed on a live
  replica: `save-all flush` was still blocked after 120 s, plain `save-all` returned
  in **19.4 s** on the same container seconds later. detmc makes chunk saves
  synchronous and pins the save schedule to gametime, so a flush waits on work the
  frozen tick loop is not doing. Saves therefore go through `rcon_soft`, which
  bounds them and tolerates a timeout.
- **Gamerules are snake_case in 26.2** (`gamerule advance_time false` is accepted,
  `doDaylightCycle` is not). Confirmed both in the jar's `deprecated.json` and by the
  server's reply to every `gamerule` command in `runs/*/r*.log`.
- **The server console executes at the world spawn**, so
  `summon minecraft:marker ~ ~ ~` lands there and gives an exact spawn position over
  rcon. Verified against `locate structure`: on seed 12345 the marker read
  `[96.0, 136.0, -32.0]`, and the nearest `village_plains` was 835 blocks away, which
  is why that seed fails the `within: 200` requirement and the prefilter matters.

Cubiomes does not support 26.2 (newest is `MC_1_21`, printed as `1.21 WD`), so
prefilter hits are candidates confirmed on a real server rather than proof. On
seed 12, cubiomes predicted spawn `-16,-48` in plains and a `village_plains` at
`160,32` (193 blocks out); the 26.2 server put spawn at `-16,71,-48` and confirmed
both the biome and the village at `[160,~,32]`, 0 blocks of error. Details and the
`/locate` fallback are in `prefilter/README.md`.

### End-to-end proof run

`run.py scenarios/zombie_siege.yaml --replicas 2 --ticks 6000 --run-id siege-e2e2`,
2026-09-11:

| | |
|---|---|
| world seed | 12, from the prefilter (cubiomes, `1.21 WD`, 12 seeds checked, < 1 s) |
| anchor | village at `160,69,32`, taken from the prefilter's structure position |
| replicas | 2, `-Ddetmc.seed=1` and `-Ddetmc.seed=2`, same world seed |
| setup | 12 villagers measured as the baseline, `siegebot` spawned by Carpet 26.2, detector datapack enabled (`file/detmc_detector (world)`) |
| run | 12 segments of 500 ticks, both replicas landed on gametime **exactly 6000** |
| hits | none -- but see the `/reload` finding above: that run set `#armed 1` right after `/reload`, so the detector was almost certainly disarmed by the first tick and "no hit" proves nothing. Re-run it before quoting it |
| wall clock | 219 s per replica, 3 min 41 s for the whole pipeline including the prefilter, both server boots, setup and teardown |

## Dry run: the whole pipeline, 2 replicas, 6000 ticks (2026-09-12)

`run.py scenarios/enderman_hunt.yaml --replicas 2 --ticks 6000 --segment-ticks 3000
--snapshot-every 3000 --run-id hunt-dry2`, exit 0:

| | |
|---|---|
| world seed | 12345 (explicit, no prefilter), spawn `96,-32` |
| anchor | `96,135,-32`, resolved by the new surface predicate -- the same y a manual scan found |
| setup | 44 s container boot, 32 s settle (31 polls), pen + 64 loose blocks + 20 endermen, detector pack loaded after **1 tick step** |
| detector | `multi`, **1547 commands** (625 `pillar3` + 882 `smiley` + counter), armed after the load completed |
| run | 2 segments of 3000; both replicas landed on gametime **exactly 6001** (base 1, from the reload tick) |
| hits | none, as expected at 6000 ticks against a 0.02/enderman-year rate |
| snapshots | 4, each `region` ~1.15 MB, `entities` **0 B** with the warning fired |
| renders | 4 PNGs, 1280x720, 1.65 MB each, 24.6-38.7 s each |
| reseed | `reseed_supported: false` recorded; the run continued, which is the point of the flag |
| wall | 180 s per replica end to end |

Per-replica `results.jsonl` now carries `setup_seconds`, `run_seconds`,
`run_ticks_per_second`, `pack_commands`, per-detector `hits`, `reseeds_applied` and
`where` (the located columns / matched windows on a hit).

## Limits and TODOs

- **`run.reseed` reseeds inside a run; a branch reseeds at a resume point.** The
  two are different things: `run.reseed` re-keys one continuous run at a tick, which
  needs no world copy, while a branch has to fork the state and therefore has to
  save, restart and reseed (see `branch.py` above).
  `/detmc reseed <long>` and `/detmc rng` exist as of commit 510a85b; `run.py`
  probes with `detmc rng` during setup
  and keys on `masterSeed` in the reply, because a bare `detmc` is an incomplete
  command and answers like a missing one. If it is there, the schedule is issued as
  `detmc reseed <long>` at exact tick boundaries while frozen, and the applied
  checkpoints land in `results.jsonl` as `reseeds_applied`. If it is not, the run
  logs one warning and carries on. `--reseed off` ignores the schedule, `--reseed on`
  refuses to run without the command. `run.reseed` is either a list of `{at, seed}`
  or `{every_ticks: N}`, which derives `seed = rng_seed * 1000003 + k` so a run
  replays from the manifest alone.
- **detmc never writes entity region files, so snapshot renders have no mobs.**
  Measured 2026-09-12, with a one-variable control. Same image, same 26.2, same
  `server.properties`, frozen, one forceload, 9 summoned endermen, then `save-all`:

  | build | `entities/r.0.-1.mca` after `save-all` |
  |---|---|
  | unmodded Fabric | **25,258 bytes** |
  | unmodded Fabric + `-Dmax.bg.threads=1` | **25,262 bytes** |
  | detmc | **0 bytes** |
  | detmc + `-Ddetmc.syncReads=false` | **0 bytes** |

  `save-all flush` does not help: on detmc it blocks the server (rcon gave up at
  121 s and every later command timed out), while on the unmodded control it
  returned quickly and grew the file to 29,281 bytes. Unfreezing does not help
  either (still 0 bytes after 480 ticks of real running). It is not the
  `max.bg.threads` flag and not the synchronous-read mixin; those are the two
  variables that were isolated, so the cause is elsewhere in the mod and is for the
  mod to fix -- `runner/` only measures it.

  Consequences: the endermen are lost across a container restart, and a render of a
  runner snapshot shows the arena with no mobs in it. **Block data is unaffected** --
  the pen walls and the loose blocks read back out of the snapshot's `region/*.mca`
  correctly, and the block art is what the detectors are looking at, so the hunt
  itself is not blocked. `Replica.snapshot` copies `entities/**` anyway (it is what
  `camera/timelapse.sh` requires and it will just start working), records
  `entity_bytes` in each `snapshot.json`, and logs a warning when they are all empty
  so nobody reads a mob-free frame as a real one.
- Carpet 26.2 (`fabric-carpet-26.2+v260616.jar`) is fetched by `fetch-carpet.sh`,
  pinned by URL plus sha256, and only mounted for scenarios that declare fake
  players. Whether Carpet's own code keeps a detmc run bit-identical is **untested**.
- Replicas here differ by `-Ddetmc.seed` on purpose, so this is a search harness,
  not a determinism check. Use `test/` for determinism.
- `anchor.y: surface` now skips `#minecraft:replaceable`, `#minecraft:leaves` and
  `#minecraft:logs` (override with `anchor.surface_skip`), so it lands on the ground
  under a lake or a tree rather than on the water or the canopy. It still assumes the
  predicate is monotone in y, which a floating island or a big overhang would break.
- `entity_count` should count a **tag**, not a distance. `run.py` merges
  `Tags:["detmc_summoned", "<summon.tag>"]` into every summon's NBT (verified in
  `hunt-dry2`: all 20 endermen went in as
  `{Tags:["detmc_summoned","hunt_enderman"],PersistenceRequired:1b,Silent:1b}`, the
  scenario's own NBT kept), and `setup.tag_entities` tags anything the scenario did
  not summon. Measured on a live 26.2 server, 5 tagged villagers:

  | event | `@e[...,tag=sv]` | `@e[...,distance=..80]` |
  |---|---|---|
  | baseline | 5 | 5 |
  | one teleported 84 blocks out, still forceloaded | **5** | 4 (false alarm) |
  | one killed | **4** | 4 |

  **The tag does not survive unloading.** `@e` only sees loaded chunks, so the same
  test with the villager at z=200, outside the forceload, dropped the tag count too.
  A tag count is only honest out to the forceloaded area, which is why
  `zombie_siege.yaml` forceloads +-64 blocks around the village -- further than a
  villager with a bed wanders. A scenario that writes its own `Tags:` in
  `summons[].nbt` keeps it verbatim and is on its own.
- Snapshots copy `level.dat`, `region/*.mca` and `entities/*.mca` in every dimension
  after `save-all` while frozen, which is what `camera/README.md` requires -- except
  that the entity files are empty, see above. Rendering is off by default because the
  path tracer is slow (measured 24.6 s for 1280x720 at 32 spp on 56 threads); run
  `camera/snap.sh <snapshot>/world <snapshot>/../camera.json out.png` later.
- `snapshots.camera` may be a path or an inline object, and `${...}` is expanded in
  either, because the shared `camera/cams/*.json` are pinned to seed 12345's spawn
  and point at empty air on any other seed. The resolved camera is written once per
  replica to `snaps/r<N>/camera.json`.
- `${...}` expressions are evaluated with `eval` under a restricted namespace. This
  is a local tool; do not feed it untrusted scenarios.

## Files

| Path | What |
|---|---|
| `run.py` | the runner |
| `branch.py` | the branching (go-explore) search driver, whole-lineage and one-leg replay, checkpoint verify, dump compare |
| `patterns.py` | ground-plane patterns; the relaxed and strict smileys transcribed from `montecarlo/sim.py` |
| `test_patterns.py` | unit tests for the matcher AND for the mcfunction it generates |
| `prefilter.py` | world-seed prefilter (Cubiomes, `/locate` fallback), `--seed-source random` |
| `bench_worldsize.py` | simulation-distance x fake-player sizing sweep for natural-map scenarios |
| `prefilter/` | Cubiomes Docker image, scan program, notes |
| `scenarios/` | example scenarios |
| `fetch-carpet.sh` | pinned Carpet 26.2 fetch into `libs/` |
| `runs/` | one directory per run: compose, mods, worlds, logs, results |

## The villager-trap hunt: an enderman walls a villager into its house

`scenarios/enderman_villager_trap.yaml` is the first scenario in this directory that
runs on a **map nobody built**: a natural seed with a real `village_plains`, natural
terrain, and the endermen that natural spawning provides. There is no pen, no
scatter, no summon. Everything the detector needs to know about the geometry is
discovered on the live world at setup.

| | |
|---|---|
| the event | a villager is indoors (within 5 blocks of a doorway cell and within 3 of a bed) while that doorway cell holds a block that blocks movement and that was air when the run started |
| second event | a villager boxed in on all four sides at foot **and** head level, with at least 3 of the 4 foot-level walls holdable |
| why it is reachable | montecarlo puts "a villager is in a 1x1 cell as its fourth wall goes up" at 3e-6/enderman-year (~17,000 years with 20 endermen). A doorway needs **one** block in one of a few dozen named cells, and the villager walks in there by itself every night |
| what sets the rate | how many endermen are alive near the village, which is a spawn-cap outcome and is therefore measured rather than chosen |

### Three new pieces of machinery, and why each exists

**`setup.survey` -- a one-shot scan that leaves its answer in the world.** A built
arena knows where its doors are; a real village does not, and finding out over rcon
is not an option (one `execute if block` round trip is ~0.17 s measured, so a
19,000-cell volume is an hour). Inside a datapack function the same conditions cost
~0.2 s, and a matching command can `summon minecraft:marker` at the cell it just
matched -- so the scan runs in the server and its **result** comes back as entity
positions in one rcon call.

**`derive` -- the block baseline, taken marker-relatively.** A second pass in the
same function walks the `detmc_door` markers it just created and marks the cells each
door opens into **that are air at that moment**. Two of a door's four horizontal
neighbours are its frame and drop out for free. That set, `detmc_dcell`, *is* the
baseline: a later "this cell holds a carried block" is a change from it by
construction, with no per-cell bookkeeping and nothing for a resumed branch node to
recompute. The markers are saved with the world, so a generation inherits them.

**`marker_relative` -- a detector that does not know any coordinates.**
`execute as @e[type=minecraft:marker,tag=detmc_dcell] at @s ... run function` is one
command whose cost is the number of markers, and whose geometry is the world's rather
than the scenario's. The whole per-tick detector path for this scenario is **2
commands** (against 882 for the strict smiley).

### "An enderman put it there", without a block-place event

No datapack has a block-placed trigger, so the built-arena scenarios seeded a block
type nothing else uses. On a natural map that is not available, and the argument is
made out of three facts instead:

1. the cell was **air** when the survey marked it (`derive: if_block: minecraft:air`);
2. it now holds a member of `#minecraft:enderman_holdable` -- the 26.2 tag, read out
   of the server jar: `#small_flowers`, `#dirt`, `#mud`, `#moss_blocks`,
   `#grass_blocks`, sand, red_sand, gravel, clay, the two mushrooms, tnt, cactus,
   pumpkin, carved_pumpkin, melon, the four nether fungi/nylia, cactus_flower. In a
   world with **no survival players** an enderman is the only thing that places any
   of them;
3. it is **not** `#minecraft:replaceable`, which throws out the members of that list
   that a villager walks straight through (small flowers) and the one the weather can
   deposit (snow layers).

And the claim that nothing already satisfies it is **measured, not argued**:
`Replica.check_unarmed` runs every detector's check exactly once while `#armed` is
still 0, records anything that fires, and resets the flags. A natural village is not
a controlled arena, so the baseline is taken on the world that is about to be run.

### `anchor.y: surface` was silently 40 blocks underground

The first setup of this scenario found **0 doors, 0 beds and 0 villagers** in a
village the 26.2 server itself locates at 240,-320, and nothing failed. The cause is
not the prefilter and not the village: it is `Replica.surface_y`, which was a binary
search over "the highest y that is not replaceable / leaves / logs" and which assumed
that predicate is monotone in y. Measured on `mc-run-trapverify-r0` at that column:

| y | 20 | 30 | 40 | 50 | 60 | 64 | 67 | 68 | 69 | 70 | 75 | 80 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| not replaceable | X | . | . | X | X | **.** | X | **X** | . | . | . | . |

`grass_block` at 68, air at 69, and a **cave at 64**. The predicate is false at 64 and
true at 60 and 68, so it is not monotone, and the binary search converged on
**y = 28**. Two things follow, and the second is the important one:

* `surface_y` is now a **descending step-1 scan** from `anchor.surface_ymax`
  (default 200), returning the topmost non-replaceable block. The step-1 default is
  itself a measurement: a first attempt used a coarse step of 8 with an upward
  refinement inside the last gap, and on this same column it stopped at **y = 48** --
  the probe above the surface (72) and the probe below it (64, the cave) are both
  air, so the coarse pass stepped over the grass at 68, and the gap it then refined
  (49..56) is below the surface it skipped. A step above 1 is only sound on terrain
  known to be thicker than the step, which is the flat-slab arenas and not a real
  world. Cost is (ymax - surface) round trips at ~0.17 s each, so the scenario sets
  `anchor.surface_ymax: 140` to keep it near 12 s; the slab arenas at y=135 pay ~65
  probes from the default 200.
* **a wrong anchor is not an error.** The run booted, resolved an anchor, forceloaded,
  surveyed, passed its baseline check and was ready to tick for hours around solid
  rock. The survey's marker counts are the thing that caught it, which is why they
  are logged (`survey in 2.9s -> {'detmc_door': 0, ...}`) and why a natural-map
  scenario should be treated as broken until they are non-zero.

The prefilter is exonerated and gets a second measurement out of it: cubiomes (1.21
worldgen) and the 26.2 `locate` backend **agree** on this seed, both putting
`village_plains` at 240,-320, 400 blocks from a world spawn of 0,0. `resolve_xz` now
prefers the live `locate structure` anyway and logs the prefilter's prediction beside
it, because "the server is authoritative and the disagreement is visible" is cheaper
than one rcon call is expensive.

### Seeds that look like seeds

`prefilter.py --seed-source random` (the new default) draws candidates from
`random.Random(master).getrandbits(64)` folded into the Java long range, and
`run.derive_rng_seeds` derives `-Ddetmc.seed` per replica with SplitMix64 instead of
handing out 1..N. Both use master `0x6d65742d64657403`, which goes into
`manifest.json` under `seed_derivation` together with the formula, so any seed in any
run can be re-derived from two numbers without the run. For the cubiomes backend a
random candidate list means one `--count 1` invocation per candidate rather than one
sweep; measured acceptance for `village_plains within 512` with no spawn-biome
requirement was **candidate 2 of 2**, so the extra docker overhead is irrelevant.

### New scenario keys, all additive

| key | kind | what it does |
|---|---|---|
| `setup.survey` | `bounds`, `marks: [{tag, block, unless_below}]`, `derive: [{tag, from, offsets/radius, if_block, unless_block}]` | one-shot `detmc:survey` over the bounds, marking blocks with `minecraft:marker`; `derive` is a marker-relative second pass in the same function. Run once by `Replica.run_survey` at setup, frozen, no tick spent. Starts with `kill @e[tag=detmc_survey]`, so running it twice does not double the markers |
| `setup.fake_players[].gamemode` | string, **default `creative`** | `gamemode <mode> <name>` right after the Carpet `player ... spawn` |
| `detector.kind: marker_relative` | `marker_tag`, `offsets`/`radius`+`y_levels`, `if_block`, `unless_block`, `then: [...]` | one command per offset, walking every marker with that tag. `then` is appended verbatim so a scenario can chain `as @e[...] at @s if entity ...` |
| `detector.kind: entity_enclosed` | `selector`, `levels`, `air_block`, `material`, `material_levels`, `min_material` | `execute as <selector> at @s align xyz` + the 4 horizontal neighbours at each `levels` offset. `air_block` may be a tag, so "solid" can mean `unless ... #minecraft:replaceable` |
| `detector.kind: region_entity_blocked` | `selector`, `regions: [{interior, blocked}]`, `block` | an entity inside a box AND every listed cell holding `block`; one command per region. For built arenas, where both are known geometry |
| `detector.kind: block_present` | `area`, `base_y`, `levels`, `block` | one command per cell; pair with `requires` |
| `detector[].requires` | list of detector names | only check this detector once those have fired. A consequence detector costs nothing until its cause happens |
| `score.metrics[].kind: marker_neighbourhood_count` | `marker_tag`, `offsets`/`radius`+`y_levels`, `if_block`, `unless_block` | `<name>_max` / `<name>_sum` / `<name>_n` over the markers |
| `score.metrics[].kind: entity_enclosure_profile` | `selector`, `levels`, `air_block`, `material`, `material_levels`, `near_sides` | `<name>_sides` (max sides walled), `_closed`, `_near`, `_mat` |
| `score.metrics[].kind: cell_block_count` | `cells` or `area`+`base_y`+`levels`+`spacing`, `block` | `<name>` = how many of those cells hold that block. `resume.restore_material.count_metric` accepts it, so a material budget can be one block id rather than "every non-air block" |
| `score.metrics[].kind: entity_count` | `selector` | `<name>` = the count. A population signal |
| `anchor.surface_ymax` / `surface_step` | ints, default 200 / 8 | the descending surface scan |

`Replica.check_unarmed` runs after the survey and before `#armed 1`: every detector's
check once, with the flags reset afterwards, so `results.jsonl` and the log carry the
false-positive set of the world as it was set up.

### Sizing a natural-map run

`simulation_distance: 4`, one fake player, for a reason that is the opposite of the
usual one -- it is not about cost, it is about **concentration**:

* spawning chunks are entity-ticking AND inside the DistanceManager's radius-8
  natural-spawn tracker AND within 128 blocks of the player, so raising the
  simulation distance past 8 buys no extra spawning chunks at all, only extra
  entity-ticking ones (81 spawning chunks at sim 4, ~201 at sim 10 -- see
  `docs/projects/minecraft-simulation-scenarios.md`);
* the MONSTER cap is 70 **per player** (`MobCategory.maxInstancesPerChunk` x
  `spawnableChunkCount` / `NaturalSpawner.MAGIC_NUMBER`, plus
  `LocalMobCapCalculator`), so a larger simulation distance spreads the same 70
  monsters over more chunks. At sim 4 they are packed into the 81 chunks around the
  village, which is where the event has to happen.

The lever that does raise the eligible population is **more players**, not more
distance, because the per-player cap is per player: two creative fake players a few
hundred blocks apart, each with the village inside its own 128-block radius, is close
to twice the monster density over the village. `setup.fake_players` is a list for
exactly that, and `bench_worldsize.py` measures the trade-off
(simulation-distance x players, ticks/s, loaded chunks, mob census, RSS, and the
derived "spawning-chunk-ticks per core-second" and "mob-ticks per core-second") on a
regular world with no arena in it.

### `bench_worldsize.py`

A standalone sizing sweep for regular (unbuilt) worlds: no arena, natural spawning on,
night, weather cycle on, creative fake players `--spread` blocks apart (default 384,
so their radius-8 spawn trackers barely overlap).

```bash
.venv/bin/python bench_worldsize.py --run-id bench1 --sim 4,6,10 --players 1,2,4,8
```

Per cell it reports boot seconds, ticks/s over `--ticks` (default 24,000) under one
`tick sprint`, the loaded-chunk count, the monster and animal census (per type, since
26.2 has no "every monster" block tag), RSS and the container's own CPU seconds, then
derives `spawning_chunk_ticks_per_core_second` and `mob_ticks_per_core_second`.

Two implementation notes that are measurements rather than choices. **Loaded chunks
are probed, not read**: there is no rcon read for "how many chunks are loaded" in
vanilla 26.2 (`forceload query` only answers about forceloads), so it walks
`execute if loaded <chunk centre>` over a (sim+3)-radius square around each player --
one round trip each, ~0.17 s, which is why it runs once per cell. **CPU is read from
cgroup v2** (`/sys/fs/cgroup/cpu.stat usage_usec`), not from `docker stats`, because
`docker stats` gives an instantaneous percentage that cannot be turned into
core-seconds after the fact. `--concurrency` is deliberately absent: the whole point
is a throughput number, and two cells at once would measure each other.

### Launching hunt-villager-wave1

The villager hunt is a `branch.py` search like the enderman waves, and it launches on
the **master** jar: `detmc rng` now answers on `fabric/build/libs/detmc-0.1.0.jar`
(sha256 `a20b86a0…`) -- measured on `mc-run-trapverify-r0`, `reseed probe -> detmc
rng: masterSeed=2345202977127152470 baseMaster=2345202977127152470 reseeded=false
reseedArg=0 issued=1571 entityOrdinal=99 gametime=0`. The `--jar` from a feature branch
that the earlier sections describe as mandatory is no longer needed.

```bash
cd runner
mkdir -p runs/hunt-villager-wave1
setsid nohup .venv/bin/python branch.py search scenarios/enderman_villager_trap.yaml \
    --run-id hunt-villager-wave1 --replicas N --generations 183 --keep-top N/2 \
    --children 2 --segment-ticks 48000 --concurrency N \
    --jar fabric/build/libs/detmc-0.1.0.jar \
    > runs/hunt-villager-wave1/driver.log 2>&1 < /dev/null &
```

`N` is not a preference. This box took a **global OOM** with several launchers
starting at once, so `N = clamp((MemAvailable - 12 GB) / 1.6 GB, 1, 8)`
measured at launch, and the enforcement is `memgate.py` rather than the number:
`branch.py` narrows wave width through `memgate.budget_nodes` and `memgate.acquire`
blocks per node. A narrower wave costs wall clock, not nodes.

`runs/hunt-villager-wave1/launch-when-free.sh` is that arithmetic plus the one gate a
number cannot express: it waits for `runs/hunt-br-wave2/DONE` before measuring,
because wave 2 owns ~13 GB and 8 containers, and it logs the node count and the
MemAvailable it launched with to `runs/hunt-villager-wave1/launcher.log`.

### Verified on a live 26.2 server (2026-09-12, `mc-run-trapverify-r0`)

World seed `3795784043239602249`, the `village_plains` the server itself locates at
240,-320, anchor `240,68,-320`, one creative Carpet fake player, natural spawning on,
48,000 ticks (two in-game days) run first so the world was not freshly generated.

**The survey.** 15 beds, 8 doors and **32 doorway cells** in **0.4-0.6 s**, against
the 3 doors / 0 beds / 0 doorway cells the first, box-based version found. Cost:
11,835 generated lines for the villager-anchored chain, against 126,764 for a box wide
enough to hold this village.

**The detector, eight cases.** One doorway cell (240,73,-284), a villager parked
beside it, `check_every_ticks: 20`, the flags reset between cases. `predicate` is the
same condition chain run by hand as a counter, so a disagreement between it and `#hit`
would separate "the world does not qualify" from "the pack does not fire".

| # | world state | predicate | `#hit_doorway_blocked` | verdict |
|---|---|---|---|---|
| A | doorway cell empty | 0 | 0 | OK |
| B | **dirt in the doorway cell** | 3 | **1** @ gt 49,001 | OK |
| C | cleared, dirt **3 blocks** from the door | 0 | 0 | OK |
| D | **dandelion** in the doorway cell | 0 | 0 | OK |
| E | dirt again | 3 | **1** @ gt 49,181 | OK |
| F | cleared again | 0 | 0 | OK |
| G | **gravel** in the doorway cell | 3 | **1** @ gt 49,301 | OK |
| H | dirt in the cell, **every villager 80 blocks away** | 0 | 0 | OK |

C is the negative the scenario is built around: the same block 3 blocks from the door
is not a trap and does not fire. D is the one the `unless_block` list exists for -- a
dandelion is holdable and is not replaceable, so before that list it would have
counted as a wall. H isolates the villager half of the predicate from the block half,
on an unchanged world.

`Replica.check_unarmed` reported `no detector fires on the world as set up (clean
baseline)` on this village, so the 32 doorway cells and the enclosure test start at
zero on a world nobody built.

**The score metrics respond.** With one doorway cell holding dirt, `function
detmc:score` ran in **0.14 s** and returned `dcell_max 1, dcell_sum 1, dcell_n 1,
dnear_max 1, dnear_sum 1, vill_sides 0` -- rank 100,001,000,100.

### Two measurement traps this cost, both in the harness rather than the mod

**`tick step N` runs at 20 TPS and returns immediately.** The first three attempts at
case B read `#hit` 0 with the predicate at 1, which looked like a detector bug. It was
not: a canary counter inside the generated `detmc:tick` read **3** after a
`tick step 60`, i.e. 3 ticks had actually happened when the scoreboard was read.
`tick step` schedules the ticks and lets them run at the normal tick rate, so a
60-tick step is 3 s of wall clock. `run.py` already knows this -- `Replica.goto`
sprints to 60 short of the target and then polls `time query gametime` until it lands
-- and any hand test has to do the same. Bulk advancement is `tick sprint`, which runs
flat out; `tick step` is only for the last few ticks.

**`function` stops at 65,536 commands and says so only on the console.** The survey's
first pass is ~9,800 offsets per villager, so ten villagers is ~98,000 command
executions in one function, over 26.2's default `max_command_sequence_length`. Over
rcon `function detmc:survey` still answers `Running function detmc:survey`; the
console says `Command execution stopped due to limit (executed 65536 commands)`. The
visible symptom was 15 beds, 0 doors, 0 doorway cells -- the first pass finished and
every later pass was cut off. The scenario now sets
`max_command_sequence_length: 1000000`, and the survey completes with no warning.

### How many endermen a real village actually gets, and what it does to the rate

This is the number the whole scenario's rate rests on, so it is measured rather than
taken from the spawn-weight arithmetic. 14 samples 2,000 ticks apart across two more
in-game days on `mc-run-trapverify-r0` (one creative fake player,
`simulation-distance: 4`, `spawn_mobs true`, clear sky), counting inside the
128x64x128 village volume the scenario's own `endermen` metric uses:

| | all 14 samples | the 5 night samples (day time 13,000-23,000) |
|---|---|---|
| endermen in the village volume | min 1, max 3, **mean 1.14** | min 1, max 1, **mean 1.00** |
| endermen loaded anywhere | min 5, max 8, mean 5.50 | min 5, max 7, mean 5.40 |
| zombies in the volume | min 5, max 25, mean 7.79 | min 5, max 7, mean 5.60 |
| skeletons | min 1, max 4, mean 1.71 | min 1, max 2, mean 1.20 |
| creepers | min 2, max 5, mean 2.64 | min 2, max 2, mean 2.00 |
| spiders | min 0, max 3, mean 0.57 | 0 |
| iron golems | 1 throughout | 1 throughout |

**One enderman.** The built-arena hunts run 20, so per in-game year this arena offers
roughly **1/20th** of the enderman-time, and that is before the event is narrowed from
"anywhere in a 25x25 pen" to "one of 32 named doorway cells". Two honest consequences:

* the villager hunt is a **longer** search than the enderman-art waves at equal wall
  clock, not a shorter one, and the doorway shape is what buys it back: a single block
  in one of 32 cells against montecarlo's 3e-6/enderman-year for a villager inside a
  closing 1x1 cell;
* `endermen` is a ranked score component for a reason. A branch whose village happens
  to hold 3 endermen is worth keeping over one that holds 1, and nothing else in the
  scenario controls that.

**The monster cap is not the binding constraint -- the player count is.** Total
monsters in the volume ran ~10-15 against the theoretical MONSTER cap of 70 per player,
so one fake player at simulation-distance 4 is nowhere near saturating it. That points
the sizing lever at **more fake players** (the cap is per player, via
`LocalMobCapCalculator`) rather than at a larger simulation distance, which only
spreads the same cap. `bench_worldsize.py` is the measurement.

Throughput on this world: **164 ticks/s** for one replica at `segment_ticks: 24000`
(48,000 ticks in 292.7 s), against 639-828 for the built arenas. A real village with
ten villager brains, POI lookups, natural spawning and ~15 monsters is simply more
world per tick. One in-game year per lineage is therefore ~15 h of ticking, not 3.

### How many fake players to put round the village

One fake player at the village centre is the cheapest thing that makes spawning run
at all, and it is not the best thing. The MONSTER cap is **per player**
(`MobCategory.maxInstancesPerChunk` x `spawnableChunkCount` /
`NaturalSpawner.MAGIC_NUMBER`, and `LocalMobCapCalculator` caps per player on top),
so N players far enough apart is close to N times the eligible population -- while a
larger `simulation-distance` only spreads one player's cap over more chunks. The
measurement above found ~10-15 monsters in the village volume against a per-player cap
of 70, i.e. one player nowhere near saturating it, which is what makes the player
count the lever worth sweeping.

The players go on a **ring at radius 72** rather than in the middle, so each one's
128-block spawning radius covers the whole village (measured extent +-34 x, +-49 z
about its centre) while the players are far enough apart to bring separate cap budgets.
Two details that make the ring work:

* the ring only works **because of the forceload**. A chunk is a spawning chunk only if
  it is entity-ticking *and* within 128 blocks of a non-spectator player
  (`ChunkMap.collectSpawningChunks` needs `holder.getTickingChunk() != null`, which is
  the entity-ticking level). At `simulation-distance: 4` a player entity-ticks +-64
  blocks, so a ring at 72 does **not** entity-tick the village centre on its own.
  `setup.forceload` (+-64 about the anchor) is what does, and the ring supplies the
  within-128 half of the test.
* they spawn at `anchor_y+16` and fall. A fixed y cannot be right at four or eight
  different points of real terrain, and creative is invulnerable, so landing (or being
  buried) is harmless -- `playerIsCloseEnoughForSpawning` only excludes spectators and
  measures distance, it does not care what block the player is in.

One more 26.2 command change, found while measuring the above: **`/time query daytime`
no longer exists.** Clocks became a registry like game rules did, so the reply is

```
Can't find element 'minecraft:daytime' of type 'minecraft:timeline'
```

The day-time clock is `time query day`, which answers `Timeline minecraft:day is at
14676 tick(s)` and keeps counting past 24,000, so the phase is that value mod 24,000.
`time query gametime` is unchanged, and `time set 13000` (what `setup.time` issues) is
unchanged. A sampler that silently got `None` for the day phase is how this surfaced,
which is the same shape of failure as the truncated survey: the reply that matters went
to a place nobody was reading.

#### Measured: 1 player beats 4 and 8, on both axes

**The reasoning above is wrong, and the measurement says so.** More players did not put
more endermen near the village. It put fewer, and it cost throughput as well. One
single-node server at a time through `memgate`, 48,000 ticks (two full day/night
cycles) per config, sampled every 3,000 ticks, same world and same seed:

| fake players | endermen in the village, night mean | night max | all-sample mean | monsters in the village, mean | ticks/s | endermen x ticks/s |
|---|---|---|---|---|---|---|
| **1** (centre) | **1.00** | 2 | 0.94 | 9.25 | **173.8** | **173.8** |
| 4 (ring r=72) | 0.12 | 1 | 0.06 | 6.12 | 84.2 | 10.1 |
| 8 (ring r=72/51) | **0.00** | 0 | 0.00 | 6.00 | 71.4 | 0.0 |

Monotone in the wrong direction on every column, and 17x then infinitely worse on the
figure of merit. The survey found the same village every time (16 beds, 9 doors, 36
doorway cells), so this is the spawning system responding to the player count and not
three different worlds.

Hypothesis (unverified, the direction and size are measured but the mechanism is not
isolated): the ring **dilutes** rather than concentrates. `NaturalSpawner` sizes its
caps from `spawnableChunkCount` and distributes spawn attempts across every spawning
chunk, and a player at radius 72 with `simulation-distance: 4` entity-ticks its own
+-64 blocks -- which is mostly empty field, not village. So each added player adds
spawn candidates away from the village and the village's share of a roughly fixed
monster budget falls. That is consistent with the per-player cap of 70 never being the
binding constraint in the first place (measured above: ~10-15 monsters with one
player), which is exactly the assumption the ring was built on.

So `setup.fake_players` stays at **one creative player at the village centre**, and the
sweep's value is the negative result: on a natural map the player count is not a lever
for concentrating mobs on a target, and `simulation-distance` is not either. Raising
the eligible enderman population needs something that changes the spawning geometry
itself -- roofing the surroundings, or a deliberate spawn platform -- which is a
different scenario, not a parameter of this one.

### village_lightning, detectors verified (2026-09-12, `mc-run-lightverify-r0`)

Same world and village, difficulty `hard`, one node through `memgate`. Real strikes are
one per ~1,235 ticks while thundering, so each detector is driven by `summon` rather
than by waiting -- the question is whether the predicate fires on the right entity in
the right place, which a summoned bolt answers exactly as a natural one does.

| # | case | detector | fired | verdict |
|---|---|---|---|---|
| 1 | no bolt | `lightning_village` | 0 | OK |
| 2 | bolt **200 blocks outside** the village volume | `lightning_village` | 0 | OK |
| 3 | bolt at the village centre | `lightning_village` | **1** | OK |
| 4 | fire in the village, **no bolt seen yet** | `fire_village` | 0 | OK -- the `requires` gate is shut |
| 5 | the same fire, **after** a bolt in the village | `fire_village` | **1** | OK -- and open |
| 6 | plain creeper | `charged_creeper` | 0 | OK |
| 7 | creeper with `powered:1b` | `charged_creeper` | **1** | OK |
| 8 | no skeleton horse | `skeleton_trap` | 0 | OK |
| 9 | skeleton horse in the village | `skeleton_trap` | **1** | OK |
| 10 | "no villager killed" | `villager_killed` | **1** | test invalid, see below |
| 11 | one tagged villager killed | `villager_killed` | **1** | OK |

Cases 4 and 5 are the pair that matters most, because a gate that is always open and a
gate that is always shut both read as "no hit": the same fire block in the same cell
does not fire before a bolt has been seen in the village and does fire after one.
Setup reported `no detector fires on the world as set up (clean baseline)`, thresholds
`#th_villager_killed = 10` (the measured setup count) and 1 for the three
presence detectors, and `function detmc:score` ran in **0.18 s**.

**Case 10 is a bad test, not a bad detector.** On `hard`, the bolt summoned in case 3
and the charged creeper in case 7 killed villagers of their own accord: the score at the
end reads `villagers: 8` against a threshold of 10, and only one of those two deaths was
the deliberate kill in case 11. So by the time the negative was run the count was
already below the setup count and `count < threshold` was true for exactly the reason it
is supposed to be. That is also the scenario's own argument for `--no-stop-on-hit` in
miniature: on hard, `villager_killed` fires early and for reasons that have nothing to
do with lightning, so it must not be allowed to end a search.

### hunt-villager-wave1, launched 2026-09-12 18:40

```bash
setsid nohup .venv/bin/python branch.py search scenarios/enderman_villager_trap.yaml \
    --run-id hunt-villager-wave1 --replicas 3 --generations 183 --keep-top 1 \
    --children 2 --segment-ticks 48000 --concurrency 3 \
    --jar /root/apps/mc-determinism/fabric/build/libs/detmc-0.1.0.jar \
    >> runs/hunt-villager-wave1/driver.log 2>&1 < /dev/null &
```

**3 nodes**, `MemAvailable` 21,315 MB at launch, on the merged master jar
(sha256 `a20b86a0`) -- no worktree `--jar` needed any more, because `detmc rng`
answers on master now. `keep-top 1` with `children 2` still runs a 3-wide wave:
`spawn_children` gives the spare child to the best surviving parent, so the width
holds.

| | measured |
|---|---|
| generation 0, whole | 7 min 18 s (18:40:23 -> 18:47:41), boot 52.5 s, setup 66.4 s |
| generation 1, whole | **3 min 42 s** (18:47:41 -> 18:51:23), boot 68.1 s, **resume 1.4 s** |
| 48,000 ticks | ~150 s per node |
| `function detmc:score` | 0.18 s |
| checkpoint | 7.2 MB per node |

**Resume is 1.4 s, against 66.4 s for the fresh setup**, and that is the whole
difference between this scenario and the built arenas: there is nothing to build,
nothing to re-summon and no survey to redo, because the villagers, the mobs and the
survey markers all come back with the world on the merged jar. `0 mobs re-summoned,
0 blocks restored` in the log is the intended reading, not a failure.

Generation 0 ranked 3,000,000,000 / 2,000,000,001 / 2,000,000,000, i.e. the beam is
already on `vill_sides` 3 -- a villager with three of its four sides solid at foot and
head level, which real village geometry supplies for free. The two terms above it
(`dcell_sum`, blocked doorway cells) are still 0, which is the correct starting point.
