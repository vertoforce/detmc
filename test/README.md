# Determinism tests

detmc claims that two servers started from the same seed play out identically,
tick for tick. These cases check that claim. One of them checks the opposite, so
that a passing run means something.

Everything runs from one script.

```bash
test/run-tests.sh --list
test/run-tests.sh --case early-1000
test/run-tests.sh --all --junit results.xml
```

The exit code is non-zero when any case fails.

`--compare-only` re-prints a verdict from the artifacts an earlier run left in
`test/.runs/<case>/`, starting no servers. Use it after changing what a case
expects, or to read the verdict again.

## The cases

| Case | Length | Expects | What it covers |
|---|---|---|---|
| `early-1000` | 1,000 ticks | match | Checkpoints at 0, 1, 5, 20, 100 and 300 ticks. Dates an early divergence to a few ticks. Per-tick digests are on. |
| `baseline-24k` | 24,000 ticks | match | One uninterrupted segment with natural spawning, mob AI and 60 summoned mobs. The longest run the mod is claimed to hold. |
| `replay-from-save` | 12,000 ticks | resume-match | Each server stops at tick 6,000, restarts from its own save and carries on. Checks that RNG state survives the restart. |
| `reseed` | 2,000 ticks | match plus diverge | Three replicas branch at tick 1,000. Two reseed to 777 and must stay together. The third reseeds to 778 and must leave. |
| `sweep` | 12,000 ticks | match | The phase-8 fix sweep. Brings its own world and its own driver. |
| `sweep-control` | 12,000 ticks | diverge | The `sweep` world with `-Ddetmc.syncRandom=false`. The control that gives `sweep` its meaning. |
| `vanilla-control` | 600 ticks | diverge | Same harness, same world, the four ordering fixes off. These two must not match. |

Measured 2026-09-12, 2 GB per server: `sweep` PASS, identical at all 12,001 gametimes
and canonically identical on disk, 25.4 min; `sweep-control` PASS, first divergence at
gametime 4, every saved region and entity file differs, 25.0 min. Eleven of the thirteen
phase-8 paths are walked; `StructurePlaceSettings` is dead code and `PlayerSpawnFinder`
is unreachable through a Carpet fake player. Details in `STATUS.md`, "sweep test".

`vanilla-control` is the case that gives the others their meaning. A harness
that cannot see a divergence would report PASS on every run, including a broken
mod. If `vanilla-control` ever reports that its two replicas matched, treat
every other PASS in that run as unproven.

## What a run does

For each replica in turn, never two at once:

1. Asks `runner/memgate.py` for 5,000 MB of host memory and waits until the host has it.
2. Starts one container from a compose file generated out of `test/compose-det.yml`.
3. Freezes ticks, sets the world up from `setup.commands`, then waits for chunk loading to settle.
4. Walks to each checkpoint by exact gametime, dumping every entity's position and motion with its UUID and type.
5. Flushes the save and hashes every saved chunk's decompressed payload.

Then it compares the replicas section by section and prints PASS or FAIL with
the first divergent gametime.

Chunk payloads, not file hashes. A hash of the `.mca` files fails on two
things that are not world state: the wall-clock timestamp table, and which
sector inside the file a chunk landed in, which follows the order chunks were
written in. One 1,000-tick pair failed that way with all 1,874 chunks
byte-identical. A chunk saved by only one replica is reported, not failed on: a
chunk unloaded while still `EMPTY` is never written at all.

Two more details are load bearing. The settle wait exists because chunk
loading keeps running while ticks are frozen, so a summon issued too early lands
at a different point in the entity sequence in each run. The exact-gametime walk
exists because `tick sprint` leaves the server running free until the freeze
lands: two runs taken at nominal tick 1 once held endermen with a vertical motion
of -0.447 and -0.652, a different number of gravity ticks. Both of those were the
harness inventing a divergence, not the mod.

## Memory

Each replica is one server with a 2 GB heap. Measured container RSS during a run
is 2.0 GiB, and the JVM costs about another gigabyte on top of the heap.

Replicas run one at a time, so a case holds one server, not two. The gate still
asks for a full pair's worth (5,000 MB) before each launch, so a case that cannot
afford its second replica waits at the start instead of failing twenty minutes in.

`runner/memgate.py` keeps 12 GB of the host unspent by default. On a 16 GB CI
runner that reserve can never be met, so the workflow sets
`DETMC_MEM_RESERVE_MB=2048`.

The gate exists because of a host OOM on 2026-09-12. Several launches with no
shared view of the host took the box down and the kernel killed a running
replica along with system daemons.

## Run times

Measured on this host, one replica at a time, with the search fleet also running.

| Case | Wall clock | Notes |
|---|---|---|
| `vanilla-control` | 5.1 min | 600 ticks, two replicas, per-tick digests on |
| `early-1000` | 6.6 min | 1,000 ticks, two replicas, per-tick digests on |
| `reseed` | 10.1 min | three replicas to tick 2,000 |
| `replay-from-save` | 8.7 min | four server lifetimes, two of them resumed |
| `baseline-24k` | 8.1 min | 24,000 ticks, two replicas, no tracing. The ad-hoc script it replaces took 22 min per replica with tracing on. |

Most of a short case is setup, not ticking. Two minutes of a six-minute case is
one replica generating its forceloaded chunks. The two forceloaded regions are
about 11 by 11 chunks each, chunk generation is serial under detmc, and the
settle wait holds for 15 seconds of a stable entity list before the run starts.

## Adding a case

Make a directory under `test/cases/`, write a `case.yaml`, and give it a
`setup.commands` if the world needs shaping. `test/cases/README.md` has every
field. The shortest useful case is a name, a kind, a tick count and an expected
outcome.

A case that needs a world the generic runner cannot build declares its own
compose file instead. The runner then hands it to `test/<name>-pair.sh` and takes
that script's exit code.

Start from `early-1000` when adding a `match` case. It is the cheapest one that
still exercises spawning and mob AI.

## Scripts these cases replaced

The old harness was 35 shell scripts with no entry point and no schema. Each one
below is now a case. The names are kept here so the runs recorded in `STATUS.md`
stay readable.

| Old script | Now |
|---|---|
| `run-pair.sh`, `run-det.sh`, `diff-det.sh` | `baseline-24k`, and the runner itself. `diff-det.sh`'s re-print is `--compare-only` |
| `pair-tick0.sh`, `run-tick0.sh` | `early-1000`, checkpoint 0 |
| `pair-early.sh`, `run-early.sh` | `early-1000` |
| `replay-pair.sh`, `run-det-resume.sh` | `replay-from-save` |
| `pair-reseed.sh`, `pair-reseed-narrow.sh`, `run-reseed.sh`, `diff-reseed.sh` | `reseed` |
| `phase7-run.sh`, `phase7b-chain.sh` through `phase7m-chain.sh` | one-off bisections, folded into the case list rather than kept |
| `tps-bench.sh`, `tps-bench3.sh`, `tps-rest.sh` | throughput measurement, recorded in `STATUS.md`, not a determinism check |
| `wait-for.sh`, `poll-rng.sh`, `long-runs.sh` | the runner waits on its own |
| `reseed-persist-check.sh`, `compose-reseed.yml` | `reseed`, whose replicas come from the generated compose |

`test/compose-det.yml`, `test/memgate.sh` and `runner/memgate.py` stayed. The
self-driven cases source `memgate.sh` for the same host budget. Everything else about a
case now lives in that case's directory.
