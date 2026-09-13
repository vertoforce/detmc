# Getting mobs into headless renders

Endermen, zombies and villagers now render as their real models, in one pass, from
a patched Chunky. `chunky-mobs.patch` adds `EndermanEntity`, `ZombieEntity` and
`VillagerEntity` plus three dispatch lines. Everything else stays on the
armour-stand marker fallback (`MOB_MARKERS=1`).

Result: `out/mobs-arena-real.png` (1280x720, 32 spp, 35 endermen).

## What upstream Chunky does

Read from `chunky-core-2.5.0-SNAPSHOT.478` bytecode and from the source at commit
`527cb4a`, not from docs:

- It **does** read `entities/*.mca`. `Chunk` handles `ENTITIES_POST_20W45A`, and
  `ChunkData.getEntities()` feeds `SceneEntities.loadEntitiesInChunk`.
- It instantiates eight ids: `armor_stand`, `painting`, `sheep`, `cow`, `pig`,
  `chicken`, `mooshroom`, `squid`, plus players and block entities. Every other
  entity is parsed and thrown away. (The README's old "item frames" claim was wrong.)
- `PlayerEntity.addArmor` draws head-slot items: `zombie_head`, `creeper_head`,
  `skeleton_skull`, `wither_skeleton_skull`, `piglin_head`, `player_head`,
  `dragon_head`, `carved_pumpkin`. `ArmorStand` delegates to it.
- The scene file's `entities` array is a supported input, parsed by
  `SceneEntities.importJsonData` via `Entity.entitiesFromJson`.
- An entity class is loaded unless a preference says otherwise:
  `EntityLoadingPreferences.shouldLoadClass` falls back to `loadOtherEntities`,
  which defaults to true. New classes need no registration to be drawn.

## Writing an entity class

Copy `SheepEntity`: static `BoxModelBuilder` quads per part, a `pose` JsonObject,
and `primitives()` chaining a `Transform` per part into the world transform.
Four conversions matter, all confirmed against `SheepEntity` and `PlayerEntity`:

| Vanilla model | Chunky |
|---|---|
| y down, origin 24px above the feet | y up, origin at the feet: `y = (24 - y_mc) / 16` |
| `texOffs(u, v)` | `.atUVCoordinates(u, v)` with `forTextureSize(w, h)` and `.flipX()` |
| `addBox(x, y, z, w, h, d)` | box sized `w`, `h`, `d` in pixels, drawn about its own pivot |
| right limb at `-x` | right limb at `+x`: Chunky's model x is mirrored |
| `xRot = a` | `.rotateX(-a)`: `Matrix3.rotX` is right-handed, so the y flip flips the sign |

Textures load by annotating the field in `Texture.java`:
`@TexturePath("assets/minecraft/textures/entity/enderman/enderman")`. No loader
entry is needed; `TexturePackLoader` registers annotated fields by reflection.

## Model notes, 26.2 textures

- Enderman: 64x32 texture, head and body at texOffs (0,0) and (32,16), 30px limbs
  at (56,0). The eyes are a second head box in `enderman_eyes.png`, which has six
  opaque pixels, drawn with emittance 1 and grown 0.001 blocks against z-fighting.
- Zombie: the plain `HumanoidModel` mesh on the 64x64 zombie texture. Arms are
  posed forward, as vanilla does for a zombie. No hat layer.
- Villager: **the base `villager.png` no longer contains the robe.** Measured on
  the 26.2 client jar: the texOffs (0,38) robe region is fully transparent and the
  torso region is plain grey. The robe lives in `entity/villager/type/<biome>.png`,
  so the villager is drawn twice, once in `villager.png` and once in the biome
  texture from `VillagerData.type` grown 0.002 blocks. Profession and level badges
  are not drawn.
- Enderman `carriedBlockState` is resolved through `BlockSpec.blockProviders` and
  drawn as a half-size cube using that block's material. It is not the block's own
  model, so a carried stair or slab looks like a full cube.

## Measurements

All on the demo arena world, `cams/arena.json`, 1280x720, 35 endermen.

| Build | spp | Passes | Seconds per frame |
|---|---|---|---|
| marker prototype | 64 | 2 | 36 |
| real models | 32 | 1 | 21.9 |
| real models | 64 | 1 | 30.2 |

Those three were taken on a quiet host. A repeat of the 32 spp render while the
host was at load average 104 on 56 cores took 58 to 63 s, so treat per-frame time
as load-dependent rather than fixed.

Pixel check against the no-mob render of the same camera, threshold 64 of 255:
the real models differ in 0.61 % of pixels, the armour-stand markers in 0.53 %,
and the two mob renders differ from each other in 0.60 %. Same places, bigger
figures, which is what swapping a 1.47-scale stand for a 2.9-block enderman
should do.

## The old marker route, still available as a fallback

`mob_markers.py` reads `entities/*.mca` and emits one `armor_stand` per mob it
knows, scaled to its height, wearing its head. Run `MOB_MARKERS=1 ./snap.sh`. It
skips the eleven ids Chunky now draws itself.

The trap, measured: injected entities are dropped whenever Chunky rebuilds the
octree with `-f`. Same camera, same octree, marker versus no marker changed
62,084 pixels; the `-f` version changed 224, which is path-tracer noise. So the
marker path renders twice, pass 1 at 1 spp purely to build the `.octree2` dump.
The patched build needs none of that: the mobs come out of the region files.

## Options not taken

| # | Path | Effort | Fidelity |
|---|---|---|---|
| 4 | Server writes marker armour stands into the world | 1-2 d | same as markers, but pollutes the save |
| 3 | Headless Fabric client under Xvfb | 5-9 d | exact, with animation |
| 2 | Mineways/jmc2obj + Blender + MCprep rigs | 6-10 d | high, rest pose only |

Option 3 has no usable timed-screenshot mod (Multishot died at 1.12, ReplayMod's
headless flags are untested 2016 code, Flashback has no CLI). Option 2 needs
Mineways under Wine, the only exporter reaching 26.2 blocks.

## Next, if more mobs are wanted

Each new mob is one class of the same shape plus one dispatch line, roughly an
hour once the model's box list is known. Creepers, skeletons and spiders are the
obvious next three. The patch is written to stay upstreamable: no changes outside
the three new files, `Entity.entitiesFromJson`, `SceneEntities.loadEntitiesInChunk`
and the texture table.
