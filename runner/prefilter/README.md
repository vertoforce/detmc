# Seed prefilter (detmc runner)

Finds world seeds whose **world spawn** satisfies a scenario's
`world.requirements` block: an allowed spawn biome plus structures within a
maximum distance of spawn. Two backends: fast (cubiomes, C, in Docker) and
authoritative-but-slow (a throwaway vanilla server driven over rcon).

Files:

| Path | What |
|---|---|
| `../prefilter.py` | CLI that `run.py` calls |
| `Dockerfile` | builds `mc-cubiomes:e61f905` (cubiomes pinned by commit) |
| `seedscan.c` | the C scanner (~300 lines) |
| `verify/docker-compose.yml` | throwaway `mc-run-prefilter-locate` server |
| `example-scenario.yaml` | the YAML block the prefilter reads |

## Cubiomes does not support 26.2

- Repo: <https://github.com/Cubitect/cubiomes>
- Pinned commit: **`e61f90580cbdd883214a8054670dacae655e59c0`** (2024-11-10) — the
  newest commit on `master` as of 2026-09-11; upstream has had no commits since.
- Newest version in its `MCVersion` enum (`biomes.h`):

  ```c
  MC_1_21_WD, // Winter Drop, version TBA
  MC_1_21 = MC_1_21_WD,
  MC_NEWEST = MC_1_21,
  ```

  `mc2str(MC_NEWEST)` prints **`1.21 WD`**.

**Mismatch:** the newest worldgen cubiomes implements is `1.21 WD` (the 1.21.4
Winter Drop). It has no 26.2 support, and `str2mc("26.2")` returns 0. `seedscan`
therefore falls back to `MC_NEWEST`, prints a warning, and every result carries
`"version_mismatch": true`. Any seed the cubiomes backend produces is a
**candidate**; confirm it on a real 26.2 server before a scenario depends on it.

## Build

```bash
docker build -t mc-cubiomes:e61f905 runner/prefilter/
```

Pinned base image `debian:bookworm-slim@sha256:88200866…4171`; cubiomes is
`git checkout`ed at the commit above (never a branch). `prefilter.py` reads the
commit out of `ARG CUBIOMES_COMMIT` in the Dockerfile, derives the image tag
`mc-cubiomes:<short-sha>`, and builds it on demand.

## CLI

```bash
runner/.venv/bin/python runner/prefilter.py \
    --scenario runner/prefilter/example-scenario.yaml \
    --count 3 [--start-seed 1] [--max-seeds 100000] \
    [--json out.json] [--method auto|cubiomes|locate]
```

Scenario block read (everything else in the YAML is ignored):

```yaml
mc_version: "26.2"            # also accepts minecraft.version / world.version
world:
  requirements:
    spawn_biome: [minecraft:plains, minecraft:sunflower_plains]
    spawn_biome_radius: 32
    structures:
      - {id: minecraft:village_plains, within: 200}
```

One JSON object on stdout (and to `--json`); exit 1 if fewer than `--count`
seeds were found, exit 3 if the cubiomes image cannot be built:

```json
{"method":"cubiomes","mc_version":"1.21 WD","version_mismatch":true,"checked":89,
 "seeds":[{"world_seed":12,"spawn":[-16,-48],"spawn_biome":"minecraft:plains",
           "structures":{"minecraft:village_plains":[160,0,32]}}]}
```

Methods:

- `cubiomes` — runs `docker run --rm mc-cubiomes:<sha> …`. ~0.5 ms/seed
  (measured: 89 seeds checked in 0.9 s wall including container start).
- `locate` — boots **one** throwaway `mc-run-prefilter-locate` server per
  candidate seed (`verify/docker-compose.yml`, relative `./data` bind mount,
  wiped per seed), `tick freeze`, then `locate structure` / `locate biome` over
  `docker exec … rcon-cli`. Authoritative for 26.2 but slow: one server boot and
  one world generation per seed, and because `./data` is wiped the 26.2 server
  jar is re-downloaded every time. Measured on this host: 18 s from the start of
  the readiness poll to rcon answering; a full one-seed run finished in under
  three minutes wall (not timed more precisely). This is the fallback, not the
  default.
- `auto` — `cubiomes` if the image exists or can be built, else `locate`.

`seedscan` itself is usable directly:

```bash
docker run --rm mc-cubiomes:e61f905 --mc 26.2 --start 1 --count 5000 --accept 3 \
  --spawn-biome minecraft:plains,minecraft:sunflower_plains \
  --struct minecraft:village_plains:200
```

## Verification on a real 26.2 server

Seed **12**, `itzg/minecraft-server:java25@sha256:c1a267d9…2bf1d`, `VERSION=26.2`,
world frozen with `tick freeze`. Predicted by cubiomes at `1.21 WD`:

| Quantity | Cubiomes predicted | 26.2 server reported | Error |
|---|---|---|---|
| World spawn | `x=-16, z=-48` | `setworldspawn` → `-16, 71, -48` | **0 blocks** |
| Spawn biome | `minecraft:plains` | `locate biome minecraft:plains` → `[-16, 71, -48]` (0 blocks away) | exact |
| `village_plains` | `x=160, z=32` (193.3 blocks from spawn) | `execute positioned 160.0 70.0 32.0 run locate structure minecraft:village_plains` → `[160, ~, 32]` (0 blocks away) | **0 blocks** |

The `locate` backend, run end-to-end against the same 26.2 server
(`--method locate --start-seed 12 --max-seeds 1`), independently produced
`{"world_seed": 12, "spawn": [-16, -48], "structures":
{"minecraft:village_plains": [160, 0, 32]}}` — identical to the cubiomes
prediction.

So for this seed, 1.21-WD worldgen and 26.2 worldgen agree exactly on spawn,
spawn biome and plains-village placement. One seed is not a proof that they
always agree — treat `version_mismatch: true` as real risk.

### `/locate structure` is ring-ordered, not nearest

From spawn, `locate structure minecraft:village_plains` reported
`[-224, ~, -144] (229 blocks away)` — farther than the village at `[160, 32]`
(193 blocks) that actually exists. Vanilla walks the structure placement grid
(villages: 34-chunk = 544-block spacing) ring by ring and returns the first
viable hit; spawn `(-16,-48)` sits in placement region `(-1,-1)` and the closer
village is in region `(0,0)`. Consequences:

- Never treat `/locate`'s answer as "the nearest" when comparing against a
  predictor. Re-probe with `execute positioned <predicted> run locate …`.
- The `locate` backend therefore probes 9 points (spawn plus `±within` on both
  axes) and keeps the closest hit. It is still a lower bound on the true
  nearest, so it can reject a seed the cubiomes backend accepts.

## Limits

- **No 26.2 worldgen.** See above. The cubiomes backend is a *filter*, not an
  oracle.
- **No Y coordinate.** Cubiomes has no surface-height model, so structure `y` is
  always `0` in the output (vanilla prints `~` for the same reason). The
  `locate` backend fills in the real `y` only when vanilla prints one.
- `getSpawn()` is cubiomes' approximation of the spawn algorithm and is
  documented upstream as "slow, and may be inaccurate because the world spawn
  depends on grass blocks". It dominates runtime.
- `spawn_biome_radius` semantics (our choice): every point on a 16-block grid
  inside that radius of spawn must also be an allowed biome. With `locate` the
  same field is reused as the tolerance for the `locate biome` distance.
- Structure ids are mapped to cubiomes `StructureType`s by a small table in
  `seedscan.c` (villages carry their variant biome, e.g.
  `minecraft:village_plains` → `Village` + `plains`). Unlisted ids are an error;
  add them to `STRUCT_IDS`.
- `locate_world_spawn()` uses `setworldspawn` with no arguments. On the
  throwaway world that is a no-op read, but it does reset the spawn angle to
  `[0.0, 0.0]` — do not point it at a world you care about.
- Only `minecraft:overworld` structures/biomes are supported.
