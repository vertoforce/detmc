#!/usr/bin/env python3
"""Seed prefilter for the detmc Minecraft scenario runner.

Finds world seeds whose *world spawn* satisfies the scenario's
``world.requirements`` block: an allowed spawn biome plus a set of structures
within a maximum distance of spawn.

Two backends:

``cubiomes``
    Runs the ``mc-cubiomes`` Docker image (see ``prefilter/Dockerfile``), which
    reimplements Minecraft worldgen in C. Milliseconds per seed. Cubiomes has
    no 26.2 support (its newest version is "1.21 WD"), so results are reported
    with ``"version_mismatch": true`` and must be verified on a real server.

``locate``
    Fallback. Boots one throwaway vanilla server per candidate seed and uses
    rcon ``locate structure`` / ``locate biome``. Authoritative for the target
    version but ~1-2 minutes per seed.

Output (stdout, one JSON object)::

    {"method": "...", "mc_version": "...", "version_mismatch": bool,
     "checked": N, "seeds": [{"world_seed": .., "spawn": [x, z],
                              "structures": {"<id>": [x, y, z]}}, ...]}

Exits non-zero if fewer than ``--count`` seeds were found.

Run with the runner venv: ``runner/.venv/bin/python runner/prefilter.py ...``
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PREFILTER_DIR = os.path.join(HERE, "prefilter")
DOCKERFILE = os.path.join(PREFILTER_DIR, "Dockerfile")
VERIFY_DIR = os.path.join(PREFILTER_DIR, "verify")
LOCATE_CONTAINER = "mc-run-prefilter-locate"

# Fallback MC version when the scenario does not name one.
DEFAULT_MC_VERSION = "26.2"


def log(msg: str) -> None:
    print(f"prefilter: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# scenario parsing
# --------------------------------------------------------------------------

def load_requirements(path: str) -> tuple[dict, str]:
    """Return (requirements, mc_version) from a scenario YAML."""
    with open(path) as fh:
        doc = yaml.safe_load(fh) or {}
    world = doc.get("world") or {}
    req = world.get("requirements") or {}

    spawn_biome = req.get("spawn_biome") or []
    if isinstance(spawn_biome, str):
        spawn_biome = [spawn_biome]

    structures = []
    for s in req.get("structures") or []:
        if isinstance(s, str):
            structures.append({"id": s, "within": 0})
        else:
            structures.append({"id": s["id"], "within": int(s.get("within", 0))})

    out = {
        "spawn_biome": list(spawn_biome),
        "spawn_biome_radius": int(req.get("spawn_biome_radius") or 0),
        "structures": structures,
    }

    # The MC version may live in a few places depending on the scenario author.
    mc = (
        doc.get("mc_version")
        or doc.get("minecraft_version")
        or (doc.get("minecraft") or {}).get("version")
        or world.get("version")
        or (doc.get("server") or {}).get("version")
        or DEFAULT_MC_VERSION
    )
    return out, str(mc)


# --------------------------------------------------------------------------
# cubiomes backend
# --------------------------------------------------------------------------

def cubiomes_commit() -> str:
    """Pinned cubiomes commit, read from the Dockerfile (single source of truth)."""
    with open(DOCKERFILE) as fh:
        for line in fh:
            m = re.match(r"\s*ARG\s+CUBIOMES_COMMIT=(\S+)", line)
            if m:
                return m.group(1)
    raise RuntimeError(f"no ARG CUBIOMES_COMMIT in {DOCKERFILE}")


def cubiomes_image() -> str:
    return f"mc-cubiomes:{cubiomes_commit()[:7]}"


def cubiomes_available(build: bool = True) -> bool:
    image = cubiomes_image()
    if subprocess.run(["docker", "image", "inspect", image],
                      stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode == 0:
        return True
    if not build:
        return False
    log(f"building {image} from {DOCKERFILE} ...")
    return subprocess.run(["docker", "build", "-t", image, PREFILTER_DIR],
                          stdout=sys.stderr.fileno(),
                          stderr=sys.stderr.fileno()).returncode == 0


# --------------------------------------------------------------------------
# candidate seeds
# --------------------------------------------------------------------------
#
# `range(start_seed, start_seed + max_seeds)` is a perfectly good way to find a
# seed and a bad way to publish one: a world seed of 12345 or 12 is
# indistinguishable from a placeholder somebody meant to change.  The default is
# now a fixed-master 64-bit draw, which looks like a seed and is still exactly
# reproducible -- `random.Random(master)` is a Mersenne Twister whose output is
# specified by CPython, and the master goes in the result and in run.py's
# manifest, so any accepted seed can be re-derived from the two numbers alone
# without re-running the search.
SEED_MASTER = 0x6d65742d64657403        # the same master run.py uses


def candidate_seeds(source: str, master: int, start_seed: int, n: int):
    if source == "sequential":
        return [start_seed + i for i in range(n)]
    rnd = random.Random(master)
    out = []
    for _ in range(n):
        v = rnd.getrandbits(64)
        out.append(v - (1 << 64) if v >= (1 << 63) else v)   # Java long
    return out


def run_cubiomes(req: dict, mc_version: str, count: int,
                 start_seed: int, max_seeds: int,
                 seed_source: str = "random", seed_master: int = SEED_MASTER) -> dict:
    """Cubiomes over a candidate list.

    The scan program walks a consecutive range, so a random candidate list is one
    `--count 1` invocation per candidate.  That is ~0.1 s of docker overhead each
    and the acceptance rate for "a village within N blocks of spawn" is high
    (measured: 1 of the first 12 sequential seeds for `village_plains within 200`),
    so the whole search is still seconds.  A sequential run keeps the old single
    invocation.
    """
    if seed_source == "sequential":
        return _run_cubiomes_range(req, mc_version, count, start_seed, max_seeds,
                                   seed_source, seed_master)
    cands = candidate_seeds(seed_source, seed_master, start_seed, max_seeds)
    seeds, checked, mcv, mism = [], 0, "unknown", True
    for s in cands:
        r = _run_cubiomes_range(req, mc_version, 1, s, 1, seed_source, seed_master)
        checked += 1
        mcv, mism = r["mc_version"], r["version_mismatch"]
        if r["seeds"]:
            seeds += r["seeds"]
            log(f"candidate {checked} seed {s} accepted ({len(seeds)}/{count})")
            if len(seeds) >= count:
                break
    return {"method": "cubiomes", "mc_version": mcv, "version_mismatch": mism,
            "checked": checked, "seeds": seeds,
            "seed_source": seed_source, "seed_master": seed_master}


def _run_cubiomes_range(req: dict, mc_version: str, count: int,
                        start_seed: int, max_seeds: int,
                        seed_source: str = "sequential",
                        seed_master: int = SEED_MASTER) -> dict:
    image = cubiomes_image()
    cmd = ["docker", "run", "--rm", image,
           "--mc", mc_version,
           "--start", str(start_seed),
           "--count", str(max_seeds),
           "--accept", str(count)]
    if req["spawn_biome"]:
        cmd += ["--spawn-biome", ",".join(req["spawn_biome"])]
    if req["spawn_biome_radius"]:
        cmd += ["--spawn-radius", str(req["spawn_biome_radius"])]
    for s in req["structures"]:
        cmd += ["--struct", f"{s['id']}:{s['within']}"]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stderr.strip():
        for line in proc.stderr.strip().splitlines():
            log(line)
    if proc.returncode != 0:
        raise RuntimeError(f"seedscan failed (exit {proc.returncode})")

    seeds, summary = [], {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("_summary"):
            summary = obj
        else:
            seeds.append(obj)

    return {
        "method": "cubiomes",
        "mc_version": summary.get("mc_version", "unknown"),
        "version_mismatch": bool(summary.get("version_mismatch", True)),
        "checked": int(summary.get("checked", 0)),
        "seeds": seeds,
        "seed_source": seed_source,
        "seed_master": seed_master,
    }


# --------------------------------------------------------------------------
# locate backend (throwaway vanilla server, slow fallback)
# --------------------------------------------------------------------------

LOCATE_RE = re.compile(
    r"is at \[?\s*(-?\d+)\s*,\s*(-?\d+|~)\s*,\s*(-?\d+)\s*\]?", re.I)
SPAWN_RE = re.compile(r"world spawn point to (-?\d+)[, ]+(-?\d+)[, ]+(-?\d+)", re.I)


def rcon(cmd: str, container: str = LOCATE_CONTAINER, timeout: int = 120) -> str:
    proc = subprocess.run(["docker", "exec", container, "rcon-cli", cmd],
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"rcon '{cmd}' failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def locate_server_up(seed: int, mc_version: str, boot_timeout: int = 600) -> None:
    """Boot the throwaway server on a fresh world for `seed`."""
    compose = os.path.join(VERIFY_DIR, "docker-compose.yml")
    data = os.path.join(VERIFY_DIR, "data")
    subprocess.run(["docker", "compose", "-f", compose, "down", "-v", "--remove-orphans"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.isdir(data):
        shutil.rmtree(data, ignore_errors=True)
    os.makedirs(data, exist_ok=True)

    env = dict(os.environ, MC_SEED=str(seed), MC_VERSION=mc_version)
    subprocess.run(["docker", "compose", "-f", compose, "up", "-d"],
                   check=True, env=env, cwd=VERIFY_DIR,
                   stdout=sys.stderr.fileno(), stderr=sys.stderr.fileno())

    log(f"waiting for {LOCATE_CONTAINER} rcon (seed {seed}, up to {boot_timeout}s) ...")
    deadline = time.time() + boot_timeout
    while time.time() < deadline:
        try:
            if "players online" in rcon("list", timeout=30).lower():
                log("server is up")
                return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError(f"{LOCATE_CONTAINER} did not come up within {boot_timeout}s")


def locate_server_down() -> None:
    subprocess.run(["docker", "compose", "-f",
                    os.path.join(VERIFY_DIR, "docker-compose.yml"),
                    "down", "-v", "--remove-orphans"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def locate_world_spawn() -> tuple[int, int]:
    """Read the world spawn via `setworldspawn` with no arguments.

    The server console's command source sits at the overworld spawn, so this is
    a no-op that echoes the coordinates back ("Set the world spawn point to
    X, Y, Z ..."). Unlike summoning a marker it also works under `tick freeze`,
    where new entities are not added to the world until a tick runs.
    """
    out = rcon("setworldspawn")
    m = SPAWN_RE.search(out)
    if not m:
        raise RuntimeError(f"cannot parse world spawn from: {out!r}")
    return int(m.group(1)), int(m.group(3))


def locate_probe(kind: str, ident: str, at: tuple[int, int] | None = None):
    """`locate structure|biome <ident>` -> (x, y, z) or None. y is 0 for '~'."""
    cmd = f"locate {kind} {ident}"
    if at is not None:
        cmd = f"execute positioned {at[0]}.0 70.0 {at[1]}.0 run " + cmd
    out = rcon(cmd)
    m = LOCATE_RE.search(out)
    if not m:
        return None
    x, y, z = m.group(1), m.group(2), m.group(3)
    return int(x), (0 if y == "~" else int(y)), int(z)


def locate_nearest_structure(ident: str, sx: int, sz: int, within: int):
    """Best-effort nearest instance of `ident` to (sx, sz).

    `/locate structure` walks the structure placement grid ring by ring and
    returns the first viable hit, which is NOT necessarily the closest in
    blocks (measured: seed 12 spawn (-16,-48) reports a village 229 blocks away
    while another exists 193 blocks away in the neighbouring placement region).
    Probing from a few offsets around spawn and keeping the closest hit works
    around that.
    """
    offs = [(0, 0)]
    if within:
        for dx in (-within, 0, within):
            for dz in (-within, 0, within):
                if (dx, dz) != (0, 0):
                    offs.append((dx, dz))
    best = None
    best_d = None
    for dx, dz in offs:
        hit = locate_probe("structure", ident, at=(sx + dx, sz + dz))
        if not hit:
            continue
        d = math.hypot(hit[0] - sx, hit[2] - sz)
        if best_d is None or d < best_d:
            best, best_d = hit, d
    return best, best_d


def check_seed_locate(seed: int, req: dict, mc_version: str) -> dict | None:
    """Boot a server on `seed` and test the requirements. Returns a hit or None."""
    locate_server_up(seed, mc_version)
    try:
        rcon("tick freeze")
        sx, sz = locate_world_spawn()

        # Spawn-biome check: `locate biome` measures from the console source,
        # i.e. the world spawn. Accept if an allowed biome is found within the
        # spawn radius (default 32 blocks; locate biome samples coarsely).
        tol = req["spawn_biome_radius"] or 32
        biome_ok = not req["spawn_biome"]
        for b in req["spawn_biome"]:
            hit = locate_probe("biome", b)
            if hit and math.hypot(hit[0] - sx, hit[2] - sz) <= tol:
                biome_ok = True
                break
        if not biome_ok:
            return None

        structures = {}
        for s in req["structures"]:
            hit, dist = locate_nearest_structure(s["id"], sx, sz, s["within"])
            if not hit:
                return None
            if s["within"] and dist > s["within"]:
                return None
            structures[s["id"]] = [hit[0], hit[1], hit[2]]

        return {"world_seed": seed, "spawn": [sx, sz], "structures": structures}
    finally:
        locate_server_down()


def run_locate(req: dict, mc_version: str, count: int,
               start_seed: int, max_seeds: int,
               seed_source: str = "random", seed_master: int = SEED_MASTER) -> dict:
    log("using the 'locate' backend: ONE throwaway server per candidate seed. "
        "This is the slow fallback (~1-2 min/seed).")
    seeds, checked = [], 0
    for seed in candidate_seeds(seed_source, seed_master, start_seed, max_seeds):
        checked += 1
        hit = check_seed_locate(seed, req, mc_version)
        if hit:
            seeds.append(hit)
            log(f"seed {seed} accepted ({len(seeds)}/{count})")
            if len(seeds) >= count:
                break
    return {
        "method": "locate",
        "mc_version": mc_version,
        "version_mismatch": False,
        "checked": checked,
        "seeds": seeds,
        "seed_source": seed_source,
        "seed_master": seed_master,
    }


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="detmc seed prefilter")
    ap.add_argument("--scenario", required=True, help="scenario YAML")
    ap.add_argument("--count", type=int, required=True, help="seeds wanted")
    ap.add_argument("--start-seed", type=int, default=1)
    ap.add_argument("--max-seeds", type=int, default=100000,
                    help="max consecutive seeds to test")
    ap.add_argument("--json", dest="json_out", help="also write the result here")
    ap.add_argument("--method", choices=["auto", "cubiomes", "locate"],
                    default="auto")
    ap.add_argument("--seed-source", choices=["random", "sequential"],
                    default="random",
                    help="random (default): candidates are "
                         "random.Random(--seed-master).getrandbits(64) folded to a "
                         "Java long, so the list is reproducible and the seeds look "
                         "like seeds.  sequential: the old start_seed.. walk")
    ap.add_argument("--seed-master", type=lambda v: int(v, 0), default=SEED_MASTER,
                    help="master value for --seed-source random")
    args = ap.parse_args(argv)

    req, mc_version = load_requirements(args.scenario)

    method = args.method
    if method == "auto":
        method = "cubiomes" if cubiomes_available() else "locate"
        log(f"method auto -> {method}")
    elif method == "cubiomes" and not cubiomes_available():
        log(f"ERROR: cannot build {cubiomes_image()}")
        return 3

    if method == "cubiomes":
        result = run_cubiomes(req, mc_version, args.count,
                              args.start_seed, args.max_seeds,
                              args.seed_source, args.seed_master)
    else:
        result = run_locate(req, mc_version, args.count,
                            args.start_seed, args.max_seeds,
                            args.seed_source, args.seed_master)

    if result["version_mismatch"]:
        log("WARNING: worldgen was evaluated with cubiomes' newest supported "
            f"version '{result['mc_version']}', NOT the requested "
            f"'{mc_version}'. Seeds are candidates only -- verify on a real "
            f"{mc_version} server (--method locate) before trusting them.")

    payload = json.dumps(result)
    print(payload)
    if args.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(args.json_out)), exist_ok=True)
        with open(args.json_out, "w") as fh:
            fh.write(payload + "\n")

    if len(result["seeds"]) < args.count:
        log(f"ERROR: found {len(result['seeds'])} of {args.count} requested "
            f"seeds after checking {result['checked']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
