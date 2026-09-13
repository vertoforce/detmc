# Headless camera: renderer comparison

Goal: world save dir + arbitrary camera (pos, yaw/pitch, FOV) -> PNG, no MC client, in Docker.

## Verified against our own world

`test/dataA/world` is `DataVersion 4903`, `Version.Name 26.2`. Two things matter:

- **Chunk NBT is unchanged** from the 1.18+ scheme: `sections[].block_states.{palette,data}`,
  `xPos/yPos/zPos`, `Status: minecraft:full`. Any renderer that reads 1.21 chunks can read ours.
- **The directory layout moved.** Overworld regions are now
  `world/dimensions/minecraft/overworld/region/*.mca`, not `world/region/`. `level.dat` also changed:
  spawn is a `spawn{pos:[I;...],yaw,pitch,dimension}` compound, not `SpawnX/Y/Z`. Tools that hardcode
  the old layout see an empty world rather than erroring.

## Candidates

| Renderer | 26.2 chunks | Arbitrary camera | Docker | Verdict |
|---|---|---|---|---|
| **Chunky 2.5.0-SNAPSHOT** | Yes | Yes, pos+yaw/pitch/roll+FOV | Build our own | **Chosen** |
| uNmINeD-cli 0.20.8 | Yes (best coverage) | No, top-down/fixed iso; iso is GUI-only | Wrappers exist | Rejected |
| Overviewer (GregoryAM fork) | No, stops at 1.21.3 | No, fixed isometric | - | Rejected |
| BlueMap 5.24 | Yes | No, emits web tiles not stills | Yes | Rejected |
| MapCrafter | No, ~1.16 | No | - | Rejected |
| Headless client (headlessmc/Xvfb) | Yes | Yes | Painful | Fallback only |

## Why Chunky

Only candidate with a real camera. Its `World.java` already resolves `dimensions/<ns>/<name>/`
(and `players/data`), so our layout needs **no shim**. 26.2 block support landed in
chunky-dev/chunky#1889.

Use the **snapshot channel, not the tagged release**: 2.4.6 (2024-01) caps at MC 1.20.4. We pin
`chunky-core-2.5.0-SNAPSHOT.478.g527cb4a` (2026-09-05) and verify every jar against the sha256 in
upstream's manifest, so the floating channel cannot change what we build.

Unknown blocks degrade to blue question marks rather than failing.
