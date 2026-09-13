# Case format

A case is one directory under `test/cases/<name>/`. It holds a `case.yaml`, and
optionally a `setup.commands` file and a `setup.mcfunction`. `test/run-tests.sh`
reads the directory, drives the servers, compares them and prints PASS or FAIL.

Nothing else in the directory is read, so notes and expected-output samples can
live beside the case.

```
test/cases/baseline-24k/
  case.yaml           required
  setup.commands      optional: rcon lines run while ticks are frozen, before the run
  setup.mcfunction    optional: installed as detmc:setup, called after setup.commands
```

## case.yaml

```yaml
name: baseline-24k          # must equal the directory name
summary: >                  # one or two lines, printed by --list
  Natural spawning and mob AI over 24,000 ticks. Two servers, same seed.
kind: pair                  # pair | resume-pair | external
expect: match               # match | diverge | resume-match
ticks: 24000                # total gametime advanced past the setup point
checkpoints: [24000]        # optional; offsets from the base gametime, ascending,
                            # last one must equal `ticks`. Default [ticks].
                            # Each checkpoint is a full entity dump, and a frozen
                            # barrier between segments, which can hide drift.

world:                      # optional, all fields optional
  seed: 12345               # Minecraft world seed
  spawn_mobs: true          # gamerule spawn_mobs
  view_distance: 4
  simulation_distance: 4
  memory: 2G                # heap per server; the memory gate reads this

jar:                        # optional
  seed: 1                   # -Ddetmc.seed
  flags: "-Ddetmc.syncChunks=true"   # extra -D flags, appended for every replica
  trace_io: false           # -Ddetmc.traceIo=true: per-tick digest lines.
                            # Needed for "first divergent gametime" to be exact.
                            # Costs roughly 15% TPS and megabytes of log.
  mod: true                 # false runs a plain Fabric server with no detmc jar

replicas:                   # optional; default is A and B, identical
  A: {}
  B:
    jar: {seed: 2}          # per-replica override of any `jar` or `world` key
    jvm_add: "-Ddetmc.syncRandom=false"
    reseed: 777             # run `/detmc reseed 777` at `reseed_at`
  C:
    reseed: 778

reseed_at: 6000             # optional: gametime offset for the per-replica reseed
resume_at: 12000            # resume-pair only: stop each server here, restart it
                            # from its own save, carry on to `ticks`

compare:                    # optional; default is the first two replicas with the
  - {a: A, b: B, expect: match}      # top-level `expect`
  - {a: A, b: C, expect: diverge}

timeout_s: 5400             # optional wall-clock cap per replica, default 5400
```

### kind

| kind | What the runner does |
|---|---|
| `pair` | Starts each replica in turn from an empty world, runs to `ticks`, compares. |
| `resume-pair` | Same, but stops each replica at `resume_at`, restarts it from its own save and carries on to `ticks`. Compares the resumed halves. |
| `external` | Runs `script:` (a path relative to `test/`) and takes its exit code, 0 for PASS. For cases whose driving does not fit the fields above. |

### expect

| expect | Passes when |
|---|---|
| `match` | Every compared pair is identical at every checkpoint. |
| `diverge` | At least one compared pair differs. A control: if this passes, the harness can see a divergence, so a `match` PASS means something. |
| `resume-match` | `kind: resume-pair`, and the resumed halves are identical. |

Per-pair `expect` under `compare:` overrides the top-level value for that pair,
which is how one case can assert both "these two agree" and "that one does not".

## setup.commands

One rcon command per line, `#` comments and blank lines skipped. Run while ticks
are frozen, after the world has settled and before any tick is advanced. The
runner always runs its own preamble first: `tick freeze`, `gamerule
advance_time false`, `time set midnight`, `weather clear`, `gamerule spawn_mobs
<world.spawn_mobs>`, then waits for chunk loading to settle.

`{{seed}}` and `{{label}}` are substituted, so a case can summon per-replica.

## setup.mcfunction

Installed as `detmc:setup` in a generated datapack and called with `/function
detmc:setup` right after `setup.commands`. Use it when the setup needs more than
a flat command list, for instance `execute` chains over a scoreboard.

## What gets compared

Per replica the runner captures, at every checkpoint:

- every entity's `Pos`, `Motion`, `UUID` and `id`
- enderman `carriedBlockState`
- the gametime the dump was taken at, which is exact, not approximate

and at the end of the run:

- region and entity `.mca` files, hashed with the save timestamps blanked
- the per-tick digest stream (`trace_io: true` only), which dates a divergence
  to a single gametime rather than to a checkpoint

The mca timestamp field is wall-clock, so raw file hashes never match between two
runs taken minutes apart. The runner ignores it.

## Running

```bash
test/run-tests.sh --list
test/run-tests.sh --case baseline-24k
test/run-tests.sh --all --junit results.xml
test/run-tests.sh --case baseline-24k --compare-only   # re-verdict, no servers
```

Exit code is non-zero if any case FAILs.

## Cost

Each replica is one server at `world.memory` heap plus about 1 GB of JVM
overhead. Replicas run one at a time, so a two-replica case holds one server,
but the runner asks `runner/memgate.py` for 5,000 MB before every launch so a
pair's worth of headroom exists on the host before anything starts. See
`test/README.md` for measured run times.

## Self-driven cases

A case that needs a world the generic runner cannot build declares its own
compose file:

```yaml
name: sweep
compose: compose-sweep.yml
```

`compose:` (or `script:`, or a `test/<name>-pair.sh` next to the runner) makes the
case external without spelling out `kind`. The runner then calls `test/<name>-pair.sh
<name>`, or the `script:` given, and takes its exit code: 0 is PASS. Everything the
case needs beyond that, the world, the datapack, the probes, belongs to that script.

A self-driven case still fills in every field above that applies, so `--list` and the
JUnit report read the same for it as for a generic case, and it puts anything only its
own driver understands under `self:`. `sweep` is the worked example:

```yaml
world:
  level_type: minecraft:flat   # self-driven only: the generic runner has no such field
self:
  player: true                 # a Carpet fake player, so the join and player-save paths run
  pack: pack                   # a datapack directory copied into world/datapacks before boot
  probes: probes.txt           # per-path evidence commands, one per line, run at each checkpoint
inherit: sweep                 # take pack/ and probes.txt from another case
```

Three things keep `sweep` out of the generic runner, and all three would have to
become schema fields and runner code before it could move: a flat level type, a
second mod jar in `mods/` (Carpet), and a datapack with a `minecraft:tick` function
tag rather than a single `setup` function.
