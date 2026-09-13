#!/usr/bin/env python3
"""Turn living entities in a world save into Chunky scene entities.

Fallback for mobs our Chunky fork still does not draw. The fork draws eleven
entity ids from the world's `entities/*.mca` (armor_stand, painting, sheep, cow,
pig, chicken, mooshroom, squid, enderman, zombie, villager) plus block
entities and players. Everything else - endermen, zombies, villagers - is
dropped. Chunky's scene file, however, accepts a hand-written `entities` array,
and `armor_stand` there is a full model: pose, scale and a `gear.head` item that
PlayerEntity.addArmor draws for zombie/skeleton/creeper/piglin/player heads.

So we read the entity NBT ourselves and emit one armor-stand marker per mob:
a humanoid stand at the mob's exact saved position wearing that mob's head.
No server mod, no datapack, no write access to the world.
"""
import argparse, json, os, struct, sys, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nbtdump as N

# mob id -> (head item drawn by PlayerEntity.addArmor, stand scale)
# Scale is the mob's height over the armor stand's ~1.975 blocks, so a marker
# occupies roughly the space the mob does.
MARKERS = {
    "minecraft:enderman":        ("minecraft:zombie_head", 1.47),
    "minecraft:zombie":          ("minecraft:zombie_head", 1.0),
    "minecraft:husk":            ("minecraft:zombie_head", 1.0),
    "minecraft:drowned":         ("minecraft:zombie_head", 1.0),
    "minecraft:zombie_villager": ("minecraft:zombie_head", 1.0),
    "minecraft:skeleton":        ("minecraft:skeleton_skull", 1.0),
    "minecraft:stray":           ("minecraft:skeleton_skull", 1.0),
    "minecraft:wither_skeleton": ("minecraft:wither_skeleton_skull", 1.22),
    "minecraft:creeper":         ("minecraft:creeper_head", 0.87),
    "minecraft:piglin":          ("minecraft:piglin_head", 1.0),
    "minecraft:piglin_brute":    ("minecraft:piglin_head", 1.0),
    "minecraft:villager":        ("minecraft:player_head", 1.0),
    "minecraft:wandering_trader":("minecraft:player_head", 1.0),
    "minecraft:witch":           ("minecraft:player_head", 1.0),
    "minecraft:iron_golem":      ("minecraft:player_head", 1.48),
    "minecraft:player":          ("minecraft:player_head", 1.0),
}

# Chunky already draws these from the world; a marker would double them up.
CHUNKY_NATIVE = {
    "minecraft:armor_stand", "minecraft:painting", "minecraft:sheep",
    "minecraft:cow", "minecraft:pig", "minecraft:chicken",
    "minecraft:mooshroom", "minecraft:squid",
    # added by our fork, see chunky-mobs.patch
    "minecraft:enderman", "minecraft:zombie", "minecraft:villager",
}


def entities_dir(world_dir, dimension):
    ns, name = dimension.split(":", 1)
    p = os.path.join(world_dir, "dimensions", ns, name, "entities")
    return p if os.path.isdir(p) else os.path.join(world_dir, "entities")


def read_mobs(world_dir, dimension="minecraft:overworld"):
    """[(id, x, y, z)] for every entity in the dimension's entity regions."""
    d = entities_dir(world_dir, dimension)
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".mca"):
            continue
        blob = open(os.path.join(d, fn), "rb").read()
        if len(blob) < 4096:
            continue
        for i in range(1024):
            off = struct.unpack_from(">I", blob, i * 4)[0] >> 8
            if not off:
                continue
            base = off * 4096
            length = struct.unpack_from(">I", blob, base)[0]
            comp = blob[base + 4]
            raw = blob[base + 5:base + 4 + length]
            try:
                nbt = zlib.decompress(raw) if comp == 2 else raw
                tag = N.parse(nbt)
            except Exception:
                continue  # a torn chunk is not worth failing the frame over
            for e in tag.get("Entities") or []:
                pos = e.get("Pos")
                if isinstance(pos, list) and len(pos) == 3:
                    out.append((e.get("id", "?"), pos[0], pos[1], pos[2]))
    return out


def marker(eid, x, y, z, visible_stand=True):
    head, scale = MARKERS[eid]
    return {
        "kind": "armor_stand",
        "position": {"x": x, "y": y, "z": z},
        "scale": scale,
        "headScale": 1.0,
        "showArms": True,
        "invisible": not visible_stand,
        "noBasePlate": True,
        "gear": {"head": {"id": head}},
        "pose": {},
    }


def build_markers(world_dir, dimension, chunk_list=None, visible_stand=True):
    keep = {tuple(c) for c in chunk_list} if chunk_list else None
    seen, out = {}, []
    for eid, x, y, z in read_mobs(world_dir, dimension):
        if eid in CHUNKY_NATIVE or eid not in MARKERS:
            seen[eid] = seen.get(eid, 0)
            continue
        if keep is not None and (int(x) >> 4, int(z) >> 4) not in keep:
            continue
        out.append(marker(eid, x, y, z, visible_stand))
        seen[eid] = seen.get(eid, 0) + 1
    return out, seen


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("world_dir")
    ap.add_argument("--dimension", default=None,
                    help="defaults to the scene's dimension, else minecraft:overworld")
    ap.add_argument("--patch-scene", help="Chunky scene .json to add the markers to")
    ap.add_argument("--out", help="write the marker array here instead")
    ap.add_argument("--invisible-stand", action="store_true",
                    help="draw only the head, not the armor stand body")
    a = ap.parse_args()

    chunks, dim = None, a.dimension
    if a.patch_scene:
        scene = json.load(open(a.patch_scene))
        chunks = scene.get("chunkList")
        dim = dim or (scene.get("world") or {}).get("dimension")
    dim = dim or "minecraft:overworld"

    markers, counts = build_markers(a.world_dir, dim, chunks,
                                    visible_stand=not a.invisible_stand)
    if a.patch_scene:
        scene["entities"] = list(scene.get("entities") or []) + markers
        json.dump(scene, open(a.patch_scene, "w"), indent=2)
    elif a.out:
        json.dump(markers, open(a.out, "w"), indent=2)
    else:
        json.dump(markers, sys.stdout, indent=2)
    for k in sorted(counts):
        print(f"{counts[k]:5d}  {k}", file=sys.stderr)
    print(f"{len(markers)} markers", file=sys.stderr)
