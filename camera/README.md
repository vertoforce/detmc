# camera — headless PNG renders of a Minecraft world

Renders a Minecraft save directory from an arbitrary camera (position, yaw/pitch or
look-at target, FOV) to a PNG, with no Minecraft client and no GUI. Built for
snapshotting the same view every N ticks and assembling time-lapses.

Renderer is **Chunky 2.5.0-SNAPSHOT.478 patched by us** (path tracer) in a local
Docker image. See `RESEARCH.md` for why, and for what changed in the 26.2 world
format. The patch adds enderman, zombie and villager models, which upstream does
not draw (`chunky-mobs.patch`, `RESEARCH-mobs.md`).

## Setup

```bash
./build-chunky.sh                                    # patched chunky-core jar -> dist/
docker build -t mc-camera-chunky:2.5.0-478-mobs1 .   # that jar + pinned deps, checksum-verified
./fetch-textures.sh 26.2                             # Mojang client jar -> cache/ (textures only)
```

`build-chunky.sh` clones chunky-dev/chunky at commit `527cb4a` (the commit the
published `2.5.0-SNAPSHOT.478.g527cb4a` nightly was built from), applies
`chunky-mobs.patch` and builds `:chunky:jar` inside a pinned `eclipse-temurin:17`
container. The checkout (`chunky-src/`) and the jar (`dist/`) are git-ignored;
the patch is the tracked artefact. A build takes about a minute.

The client jar is the texture source. It stays in `cache/` rather than being baked into
the image, since it is Mojang's binary. `fetch-textures.sh` verifies Mojang's own SHA-1.

## Single frame

```bash
./snap.sh <world_dir> <camera.json> <out.png>
./snap.sh testsrv/data/world cams/oblique.json out/spawn.png
```

`<world_dir>` is the save root — the directory holding `level.dat`, e.g. `.../data/world`.

### camera.json

```jsonc
{
  "x": 62, "y": 182, "z": -66,          // camera position
  "target": {"x": 96, "y": 150, "z": -32}, // look-at; overrides yaw/pitch
  "yaw": 0, "pitch": 90,                // Minecraft convention: yaw 0 = +Z, pitch +90 = down
  "fov": 40,
  "width": 1280, "height": 720,
  "samples": 32,                        // target SPP; quality vs time knob
  "chunk_radius": 5,                    // chunks loaded around the subject
  "projection": "PINHOLE",              // or PARALLEL / FISHEYE / PANORAMIC
  "dimension": "minecraft:overworld",
  "y_clip_min": -64, "y_clip_max": 320
}
```

Angles are Minecraft's, not Chunky's; `scene_gen.py` converts to Chunky's radians.

Env overrides: `CHUNKY_THREADS` (default `nproc`), `CHUNKY_MEM` (default 4G),
`CHUNKY_IMAGE`, `MC_TEXTURES`, `CHUNKY_NAME` (names the container, so a batch of
renders is greppable in `docker ps`; unset means Docker picks the name).

## Time-lapse

```bash
./timelapse.sh <container> <world_dir> <camera.json> <out.mp4> \
    --frames 40 --ticks 2000 --fps 8 --jobs 6
```

Per frame it runs `tick sprint <ticks>`, waits for the sprint to actually finish,
then `tick freeze` → `save-all flush` → copy → `tick unfreeze`. Afterwards it renders
every snapshot with the same camera (`--jobs` in parallel) and stitches them with a
pinned ffmpeg image.

`out/enderman-timelapse-mobs.mp4` is the demo, 35 endermen wandering the test arena
and rearranging its blocks over 80k ticks:

```bash
./timelapse.sh mc-camera-test testsrv/data/world cams/arena.json \
    out/enderman-timelapse-mobs.mp4 --frames 40 --ticks 2000 --fps 8 --jobs 6
```

Measured 2026-09-11, 56 cores, 1280x720 at 32 spp with `mc-camera-chunky:2.5.0-478-mobs1`:
436s of render wall for 40 frames, so **10.9s/frame** at `--jobs 6` (9 threads each);
518s end to end including the tick sprints and the copies. A single frame on its own
is 22s with all 56 threads, 37s with 9. (`out/enderman-timelapse.mp4` is the older
run of the same world, from before entity files were copied, so it has no mobs in
frame.)

### Copying a world out from under a running server

Safe, with three rules:

- Copy **after** `save-all flush`, and freeze the tick loop across the copy, so no
  chunk can be rewritten midway. `timelapse.sh` does both.
- Copy `level.dat`, `dimensions/**/region/*.mca` and `dimensions/**/entities/*.mca`
  only. Never copy `session.lock` — the live server holds it, and Chunky does not
  need it. Because we copy rather than open the live directory, the server's lock is
  never contended.
- `entities/` is what puts mobs in the frame: Chunky reads living entities from
  `entities/*.mca`, never from `region/*.mca`. A snapshot without it renders the
  world empty no matter which Chunky build you use.

Region files for a small build area are a few MB, so per-frame copies are cheap
(~3 MB/frame for the demo world).

## First find

The block-rearrangement hunt's first confirmed grief: a free-standing 3-stack of dirt
at x=89, z=-21, y=136-138, standing on the pen floor at y=135. Pen is walls at
x=83/109 and z=-45/-19. Lineage pointer:
`runner/runs/hunt-br-wave1/lineage.jsonl`, node `g18n0`, gametime 912001.

```bash
cp -r runner/runs/hunt-br-wave1/checkpoints/g18n0/world /tmp/g18n0-world
./snap.sh /tmp/g18n0-world cams/find-g18n0-pillar3.json out/find-g18n0-pillar3.png
./snap.sh /tmp/g18n0-world cams/find-g18n0-pen.json     out/find-g18n0-pen.png
```

`cams/find-g18n0-pillar3.json` is a low three-quarter view from 12 blocks northeast of
the stack (camera y=142, 5 blocks above the stack's top), which puts the stack, the
south wall and the west wall in one frame.
`cams/find-g18n0-pen.json` is the wide overhead of the whole pen. Measured 2026-09-12,
56 threads, 1280x720 at 32 spp: 21.0s and 26.9s. Renders land in `out/`, which is
git-ignored, so the cameras are the tracked artefact and the PNGs are reproducible
from them.

Copy the checkpoint out of `runs/` first. The hunt writes there while it runs, and the
copy is also what keeps `snap.sh` from touching a live run's files.

No mobs appear in either render. The checkpoint's `dimensions/*/entities/*.mca` are
0 bytes: the mod at that jar does not save entities into the checkpoint, so there is
nothing for Chunky to draw. The griefing is visible only as the blocks the endermen
left behind.

## Second find, as a time-lapse: the g48n5 pillar, with the endermen in frame

`runner/runs/hunt-br-wave1/lineage.jsonl` node `g48n5` hit `pillar3` at gametime
**2,350,060**, stack at (84,136,-20) / (84,137,-20) / (84,138,-20). The hunt's
checkpoint was taken 1,941 ticks later and no longer contains it, so the only way
to see this one is to replay the leg and photograph it on the way past.

47 frames, 2026-09-12:

```bash
cd ../runner
.venv/bin/python branch.py replay-leg runs/hunt-br-wave1/lineage.jsonl g48n5 \
    --run-id replay-frames-g48n5 --concurrency 1 --column 84,-20,135,139 \
    --jar ../fabric/build/libs/detmc-0.1.0.jar \
    --frames-at '2304001,2312001,2320001,2328001,2336001,2338000:2351650:350,2350060,2352001'

cd ../camera
export CHUNKY_THREADS=16 CHUNKY_MEM=2G
FR=../runner/runs/replay-frames-g48n5/frames
ls $FR | sort -n | xargs -P 3 -I{} bash -c \
  'CHUNKY_NAME=mc-cam-overhead-{} ./snap.sh '"$FR"'/{} \
     cams/find-g48n5-pen-overhead.json out/frames-overhead/frame-{}.png'
# then ffmpeg -framerate 8 -pattern_type glob -i 'frame-*.png' -c:v libx264 \
#      -pix_fmt yuv420p -crf 18, and palettegen/paletteuse for the GIF
```

`--frames-at` is the runner flag this needed (runner/README.md). It writes one
save root per gametime, `entities/` included, which is the difference between a
frame with 20 endermen in it and an empty pen.

| camera | what it is for |
|---|---|
| `cams/find-g48n5-pen-overhead.json` | 41 blocks above the pen centre, tilted 20 degrees off vertical, fov 60. The whole 25x25 pen, its wall and all 20 endermen |
| `cams/find-g48n5-pillar3.json` | 12.2 blocks from the stack, 4.5 above it, fov 65. The pillar reads as a pillar; 2 or 3 endermen are in frame at a time |

| output | |
|---|---|
| `out/find-g48n5-pillar3-overhead.mp4` | 47 frames, 8 fps, 1280x720, 11.0 MB |
| `out/find-g48n5-pillar3-overhead.gif` | the same at 800 px wide, 8 fps, palettegen/paletteuse, **10.6 MB** (960 px came out 15.8 MB, over the 15 MB budget; dropping the width rather than the frame rate keeps the motion) |
| `out/find-g48n5-pillar3-timelapse.mp4` | the low three-quarter camera, 47 frames, 8 fps, 1280x720, 5.4 MB (`ffprobe`: 47 frames, 8/1, 5.875 s) |
| `docs/media/` | both mp4s, the GIF and the two stills `find-g48n5-pillar3-2350060.png` (low angle) and `...-2350060-overhead.png`, because `out/` is git-ignored |

Measured on this box (56 cores, and busy: the hunt had 8 servers ticking
throughout), 1280x720 at 32 spp, 3 concurrent renders at 16 threads each:

| | measured |
|---|---|
| overhead, 47 frames, 3 concurrent at 16 threads | 1051 s, **22.4 s/frame effective** |
| low angle, 32 frames, 4 concurrent at 13 threads | 427 s, **13.3 s/frame effective** |
| one overhead frame alone, 8 threads | 51.9 s |
| one low-angle frame alone, 8 threads | 39.0 s |

Concurrency is a host budget, not a quality knob: each Chunky is `CHUNKY_MEM=2G`,
and the hunt's 8 servers hold ~1.5 GiB each, so check `MemAvailable` before
adding a render rather than after.

**The pillar is in exactly one frame.** Read straight out of each frame's region
file, (84,138,-20) is `air` in all 47 frames except `frames/2350060`, where it is
`dirt`; (84,136,-20) and (84,137,-20) are dirt in every frame. The third block
stood for at most 190 ticks, the gap to the next frame. At 8 fps that is one
eighth of a second of video, so `docs/media/find-g48n5-pillar3-2350060-overhead.png`
is the still.

The endermen are the thing the overhead camera is for, and they are visible:
20 of them alive at the hit tick, 12 carrying dirt (entity dump, `t2350060`),
and they are countable in the render. A 3-high stack seen from almost straight
down is foreshortened into a block, though, so the overhead does not show the
find. The low-angle frame does, and shows what it cost: at gametime 2,350,060
there is an enderman **standing on top of the pillar it just finished**.

**Which jar, and the honest caveat.** These frames are the **merged master jar**
(`fabric/build/libs/detmc-0.1.0.jar`, commit 44c6757, sha256 `a20b86a0...`), not
the hunt's `fec838e1`. The hunt jar writes `entities/*.mca` **0 bytes on every
save** (632 of 632 files across hunt-br-wave1 and hunt-br-wave2, measured), so no
jar but this one can put a mob in a frame, with or without `MOB_MARKERS`. The
merged jar replays the *event* exactly (same hit gametime, same pillar column,
all 10 score metrics) but **not** the whole world: the leaf decay around the pen
differs. runner/README.md, "The merged master jar replays the event but not the
whole world", has the full comparison.

## Known limits

- **Eleven entity ids are drawn** from `entities/*.mca`: upstream's armour stands,
  paintings, sheep, cows, pigs, chickens, mooshrooms and squid, plus our endermen,
  zombies and villagers. Every other mob is read and dropped. Set `MOB_MARKERS=1`
  to stand an armour stand wearing that mob's head at each remaining mob's
  position. See `RESEARCH-mobs.md`.
- Villagers are drawn with their biome-type robe but no profession or level badge,
  and no mob holds an item. Endermen do show `carriedBlockState`, as a half-size
  cube of that block's main texture rather than the block's real model.
- Unknown block IDs render as blue question marks rather than failing, so a future
  format bump shows up as visible artefacts.
- A fresh scene has no `.octree2` dump, so `snap.sh` passes `-f`. Without it Chunky
  treats the missing dump as fatal even though it just rebuilt the octree.

## Files

| Path | What |
|---|---|
| `Dockerfile` | Chunky image: our `dist/` jar plus deps pinned by SHA-256 |
| `libs.sha256` | the pinned dependency checksums |
| `build-chunky.sh` | clone + patch + build chunky-core in Docker -> `dist/` |
| `chunky-mobs.patch` | our enderman, zombie and villager models for Chunky |
| `snap.sh` | one world + one camera -> one PNG |
| `scene_gen.py` | camera.json -> Chunky scene description |
| `timelapse.sh` | tick / snapshot / render / stitch loop |
| `fetch-textures.sh` | Mojang client jar fetch, SHA-1 verified |
| `mob_markers.py` | mobs in `entities/*.mca` -> armour-stand markers in the scene |
| `blockdiff.py` | which chunks changed between two region dirs |
| `nbtdump.py` | minimal NBT reader used by the above |
| `cams/` | example cameras: oblique, top-down, arena, plus the g18n0 find pair |
| `testsrv/` | throwaway 26.2 server compose used to produce a demo world |
