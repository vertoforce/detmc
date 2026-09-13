# Cross-machine determinism: 24000 ticks, two hosts, full match

**Verdict: no divergence.** Two detmc servers, one on host A and one on host B,
ran the same 24000-tick script and produced the same world.
The per-tick entity digest is equal at all 24001 gametimes, the four `entities/*.mca` files
are byte-equal at 330,476 bytes, and all 12 timestamp-blind region md5s match.

Measured 2026-09-12. Jar `detmc-0.1.0.jar`, sha256
`b2205b1b7844cc058bc873d13ed91c1f3d761dc6b1aec694e78d71d338eb409a` (md5 `2e4512cb`, 51,829
bytes, the phase 7 final jar at commit 61d213b). Both halves loaded that jar. The sha256 is
recorded in `out-hostA/params.txt` and `out-hostB/params.txt` and was verified on host B
after the copy.

A newer jar was built while this pair was running (at roughly gametime 15000) and copied
into `test/mods/`. It did not reach either half: both read from their own `mods/` copy,
taken before the run started. The result below is about the jar named above, not about the
newer one.

## Result

| Check | Tool | Result |
|---|---|---|
| per-tick entity digest, every gametime 0..24000 | `compare-dg.py` (same comparison as `test/analyze-probe.py`) | IDENTICAL, 24001 of 24001 |
| level RNG draws and state, every gametime | same | IDENTICAL, 24001 of 24001 |
| Pos, Motion, carriedBlockState, UUID at t24000 | `test/diff-det.sh` | MATCH, every section |
| windowed entity trace, gt 0:3 and 2190:2210 | `test/analyze-ew.py` | IDENTICAL over 25 gametimes |
| region md5, timestamp-blind | `test/region-md5.py` | MATCH, 12 of 12 files |
| `entities/*.mca` | `du -b`, `test/nbt-canon.py` | 330,476 B on both, 42 chunks canonically identical |
| `region/*.mca` | `test/nbt-canon.py` | 333 chunks on both, all identical, neither side holds an extra chunk |

Digest at gametime 24000, both halves: `n=239 h=ece955b3ba726416 levelRng=14377975,bc4507945148`.

Those are the same values the four same-host 24000-tick runs of 2026-09-12 morning produced
(`phase7/M24kA`, `M24kB`, `M24kA2`, `M24kB2`). Running `compare-dg.py out-hostB/dg-B.txt
phase7/M24kA/dg-A.txt` gives IDENTICAL at all 24001 gametimes, so six runs of this jar now
agree tick for tick, two of them on a different CPU model.

Raw output: `xm-compare.out`.

## The two halves

| | A half | B half |
|---|---|---|
| host | host A (KVM guest, CPU model passed through) | host B (KVM guest, default `kvm64` CPU model) |
| container | `mc-xm-A` | `mc-det-hostB` |
| directory | `test/cross-machine/` | `~/detmc-xm/` |
| artifacts | `out-hostA/` | `out-hostB/` (copied back) |

Harness: `xm-run.sh`, which calls `run-det-xm.sh`, a byte copy of `test/run-det.sh` with one
line changed (`ctr=${CTR:-mc-det-$label}`, so the container name can be set). Environment on
both halves: `TARGETS=24000 SPRINT_MARGIN=100000
DETMC_EXTRA_OPTS="-Ddetmc.traceIo=true -Ddetmc.traceEntityWindow=0:3,2190:2210"`.
`SPRINT_MARGIN` is larger than the target on purpose, so the run uses `tick step` only and
never `tick sprint`. Comparison is `xm-compare.sh`, which runs `diff-det.sh` exactly as
`run-pair.sh` does, then the digest check, then the region and NBT checks.

## What was held equal, and what was not

Equal by construction:

- image `itzg/minecraft-server:latest@sha256:c1a267d9ed6de3d1157859753a002aa35f8366d8db00463df3d6b07e86d2bf1d`, resolved to image id `sha256:17e24b4f9023...` on both hosts
- JVM: Temurin 25.0.4+7, OpenJDK 25.0.4 LTS, `/opt/java/openjdk/bin/java`, in both containers
- `SEED=12345`, `-Ddetmc.seed=1`, `-Ddetmc.freezeOnStart=true`, `-Dmax.bg.threads=1`, `MEMORY=4G`
- `server.properties`: 69 of 71 lines equal, including `level-seed=12345`, `view-distance=4`, `simulation-distance=4`, `spawn-monsters=true`. The two that differ are a wall-clock comment line the properties writer emits, and `management-server-secret`, which the server generates per install. Neither is a world-state input.

Different:

| | host A | host B |
|---|---|---|
| `lscpu` model name | `Intel(R) Xeon(R) CPU E5-2690 v4 @ 2.60GHz` | `Common KVM processor` |
| CPU family / model / stepping | 6 / 79 / 1 | 15 / 6 / 1 |
| vCPUs | 56 | 12 |
| CPU flags the guest sees | includes avx, avx2, fma, aes, bmi1, bmi2, popcnt, sse4_1, sse4_2, rdrand, rdseed | none of those. sse2 and pni are the top of the SIMD stack |
| host RAM | 73,605 MB | 14,626 MB |
| 1-minute load at launch | 24.43 | 0.37 |
| kernel | 6.1.0-52-amd64 | 6.1.0-51-amd64 |
| Docker | 29.6.1 | 29.8.0 |

The flag row is the one that matters for this test. Host A's guest is configured to pass the
physical CPU model through; host B's guest gets the hypervisor's generic `kvm64` model.
HotSpot picks intrinsics from those flags, so the two JVMs compiled different machine code
for the same bytecode and still produced the same world.

Host A also carried eight other Minecraft containers and a load average of 24 during the
run, while host B was idle. That did not move the result either.

## Evidence the wall clocks really did differ

Both servers freeze before tick 1 and tick while frozen, so the number of frozen ticks before
gametime 1 is a direct read of how much wall time each spent in startup and scripted setup.

| run | frozen ticks before gametime 1 |
|---|---|
| host A (this pair) | 934 |
| host B (this pair) | 834 |
| `phase7/M24kA` (same host, this morning) | 905 |

100 frozen ticks apart, identical world. That is the input phase 7 named as the cause of the
earlier divergences, and it no longer moves anything.

## Two differences that are not world state

**`clockTasks` in the `[detmc-dg]` line first differs at gametime 1** (A 2, B 1). It counts
server-queue tasks run in that tick, not world state, and it differs between two same-host
runs whose worlds are identical: `phase7/M24kA` and `phase7/M24kB` differ at gametime 4.
`compare-dg.py` reports it as advisory and ignores it in the exit code.

**The raw `### region md5` section differs on 7 of 8 lines.** Bytes 4096..8191 of every
region file are a per-chunk wall-clock timestamp, rewritten on every save, so a raw md5 can
never match across two runs taken seconds apart. `region-md5.py` blanks that table, and the
`### region md5 no timestamps` section matches on all 12 files.

## Timings

| | host A | host B |
|---|---|---|
| server `Done` | 16.259 s | 16.657 s |
| `Done` to artifacts written | 22 min 9 s | 22 min 8 s |
| tick rate during `tick step` | 20.0 tick/s | 20.0 tick/s (6244 vs 6243 ticks over the same 5-minute window) |
| container log | 49.2 MB, 423,574 lines | 48.3 MB, 418,947 lines |

Both halves ran at the same time, one per machine. 24000 ticks at the server tick rate is
20 minutes and that is the floor for a run of this length. `SPRINT_MARGIN=100000` gives up
`tick sprint` in exchange for never free-running against the wall clock.

`save-all flush` returned normally on both halves and the mod's `saveEverything done` line
arrived after one 0.5 s poll. The `i/o timeout` that every phase 7 result file carries did
not appear in either half.

## What this does not show

- **Not two physical machines.** Both guests run on one hypervisor host (dual E5-2690 v4),
  so the silicon is shared. The CPU model, flag set, core count and load the guests see are
  not shared. A test on genuinely different silicon has not been run.
- Same JVM build on both sides, because the image digest is pinned. A different JDK vendor or
  version is untested.
- One world, one seed (`12345`), one script. No players, no villagers, no raids.
- Replay from a save across machines is untested. `test/replay-pair.sh` has still never been run.

## Re-running

```bash
cd test/cross-machine
export REMOTE=user@hostB            # ssh target for the B half
export TARGETS=24000 SPRINT_MARGIN=100000
export DETMC_EXTRA_OPTS="-Ddetmc.traceIo=true -Ddetmc.traceEntityWindow=0:3,2190:2210"
./xm-run.sh A mcA mc-xm-A hostA > xm-local.log 2>&1 &
ssh "$REMOTE" 'cd ~/detmc-xm && TARGETS=24000 SPRINT_MARGIN=100000 \
  DETMC_EXTRA_OPTS="-Ddetmc.traceIo=true -Ddetmc.traceEntityWindow=0:3,2190:2210" \
  ./xm-run.sh B mcB mc-det-hostB hostB > xm-remote.log 2>&1'
rsync -a -e ssh "$REMOTE":~/detmc-xm/out-hostB/ ./out-hostB/
./xm-compare.sh
```

`README-remote.md` is the copy left on host B, with the gotchas that bite when the harness is
moved to a new host.
