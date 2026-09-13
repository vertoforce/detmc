#!/usr/bin/env python3
"""detmc scenario runner.

One declarative YAML scenario -> N replica servers (same world seed, different
-Ddetmc.seed), each set up by rcon in a fixed order while ticks are frozen, then
advanced in exact segments with a generated detector datapack watching for the
event.  Hits go to results.jsonl.

Run with the venv:  .venv/bin/python run.py scenarios/zombie_siege.yaml
"""
import argparse
import gzip
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

import memgate
import patterns

RUNNER = Path(__file__).resolve().parent
PROJECT = RUNNER.parent

# itzg/minecraft-server, same digest test/compose-det.yml pins.  The tag is the
# image's own org.opencontainers.image.version label; the digest is what docker
# enforces.
IMAGE = ("itzg/minecraft-server:java25@sha256:"
         "c1a267d9ed6de3d1157859753a002aa35f8366d8db00463df3d6b07e86d2bf1d")  # pinned 2026-09-11
# server-26.2.jar version.json -> pack_version.data_major
PACK_FORMAT = 107
FABRIC_LOADER = "0.19.5"
MC_VERSION = "26.2"
CARPET_JAR = RUNNER / "libs" / "fabric-carpet-26.2+v260616.jar"

# Seeds that look like seeds.  A world seed of 12345 and `-Ddetmc.seed` of 1..8 are
# perfectly valid inputs, and they are also indistinguishable from a placeholder, so
# a reader cannot tell a chosen seed from a forgotten one.  Both are now drawn from
# a fixed-master generator instead, which keeps them reproducible: the master value
# and the function are recorded in the manifest, so any seed in any run can be
# re-derived without the run.
SEED_MASTER = 0x6d65742d64657403        # "met-det\x03", the fixed master
UINT64 = (1 << 64) - 1
INT64_MIN = -(1 << 63)


def splitmix64(x):
    """One step of SplitMix64 (Steele/Lea/Flood 2014), the generator every seed in
    this directory is derived with.  Deterministic, cheap, and good enough that
    consecutive inputs give unrelated outputs -- which is the whole point, since the
    inputs here ARE consecutive (a replica index)."""
    x = (x + 0x9E3779B97F4A7C15) & UINT64
    z = x
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & UINT64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & UINT64
    return z ^ (z >> 31)


def as_signed64(v):
    """Minecraft world seeds and `-Ddetmc.seed` are Java longs, so an unsigned draw
    has to be folded into the signed range or the server rejects it."""
    v &= UINT64
    return v - (1 << 64) if v >= (1 << 63) else v


def derive_rng_seeds(world_seed, n, master=SEED_MASTER):
    """The `-Ddetmc.seed` list for `n` replicas of one world.

    `splitmix64(master ^ world_seed ^ (i * GOLDEN))`, folded to a positive long so
    it reads as a seed and never collides with the small integers earlier runs used.
    A lineage keeps its seed for its whole life (branch.py), so these are per-replica
    identities, not per-generation draws."""
    out = []
    for i in range(n):
        v = splitmix64((master ^ (world_seed & UINT64)
                        ^ ((i + 1) * 0x9E3779B97F4A7C15)) & UINT64)
        out.append(v & ((1 << 62) - 1))     # positive, still 62 bits of entropy
    return out


SPRINT_MARGIN = 60          # ticks left for "tick step" to land exactly (as in test/run-det.sh)
SETTLE_STABLE_POLLS = 30    # test/run-det.sh: 30 stable polls (15 s), 3 s lulls are normal


# --------------------------------------------------------------------------- util

def sh(cmd, check=True, timeout=600, env=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       env=({**os.environ, **env} if env else None))
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> rc={p.returncode}\n{p.stdout}\n{p.stderr}")
    return (p.stdout or "") + (p.stderr or "")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


SAFE = re.compile(r"^[0-9+\-*/() a-z_.]+$")


def bad_nbt_files(world):
    """Every gzipped NBT sidecar under a world save that does not decompress.

    A branch checkpoint is copied out from under a RUNNING server
    (branch.BranchRun.checkpoint) and vanilla writes `data/**/*.dat` in place --
    no temp file, no rename -- so a copy can catch one mid-write.  Measured on
    hunt-br-wave1 2026-09-12: `checkpoints/g44n4/world/data/minecraft/
    scoreboard.dat` came out 10 bytes against a healthy 352, the server that
    resumed it logged

        Error loading saved data: SavedDataType[minecraft:scoreboard]
        java.io.EOFException: Unexpected end of ZLIB input stream

    so the whole `detmc` objective was missing, `scoreboard players get #loaded
    detmc` answered "Unknown scoreboard objective" and both of that node's
    children died with `detmc:load did not run at startup`.  These files are a
    few hundred bytes each, so checking them costs nothing next to the copy.
    """
    world = Path(world)
    bad = []
    cands = [world / "level.dat"] + sorted(world.glob("data/**/*.dat"))
    for f in cands:
        if not f.exists():
            continue
        try:
            with gzip.open(f, "rb") as fh:
                data = fh.read()
        except Exception as exc:                       # noqa: BLE001 - reported
            bad.append(f"{f.relative_to(world)}: {type(exc).__name__}: {exc}")
            continue
        if not data:
            bad.append(f"{f.relative_to(world)}: decompressed to 0 bytes")
        elif f.name == "scoreboard.dat" and b"detmc" not in data:
            bad.append(f"{f.relative_to(world)}: no `detmc` objective in it")
    return bad


def subst(value, vars):
    """Expand ${expr} in strings.  expr is integer arithmetic over vars only."""
    if isinstance(value, list):
        return [subst(v, vars) for v in value]
    if isinstance(value, dict):
        return {k: subst(v, vars) for k, v in value.items()}
    if not isinstance(value, str):
        return value
    def one(m):
        expr = m.group(1).strip()
        if not SAFE.match(expr):
            raise ValueError(f"unsafe expression: {expr!r}")
        return str(int(eval(expr, {"__builtins__": {}}, vars)))  # noqa: S307 - local tool
    out = re.sub(r"\$\{([^}]+)\}", one, value)
    return out


# ----------------------------------------------------------------------- scenario

DEFAULTS = {
    "server": {"difficulty": "normal", "simulation_distance": 4, "view_distance": 4,
               "spawn_monsters": True, "memory": "4G"},
    "world": {"seeds": [], "prefilter": {"count": 1, "start_seed": 1, "max_seeds": 100000}},
    "anchor": {"from": "spawn", "offset": [0, 0, 0], "y": "surface"},
    "setup": {"gamerules": {}, "forceload": [], "fake_players": [], "prep": [], "summons": [],
              "time": {"set": "midnight"}, "weather": {"policy": "clear", "duration": 1000000}},
    "detector": {"kind": "none", "interval_ticks": 200, "stop_on_hit": True},
    "run": {"ticks": 6000, "segment_ticks": 1000, "replicas": 2, "rng_seeds": [], "reseed": []},
    "snapshots": {"enabled": False, "every_ticks": 2000, "camera": "../camera/cams/oblique.json",
                  "render": False, "image": "mc-camera-chunky:2.5.0-478-mobs1",
                  "threads": None},
    # `score` is the progress metric branch.py ranks replicas by.  It costs nothing
    # in a plain run.py run: the functions are generated into the same datapack but
    # only ever execute when something calls `function detmc:score` by hand.
    "score": {"metrics": [], "rank": None},
    # how a saved world is picked back up (branch.py only); see Replica.resume
    "resume": {"resummon": True, "restore_material": None},
}


def merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _raw_scenario(path, chain=()):
    """The YAML of one scenario with its `extends:` chain already merged in.

    `extends: <file>` (a path relative to the extending file) loads that scenario
    first and deep-merges this one over it, so a variant is a DIFF rather than a
    copy.  `merge` recurses into dicts and REPLACES lists, which is what a variant
    wants: `detector.detectors: [...]` swaps the whole detector set while
    `detector.check_every_ticks` and every untouched section stay as the base
    declared them.  A copy would drift from the base the first time the arena
    changed under it, and the arena is what makes two runs comparable.
    """
    p = Path(path).resolve()
    if p in chain:
        raise ValueError(f"scenario `extends` cycle: "
                         f"{' -> '.join(str(x) for x in chain + (p,))}")
    raw = yaml.safe_load(p.read_text()) or {}
    base = raw.pop("extends", None)
    if base:
        raw = merge(_raw_scenario((p.parent / base), chain + (p,)), raw)
    return raw


def load_scenario(path):
    raw = _raw_scenario(path)
    scn = dict(raw)
    for k, d in DEFAULTS.items():
        scn[k] = merge(d, raw.get(k))
    scn.setdefault("name", Path(path).stem)
    return scn


# ------------------------------------------------------------------------- seeds

def resolve_seeds(scn, args):
    """-> list of dicts {world_seed, spawn, structures}"""
    if args.seeds:
        return [{"world_seed": int(s), "spawn": None, "structures": {}}
                for s in args.seeds.split(",")]
    if scn["world"]["seeds"]:
        return [{"world_seed": int(s), "spawn": None, "structures": {}}
                for s in scn["world"]["seeds"]]
    pf = scn["world"]["prefilter"]
    cmd = [sys.executable, str(RUNNER / "prefilter.py"),
           "--scenario", str(args.scenario), "--count", str(pf["count"]),
           "--start-seed", str(pf["start_seed"]), "--max-seeds", str(pf["max_seeds"])]
    log(f"prefilter: {' '.join(cmd)}")
    out = sh(cmd, timeout=args.prefilter_timeout)
    data = json.loads(out[out.index("{"):out.rindex("}") + 1])
    log(f"prefilter: method={data.get('method')} mc_version={data.get('mc_version')} "
        f"checked={data.get('checked')} mismatch={data.get('version_mismatch')}")
    return data["seeds"]


# ---------------------------------------------------------------- detector packs
#
# A scenario declares either one detector, or `kind: multi` with a list of them.
# Every detector has a name and its own scores in objective `detmc`:
#
#   #hit_<name>   0/1          #gt_<name>    gametime of that detector's hit
#   #val_<name>   last value    #th_<name>   threshold (set by run.py after setup)
#
# plus three globals: `#hit` / `#hitgt` (the FIRST detector to fire, so a scenario
# with one detector reads exactly as it did before) and `#armed`, which run.py
# sets to 1 as the very last setup command.  `#armed` is what keeps the scripted
# setup -- which spends `tick step`s in await_reload -- from firing every detector
# before the run has begun.
#
# Measured on mc-run-pillar3probe 2026-09-12 (26.2 + detmc, `-Ddetmc.freezeOnStart`):
# while the game is frozen the minecraft:tick tag does NOT run -- `#tick` held the
# same value across 5 s of wall clock with `tick query` reporting "The game is
# frozen" and 0.2 ms/tick -- and neither does the minecraft:load tag at startup;
# both run on the first TICKED tick, which is why detmc:load must not clobber
# `#armed` (see detector_pack).

MAX_INT = 2147483647

# The four horizontal neighbours of a cell, in a fixed order so the generated
# text is a pure function of the scenario.  `entity_enclosed` and
# `entity_enclosure_profile` both walk them.
SIDES = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _as_list(v):
    """`if_block` / `unless_block` take one block id or tag, or a list of them.

    A list is not a convenience here, it is what makes the "a mob put this here"
    test correct.  `#minecraft:enderman_holdable` AND NOT `#minecraft:replaceable`
    looks like "a solid block a mob can carry", and it is not: the 26.2 tags say
    `#minecraft:small_flowers` is holdable and is **not** replaceable (checked
    against the tag JSON in the server jar, whose `replaceable` values are air,
    water, lava, short_grass, fern, bushes, dry grass, seagrass, fire, snow, vine,
    glow_lichen, resin_clump, light, tall_grass, large_fern, structure_void, the
    three air variants, bubble_column, the nether roots, leaf_litter and
    hanging_roots -- no flowers, no mushrooms, no fungi).  So a dandelion an
    enderman dropped in a doorway would have satisfied the detector, and a villager
    walks straight through a dandelion.
    """
    if v is None:
        return []
    return [str(v)] if isinstance(v, (str, int)) else [str(x) for x in v]


def rel_offsets(spec):
    """`offsets: [[dx,dy,dz], ...]`, or `radius: R` + `y_levels: [...]`.

    The explicit form is for the cells whose meaning is specific (the two cells a
    door opens into).  The radius form is for a neighbourhood count, where writing
    146 offsets into a YAML file would hide rather than document what is measured.
    """
    if spec.get("offsets"):
        return [tuple(int(v) for v in o) for o in spec["offsets"]]
    r = int(spec.get("radius", 1))
    ys = [int(v) for v in spec.get("y_levels", [0, 1])]
    keep_self = bool(spec.get("include_self"))
    return [(dx, dy, dz) for dy in ys
            for dx in range(-r, r + 1) for dz in range(-r, r + 1)
            if keep_self or (dx, dy, dz) != (0, 0, 0)]


def _side_conds(dys, air, block=None, op="unless"):
    """`unless block ~dx ~dy ~dz <air>` for every side at every dy, in SIDES order.

    With `block` given and `op="if"` it is the positive test for that block id
    instead, which is how the material gate is written.
    """
    return [f"{op} block ~{dx} ~{dy} ~{dz} {block or air}"
            for dy in dys for dx, dz in SIDES]


def det_list(scn):
    """Normalise `scenario.detector` to a list of named detector dicts."""
    det = scn["detector"]
    if det["kind"] != "multi":
        d = dict(det)
        d.setdefault("name", d["kind"])
        return [d]
    out = []
    for i, sub in enumerate(det["detectors"]):
        d = dict(sub)
        d.setdefault("name", f"{d['kind']}{i}")
        d.setdefault("interval_ticks", det.get("interval_ticks", 200))
        out.append(d)
    names = [d["name"] for d in out]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate detector names: {names}")
    return out


def holders(name):
    return {"hit": f"#hit_{name}", "gt": f"#gt_{name}",
            "val": f"#val_{name}", "thresh": f"#th_{name}"}


def _check_body(det, h, counters):
    """The per-detector check lines.  `counters` collects extra function files."""
    kind = det["kind"]
    name = det["name"]
    hit_fn = f"detmc:hit_{name}"
    if kind == "entity_count":
        op = det.get("op", "<")
        # `selector` should count a TAG, not a distance: a villager that walks out
        # of `distance=..80` reads identical to a dead one.  setup.tag_entities and
        # the auto-tag on summons exist for exactly this.
        return [f'# {name}: {det["selector"]} {op} threshold',
                f'scoreboard players set {h["val"]} detmc 0',
                f'execute store result score {h["val"]} detmc if entity {det["selector"]}',
                f'execute if score {h["hit"]} detmc matches 0 '
                f'if score {h["val"]} detmc {op} {h["thresh"]} detmc '
                f'run function {hit_fn}']
    if kind == "column_stack":
        x1, z1, x2, z2 = (int(v) for v in det["area"])
        base = int(det["base_y"])
        height = int(det.get("height", 3))
        air = det.get("air_block", "minecraft:air")
        out = [f"# {name}: a column of {height} non-air blocks from y={base} up, "
               f"anywhere in [{x1},{z1}]..[{x2},{z2}]"]
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for z in range(min(z1, z2), max(z1, z2) + 1):
                conds = " ".join(f"unless block {x} {base + i} {z} {air}" for i in range(height))
                out.append(f'execute if score {h["hit"]} detmc matches 0 {conds} '
                           f"run function {hit_fn}")
        return out
    if kind == "pattern":
        pat = patterns.get_pattern(det.get("pattern", "smiley_relaxed"))
        x1, z1, x2, z2 = (int(v) for v in det["area"])
        base = int(det["base_y"])
        nlev = int(det.get("levels", 2))
        levels = [base + i for i in range(nlev)]
        air = det.get("air_block", "minecraft:air")
        count_fn = f"detmc:count_{name}"
        counters[f"data/detmc/function/count_{name}.mcfunction"] = \
            patterns.count_function(pat, hit_fn, air, f"#c_{name}")
        return patterns.check_lines(pat, (x1, z1, x2, z2), levels, h["hit"], count_fn, air)
    if kind == "entity_enclosed":
        # A MOB-relative detector, not an area scan: `execute as <selector> at @s
        # align xyz` puts the position at the entity's own feet block, so the whole
        # test is 1 command per matching entity per check instead of one per column
        # of the arena.  `align xyz` floors the entity's fractional position to
        # block coordinates (a villager standing on a full block has y exactly at
        # the block top, and a SLEEPING villager is at bedY + 0.6875, which floors
        # to the bed's own cell).
        #
        # montecarlo's trap definitions, cell for cell:
        #   sim.py:194-213 `_check_trap`  "a 1x1 cell whose 4 horizontal neighbours
        #       just became placed blocks": the open cell's floor surface is `y`
        #       and each neighbour's TOP block is required to be at exactly that y,
        #       i.e. a ONE-high wall around the cell.  That is `levels: [0]` here.
        #   sim.py:290-297 `trap1`        the same predicate, vectorised over every
        #       interior column, and the rate montecarlo reports as
        #       "trap cell (1x1, 4 walls)".
        #   sim.py:298-308 `trap2`        the 2x1 domino with 6 wall cells.
        # montecarlo/README.md flags the 1-high cell as jumpable ("a 1-high wall is
        # also jumpable, so the true 'villager is trapped' rate is lower still"),
        # so `levels: [0, 1]` -- feet AND head -- is the real trap, and a scenario
        # can declare both and get montecarlo parity plus the honest event.
        #
        # `material` is what makes "an enderman put it there" checkable without a
        # block-place event, which no datapack has.  It is the loose holdable block
        # the arena was seeded with, counted only at the `material_levels` offsets;
        # the scenario is responsible for choosing those so that the seeded layout
        # cannot supply `min_material` of them (see the scenario header).
        sel = det["selector"]
        air = det.get("air_block", "minecraft:air")
        dys = [int(v) for v in det.get("levels", [0, 1])]
        mat = det.get("material")
        mdys = [int(v) for v in det.get("material_levels", [max(dys)])]
        minmat = int(det.get("min_material", 1 if mat else 0))
        conds = " ".join(_side_conds(dys, air))
        head = (f"# {name}: {sel} with all 4 horizontal neighbours non-air at dy "
                f"{dys}" + (f", and >= {minmat} of the 4 sides at dy {mdys} being "
                            f"{mat}" if mat and minmat > 0 else ""))
        if not (mat and minmat > 0):
            return [head,
                    f'execute as {sel} at @s align xyz if score {h["hit"]} detmc '
                    f"matches 0 {conds} run function {hit_fn}"]
        ctr = f"#m_{name}"
        sub = [f"# {name}: this entity is enclosed; how many of the enclosing "
               f"blocks are {mat}?",
               f"scoreboard players set {ctr} detmc 0"]
        sub += [f"execute {c} run scoreboard players add {ctr} detmc 1"
                for c in _side_conds(mdys, air, block=mat, op="if")]
        sub += [f'scoreboard players operation {h["val"]} detmc = {ctr} detmc',
                f"execute if score {ctr} detmc matches {minmat}.. run function {hit_fn}"]
        counters[f"data/detmc/function/encl_{name}.mcfunction"] = "\n".join(sub) + "\n"
        return [head,
                f'execute as {sel} at @s align xyz if score {h["hit"]} detmc '
                f"matches 0 {conds} run function detmc:encl_{name}"]
    if kind == "region_entity_blocked":
        # "a villager is inside this house AND the cells that block its doorway are
        # full", one command per region.  This is the cheapest useful shape a
        # detector can have here: the house interiors and the doorway cells are
        # built by `setup.prep` at known coordinates, so both halves of the claim
        # are static geometry and the only runtime terms are an entity-volume test
        # and a handful of `if block`s.
        #
        # The entity volume is the selector's own x/y/z + dx/dy/dz box, which
        # selects an entity whose BOUNDING BOX overlaps it.  The scenario therefore
        # declares the interior shrunk to the interior columns only: a villager
        # standing in the door cell itself has a 0.6-wide box one whole block
        # outside that range and does not count as "inside".
        #
        # `block` is the loose holdable material the arena was seeded with, and the
        # scenario clears every listed cell after the scatter, so a listed cell
        # holding it was filled by a mob.  Requiring TWO stacked cells is what makes
        # it a trap rather than a step: a villager climbs 1 block and cannot climb 2.
        sel = det["selector"]
        blk = det.get("block", "minecraft:air")
        op = "unless" if blk == (det.get("air_block") or "minecraft:air") else "if"
        out = [f"# {name}: {sel} inside a region whose listed cells all hold {blk}"]
        for i, reg in enumerate(det["regions"]):
            x1, y1, z1, x2, y2, z2 = (int(v) for v in reg["interior"])
            xa, xb = min(x1, x2), max(x1, x2)
            ya, yb = min(y1, y2), max(y1, y2)
            za, zb = min(z1, z2), max(z1, z2)
            vol = (f"x={xa},y={ya},z={za},dx={xb - xa},dy={yb - ya},dz={zb - za}")
            inner = sel.rstrip("]")
            inner += ("," if not inner.endswith("[") else "") + vol + "]"
            conds = " ".join(f"{op} block {int(c[0])} {int(c[1])} {int(c[2])} {blk}"
                             for c in reg["blocked"])
            out.append(f'execute if score {h["hit"]} detmc matches 0 '
                       f"if entity {inner} {conds} run function {hit_fn}")
        return out
    if kind == "marker_relative":
        # The detector for a world nobody built: it does not know where the doors
        # are, and it does not have to.  `setup.survey` marks every door and every
        # bed in the village with a `minecraft:marker` at setup, and this walks
        # those markers.  `execute as @e[tag=...] at @s positioned ~dx ~dy ~dz` puts
        # the position on one of the cells the door opens into, so the generated
        # text is a function of the scenario while the geometry is a function of the
        # world.  The markers are saved with the world (the merged jar writes entity
        # region files again, STATUS.md defect 3), so a resumed branch node inherits
        # them and needs no second survey and no reload.
        #
        # `if_block` / `unless_block` are the "an enderman put this here" test.
        # `#minecraft:enderman_holdable` is the only block set any mob can place in
        # a world with no players (verified against the 26.2 tag), and
        # `unless block ... #minecraft:replaceable` throws out flowers, grass and
        # snow layers, which are holdable but do not block a villager.  The claim
        # that no listed cell already satisfies this at setup is MEASURED per run,
        # not assumed: run.py runs the check once with `#armed 0` (see README).
        #
        # `then` is appended verbatim, which is how "and a villager that is next to
        # a bed is next to this cell" is expressed:
        #     as @e[type=minecraft:villager,distance=..5] at @s
        #     if entity @e[type=minecraft:marker,tag=detmc_bed,distance=..3]
        tag = det.get("marker_tag", "detmc_door")
        ifb = _as_list(det.get("if_block"))
        unb = _as_list(det.get("unless_block"))
        then = " ".join(str(t) for t in (det.get("then") or []))
        offs = rel_offsets(det)
        out = [f"# {name}: markers tagged {tag}, offsets {offs}"
               + (f", if block {ifb}" if ifb else "")
               + (f", unless block {unb}" if unb else "")
               + (f", then {then}" if then else "")]
        for dx, dy, dz in offs:
            conds = [f"if block ~ ~ ~ {v}" for v in ifb]
            conds += [f"unless block ~ ~ ~ {v}" for v in unb]
            out.append(f'execute if score {h["hit"]} detmc matches 0 '
                       f"as @e[type=minecraft:marker,tag={tag}] at @s "
                       f"positioned ~{dx} ~{dy} ~{dz} " + " ".join(conds)
                       + (f" {then}" if then else "")
                       + f" run function {hit_fn}")
        return out
    if kind == "block_present":
        # One command per cell per level, so this is the expensive kind.  Pair it
        # with `requires: [<other detector>]` to keep it switched off until the
        # event it is a consequence of has happened.
        x1, z1, x2, z2 = (int(v) for v in det["area"])
        base = int(det["base_y"])
        nlev = int(det.get("levels", 1))
        blk = det["block"]
        out = [f"# {name}: any {blk} in [{x1},{z1}]..[{x2},{z2}] at y "
               f"{base}..{base + nlev - 1}"]
        for y in range(base, base + nlev):
            for x in range(min(x1, x2), max(x1, x2) + 1):
                for z in range(min(z1, z2), max(z1, z2) + 1):
                    out.append(f'execute if score {h["hit"]} detmc matches 0 '
                               f"if block {x} {y} {z} {blk} run function {hit_fn}")
        return out
    raise ValueError(f"unknown detector kind {kind}")


def detector_pack(scn, vars):
    """Return {relpath: text} for the generated datapack, or None."""
    dets = [subst(d, vars) for d in det_list(scn)]
    if any(d["kind"] in ("custom_datapack", "custom_scarpet") for d in dets):
        if len(dets) > 1:
            raise ValueError("custom_* detectors cannot be combined in a multi detector")
        return None
    dets = [d for d in dets if d["kind"] != "none"]
    if not dets:
        return None
    load = ["# created by runner/run.py",
            "scoreboard objectives add detmc dummy",
            "scoreboard players set #hit detmc 0",
            "scoreboard players set #hitgt detmc -1",
            # `#armed` is INITIALISED here, never reset.  Measured on
            # mc-run-pillar3probe 2026-09-12, booting the hunt-br-wave1 g18n0
            # checkpoint with -Ddetmc.freezeOnStart=true: the minecraft:load tag
            # does NOT run at startup while the tick loop is frozen, it runs on
            # the first TICKED tick (#tick read 48000 -- the saved value -- for
            # the whole frozen boot, then read 25 after a `tick step 25`, i.e.
            # detmc:load zeroed it inside the step).  A resumed world is armed by
            # Replica.resume while frozen, so an unconditional `set #armed 0` here
            # disarmed the detector on the first tick of every resumed segment and
            # it stayed disarmed for all 48000 of them.  `unless ... matches
            # <int range>` is the "has no value yet" test (verified over rcon on
            # the same server), so a brand-new world still starts disarmed and a
            # resumed one keeps the arming it was given.
            f"execute unless score #armed detmc matches {-MAX_INT - 1}.. run "
            "scoreboard players set #armed detmc 0",
            "scoreboard players set #tick detmc 0",
            # /reload is asynchronous: the minecraft:load tag fires when the
            # reload finishes, which is AFTER the rcon call returns.  run.py polls
            # #loaded so it can never set #armed before this function undoes it.
            # NOTE: on a RESUMED world `#loaded` is 1 from the save before this
            # function has run at all, so it is not evidence that it has.
            "scoreboard players set #loaded detmc 1"]
    files = {}
    check = []
    for d in dets:
        name = d["name"]
        h = holders(name)
        load += [f'scoreboard players set {h["hit"]} detmc 0',
                 f'scoreboard players set {h["gt"]} detmc -1',
                 f'scoreboard players set {h["val"]} detmc 0',
                 f'scoreboard players set {h["thresh"]} detmc {MAX_INT}']
        # `requires: [<name>, ...]`: only run this detector's check once those
        # detectors have already fired.  A consequence detector (fire after a
        # lightning strike) is an area scan that costs nothing at all until the
        # cause has happened, and reads as "fire AFTER the strike" rather than
        # "fire at some point", which is the claim actually wanted.
        need = [str(r) for r in (d.get("requires") or [])]
        unknown = [r for r in need if r not in {x["name"] for x in dets}]
        if unknown:
            raise ValueError(f"detector {name}: requires unknown detector(s) {unknown}")
        gate = "".join(f"if score #hit_{r} detmc matches 1 " for r in need)
        check.append(f'execute if score {h["hit"]} detmc matches 0 {gate}'
                     f"run function detmc:check_{name}")
        files[f"data/detmc/function/check_{name}.mcfunction"] = \
            "\n".join(_check_body(d, h, files)) + "\n"
        # the global #hit / #hitgt record the FIRST detector to fire and are never
        # overwritten afterwards, so `results.jsonl` keeps a stable "time to first
        # anything" column while the per-detector scores keep the detail.
        files[f"data/detmc/function/hit_{name}.mcfunction"] = "\n".join([
            f'scoreboard players set {h["hit"]} detmc 1',
            f'execute store result score {h["gt"]} detmc run time query gametime',
            "execute if score #hit detmc matches 0 store result score #hitgt detmc "
            "run time query gametime",
            "scoreboard players set #hit detmc 1"]) + "\n"
    # The tick tag fires every tick.  `check_every_ticks` > 1 trades hit-time
    # resolution for tick cost; the default runs the checks every tick.
    every = int(scn["detector"].get("check_every_ticks", 1) or 1)
    if every > 1:
        tick = ["scoreboard players add #tick detmc 1",
                f"execute if score #armed detmc matches 1 if score #tick detmc "
                f"matches {every}.. run function detmc:gate"]
        files["data/detmc/function/gate.mcfunction"] = (
            "scoreboard players set #tick detmc 0\nfunction detmc:check\n")
    else:
        tick = ["execute if score #armed detmc matches 1 run function detmc:check"]
    files.update({
        # 26.2 rejects a bare pack_format above 81: "Pack declares support for
        # version newer than 81, but is missing mandatory fields min_format and
        # max_format" (measured on mc-run-det 2026-09-12; it warns, falls back and
        # still loads, but the fallback is not something to rely on).
        "pack.mcmeta": json.dumps({"pack": {"pack_format": PACK_FORMAT,
                                            "min_format": PACK_FORMAT,
                                            "max_format": PACK_FORMAT,
                                            "description": "detmc scenario detector"}}, indent=2),
        "data/detmc/function/load.mcfunction": "\n".join(load) + "\n",
        "data/detmc/function/tick.mcfunction": "\n".join(tick) + "\n",
        "data/detmc/function/check.mcfunction": "\n".join(check) + "\n",
        "data/minecraft/tags/function/load.json": json.dumps({"values": ["detmc:load"]}),
        "data/minecraft/tags/function/tick.json": json.dumps({"values": ["detmc:tick"]}),
    })
    # `detmc:score` is generated into the same pack but is in no function tag, so
    # it costs nothing until a caller runs it by hand at a checkpoint.
    files.update(score_files(scn, vars))
    files.update(survey_files(scn, vars))
    return files


# ------------------------------------------------------------- progress scoring
#
# A detector answers yes/no.  A branching search needs "how close is this world to
# the thing", so `scenario.score` declares metrics that are computed ON DEMAND --
# `function detmc:score` over rcon at a checkpoint, while ticks are frozen -- and
# read back out of the same `detmc` objective.  Nothing here runs per tick, so the
# cost is paid once per checkpoint per replica instead of 20 times a second.
#
#   column_stack_profile  #s_<name>_<h> = columns with >= h stacked blocks, h=1..H
#                         -> <name>_max (tallest stack), <name>_2 (the 2-stacks),
#                            <name>_blocks (total blocks = sum over h)
#   pattern_best          #s_<name>       = most pattern cells (required + any_of)
#                                           at one scan level in any window
#                         #s_<name>_gated = most any_of cells in a window whose
#                                           `required` cells ALL match; -1 when no
#                                           window has them, which is the honest
#                                           "not even close" value

def survey_files(scn, vars):
    """`detmc:survey` -- a one-shot scan of the world that leaves markers behind.

    A built arena knows where its doors are.  A real village does not, and finding
    out over rcon is not an option: one `execute if block` round trip is ~0.17 s
    measured (README, "A column_stack scan is one rcon round trip per column, 625
    here, measured at 107 s"), so a 19,000-cell volume would take an hour.  Inside a
    datapack function the same 19,000 conditions cost ~0.2 s, and a command can
    `summon minecraft:marker` at the cell it just matched -- so the scan runs in the
    server and its RESULT is read back in one rcon call (the marker positions).

    The function is in no function tag, exactly like `detmc:score`: it runs when
    run.py calls it at setup and never again.  It starts by killing its own markers,
    so running it twice is not a way to double them.
    """
    spec = subst(scn["setup"].get("survey") or {}, vars)
    if not spec:
        return {}
    marks = spec.get("marks") or []
    lines = ["# created by runner/run.py: one-shot world survey, run at setup",
             "kill @e[type=minecraft:marker,tag=detmc_survey]"]
    if marks:
        x1, y1, z1, x2, y2, z2 = (int(v) for v in spec["bounds"])
        lines.insert(1, f"# bounds [{x1},{y1},{z1}]..[{x2},{y2},{z2}], "
                        f"{len(marks)} mark(s): {[m['tag'] for m in marks]}")
    for mk in marks:
        tag, blk = mk["tag"], mk["block"]
        below = mk.get("unless_below")
        lines.append(f"# {tag}: every {blk}"
                     + (f" whose block below is not {below}" if below else ""))
        for y in range(min(y1, y2), max(y1, y2) + 1):
            for x in range(min(x1, x2), max(x1, x2) + 1):
                for z in range(min(z1, z2), max(z1, z2) + 1):
                    c = f"if block {x} {y} {z} {blk}"
                    if below:
                        c += f" unless block {x} {y - 1} {z} {below}"
                    lines.append(f"execute {c} run summon minecraft:marker "
                                 f'{x} {y} {z} {{Tags:["detmc_survey","{tag}"]}}')
    # `derive`: a second pass that is MARKER-relative rather than bounds-relative,
    # so it needs no coordinates and runs inside the same function, after the marks
    # it reads exist (function commands execute in order).  This is how the block
    # BASELINE is taken: `detmc_dcell` marks the cells a door opens into that were
    # AIR when the scenario was set up, and nothing else.  A later "this cell now
    # holds a carried block" is then a change from the baseline by construction,
    # with no per-cell bookkeeping and nothing for a resumed node to recompute.
    for d in spec.get("derive") or []:
        tag = d["tag"]
        # `from` is a marker tag this pass walks; `from_selector` is any selector,
        # which is what lets the chain start from the VILLAGERS instead of from a
        # guessed box.  Measured on `mc-run-trapverify-r0`: a +-32 box around the
        # `locate structure` position of a real plains village held 3 doors and
        # ZERO beds, while the villagers themselves spanned x 208..275, z -354..-257
        # and one of them named its home bed at 238,70,-354 -- outside the box.  A
        # village's extent is not its reference position, and the mobs know where
        # they live, so the survey asks them.
        src = d.get("from_selector") or f"@e[type=minecraft:marker,tag={d['from']}]"
        ifb, unb = _as_list(d.get("if_block")), _as_list(d.get("unless_block"))
        below = d.get("unless_below")
        offs = rel_offsets(d)
        lines.append(f"# {tag}: {len(offs)} offsets of every `{src}`"
                     + (f" that are {ifb}" if ifb else "")
                     + (f" and not {unb}" if unb else "")
                     + (f", block below not {below}" if below else ""))
        for dx, dy, dz in offs:
            # `align xyz` is not cosmetic.  `execute as <villager> at @s positioned
            # ~dx ~dy ~dz run summon minecraft:marker ~ ~ ~` puts the marker at the
            # VILLAGER's fractional position plus the offset -- measured on
            # mc-run-trapverify-r0, the first markers came back at
            # (236.3601359733771, 70.5625, -352.6586642167119), i.e. a villager's own
            # x/z decimals and a bed's 0.5625 height.  The `if block` tests still
            # floor to the right block, so the survey found the right beds; what
            # breaks is everything that measures a DISTANCE, including the dedup
            # below, because two villagers standing differently mark the same bed at
            # two positions 0.5 blocks apart.  Aligning first puts every marker on
            # its block corner.
            c = [f"as {src}", "at @s", "align xyz",
                 f"positioned ~{dx} ~{dy} ~{dz}"]
            c += [f"if block ~ ~ ~ {v}" for v in ifb]
            c += [f"unless block ~ ~ ~ {v}" for v in unb]
            if below:
                c.append(f"unless block ~ ~-1 ~ {below}")
            # Dedup, and it is required rather than tidy: several villagers share a
            # house, so the same bed and the same door would be marked once per
            # villager and every count that walks the markers would multiply.  A
            # marker already at this exact cell is at distance 0.
            # 0.9, not 0.4: an aligned marker is at distance 0, while the
            # bounds-based `marks` pass summons at integer coordinates, which the
            # `summon` command block-CENTRES (measured: `summon marker 213 67 -339`
            # reads back as 213.5, 67.0, -339.5), so a legacy marker is 0.71 away.
            # The nearest wrong cell is an edge neighbour at 1.0, so 0.9 separates
            # them.
            c.append(f"unless entity @e[type=minecraft:marker,tag={tag},distance=..0.9]")
            lines.append("execute " + " ".join(c) + " run summon minecraft:marker "
                         f'~ ~ ~ {{Tags:["detmc_survey","{tag}"]}}')
    return {"data/detmc/function/survey.mcfunction": "\n".join(lines) + "\n"}


def score_list(scn):
    return [m for m in (scn.get("score") or {}).get("metrics", []) if m]


def _area_cells(area):
    x1, z1, x2, z2 = (int(v) for v in area)
    return (min(x1, x2), max(x1, x2), min(z1, z2), max(z1, z2))


def _metric_cells(m):
    """`cell_block_count` cells: an explicit list, or a grid over an area.

    `cells: [[x,y,z], ...]` is the doorway form -- a short, hand-listed set of
    cells whose contents mean something specific.  `area` + `base_y` (+ `levels`,
    `spacing`) is the census form, used for the material budget: every cell of the
    arena at the scanned levels, which is what `restore_material` needs to know how
    much of the seeded material is still in play."""
    if m.get("cells"):
        return [[int(v) for v in c] for c in m["cells"]]
    xa, xb, za, zb = _area_cells(m["area"])
    base = int(m["base_y"])
    step = int(m.get("spacing", 1))
    return [[x, base + dy, z]
            for dy in range(int(m.get("levels", 1)))
            for x in range(xa, xb + 1, step)
            for z in range(za, zb + 1, step)]


def score_files(scn, vars):
    """-> {relpath: text} adding `detmc:score` to the generated pack, or {}."""
    mets = [subst(m, vars) for m in score_list(scn)]
    if not mets:
        return {}
    files = {}
    reset, body = ["scoreboard objectives add detmc dummy"], []
    for m in mets:
        name, kind = m["name"], m["kind"]
        air = m.get("air_block", "minecraft:air")
        if kind == "column_stack_profile":
            xa, xb, za, zb = _area_cells(m["area"])
            base, H = int(m["base_y"]), int(m.get("max_height", 4))
            reset += [f"scoreboard players set #s_{name}_{h} detmc 0" for h in range(1, H + 1)]
            body.append(f"# {name}: column height profile over [{xa},{za}]..[{xb},{zb}] "
                        f"from y={base}, heights 1..{H}")
            for x in range(xa, xb + 1):
                for z in range(za, zb + 1):
                    conds = ""
                    for h in range(1, H + 1):
                        conds += f" unless block {x} {base + h - 1} {z} {air}"
                        body.append(f"execute{conds} run "
                                    f"scoreboard players add #s_{name}_{h} detmc 1")
        elif kind == "pattern_best":
            pat = patterns.get_pattern(m.get("pattern", "smiley_relaxed"))
            xa, xb, za, zb = _area_cells(m["area"])
            base = int(m["base_y"])
            levels = [base + i for i in range(int(m.get("levels", 2)))]
            reset += [f"scoreboard players set #s_{name} detmc 0",
                      f"scoreboard players set #s_{name}_gated detmc -1"]
            files[f"data/detmc/function/score_{name}.mcfunction"] = \
                patterns.best_match_function(pat, name, air)
            body.append(f"# {name}: best partial {pat.name} over {pat.size}x{pat.size} "
                        f"windows in [{xa},{za}]..[{xb},{zb}] at y {levels}")
            w = pat.size
            for y in levels:
                for x0 in range(xa, xb - w + 2):
                    for z0 in range(za, zb - w + 2):
                        body.append(f"execute positioned {x0} {y} {z0} run "
                                    f"function detmc:score_{name}")
        elif kind == "entity_enclosure_profile":
            # The progress metric for `entity_enclosed`: how close is the closest
            # thing to being walled in.  Per entity, not per column, so it is one
            # function call per matching entity at a checkpoint.
            sel = m["selector"]
            dys = [int(v) for v in m.get("levels", [0, 1])]
            mat = m.get("material")
            mdys = [int(v) for v in m.get("material_levels", [max(dys)])]
            near = int(m.get("near_sides", 3))
            es, em = f"#es_{name}", f"#em_{name}"
            reset += [f"scoreboard players set #s_{name}_sides detmc 0",
                      f"scoreboard players set #s_{name}_closed detmc 0",
                      f"scoreboard players set #s_{name}_near detmc 0",
                      f"scoreboard players set #s_{name}_mat detmc 0"]
            sub = [f"# {name}: sides of THIS entity that are non-air at every dy "
                   f"{dys}, and how many of them are {mat} at dy {mdys}",
                   f"scoreboard players set {es} detmc 0"]
            for dx, dz in SIDES:
                c = " ".join(f"unless block ~{dx} ~{dy} ~{dz} {air}" for dy in dys)
                sub.append(f"execute {c} run scoreboard players add {es} detmc 1")
            sub += [f"scoreboard players operation #s_{name}_sides detmc > {es} detmc",
                    f"execute if score {es} detmc matches 4.. run "
                    f"scoreboard players add #s_{name}_closed detmc 1",
                    f"execute if score {es} detmc matches {near}.. run "
                    f"scoreboard players add #s_{name}_near detmc 1"]
            if mat:
                sub.append(f"scoreboard players set {em} detmc 0")
                sub += [f"execute {c} run scoreboard players add {em} detmc 1"
                        for c in _side_conds(mdys, air, block=mat, op="if")]
                sub.append(f"scoreboard players operation #s_{name}_mat detmc > {em} detmc")
            files[f"data/detmc/function/score_{name}.mcfunction"] = "\n".join(sub) + "\n"
            body += [f"# {name}: enclosure profile over {sel}",
                     f"execute as {sel} at @s align xyz run function detmc:score_{name}"]
        elif kind == "marker_neighbourhood_count":
            # "how many cells around each door hold a carried block", as a max over
            # doors and a total over the village, from the same walk.  The max is
            # what a branching search should climb (one door nearly blocked beats
            # every door slightly dusted); the total is the dense tiebreaker.
            tag = m.get("marker_tag", "detmc_door")
            ifb, unb = _as_list(m.get("if_block")), _as_list(m.get("unless_block"))
            offs = rel_offsets(m)
            ctr = f"#dn_{name}"
            reset += [f"scoreboard players set #s_{name}_max detmc 0",
                      f"scoreboard players set #s_{name}_sum detmc 0",
                      f"scoreboard players set #s_{name}_n detmc 0"]
            sub = [f"# {name}: {len(offs)} offsets around THIS {tag} marker",
                   f"scoreboard players set {ctr} detmc 0"]
            for dx, dy, dz in offs:
                c = [f"if block ~{dx} ~{dy} ~{dz} {v}" for v in ifb]
                c += [f"unless block ~{dx} ~{dy} ~{dz} {v}" for v in unb]
                sub.append(f"execute {' '.join(c)} run "
                           f"scoreboard players add {ctr} detmc 1")
            sub += [f"scoreboard players operation #s_{name}_max detmc > {ctr} detmc",
                    f"scoreboard players operation #s_{name}_sum detmc += {ctr} detmc",
                    f"execute if score {ctr} detmc matches 1.. run "
                    f"scoreboard players add #s_{name}_n detmc 1"]
            files[f"data/detmc/function/score_{name}.mcfunction"] = "\n".join(sub) + "\n"
            body += [f"# {name}: neighbourhood count over every {tag} marker",
                     f"execute as @e[type=minecraft:marker,tag={tag}] at @s run "
                     f"function detmc:score_{name}"]
        elif kind == "entity_count":
            # A population signal.  On a natural map the event rate is proportional
            # to how many endermen are actually alive in the village, which is a
            # spawn-cap outcome and not a scenario parameter, so it is measured at
            # every checkpoint rather than assumed.
            reset.append(f"scoreboard players set #s_{name} detmc 0")
            body += [f"# {name}: count of {m['selector']}",
                     f"execute store result score #s_{name} detmc if entity "
                     f"{m['selector']}"]
        elif kind == "cell_block_count":
            # How many of a FIXED list of cells hold `block`.  Used for the
            # doorway metric: the cells either side of every door are listed in
            # the scenario, the arena is built with none of them holding the loose
            # material, so a count above 0 is material an enderman carried there.
            blk = m["block"]
            cells = _metric_cells(m)
            reset.append(f"scoreboard players set #s_{name} detmc 0")
            body.append(f"# {name}: how many of {len(cells)} listed cells hold {blk}")
            for x, y, z in cells:
                body.append(f"execute if block {x} {y} {z} {blk} run "
                            f"scoreboard players add #s_{name} detmc 1")
        else:
            raise ValueError(f"unknown score metric kind {kind}")
    files["data/detmc/function/score.mcfunction"] = \
        "\n".join(["# created by runner/run.py: one-shot progress score, called by branch.py"]
                  + reset + body) + "\n"
    return files


def score_holders(scn):
    """-> [(metric, holder, key)] so a caller can read the scores back."""
    out = []
    for m in score_list(scn):
        name, kind = m["name"], m["kind"]
        if kind == "column_stack_profile":
            for h in range(1, int(m.get("max_height", 4)) + 1):
                out.append((m, f"#s_{name}_{h}", f"{name}_{h}"))
        elif kind == "entity_enclosure_profile":
            for suf in ("sides", "closed", "near", "mat"):
                out.append((m, f"#s_{name}_{suf}", f"{name}_{suf}"))
        elif kind == "marker_neighbourhood_count":
            for suf in ("max", "sum", "n"):
                out.append((m, f"#s_{name}_{suf}", f"{name}_{suf}"))
        elif kind in ("cell_block_count", "entity_count"):
            out.append((m, f"#s_{name}", name))
        else:
            out.append((m, f"#s_{name}", name))
            out.append((m, f"#s_{name}_gated", f"{name}_gated"))
    return out


RANK_BUILTINS = {"max": max, "min": min, "abs": abs}


def rank_of(scn, vals):
    """`scenario.score.rank` over the measured metric values."""
    expr = (scn.get("score") or {}).get("rank")
    if not expr:
        return 0
    return eval(str(expr), {"__builtins__": {}},                 # noqa: S307 - local tool
                {**RANK_BUILTINS, **vals})


# ------------------------------------------------------------------------ replica

class Replica:
    def __init__(self, run, idx, world_seed, rng_seed):
        self.run = run
        self.idx = idx
        self.name = f"r{idx}"
        self.container = f"mc-run-{run.run_id}-r{idx}"
        self.world_seed = world_seed
        self.rng_seed = rng_seed
        self.data = run.dir / f"data-{self.name}"
        self.vars = {}
        self.hit = None
        self.hits = {}
        self.reseeded = set()
        self.baseline = None
        self.baselines = {}
        self.thresholds = {}
        self.reached = 0
        self.wall = 0.0
        self.fake_players_spawned = []   # what spawn_fake_players last did, verbatim
        self.logf = run.dir / f"{self.name}.log"

    # -- plumbing
    def rcon(self, cmd, check=True, timeout=300):
        out = sh(["docker", "exec", self.container, "rcon-cli", cmd], check=check, timeout=timeout)
        with self.logf.open("a") as fh:
            fh.write(f">>> {cmd}\n{out}\n")
        return out

    def rcon_soft(self, cmd, timeout=120):
        """Best effort, never raises.  Used for saves: measured, `save-all flush` did
        not return within 120 s on a frozen server with detmc's synchronous chunk
        saves, while plain `save-all` returned in 19 s."""
        try:
            return self.rcon(cmd, check=False, timeout=timeout)
        except subprocess.TimeoutExpired:
            log(f"{self.name}: {cmd!r} timed out after {timeout}s, continuing")
            return ""

    def dlogs(self, since=None):
        cmd = ["docker", "logs"]
        if since is not None:
            cmd += ["--since", str(since)]
        return sh(cmd + [self.container], check=False, timeout=120)

    def gametime(self):
        m = re.findall(r"-?\d+", self.rcon("time query gametime"))
        return int(m[-1])

    def score(self, holder, obj="detmc"):
        out = self.rcon(f"scoreboard players get {holder} {obj}", check=False)
        m = re.search(r"has (-?\d+)", out)
        return int(m.group(1)) if m else None

    def wait_started(self, timeout=600):
        t0 = time.time()
        while time.time() - t0 < timeout:
            out = self.dlogs()
            if "Done (" in out:
                return True
            if "Minecraft server failed" in out or "FAILED TO BIND" in out:
                raise RuntimeError(f"{self.container} failed to start")
            time.sleep(1)
        raise TimeoutError(f"{self.container} never logged 'Done ('")

    # -- setup
    def settle(self):
        """Chunk loading continues while ticks are frozen; wait for the entity list
        to stop changing (test/run-det.sh found 30 stable polls necessary)."""
        prev, same = None, 0
        for i in range(900):
            now = self.rcon("execute as @e run data get entity @s Pos", check=False)
            same = same + 1 if now == prev else 0
            if same >= SETTLE_STABLE_POLLS:
                break
            prev = now
            time.sleep(0.5)
        log(f"{self.name}: settled after {i + 1} polls")

    # Blocks that "surface" must look through.  #minecraft:replaceable covers air,
    # water, lava, short_grass, ferns, flowers, snow layers and fire (verified on a
    # live 26.2 server: it passes for water, and for air, and fails for stone);
    # leaves and logs are not replaceable, so a tree canopy needs its own two tags,
    # which is the "highest motion-blocking non-leaf block" rule.  Measured on
    # mc-run-lab 2026-09-12: a dirt/log/log/leaves column resolves to the dirt and a
    # grass/short_grass/snow column resolves to the grass block.
    SURFACE_SKIP = ("#minecraft:replaceable", "#minecraft:leaves", "#minecraft:logs")

    def surface_y(self, x, z, ymin=-60, ymax=None, step=None):
        """Highest y at (x,z) whose block is NOT in SURFACE_SKIP, by a DESCENDING
        coarse-then-fine scan.

        This was a binary search, and the binary search is wrong.  It assumed the
        predicate is monotone in y -- everything above the ground replaceable,
        everything at or below it not -- and a cave breaks that.  Measured
        2026-09-12 on `mc-run-trapverify-r0`, world seed 3795784043239602249 at the
        `village_plains` the 26.2 server locates at 240,-320:

            y  20  30  40  50  60  64  67  68  69  70  75  80
               X   .   .   X   X   .   X   X   .   .   .   .      (X = not replaceable)

        i.e. grass_block at 68 with a cave at 64, so the predicate is false at 64 and
        true at 60 and at 68.  The binary search converged on **y = 28**, 40 blocks
        underground; `anchor.y: surface` put the whole scenario there, the survey
        scanned rock and found 0 doors, 0 beds and 0 villagers, and NOTHING FAILED --
        the run booted, set up and was ready to tick for hours around nothing.  That
        is the failure mode this method exists to avoid.

        The scan descends from `ymax` in `step`s and returns the TOPMOST
        non-replaceable block, so a cave underneath is irrelevant.  `step` defaults
        to **1**, and that default is a measurement rather than caution: a coarse
        step of 8 on this same column stopped at **y = 48**, because the probe above
        the surface (72) and the probe below it (64, the cave) are both air, so the
        coarse pass stepped straight over the grass at 68 and kept descending.
        Refining upward inside the last gap does not rescue it -- the gap it lands in
        (49..56) is below the surface it skipped.  A step above 1 is only safe on
        terrain known to be thicker than the step, i.e. the flat-slab arenas, not a
        real world.

        Cost at the defaults is (ymax - surface) round trips: ~65 for the
        `enderman_hunt` slab at y=135 and ~130 for a village at y=68, measured
        ~0.17 s each, so 11-22 s once per replica at setup.  Lower
        `anchor.surface_ymax` to cut it; raise `anchor.surface_step` only for a slab.
        """
        anch = self.run.scn["anchor"]
        ymax = int(anch.get("surface_ymax", 200) if ymax is None else ymax)
        step = int(anch.get("surface_step", 1) if step is None else step)
        skip = anch.get("surface_skip") or list(self.SURFACE_SKIP)
        conds = " ".join(f"unless block {x} {{y}} {z} {b}" for b in skip)

        def solid(y):
            return "Test passed" in self.rcon("execute " + conds.format(y=y),
                                              check=False)
        y = ymax
        while y > ymin and not solid(y):
            y -= step
        best = y if y > ymin else ymin
        for yy in range(y + 1, min(y + step, ymax) + 1):
            if solid(yy):
                best = yy
        return best

    def locate_structure(self, sid):
        out = self.rcon(f"locate structure {sid}")
        m = re.search(r"\[(-?\d+), (-?\d+|~), (-?\d+)\]", out)
        if not m:
            raise RuntimeError(f"locate structure {sid} failed: {out.strip()}")
        x, y, z = m.group(1), m.group(2), m.group(3)
        return int(x), (None if y == "~" else int(y)), int(z)

    def world_spawn(self):
        """The server console executes at the world spawn (MinecraftServer
        .createCommandSourceStack uses getSharedSpawnPos), so a marker summoned at
        `~ ~ ~` lands there.  Summoned and killed in the same fixed setup order on
        every replica, so it cannot skew one replica against another."""
        self.rcon('summon minecraft:marker ~ ~ ~ {Tags:["detmc_spawn"]}')
        out = self.rcon('data get entity @e[type=minecraft:marker,tag=detmc_spawn,limit=1] Pos')
        nums = re.findall(r"-?\d+\.?\d*", out.split("Pos:")[-1])
        self.rcon("kill @e[type=minecraft:marker,tag=detmc_spawn]")
        return int(float(nums[0])), int(float(nums[2]))

    def resolve_xz(self, seedinfo):
        """anchor x/z.  `locate structure` works on ungenerated chunks, so this runs
        before the forceload."""
        anch = self.run.scn["anchor"]
        wx, wz = self.world_spawn()
        if anch["from"] == "structure":
            # `locate structure` on the LIVE 26.2 server wins, and the prefilter's
            # prediction is only logged next to it.  Measured 2026-09-12 on
            # `trapverify`: cubiomes (1.21 worldgen, the newest it supports) put a
            # `village_plains` at 240,-320 on world seed 3795784043239602249, the
            # runner anchored there, and the 26.2 server's own survey of that spot
            # found 0 doors, 0 beds and 0 villagers -- `anchor.y: surface` resolved
            # to y=28, i.e. a sea floor.  A cubiomes STRUCTURE position is a
            # different-version guess, and anchoring a whole hunt on it fails
            # silently: the run boots, sets up and ticks for hours around nothing.
            sid = anch["structure"]
            pos = (seedinfo.get("structures") or {}).get(sid)
            try:
                lx, _ly, lz = self.locate_structure(sid)
            except RuntimeError as exc:
                if not pos:
                    raise
                log(f"{self.name}: WARNING {exc}; falling back to the prefilter's "
                    f"{sid} at {pos}")
                lx, lz = int(pos[0]), int(pos[-1])
            if pos and (int(pos[0]), int(pos[-1])) != (lx, lz):
                d = max(abs(int(pos[0]) - lx), abs(int(pos[-1]) - lz))
                log(f"{self.name}: WARNING prefilter predicted {sid} at "
                    f"{int(pos[0])},{int(pos[-1])} and this 26.2 server locates it "
                    f"at {lx},{lz} ({d} blocks apart); using the server's")
            ax, az = lx, lz
        elif anch["from"] == "fixed":
            ax, az = int(anch["pos"][0]), int(anch["pos"][2])
        else:
            ax, az = wx, wz
        ax += int(anch["offset"][0])
        az += int(anch["offset"][2])
        self.vars = {"spawn_x": wx, "spawn_z": wz, "anchor_x": ax, "anchor_z": az}
        log(f"{self.name}: world spawn {wx},{wz}; anchor x/z {ax},{az}")
        return self.vars

    def resolve_y(self):
        """anchor y.  Needs the anchor chunk loaded, so this runs after the forceload
        and the settle; `execute if block` on an unloaded chunk always fails."""
        anch = self.run.scn["anchor"]
        x, z = self.vars["anchor_x"], self.vars["anchor_z"]
        ay = self.surface_y(x, z) if anch["y"] == "surface" else int(anch["y"])
        self.vars["anchor_y"] = ay + int(anch["offset"][1])
        log(f"{self.name}: anchor {x},{self.vars['anchor_y']},{z}")
        return self.vars

    def setup(self, seedinfo):
        """Fixed order, every replica identical: freeze, gamerules, time/weather,
        anchor x/z, forceload, settle, anchor y, prep blocks, fake players, summons,
        detector."""
        scn = self.run.scn
        self.rcon("tick freeze")
        log(f"{self.name}: gametime at freeze {self.gametime()}")
        self.rcon(f"difficulty {scn['server']['difficulty']}")
        for rule, val in scn["setup"]["gamerules"].items():
            v = str(val).lower() if isinstance(val, bool) else val
            self.rcon(f"gamerule {rule} {v}")
        t = scn["setup"]["time"]
        if t.get("set") is not None:
            self.rcon(f"time set {t['set']}")
        w = scn["setup"]["weather"]
        if w["policy"] != "natural":
            self.rcon(f"weather {w['policy']} {w['duration']}")
        v = self.resolve_xz(seedinfo)
        for region in subst(scn["setup"]["forceload"], v):
            a, b = region["from"], region["to"]
            self.rcon(f"forceload add {int(a[0])} {int(a[1])} {int(b[0])} {int(b[1])}")
        self.settle()
        v = self.resolve_y()
        for entry in scn["setup"]["prep"]:
            for cmd in expand_prep(entry, v):
                self.rcon(cmd)
        self.spawn_fake_players(v)
        self.do_summons(v)
        # Tag entities that were NOT summoned (worldgen villagers, say) so a count
        # detector can count a tag instead of a distance: out-of-range and dead read
        # the same through `distance=`, and only one of them is the event.
        # `tag_entities` stores a count into the `detmc` objective to report what it
        # tagged, and at this point in setup the objective does not exist yet, so
        # the read came back None and the log line said "tagged X: None entities".
        # Creating it here is free (the detector pack's load function does the same
        # `scoreboard objectives add`, which is idempotent).
        self.rcon("scoreboard objectives add detmc dummy", check=False)
        for te in subst(scn["setup"].get("tag_entities") or [], v):
            self.rcon(f"tag {te['selector']} add {te['tag']}")
            self.rcon(f'execute store result score #val_tagcheck detmc '
                      f'if entity @e[tag={te["tag"]}]', check=False)
            log(f"{self.name}: tagged {te['tag']}: {self.score('#val_tagcheck')} entities")
        self.reseed_supported = self.probe_reseed()
        self.install_detector(v)

    def spawn_fake_players(self, v, tries=30, delay=1.0):
        """Put every `setup.fake_players` entry on the server and PROVE it is there.

        Split out of `setup` because a resumed world has to re-run exactly this:
        Carpet logs its fake players out on shutdown and writes nothing that brings
        them back, so a copied world boots with nobody in it -- and natural spawning
        needs a non-spectator player within 128 blocks
        (ChunkMap.playerIsCloseEnoughForSpawning:1014), so a village with no player
        is a village where the scenario cannot happen.  Measured 2026-09-13: `list`
        on a freshly booted copy of a set-up world reads 0 players, and
        hunt-villager-wave1 -- which resumed 548 times without this -- scored
        `endermen` 0 in 548 of its 549 node-generations.

        Two things are retried rather than assumed, both measured:
        * Carpet's `player <name> spawn` returns BEFORE the player joins, so a
          `gamemode` issued on the next rcon round trip answers "No player was
          found" and the player silently stays in SURVIVAL -- a different world,
          because mobs target a survival player and a creative one is invulnerable
          and therefore invisible to targeting.  `bench_tps.py spawn_player` found
          this; the retry here is the same one.
        * the join is then verified against `list`.  A missing player raises, which
          in branch.py FAILS the node (dropped and retried once) instead of ticking
          an empty world at 1.4x and reporting it as progress.

        Nothing here steps the tick loop -- every caller holds `tick freeze` -- so
        the retry spends wall clock, never gametime, and the gametime a resume point
        is pinned to cannot move while a player is joining.

        Fake players default to CREATIVE, and that is a correctness choice rather
        than a convenience.  Read out of the 26.2 source:
          GameType.updatePlayerAbilities:63-66  creative sets abilities.invulnerable
          Player.canBeSeenAsEnemy:724           `return !getAbilities().invulnerable
                                                 && super.canBeSeenAsEnemy()`
        so no mob will ever target a creative fake player -- no creeper walks over
        and explodes a hole in the village, no zombie drags the villagers into a
        fight the scenario is not about.  And the thing the player is there FOR is
        untouched, because playerIsCloseEnoughForSpawning only excludes SPECTATORS
        (`if (player.isSpectator()) return false;` then a plain 128-block test), so
        the chunk is still a spawning chunk and lightning and natural spawns still
        run around it.  Both halves are verified on a live server; see README,
        "A creative fake player gates spawning and is invisible to mobs".
        """
        fps = subst(self.run.scn["setup"]["fake_players"], v)
        self.fake_players_spawned = []
        if not fps:
            return self.fake_players_spawned
        before = self.rcon("list", check=False).strip()
        for fp in fps:
            pos = [int(x) for x in fp["at"]]
            extra = f" facing {fp['facing'][0]} {fp['facing'][1]}" if fp.get("facing") else ""
            self.rcon(f"player {fp['name']} spawn at {pos[0]} {pos[1]} {pos[2]}{extra}")
            mode = fp.get("gamemode", "creative")
            waited = 0
            if mode:
                for attempt in range(tries):
                    out = self.rcon(f"gamemode {mode} {fp['name']}", check=False)
                    if "No player was found" not in out:
                        break
                    waited = attempt + 1
                    time.sleep(delay)
            listed = self.rcon("list", check=False).strip()
            if fp["name"] not in listed:
                raise RuntimeError(
                    f"{self.name}: fake player {fp['name']} never joined after "
                    f"`player ... spawn` and {waited} gamemode retries; "
                    f"`list` says: {listed[:160]}")
            # Read the mode back rather than trust the reply: `playerGameType` 1 is
            # creative (GameType.CREATIVE.getId()).
            raw = self.rcon(f"data get entity {fp['name']} playerGameType",
                            check=False).strip()
            if mode == "creative" and not raw.rstrip().endswith("1"):
                raise RuntimeError(f"{self.name}: fake player {fp['name']} is not "
                                   f"creative after {waited} retries: {raw[:120]}")
            self.fake_players_spawned.append(
                {"name": fp["name"], "at": pos, "gamemode": mode,
                 "gamemode_retries": waited, "gametype_raw": raw[:80],
                 "listed": listed[:160]})
            log(f"{self.name}: fake player {fp['name']} at "
                f"{pos[0]},{pos[1]},{pos[2]} online, {mode} "
                f"(after {waited} gamemode retries); before: {before[:80]}")
        return self.fake_players_spawned

    def do_summons(self, v):
        """`setup.summons`, in file order.  Split out of `setup` because a resumed
        world has to re-run exactly this: detmc writes no entity region files, so a
        saved world comes back with its blocks and none of its mobs (measured, see
        README 'What a resumed world keeps')."""
        n = 0
        for s_ in subst(self.run.scn["setup"]["summons"], v):
            tag = s_.get("tag", SUMMON_TAG)
            nbt = merge_tags(s_.get("nbt"), [SUMMON_TAG] + ([tag] if tag != SUMMON_TAG else []))
            for x, y, z in summon_positions(s_):
                self.rcon(f"summon {s_['type']} {x} {y} {z} {nbt}")
                n += 1
        return n

    def probe_reseed(self):
        """Is there a `/detmc reseed <long>` command on this server?  Another agent
        is adding one; the wiring here must not break while it is absent."""
        if self.run.args.reseed == "off":
            return False
        # `detmc` on its own is an incomplete command and answers like a missing
        # one, so probe with the real read-only subcommand instead and key on its
        # output.  It also puts the branch's starting master key in the log.
        out = self.rcon("detmc rng", check=False)
        ok = "masterSeed" in out
        log(f"{self.name}: reseed probe -> {out.strip()[:160]}")
        if not ok and self.run.args.reseed == "on":
            raise RuntimeError("--reseed on, but the mod has no `detmc` command")
        return ok

    def reseed(self, seed):
        out = self.rcon(f"detmc reseed {seed}", check=False)
        log(f"{self.name}: detmc reseed {seed} -> {out.strip()[:120]}")
        return "Unknown or incomplete command" not in out

    def step_until_loaded(self, max_ticks=40):
        """Drive the tick loop until `detmc:load` has run, or give up."""
        for i in range(max_ticks):
            if self.score("#loaded") == 1:
                return i
            self.rcon("tick step 1", check=False)
            time.sleep(0.3)
        return None

    def await_reload(self, max_ticks=40):
        """`/reload`, then drive the tick loop until the load functions have run.

        Measured on mc-run-det 2026-09-12, and it is not what the command's reply
        suggests: `/reload` answers "Reloading!" immediately, but on a FROZEN server
        the reload never completes -- `#loaded` was still 0 after 20 s of polling,
        and a single `tick step 1` finished it on the spot.  `minecraft:load` then
        runs and resets `#armed` to 0.  So a setup that issued `/reload` and set
        `#armed 1` afterwards had its detector silently disarmed by the first tick
        of the first segment: every score looked right, `#tick` kept counting, and
        nothing could ever fire.  This is what the earlier `datapack enable` +
        `scoreboard players set #armed detmc 1` sequence did.

        The ticks spent here happen with `#armed` still 0, so they cannot produce a
        spurious hit, and `Run.execute` reads the tick base after setup, so they do
        not shift the run either.
        """
        self.rcon("scoreboard objectives add detmc dummy", check=False)
        self.rcon("scoreboard players set #loaded detmc 0")
        self.rcon("reload")
        n = self.step_until_loaded(max_ticks)
        if n is None:
            raise TimeoutError(f"{self.name}: detmc:load never ran after /reload "
                               f"({max_ticks} tick steps)")
        log(f"{self.name}: detector pack loaded after {n} tick steps")
        return True

    def ensure_detmc_loaded(self):
        """Make sure a freshly booted world HAS the detmc objective, and repair it
        if it does not.  Returns "saved", or "repaired ...".

        This is the resume-path counterpart of `await_reload`, and it deliberately
        does not wait for `detmc:load` to run, because on a resumed world it cannot:

        * `#loaded` comes back as 1 **from the save**, before `detmc:load` has run
          in this session at all, so reading it proves nothing (measured on
          mc-run-pillar3probe 2026-09-12: `#tick` reads the saved 48000 for the
          whole frozen boot and only resets inside the first `tick step`, i.e. the
          `minecraft:load` tag runs on the first TICKED tick, not at startup);
        * a branch's resume gametime has to stay exactly the parent's checkpoint
          gametime, so `tick step` is not available here to force it.

        What that late load tag does is now harmless -- it initialises `#armed`
        instead of clobbering it (see `detector_pack`) -- and `branch._advance_one`
        re-reads `#armed` at the end of every segment, so a node that was watched
        by nothing cannot be mistaken for a node that saw nothing.

        The one thing that genuinely breaks a resume is the objective not being
        there at all.  Measured on hunt-br-wave1 generation 45 (2026-09-12): the
        copied world's `data/minecraft/scoreboard.dat` was truncated by a
        checkpoint copy that raced the save (see `bad_nbt_files`), the server
        logged `Error loading saved data: SavedDataType[minecraft:scoreboard]
        java.io.EOFException`, `scoreboard players get #loaded detmc` answered
        "Unknown scoreboard objective", and 2 of 8 nodes died -- taking the other
        44 clean generations of the search with them.  `function detmc:load` is
        the same function the tag runs and its first line is `scoreboard
        objectives add detmc dummy`, so running it by hand rebuilds the objective
        and every holder, costs no tick, and leaves `#armed` alone.
        """
        if self.score("#loaded") == 1:
            return "saved"
        out = self.rcon("scoreboard players get #loaded detmc", check=False)
        missing = "Unknown scoreboard objective" in out
        log(f"{self.name}: WARNING the detmc objective is "
            f"{'MISSING' if missing else 'not marked loaded'} on this world "
            f"({out.strip()[:80]!r}); running `function detmc:load` by hand")
        self.rcon("scoreboard objectives add detmc dummy", check=False)
        ran = self.rcon("function detmc:load", check=False)
        if self.score("#loaded") != 1:
            raise RuntimeError(
                f"{self.name}: no detmc objective on this world and `function "
                f"detmc:load` did not create one ({ran.strip()[:120]!r}); the "
                f"datapack or the copied world is broken")
        return ("repaired by hand (the objective was missing: a corrupt "
                "scoreboard.dat in the checkpoint)" if missing else
                "repaired by hand (`#loaded` was not 1)")

    def install_detector(self, v):
        dets = [subst(d, v) for d in det_list(self.run.scn)]
        kinds = {d["kind"] for d in dets}
        if kinds == {"none"}:
            return
        packs = self.data / "world" / "datapacks"
        packs.mkdir(parents=True, exist_ok=True)
        if "custom_scarpet" in kinds:
            src = (RUNNER / dets[0]["path"]).resolve()
            dst = self.data / "world" / "scripts"
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst / src.name)
            self.rcon(f"script load {src.stem}")
        else:
            if "custom_datapack" in kinds:
                src = (RUNNER / dets[0]["path"]).resolve()
                name = src.name
                shutil.copytree(src, packs / name, dirs_exist_ok=True)
            else:
                name = "detmc_detector"
                files = detector_pack(self.run.scn, v)
                shutil.rmtree(packs / name, ignore_errors=True)
                for rel, text in files.items():
                    p = packs / name / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(text)
                self.pack_commands = sum(
                    len([l for l in t.splitlines() if l and not l.startswith("#")])
                    for rel, t in files.items() if rel.endswith(".mcfunction"))
            # Order matters and cost a run to learn: the server only discovers a
            # pack folder on a reload, so `datapack enable` before the first reload
            # answers "Unknown data pack".  Reload first (26.2 auto-enables a new
            # world datapack, so this usually also runs detmc:load), then enable
            # only if it did not come up enabled, and wait for the load again.
            self.await_reload()
            out = ""
            listed = self.rcon("datapack list enabled")
            if name not in listed:
                out = self.rcon(f'datapack enable "file/{name}"', check=False)
                self.rcon("scoreboard players set #loaded detmc 0")
                if self.step_until_loaded() is None:
                    raise TimeoutError(f"{self.name}: detmc:load never ran after enable")
                listed = self.rcon("datapack list enabled")
            if name not in listed:
                raise RuntimeError(f"detector datapack {name} not enabled: {out}\n{listed}")
        self.rcon("scoreboard objectives add detmc dummy", check=False)
        self.rcon("scoreboard players set #hit detmc 0")
        self.rcon("scoreboard players set #hitgt detmc -1")
        self.thresholds, self.baselines = {}, {}
        for d in dets:
            name, h = d["name"], holders(d["name"])
            thresh = d.get("threshold")
            if d["kind"] == "entity_count":
                self.rcon(f'scoreboard players set {h["val"]} detmc 0')
                self.rcon(f'execute store result score {h["val"]} detmc '
                          f'if entity {d["selector"]}')
                base = self.score(h["val"])
                self.baselines[name] = base
                thresh = eval(str(thresh), {"__builtins__": {}}, {"baseline": base}) \
                    if thresh is not None else base
                log(f"{self.name}: detector {name} baseline={base} threshold={thresh}")
            if thresh is not None:
                self.rcon(f'scoreboard players set {h["thresh"]} detmc {int(thresh)}')
                self.thresholds[name] = int(thresh)
            self.rcon(f'scoreboard players set {h["hit"]} detmc 0')
            self.rcon(f'scoreboard players set {h["gt"]} detmc -1')
        # one detector for backwards compatibility with the old single-detector
        # record fields
        if len(dets) == 1:
            self.baseline = self.baselines.get(dets[0]["name"])
            self.threshold = self.thresholds.get(dets[0]["name"])
        self.survey_counts = self.run_survey()
        self.setup_false_positives = self.check_unarmed()
        self.rcon("scoreboard players set #armed detmc 1")   # last: arms the detectors

    def run_survey(self):
        """Run `detmc:survey` once, frozen, and count what it marked.

        Frozen is fine and is the point: `summon` works while the tick rate manager
        is frozen (the same reason the scripted setup can build an arena), the marker
        order is the function's line order, so it is identical on every replica, and
        no tick is spent."""
        spec = subst(self.run.scn["setup"].get("survey") or {}, self.vars)
        if not spec:
            return {}
        t0 = time.time()
        self.rcon("function detmc:survey", timeout=900)
        counts = {}
        tags = [mk["tag"] for mk in (spec.get("marks") or [])]
        tags += [d["tag"] for d in (spec.get("derive") or [])]
        for tag in tags:
            self.rcon("execute store result score #val_survey detmc if entity "
                      f"@e[type=minecraft:marker,tag={tag}]", check=False)
            counts[tag] = self.score("#val_survey")
        log(f"{self.name}: survey in {time.time() - t0:.1f}s -> {counts}")
        return counts

    def check_unarmed(self):
        """Run every detector's check ONCE while `#armed` is still 0, and report
        which ones fire.  This is the negative control the natural-map scenarios
        need and a built arena did not: nothing guarantees that a real village has
        no cell which already satisfies "a carried block beside a door", so the
        baseline is measured on the world that is about to be run rather than
        argued from the block palette.  Any non-zero here is a false positive, and
        it is logged and recorded in results.jsonl instead of being discovered as a
        hit at gametime 20."""
        dets = [d for d in det_list(self.run.scn) if d["kind"] != "none"]
        fired = {}
        for d in dets:
            h = holders(d["name"])
            self.rcon(f"function detmc:check_{d['name']}", check=False)
            if self.score(h["hit"]) == 1:
                fired[d["name"]] = self.score(h["val"])
            self.rcon(f'scoreboard players set {h["hit"]} detmc 0')
            self.rcon(f'scoreboard players set {h["gt"]} detmc -1')
        self.rcon("scoreboard players set #hit detmc 0")
        self.rcon("scoreboard players set #hitgt detmc -1")
        log(f"{self.name}: unarmed baseline check -> "
            + (f"FALSE POSITIVES {fired}" if fired else "no detector fires on the "
               "world as set up (clean baseline)"))
        return fired

    # -- time
    def sprint(self, ticks):
        since = int(time.time()) - 1
        self.rcon("tick unfreeze")
        self.rcon(f"tick sprint {ticks}")
        for _ in range(12000):
            if "Sprint completed" in self.dlogs(since=since):
                break
            time.sleep(0.2)
        self.rcon("tick freeze")

    def goto(self, target):
        """Land on exactly gametime `target` (test/run-det.sh goto_tick, in python)."""
        need = target - self.gametime()
        if need > SPRINT_MARGIN:
            self.sprint(need - SPRINT_MARGIN)
        need = target - self.gametime()
        if need > 0:
            self.rcon(f"tick step {need}")
            prev, stalls = -1, 0
            for _ in range(6000):
                now = self.gametime()
                if now >= target:
                    break
                stalls = stalls + 1 if now == prev else 0
                if stalls >= 20:
                    break
                prev = now
                time.sleep(0.5)
        cur = self.gametime()
        if cur != target:
            log(f"{self.name}: WARNING wanted gametime {target}, landed on {cur}")
        self.reached = cur
        return cur

    def hit_summary(self):
        if not self.hits:
            return "no"
        return "HIT:" + ",".join(sorted(self.hits))

    def read_detector(self):
        dets = [d for d in det_list(self.run.scn) if d["kind"] != "none"]
        if not dets:
            return None
        per = {}
        for d in dets:
            h = holders(d["name"])
            per[d["name"]] = {"hit": self.score(h["hit"]) or 0,
                              "hit_gametime": self.score(h["gt"]),
                              "value": self.score(h["val"])}
        first = dets[0]["name"]
        return {"hit": self.score("#hit") or 0, "hit_gametime": self.score("#hitgt"),
                "value": per[first]["value"], "per_detector": per,
                "all_hit": all(v["hit"] for v in per.values())}

    # -- progress score, saves, resume (used by branch.py)
    def read_scores(self):
        """Run `detmc:score` once, frozen, and read the metric holders back.

        -> (values, rank, seconds).  `values` also carries the derived
        `<name>_max` (tallest stack) and `<name>_blocks` (total blocks) for every
        column_stack_profile metric, because those are what a search ranks on."""
        mets = score_list(self.run.scn)
        if not mets:
            return {}, 0, 0.0
        t0 = time.time()
        self.rcon("function detmc:score", timeout=900)
        elapsed = time.time() - t0
        vals = {}
        for _m, holder, key in score_holders(self.run.scn):
            vals[key] = self.score(holder)
        for m in mets:
            if m["kind"] != "column_stack_profile":
                continue
            name, H = m["name"], int(m.get("max_height", 4))
            heights = [vals.get(f"{name}_{h}") or 0 for h in range(1, H + 1)]
            vals[f"{name}_blocks"] = sum(heights)
            vals[f"{name}_max"] = max([0] + [h for h, v in enumerate(heights, 1) if v > 0])
        return vals, rank_of(self.run.scn, vals), elapsed

    # The dump sections test/run-reseed.sh writes, in the same order and with the
    # same `### [label] ...` headers, so the same section-diff dates a divergence.
    DUMP_FIELDS = (("all entities Pos", "execute as @e run data get entity @s Pos"),
                   ("all entities Motion", "execute as @e run data get entity @s Motion"),
                   ("all entities UUID", "execute as @e run data get entity @s UUID"),
                   ("all entities id", "execute as @e run data get entity @s id"),
                   ("enderman carriedBlockState",
                    "execute as @e[type=enderman] run data get entity @s carriedBlockState"))

    def entity_dump(self, path, label):
        """Append one checkpoint's entity state to `path`, frozen."""
        with Path(path).open("a") as fh:
            for title, cmd in self.DUMP_FIELDS:
                fh.write(f"### [{label}] {title}\n>>> {cmd}\n")
                fh.write(self.rcon(cmd, check=False) + "\n")
        return str(path)

    def save_world(self, timeout=240):
        """`save-all` and WAIT for it to land, by watching the mod's own sidecar.

        Not `save-all flush`: measured twice on detmc servers (README, and
        test/run-reseed.sh's note) it does not return -- rcon gives up with an i/o
        timeout and the save never lands, because `SaveAllCommand` is silent there
        is no log line to wait on either.  Plain `save-all` returns and rewrites
        `<world>/detmc-rng.properties`, so that file's mtime is the completion
        signal."""
        side = self.data / "world" / "detmc-rng.properties"
        before = side.stat().st_mtime if side.exists() else 0
        t0 = time.time()
        self.rcon_soft("save-all")
        for _ in range(timeout * 2):
            if side.exists() and side.stat().st_mtime != before:
                return time.time() - t0
            time.sleep(0.5)
        log(f"{self.name}: WARNING detmc-rng.properties did not move in {timeout}s "
            "after save-all; the checkpoint may be a previous save")
        return time.time() - t0

    def resume(self, vars, expect_gametime, reseed_seed):
        """Pick a copied world back up and branch it.  No `/reload`, no tick step.

        Measured on mc-run-probe2 2026-09-12, on a world copied out of a live
        server after a plain `save-all`: gametime, the forceload, every gamerule,
        the block state and the enabled datapack all come back, and the mod restores
        its own stream from `detmc-rng.properties`.  `#loaded` reads 1 and the whole
        `detmc` objective comes back too, but **from the save**: measured 2026-09-12,
        `detmc:load` has NOT run at that point on a frozen boot -- the
        `minecraft:load` tag fires on the first ticked tick, i.e. inside the segment
        this method is setting up.  That is why the generated load function
        initialises `#armed` instead of setting it, and why `ensure_detmc_loaded`
        checks for the objective instead of waiting on a flag the save carries.  **The entities do
        not** -- every `entities/*.mca` is 0 bytes on detmc, so the world comes
        back with 0 mobs.  **Neither do the players**: Carpet logs its fake players
        out on shutdown, so the copy boots empty and natural spawning -- which needs
        a non-spectator player within 128 blocks -- is off until one is put back
        (measured 2026-09-13; see `spawn_fake_players`).  So the order here is:
        re-summon first (frozen, no tick passes), restore the blocks the despawned
        mobs were holding, re-spawn the scenario's fake players, and only then
        reseed, which is the single thing that makes a sibling different.

        Because nothing here steps the tick loop, the gametime at the reseed is
        exactly the parent's checkpoint gametime, which is what makes a lineage
        replayable as `(gametime, reseed)` pairs."""
        self.vars = dict(vars)
        self.rcon("tick freeze")
        gt = self.gametime()
        self.resumed_at = gt
        if expect_gametime is not None and gt != expect_gametime:
            raise RuntimeError(f"{self.name}: resumed world is at gametime {gt}, "
                               f"expected {expect_gametime}")
        listed = self.rcon("datapack list enabled")
        if "detmc_detector" not in listed and score_list(self.run.scn):
            raise RuntimeError(f"{self.name}: detector pack not enabled after resume: {listed}")
        how = self.ensure_detmc_loaded()
        if how != "saved":
            log(f"{self.name}: detmc state {how}")
        self.reseed_supported = self.probe_reseed()
        res = self.run.scn.get("resume") or {}
        self.resummoned = self.do_summons(self.vars) if res.get("resummon", True) else 0
        self.restored_blocks = self.restore_material()
        # The fake players, BEFORE the reseed, for the same reason the re-summon and
        # the material restore are before it: everything that touches the world on a
        # resume has to be identical across a parent's children, so the `detmc
        # reseed` argument stays the ONE thing that makes a sibling different.  A
        # player spawned after the reseed would draw from the new stream, and each
        # sibling would start its segment at a different offset into its own seed --
        # deterministic, but a second difference the lineage does not record.  Here
        # the draws (if Carpet makes any) come out of the parent's saved stream,
        # which every sibling shares, and the reseed then replaces that stream
        # wholesale.  Gametime does not move either way: the world is frozen, which
        # is what keeps a lineage replayable as (gametime, reseed) pairs.
        self.spawn_fake_players(self.vars)
        # No settle here, and that is measured rather than assumed: waiting for
        # the entity list to stop moving after the join (`settle()`, 31 polls,
        # 22 s per node) did NOT make two containers agree -- entityOrdinal came
        # out 42118 against 42128 with it, against 42131/42159 without, and the
        # score metrics diverged in that arm too.  The divergence is the known
        # per-entity RandomSource gap (STATUS.md, "Resumed does not equal
        # uninterrupted"), which no pre-tick wait can close, so the wait would be
        # 22 s a node for nothing.
        self.applied_reseed = None
        if reseed_seed is not None:
            if not self.reseed_supported:
                raise RuntimeError(f"{self.name}: no `detmc reseed` command on this server")
            if not self.reseed(reseed_seed):
                raise RuntimeError(f"{self.name}: `detmc reseed {reseed_seed}` was rejected")
            self.applied_reseed = reseed_seed
            self.reseeded.add(gt)
        self.rcon("scoreboard players set #armed detmc 1")
        log(f"{self.name}: resumed at gametime {gt}, {self.resummoned} mobs re-summoned, "
            f"{self.restored_blocks} blocks restored, "
            f"{len(self.fake_players_spawned)} fake players re-spawned "
            f"({', '.join(f['name'] for f in self.fake_players_spawned) or 'none'}), "
            f"reseed {self.applied_reseed}")
        return gt

    def restore_material(self):
        """Put back the blocks the checkpoint's mobs were carrying.

        An enderman that despawns with a block in hand takes it out of the arena,
        and the arena's loose-block count IS the search's material budget (see
        montecarlo: the mob never creates material, it only recycles it).  So the
        scenario declares how many blocks the arena is supposed to hold and where
        the spares go; this counts what is actually there with the same score
        function the ranking uses, and refills the deficit into the first empty
        cells of a fixed grid, in a fixed order, so a replay puts them back in
        exactly the same places."""
        spec = (self.run.scn.get("resume") or {}).get("restore_material")
        if not spec:
            return 0
        spec = subst(spec, self.vars)
        vals, _rank, _s = self.read_scores()
        # `<name>_blocks` is the column_stack_profile total (every non-air block in
        # the area).  A `cell_block_count` metric measures one BLOCK ID instead,
        # which is what an arena containing fixtures needs: counting "non-air" over
        # a village plot counts the houses, the beds and the fence as material.
        cm = spec["count_metric"]
        have = vals.get(f"{cm}_blocks", vals.get(cm))
        if have is None:
            raise RuntimeError(f"{self.name}: restore_material wants metric "
                               f"{cm}_blocks or {cm}, neither of which is measured")
        want = int(spec["blocks"])
        need = want - int(have)
        if need <= 0:
            return 0
        xa, xb, za, zb = _area_cells(spec["grid"]["area"])
        y, step = int(spec["grid"]["y"]), int(spec["grid"].get("spacing", 3))
        block = spec.get("block", "minecraft:dirt")
        air = spec.get("air_block", "minecraft:air")
        placed = 0
        for x in range(xa, xb + 1, step):
            for z in range(za, zb + 1, step):
                if placed >= need:
                    break
                if "Test passed" not in self.rcon(
                        f"execute if block {x} {y} {z} {air}", check=False):
                    continue
                self.rcon(f"setblock {x} {y} {z} {block}")
                placed += 1
            if placed >= need:
                break
        if placed < need:
            log(f"{self.name}: WARNING wanted {need} blocks back, the grid only had "
                f"{placed} free cells")
        return placed

    def locate_hits(self, limit=10):
        """After a hit, find WHERE, so `results.jsonl` records the actual shape and
        a render can be aimed at it.  One entry per detector that fired."""
        found = {}
        for d in [subst(x, self.vars) for x in det_list(self.run.scn)]:
            if not (self.hits or {}).get(d["name"], {}).get("hit"):
                continue
            if d["kind"] == "column_stack":
                x1, z1, x2, z2 = (int(v) for v in d["area"])
                base, h = int(d["base_y"]), int(d.get("height", 3))
                hit = []
                for x in range(min(x1, x2), max(x1, x2) + 1):
                    for z in range(min(z1, z2), max(z1, z2) + 1):
                        conds = " ".join(f"unless block {x} {base + i} {z} minecraft:air"
                                         for i in range(h))
                        if "Test passed" in self.rcon(f"execute {conds}", check=False):
                            hit.append([x, base, z])
                            if len(hit) >= limit:
                                break
                    if len(hit) >= limit:
                        break
                found[d["name"]] = hit
            elif d["kind"] == "pattern":
                found[d["name"]] = self.locate_pattern(d, limit)
            elif d["kind"] == "marker_relative":
                found[d["name"]] = self.locate_marker_relative(d, limit)
            elif d["kind"] == "entity_enclosed":
                found[d["name"]] = self.locate_enclosed(d, limit)
        return found

    def locate_marker_relative(self, det, limit=10):
        """Which marker's cell is blocked.  Re-tests the detector's own block
        conditions from the marker positions, over rcon, so the located cell is
        confirmed by a second implementation rather than inferred."""
        tag = det.get("marker_tag", "detmc_door")
        ifb, unb = _as_list(det.get("if_block")), _as_list(det.get("unless_block"))
        offs = rel_offsets(det)
        out = self.rcon(f"execute as @e[type=minecraft:marker,tag={tag}] at @s run "
                        "data get entity @s Pos", check=False)
        import math
        found = []
        for line in out.splitlines():
            if "Pos:" not in line:
                continue
            nums = re.findall(r"-?\d+\.?\d*", line.split("Pos:")[-1])
            if len(nums) < 3:
                continue
            mx, my, mz = (math.floor(float(n)) for n in nums[:3])
            for dx, dy, dz in offs:
                x, y, z = mx + dx, my + dy, mz + dz
                ok = all("Test passed" in self.rcon(
                    f"execute if block {x} {y} {z} {v}", check=False) for v in ifb)
                ok = ok and all("Test passed" in self.rcon(
                    f"execute unless block {x} {y} {z} {v}", check=False) for v in unb)
                if ok:
                    found.append([x, y, z, mx, my, mz])
                    if len(found) >= limit:
                        return found
        return found

    def locate_enclosed(self, det, limit=10):
        """Which entity is walled in, and where.  Re-runs the detector's own
        predicate over rcon per entity, so it both locates the hit and confirms it
        against a second implementation of the same test."""
        sel = det["selector"]
        air = det.get("air_block", "minecraft:air")
        dys = [int(v) for v in det.get("levels", [0, 1])]
        mat = det.get("material")
        mdys = [int(v) for v in det.get("material_levels", [max(dys)])]
        out = self.rcon(f"execute as {sel} at @s run data get entity @s Pos", check=False)
        found = []
        for line in out.splitlines():
            nums = re.findall(r"-?\d+\.?\d*", line.split("Pos:")[-1]) if "Pos:" in line else []
            if len(nums) < 3:
                continue
            import math
            bx, by, bz = (math.floor(float(n)) for n in nums[:3])
            sides = 0
            for dx, dz in SIDES:
                if all("Test passed" in self.rcon(
                        f"execute unless block {bx + dx} {by + dy} {bz + dz} {air}",
                        check=False) for dy in dys):
                    sides += 1
            nmat = 0
            if mat:
                for dy in mdys:
                    for dx, dz in SIDES:
                        if "Test passed" in self.rcon(
                                f"execute if block {bx + dx} {by + dy} {bz + dz} {mat}",
                                check=False):
                            nmat += 1
            found.append([bx, by, bz, sides, nmat])
            if len(found) >= limit:
                break
        return found

    def locate_pattern(self, det, limit=10):
        """Read every column top in the detector area over rcon, then re-run the
        Python matcher on it.  Same predicate as the datapack (cross-checked in
        test_patterns.py), so this both locates the hit and confirms it."""
        pat = patterns.get_pattern(det.get("pattern", "smiley_relaxed"))
        x1, z1, x2, z2 = (int(v) for v in det["area"])
        base = int(det["base_y"])
        levels = [base + i for i in range(int(det.get("levels", 2)))]
        tops = {}
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for z in range(min(z1, z2), max(z1, z2) + 1):
                for y in reversed(levels):
                    if "Test passed" in self.rcon(
                            f"execute unless block {x} {y} {z} minecraft:air", check=False):
                        tops[(x, z)] = y
                        break
        return [list(t) for t in patterns.match_tops(
            tops, pat, (x1, z1, x2, z2), levels)[:limit]]

    def snapshot(self, tick):
        snaps = self.run.dir / "snaps" / self.name / f"t{tick}"
        # `save-all`, not `save-all flush` (see rcon_soft).  server.properties has
        # sync-chunk-writes=true and ticks stay frozen across the copy, which is what
        # camera/README.md requires.
        self.rcon_soft("save-all")
        time.sleep(2)
        src = self.data / "world"
        (snaps / "world").mkdir(parents=True, exist_ok=True)
        shutil.copy(src / "level.dat", snaps / "world" / "level.dat")
        # region/ AND entities/, in every dimension, exactly as camera/timelapse.sh
        # does: Chunky reads living entities from entities/*.mca and never from
        # region/*.mca, so a snapshot without them renders an empty arena.
        # session.lock is deliberately not copied - the live server holds it.
        for sub in ("region", "entities"):
            for dim in (src / "dimensions").glob(f"*/*/{sub}"):
                dst = snaps / "world" / dim.relative_to(src)
                dst.mkdir(parents=True, exist_ok=True)
                for mca in dim.glob("*.mca"):
                    shutil.copy(mca, dst / mca.name)
            flat = src / sub
            if flat.exists():
                (snaps / "world" / sub).mkdir(parents=True, exist_ok=True)
                for mca in flat.glob("*.mca"):
                    shutil.copy(mca, snaps / "world" / sub / mca.name)
        ent = list((snaps / "world").glob("dimensions/*/*/entities/*.mca"))
        ent_bytes = sum(f.stat().st_size for f in ent)
        reg_bytes = sum(f.stat().st_size
                        for f in (snaps / "world").glob("dimensions/*/*/region/*.mca"))
        log(f"{self.name}: snapshot t{tick} -> {snaps} "
            f"(region {reg_bytes} B, entities {ent_bytes} B in {len(ent)} files)")
        if ent and not ent_bytes:
            # Measured 2026-09-12, and confirmed against an unmodded control: on a
            # detmc server every entities/*.mca stays 0 bytes, so a render of this
            # snapshot shows the block art but no mobs.  See README, "detmc never
            # writes entity region files".  Blocks are unaffected.
            log(f"{self.name}: WARNING every entities/*.mca is empty; this render "
                "will have no mobs in it (detmc entity-save defect)")
        (snaps / "snapshot.json").write_text(json.dumps({
            "tick": tick, "gametime": self.reached, "replica": self.name,
            "region_bytes": reg_bytes, "entity_bytes": ent_bytes,
            "entities_written": bool(ent_bytes),
            "vars": self.vars}, indent=2))
        if self.run.scn["snapshots"]["render"]:
            out = snaps / "frame.png"
            # snap.sh defaults to the patched Chunky already; setting it here keeps
            # the renderer that produced a frame recorded in the scenario file.
            env = {"CHUNKY_IMAGE": self.run.scn["snapshots"].get(
                "image", "mc-camera-chunky:2.5.0-478-mobs1")}
            if self.run.scn["snapshots"].get("threads"):
                env["CHUNKY_THREADS"] = str(self.run.scn["snapshots"]["threads"])
            try:
                t0 = time.time()
                sh([str(PROJECT / "camera" / "snap.sh"), str(snaps / "world"),
                    str(self.camera_file()), str(out)], timeout=3600, env=env)
                log(f"{self.name}: rendered {out} in {time.time() - t0:.1f}s "
                    f"({out.stat().st_size} bytes)")
            except Exception as exc:                     # rendering is optional
                log(f"{self.name}: render failed: {exc}")
        return str(snaps)

    def camera_file(self):
        """`snapshots.camera` is a path or an inline object, and either may contain
        ${anchor_x} style expressions, because a camera pinned to one world's spawn
        is useless on the next seed.  The resolved camera is written once per
        replica next to the snapshots."""
        if getattr(self, "_camera", None):
            return self._camera
        cam = self.run.scn["snapshots"]["camera"]
        raw = json.loads((RUNNER / cam).read_text()) if isinstance(cam, str) else cam
        def coerce(v):
            if isinstance(v, dict):
                return {k: coerce(x) for k, x in v.items()}
            if isinstance(v, list):
                return [coerce(x) for x in v]
            if isinstance(v, str):
                t = subst(v, self.vars)
                try:
                    return int(t)
                except ValueError:
                    return t
            return v
        out = self.run.dir / "snaps" / self.name / "camera.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(coerce(raw), indent=2))
        self._camera = out
        return out


def expand_prep(entry, vars):
    """A prep entry is a command string, or {scatter: {...}} which expands to setblocks."""
    if isinstance(entry, str):
        return [subst(entry, vars)]
    if "cmd" in entry:
        return [subst(entry["cmd"], vars)]
    sc = subst(entry["scatter"], vars)
    x1, z1, x2, z2 = (int(v) for v in sc["area"])
    y = int(sc["y"])
    step = int(sc.get("spacing", 3))
    return [f"setblock {x} {y} {z} {sc['block']}"
            for x in range(min(x1, x2), max(x1, x2) + 1, step)
            for z in range(min(z1, z2), max(z1, z2) + 1, step)]


SUMMON_TAG = "detmc_summoned"


def merge_tags(nbt, tags):
    """Add `Tags` to a summon's SNBT so the entity can be counted by tag later.

    A scenario that writes its own `Tags:` keeps it verbatim; everything else gets
    the tags injected right after the opening brace."""
    tagstr = "Tags:[" + ",".join(f'"{t}"' for t in tags) + "]"
    if not nbt:
        return "{" + tagstr + "}"
    nbt = nbt.strip()
    if "Tags:" in nbt:
        return nbt
    inner = nbt[1:-1].strip()
    return "{" + tagstr + ("," + inner if inner else "") + "}"


def reseed_schedule(spec, ticks, rng_seed):
    """`run.reseed` -> [(tick, seed)].

    Either an explicit list of {at, seed}, or {every_ticks: N}, which derives a
    seed per checkpoint from the replica's own rng seed so the schedule is a pure
    function of the manifest and a run can be replayed."""
    if not spec:
        return []
    if isinstance(spec, dict):
        every = int(spec["every_ticks"])
        base = int(spec.get("seed_base", rng_seed))
        return [(k * every, base * 1000003 + k)
                for k in range(1, ticks // every + 1)]
    return sorted((int(e["at"]), int(e["seed"])) for e in spec)


def summon_positions(s):
    """point | grid | ring position specs -> list of (x,y,z)."""
    at = s["at"]
    mode = at.get("mode", "point")
    ox, oy, oz = (int(v) for v in at["origin"])
    n = int(s.get("count", 1))
    if mode == "point":
        return [(ox, oy, oz)] * n
    if mode == "grid":
        sp = int(at.get("spacing", 3))
        cols = int(at.get("cols", max(1, int(n ** 0.5))))
        return [(ox + (i % cols) * sp, oy, oz + (i // cols) * sp) for i in range(n)]
    if mode == "ring":
        import math
        r = int(at.get("radius", 8))
        return [(ox + round(r * math.cos(2 * math.pi * i / n)), oy,
                 oz + round(r * math.sin(2 * math.pi * i / n))) for i in range(n)]
    raise ValueError(f"unknown position mode {mode}")


def service_spec(scn, container, data_rel, world_seed, rng_seed, mods_rel):
    """One compose service for one replica.  `-Ddetmc.seed` stays the SAME for every
    node of a branch lineage: the mod validates `detmc-rng.properties` against
    `baseMaster = seed ^ worldSeed`, so a resumed world must be launched with the
    seed it was created with, and the branch comes from `/detmc reseed` instead."""
    return {
        "image": IMAGE,
        "container_name": container,
        "restart": "no",
        "tty": True,
        "stdin_open": True,
        "environment": [
            "EULA=TRUE", "TYPE=FABRIC", f"VERSION={MC_VERSION}",
            f"FABRIC_LOADER_VERSION={FABRIC_LOADER}",
            f"SEED={world_seed}",
            f"DIFFICULTY={scn['server']['difficulty']}",
            f"SIMULATION_DISTANCE={scn['server']['simulation_distance']}",
            f"VIEW_DISTANCE={scn['server']['view_distance']}",
            f"SPAWN_MONSTERS={str(scn['server']['spawn_monsters']).lower()}",
            "SPAWN_PROTECTION=0", "ONLINE_MODE=FALSE", "ENABLE_RCON=TRUE",
            "RCON_PASSWORD=${RCON_PASSWORD}",
            f"MEMORY={scn['server']['memory']}",
            "USE_AIKAR_FLAGS=false", "PAUSE_WHEN_EMPTY_SECONDS=-1",
            # the tick-end chunk-task drain collapses a forceload into one
            # tick; the default 60 s watchdog would kill the server
            "MAX_TICK_TIME=-1",
            (f"JVM_OPTS=-Ddetmc.seed={rng_seed} -Ddetmc.freezeOnStart=true "
             f"-Dmax.bg.threads=1 {os.environ.get('DETMC_EXTRA_OPTS', '')}").strip(),
        ],
        "volumes": [f"{data_rel}:/data", f"{mods_rel}:/data/mods"],
    }


# ---------------------------------------------------------------------------- run

class Run:
    def __init__(self, scn, args, seeds):
        self.scn = scn
        self.args = args
        self.run_id = args.run_id or f"{scn['name']}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.dir = RUNNER / "runs" / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.world = seeds[0]
        self.n = args.replicas or scn["run"]["replicas"]
        # A scenario that names its own `rng_seeds` keeps them verbatim (branch.py
        # reads the same key, so a lineage's identity does not change under it).
        # Otherwise they are DERIVED from the world seed rather than being 1..N.
        rs = scn["run"]["rng_seeds"] or derive_rng_seeds(
            int(seeds[0]["world_seed"]), self.n)
        off = args.rng_seed_offset
        self.replicas = [Replica(self, i, int(self.world["world_seed"]),
                                 int(rs[i % len(rs)]) + off)
                         for i in range(self.n)]
        self.ticks = args.ticks or scn["run"]["ticks"]
        if args.memory:
            scn["server"]["memory"] = args.memory
        if args.segment_ticks:
            scn["run"]["segment_ticks"] = args.segment_ticks
        if args.snapshot_every:
            scn["snapshots"]["every_ticks"] = args.snapshot_every
            scn["snapshots"]["enabled"] = True
        if args.no_render:
            scn["snapshots"]["render"] = False
        self.results = self.dir / "results.jsonl"

    # -- compose
    def write_compose(self):
        scn = self.scn
        needs_carpet = bool(scn["setup"]["fake_players"])
        mods = self.dir / "mods"
        mods.mkdir(exist_ok=True)
        jars = sorted((PROJECT / "fabric" / "build" / "libs").glob("detmc-*.jar"))
        if not jars:
            raise RuntimeError("no detmc jar in fabric/build/libs; run ./docker-gradle.sh :fabric:build")
        shutil.copy(jars[-1], mods / jars[-1].name)
        if needs_carpet:
            if not CARPET_JAR.exists():
                raise RuntimeError(f"{CARPET_JAR} missing; run ./fetch-carpet.sh")
            shutil.copy(CARPET_JAR, mods / CARPET_JAR.name)
        rcon_pw = secrets.token_hex(16)
        (self.dir / ".env").write_text(f"RCON_PASSWORD={rcon_pw}\n")
        services = {}
        for r in self.replicas:
            r.data.mkdir(exist_ok=True)
            services[r.name] = service_spec(scn, r.container, f"./data-{r.name}",
                                            r.world_seed, r.rng_seed, "./mods")
        compose = {"services": services}
        (self.dir / "compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
        return self.dir / "compose.yml"

    def node_need_mb(self):
        """Host cost of one replica: its heap plus JVM/container overhead."""
        return memgate.job_need_mb(self.scn["server"].get("memory"))

    def compose(self, *args, check=True, need_mb=None):
        # Shared host memory gate, same reason as branch.py: no launcher may push
        # the box past budget (2026-09-12 OOM).  Only `up` allocates, so only `up`
        # waits; `down`/`rm` must never block.  The whole-run `up` passes the total
        # for every replica it starts at once.
        if args and args[0] == "up":
            memgate.acquire(need_mb or self.node_need_mb())
        # Project name per run.  Compose defaults the project to the compose
        # directory name, so two runs shared project "compose" and a service
        # named after the node; on 2026-09-13 05:38 hunt-villager-wave1 `up`
        # for its g154n0 recreated hunt-br-wave2's g154n0 container, which
        # dropped three wave-2 nodes.
        project = "detmc-" + re.sub(r"[^a-z0-9_-]", "-", self.run_id.lower())
        return sh(["docker", "compose", "-p", project, "-f", str(self.dir / "compose.yml"),
                   "--env-file", str(self.dir / ".env")] + list(args), check=check, timeout=600)

    def manifest(self):
        jars = {}
        for j in sorted((self.dir / "mods").glob("*.jar")):
            jars[j.name] = sh(["sha256sum", str(j)]).split()[0]
        return {
            "run_id": self.run_id, "scenario": self.scn["name"],
            "scenario_file": str(Path(self.args.scenario).resolve()),
            "image": IMAGE, "mc_version": MC_VERSION, "mods": jars,
            "world": self.world, "ticks": self.ticks,
            "rng_seed_offset": self.args.rng_seed_offset,
            # How to re-derive every seed in this run without the run.  `world`
            # carries the prefilter's own `seed_source` / `seed_master` when the
            # seed came from there.
            "seed_derivation": {
                "generator": "splitmix64",
                "master": SEED_MASTER,
                "rng_seeds": "run.rng_seeds from the scenario if set, else "
                             "run.derive_rng_seeds(world_seed, replicas, master)",
                "world_seed": "prefilter.py --seed-source random --seed-master M, "
                              "candidates random.Random(M).getrandbits(64) folded "
                              "to a signed long, first accepted wins",
            },
            "replicas": [{"name": r.name, "container": r.container,
                          "world_seed": r.world_seed, "rng_seed": r.rng_seed}
                         for r in self.replicas],
            "resolved_scenario": self.scn,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }

    # -- main
    def execute(self):
        scn = self.scn
        dets = det_list(scn)
        interval = min([scn["run"]["segment_ticks"]] +
                       [int(d.get("interval_ticks", 200)) for d in dets])
        # Snapshots are taken at segment boundaries, so a segment longer than the
        # snapshot cadence silently drops frames.
        if scn["snapshots"]["enabled"]:
            snap = int(scn["snapshots"]["every_ticks"])
            if snap % interval:
                log(f"WARNING snapshots.every_ticks {snap} is not a multiple of the "
                    f"{interval}-tick segment; frames will land late")
            interval = min(interval, snap)
        self.write_compose()
        (self.dir / "manifest.json").write_text(json.dumps(self.manifest(), indent=2, default=str))
        if self.args.dry_run:
            log(f"dry run: compose + manifest written to {self.dir}")
            return 0
        log(f"starting {self.n} replicas: {[r.container for r in self.replicas]}")
        self.compose("up", "-d", need_mb=len(self.replicas) * self.node_need_mb())
        t0 = time.time()
        status, error = 0, None
        try:
            for r in self.replicas:
                try:
                    r.wait_started(timeout=self.args.start_timeout)
                except (RuntimeError, TimeoutError) as exc:
                    # the itzg image downloads the Fabric server jar on first start and
                    # that download fails here often enough to matter (test/run-pair.sh
                    # retries once for the same reason)
                    log(f"{r.name}: {exc}; retrying once from a clean data dir")
                    self.compose("rm", "-sf", r.name, check=False)
                    shutil.rmtree(r.data, ignore_errors=True)
                    r.data.mkdir(exist_ok=True)
                    self.compose("up", "-d", r.name)
                    r.wait_started(timeout=self.args.start_timeout)
                log(f"{r.name}: started (world seed {r.world_seed}, rng seed {r.rng_seed})")
            # detector pack text depends on the resolved anchor, so setup first
            threads = []
            for r in self.replicas:
                th = threading.Thread(target=self._setup_one, args=(r,), daemon=True)
                th.start()
                threads.append(th)
            for th in threads:
                th.join()
            failed = [r for r in self.replicas if getattr(r, "setup_error", None)]
            if failed:
                raise RuntimeError("setup failed on " +
                                   ", ".join(f"{r.name}: {r.setup_error}" for r in failed))
            base = {r.name: r.gametime() for r in self.replicas}
            self.setup_seconds = time.time() - t0
            log(f"setup done in {self.setup_seconds:.1f}s (boot + setup), tick base {base}")
            t_run = time.time()
            snap_every = scn["snapshots"]["every_ticks"] if scn["snapshots"]["enabled"] else 0
            for r in self.replicas:
                r.reseeds = reseed_schedule(scn["run"]["reseed"], self.ticks, r.rng_seed)
                if r.reseeds and not getattr(r, "reseed_supported", False):
                    log(f"{r.name}: WARNING run.reseed declares {len(r.reseeds)} checkpoints "
                        "but this server has no `detmc reseed` command; skipping them")
            done = {r.name: False for r in self.replicas}
            tick = 0
            while tick < self.ticks and not all(done.values()):
                step = min(interval, self.ticks - tick)
                tick += step
                threads = []
                for r in self.replicas:
                    if done[r.name]:
                        continue
                    th = threading.Thread(target=self._segment_one,
                                          args=(r, base[r.name] + tick, tick, snap_every, done),
                                          daemon=True)
                    th.start()
                    threads.append(th)
                for th in threads:
                    th.join()
                log(f"t+{tick}/{self.ticks}  " + "  ".join(
                    f"{r.name}:{r.hit_summary()}" for r in self.replicas))
            self.run_seconds = time.time() - t_run
            log(f"ticking done in {self.run_seconds:.1f}s for {tick} ticks "
                f"({tick / max(self.run_seconds, 1e-9):.0f} ticks/s per replica, "
                f"{self.n} replicas in parallel)")
            for r in self.replicas:
                r.rcon_soft("save-all")
                r.wall = time.time() - t0
                self._record(r, tick)
        except BaseException as exc:                   # noqa: BLE001 - re-raised
            status, error = 1, f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if not self.args.keep:
                log("tearing down")
                self.compose("down", check=False)
            # The marker is written in `finally`, so a detached run leaves one
            # either way: clean finish or crash.
            self.write_done(status, error, t0)
        return 0

    def _setup_one(self, r):
        """Record the exception: a thread that dies here used to leave the run going
        with no detector installed at all."""
        try:
            r.setup(self.world)
        except Exception as exc:                       # noqa: BLE001 - re-raised below
            r.setup_error = exc
            log(f"{r.name}: SETUP FAILED: {exc}")

    def _segment_one(self, r, target, tick, snap_every, done):
        r.goto(target)
        # Reseeds land on an exact tick boundary, while frozen, after the segment
        # that reaches them -- the same place every replica issues them.
        if getattr(r, "reseed_supported", False):
            for at, seed in getattr(r, "reseeds", []):
                if at <= tick and at not in r.reseeded:
                    r.reseed(seed)
                    r.reseeded.add(at)
        d = r.read_detector()
        if d:
            for name, sub in d["per_detector"].items():
                if sub["hit"] and name not in r.hits:
                    r.hits[name] = sub
                    log(f"{r.name}: HIT {name} at gametime {sub['hit_gametime']} "
                        f"(value {sub['value']})")
            r.hit = d if d["hit"] else None
            stop = self.scn["detector"]["stop_on_hit"]
            if (stop == "all" and d["all_hit"]) or (stop is True and d["hit"]):
                done[r.name] = True
        if snap_every and tick % snap_every == 0:
            r.snapshot(tick)

    def _record(self, r, tick):
        rec = {"run_id": self.run_id, "scenario": self.scn["name"], "replica": r.name,
               "world_seed": r.world_seed, "rng_seed": r.rng_seed,
               "detector": self.scn["detector"]["kind"],
               "detectors": [d["name"] for d in det_list(self.scn)],
               "baseline": r.baseline, "threshold": getattr(r, "threshold", None),
               "baselines": getattr(r, "baselines", {}),
               "thresholds": getattr(r, "thresholds", {}),
               "pack_commands": getattr(r, "pack_commands", None),
               "ticks_target": self.ticks, "gametime_reached": r.reached,
               "hit": bool(r.hits), "hit_gametime": (r.hit or {}).get("hit_gametime"),
               "detector_value": (r.hit or {}).get("value"),
               "hits": {k: v["hit_gametime"] for k, v in r.hits.items()},
               "reseeds_applied": sorted(r.reseeded),
               "reseed_supported": getattr(r, "reseed_supported", None),
               "wall_seconds": round(r.wall, 1),
               "setup_seconds": round(getattr(self, "setup_seconds", 0), 1),
               "run_seconds": round(getattr(self, "run_seconds", 0), 1),
               # ticks the segment loop actually advanced, per second of wall clock.
               # This is the number to extrapolate a long run from; `wall_seconds`
               # also carries the one-off container boot and the scripted setup.
               "run_ticks_per_second": (round(tick / self.run_seconds, 1)
                                        if getattr(self, "run_seconds", 0) else None),
               "data_dir": str(r.data), "container": r.container,
               "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if r.hits:
            rec["where"] = r.locate_hits()
        with self.results.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        log(f"{r.name}: {json.dumps(rec)}")

    def write_done(self, status, error, t0):
        """Write `runs/<run_id>/DONE`, the run's completion marker.

        A detached driver (`nohup run.py ... &`) is otherwise only observable by
        tailing its log, and "the log stopped moving" does not distinguish a
        finished run from a killed one.  This file appears exactly once, at the
        end, from `execute`'s finally block, so it covers the crash path too:
        `status` 0 means the tick loop completed, 1 means it raised and `error`
        says what.  Poll for the file, then read `hits_total` and
        `replica_summary`; `results.jsonl` still carries the full per-replica
        record.
        """
        summary = {r.name: {"rng_seed": r.rng_seed,
                            "gametime_reached": getattr(r, "reached", 0),
                            "hits": {k: v["hit_gametime"] for k, v in r.hits.items()}}
                   for r in self.replicas}
        done = {"run_id": self.run_id, "scenario": self.scn["name"],
                "status": status, "error": error,
                "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "wall_seconds": round(time.time() - t0, 1),
                "setup_seconds": round(getattr(self, "setup_seconds", 0), 1),
                "run_seconds": round(getattr(self, "run_seconds", 0), 1),
                "ticks_target": self.ticks, "replicas": self.n,
                "world_seed": int(self.world["world_seed"]),
                "results": str(self.results),
                "hits_total": sum(len(v["hits"]) for v in summary.values()),
                "replica_summary": summary}
        (self.dir / "DONE").write_text(json.dumps(done, indent=2) + "\n")
        log(f"wrote {self.dir / 'DONE'}: status {status}, "
            f"{done['hits_total']} detector hits")


def main():
    ap = argparse.ArgumentParser(description="detmc scenario runner")
    ap.add_argument("scenario")
    ap.add_argument("--replicas", type=int)
    ap.add_argument("--ticks", type=int)
    ap.add_argument("--run-id")
    ap.add_argument("--seeds", help="comma-separated world seeds, skips the prefilter")
    ap.add_argument("--keep", action="store_true", help="leave containers up after the run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start-timeout", type=int, default=900)
    ap.add_argument("--prefilter-timeout", type=int, default=3600)
    ap.add_argument("--segment-ticks", type=int, help="override run.segment_ticks")
    ap.add_argument("--memory", help="override server.memory (e.g. 1G)")
    ap.add_argument("--rng-seed-offset", type=int, default=0,
                    help="add N to every rng seed; how a 30-replica hunt is run as "
                         "three memory-safe waves of 10 (0, 10, 20)")
    ap.add_argument("--snapshot-every", type=int,
                    help="override snapshots.every_ticks (use for short proof runs)")
    ap.add_argument("--no-render", action="store_true", help="snapshot but do not render")
    ap.add_argument("--reseed", choices=("auto", "on", "off"), default="auto",
                    help="auto: use `detmc reseed` if the mod has it, warn if not; "
                         "on: require it; off: ignore run.reseed")
    args = ap.parse_args()
    scn = load_scenario(args.scenario)
    seeds = resolve_seeds(scn, args)
    log(f"world seed {seeds[0]['world_seed']}")
    return Run(scn, args, seeds).execute()


if __name__ == "__main__":
    sys.exit(main())
