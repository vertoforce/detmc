#!/usr/bin/env python3
"""How much world is worth loading: a sizing sweep for natural-map scenarios.

`run.py`'s built-arena scenarios need no player and one small forceload, so their
only sizing question is how many replicas fit in RAM.  A natural-map scenario is the
opposite shape: the event rate is proportional to how many eligible mobs are alive
near the thing being watched, and that population is set by two knobs that also cost
CPU -- `simulation-distance` and how many (creative, fake) players are standing in
the world.

    ServerChunkCache.tickChunks -> ChunkMap.collectSpawningChunks: a chunk is a
    SPAWNING chunk only if it is entity-ticking (Chebyshev <= simulation-distance of
    some player's chunk), inside the DistanceManager's radius-8 natural-spawn
    tracker, and within 128 blocks of a non-spectator player
    (ChunkMap.playerIsCloseEnoughForSpawning).  The MONSTER cap is
    MobCategory.maxInstancesPerChunk x spawnableChunkCount / MAGIC_NUMBER, and
    LocalMobCapCalculator also caps per player -- so N players far enough apart is
    ~N times the monster population, while a larger simulation distance SPREADS the
    same per-player cap over more chunks.

Both of those are predictions.  This measures them: for each (sim distance, players)
it runs one server on a natural seed, sprints 24,000 ticks with natural spawning on
at night, and reports ticks/s, the mob census, the loaded-chunk count and RSS, then
derives the two efficiency numbers a fleet should actually maximise:

    spawning-chunk-ticks / core-second   how much eligible WORLD is simulated
    mob-ticks / core-second              how much eligible POPULATION is simulated

Usage:
    .venv/bin/python bench_worldsize.py --run-id bench1 \\
        --sim 4,6,10 --players 1,2,4,8 --ticks 24000 --seed <long>

One server at a time by default (`--concurrency 1`), because the point is a
throughput measurement and two of them would measure each other.
"""
import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

import memgate
import run as runner

RUNNER = Path(__file__).resolve().parent

# Per-type counts rather than one tag: `#minecraft:...` has no "every monster" tag in
# 26.2, and the split is worth having anyway -- the monster cap and the worldgen
# animal population are different mechanisms with different responses to the knobs.
MONSTERS = ("zombie", "skeleton", "creeper", "spider", "cave_spider", "enderman",
            "witch", "slime", "husk", "drowned", "phantom", "zombie_villager")
ANIMALS = ("cow", "pig", "sheep", "chicken", "horse", "donkey", "rabbit", "fox",
           "wolf", "llama", "goat")


def sh(cmd, check=True, timeout=600):
    return runner.sh(cmd, check=check, timeout=timeout)


class Bench:
    def __init__(self, args):
        self.args = args
        self.dir = RUNNER / "runs" / args.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rows = []

    # -- one container ----------------------------------------------------
    def compose_for(self, name, sim, memory):
        mods = self.dir / "mods"
        mods.mkdir(exist_ok=True)
        jars = sorted((RUNNER.parent / "fabric" / "build" / "libs").glob("detmc-*.jar"))
        if not jars:
            raise RuntimeError("no detmc jar; run ../docker-gradle.sh :fabric:build")
        shutil.copy(jars[-1], mods / jars[-1].name)
        if not runner.CARPET_JAR.exists():
            raise RuntimeError(f"{runner.CARPET_JAR} missing; run ./fetch-carpet.sh")
        shutil.copy(runner.CARPET_JAR, mods / runner.CARPET_JAR.name)
        data = self.dir / f"data-{name}"
        data.mkdir(exist_ok=True)
        scn = {"server": {"difficulty": "easy", "simulation_distance": sim,
                          "view_distance": sim, "spawn_monsters": True,
                          "memory": memory}}
        svc = runner.service_spec(scn, f"mc-bench-{self.args.run_id}-{name}",
                                  f"./data-{name}", self.args.seed,
                                  self.args.rng_seed, "./mods")
        (self.dir / f"compose-{name}.yml").write_text(
            yaml.safe_dump({"services": {name: svc}}, sort_keys=False))
        if not (self.dir / ".env").exists():
            (self.dir / ".env").write_text(f"RCON_PASSWORD={secrets.token_hex(16)}\n")
        return self.dir / f"compose-{name}.yml"

    def compose(self, f, *a, check=True):
        return sh(["docker", "compose", "-f", str(f), "--env-file",
                   str(self.dir / ".env")] + list(a), check=check)

    # -- one (sim, players) cell ------------------------------------------
    def cell(self, sim, nplayers):
        name = f"s{sim}p{nplayers}"
        cont = f"mc-bench-{self.args.run_id}-{name}"
        logf = self.dir / f"{name}.log"
        f = self.compose_for(name, sim, self.args.memory)
        # One server at a time is not enough on its own: the search containers share
        # this host, and it once took a global OOM when several launchers each
        # looked at MemAvailable independently.  memgate.py is
        # the shared budget; it blocks until this cell's RSS fits with the host's
        # reserve intact, and the lock is released before the container starts.
        memgate.acquire(memgate.job_need_mb(self.args.memory))
        t0 = time.time()
        self.compose(f, "up", "-d")

        def rcon(cmd, check=False, timeout=300):
            out = sh(["docker", "exec", cont, "rcon-cli", cmd], check=check,
                     timeout=timeout)
            with logf.open("a") as fh:
                fh.write(f">>> {cmd}\n{out}\n")
            return out

        def count(sel):
            rcon(f"execute store result score #b detmc if entity {sel}")
            m = re.search(r"has (-?\d+)", rcon("scoreboard players get #b detmc"))
            return int(m.group(1)) if m else 0

        try:
            for _ in range(600):
                if "Done (" in sh(["docker", "logs", cont], check=False):
                    break
                time.sleep(1)
            else:
                raise TimeoutError(f"{cont} never started")
            boot = time.time() - t0
            rcon("tick freeze")
            rcon("scoreboard objectives add detmc dummy")
            # Natural spawning at night, weather cycling, nothing built.  `time set
            # 13000` is just after dusk so the monster cap fills inside the sprint
            # instead of after it.
            for rule, val in (("spawn_mobs", "true"), ("advance_time", "true"),
                              ("advance_weather", "true"), ("random_tick_speed", "3"),
                              ("spawn_patrols", "false"),
                              ("spawn_wandering_traders", "false"),
                              ("spawn_phantoms", "false"), ("mob_griefing", "true")):
                rcon(f"gamerule {rule} {val}")
            rcon("time set 13000")
            rcon("weather thunder 1000000")
            sx, sz = self.world_spawn(rcon)
            # Creative fake players, >= `--spread` blocks apart so their radius-8
            # spawn trackers barely overlap and each brings its own population.
            # Creative for the same reason the scenarios use it: GameType sets
            # abilities.invulnerable and Player.canBeSeenAsEnemy:724 is
            # `!invulnerable && super...`, so nothing targets them, while
            # ChunkMap.playerIsCloseEnoughForSpawning only excludes spectators.
            spread = self.args.spread
            pos = []
            for i in range(nplayers):
                px = sx + (i % 4) * spread
                pz = sz + (i // 4) * spread
                pos.append((px, pz))
                rcon(f"forceload add {px - 16} {pz - 16} {px + 16} {pz + 16}")
            time.sleep(10)
            for i, (px, pz) in enumerate(pos):
                y = self.surface_y(rcon, px, pz)
                rcon(f"player bench{i} spawn at {px} {y + 1} {pz}")
                rcon(f"gamemode creative bench{i}")
            online = rcon("list")
            loaded = self.loaded_chunks(rcon, pos, sim)
            # sprint
            since = int(time.time()) - 1
            rcon("tick unfreeze")
            ts = time.time()
            rcon(f"tick sprint {self.args.ticks}")
            for _ in range(20000):
                if "Sprint completed" in sh(["docker", "logs", "--since", str(since),
                                             cont], check=False):
                    break
                time.sleep(0.5)
            tick_s = time.time() - ts
            rcon("tick freeze")
            mobs = {t: count(f"@e[type=minecraft:{t}]") for t in MONSTERS}
            animals = {t: count(f"@e[type=minecraft:{t}]") for t in ANIMALS}
            villagers = count("@e[type=minecraft:villager]")
            rss = self.rss(cont)
            cpu = self.cpu_seconds(cont)
            row = {
                "sim": sim, "players": nplayers, "boot_seconds": round(boot, 1),
                "ticks": self.args.ticks, "tick_seconds": round(tick_s, 1),
                "ticks_per_second": round(self.args.ticks / tick_s, 1),
                "loaded_chunks": loaded,
                "monsters": sum(mobs.values()), "monsters_by_type": mobs,
                "animals": sum(animals.values()), "animals_by_type": animals,
                "villagers": villagers,
                "endermen": mobs.get("enderman", 0),
                "rss_gib": rss, "cpu_seconds": cpu,
                "players_online": online.strip()[:200],
            }
            # The two efficiency numbers.  `cpu_seconds` is the container's own CPU
            # time over the whole cell, so these are per CORE-second and therefore
            # comparable across cells that used a different number of cores.
            if cpu:
                row["spawning_chunk_ticks_per_core_second"] = round(
                    loaded * self.args.ticks / cpu, 0)
                row["mob_ticks_per_core_second"] = round(
                    (row["monsters"] + row["animals"]) * self.args.ticks / cpu, 0)
            self.rows.append(row)
            (self.dir / "results.jsonl").open("a").write(json.dumps(row) + "\n")
            runner.log(f"{name}: {row['ticks_per_second']} ticks/s, "
                       f"{loaded} loaded chunks, {row['monsters']} monsters "
                       f"({row['endermen']} endermen), {row['animals']} animals, "
                       f"RSS {rss} GiB, {cpu}s CPU")
            return row
        finally:
            if not self.args.keep:
                self.compose(f, "down", "-v", check=False)

    # -- readings ---------------------------------------------------------
    @staticmethod
    def world_spawn(rcon):
        rcon('summon minecraft:marker ~ ~ ~ {Tags:["bench_spawn"]}')
        out = rcon("data get entity @e[type=minecraft:marker,tag=bench_spawn,limit=1] Pos")
        nums = re.findall(r"-?\d+\.?\d*", out.split("Pos:")[-1])
        rcon("kill @e[type=minecraft:marker,tag=bench_spawn]")
        return int(float(nums[0])), int(float(nums[2]))

    @staticmethod
    def surface_y(rcon, x, z, lo=-60, hi=310):
        skip = ("#minecraft:replaceable", "#minecraft:leaves", "#minecraft:logs")
        while lo < hi:
            mid = (lo + hi + 1) // 2
            conds = " ".join(f"unless block {x} {mid} {z} {b}" for b in skip)
            if "Test passed" in rcon("execute " + conds):
                lo = mid
            else:
                hi = mid - 1
        return lo

    @staticmethod
    def loaded_chunks(rcon, positions, sim):
        """`execute if loaded <pos>` over a chunk grid around every player.

        There is no rcon read for "how many chunks are loaded" in vanilla 26.2, and
        `forceload query` only answers about forceloads, so this probes instead: one
        round trip per candidate chunk over a (sim+3) radius square around each
        player, de-duplicated.  ~0.17 s per probe measured, so a sim-10 cell is
        ~2 min of probing -- which is why it is done once per cell and not per
        segment."""
        seen = set()
        r = sim + 3
        for px, pz in positions:
            cx0, cz0 = px >> 4, pz >> 4
            for cx in range(cx0 - r, cx0 + r + 1):
                for cz in range(cz0 - r, cz0 + r + 1):
                    if (cx, cz) in seen:
                        continue
                    if "Test passed" in rcon(
                            f"execute if loaded {cx * 16 + 8} 64 {cz * 16 + 8}"):
                        seen.add((cx, cz))
        return len(seen)

    @staticmethod
    def rss(cont):
        out = sh(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}",
                  cont], check=False)
        m = re.search(r"([\d.]+)\s*([GM])iB", out)
        if not m:
            return None
        v = float(m.group(1))
        return round(v if m.group(2) == "G" else v / 1024, 2)

    @staticmethod
    def cpu_seconds(cont):
        """Container CPU seconds, read from cgroup v2 rather than sampled: `docker
        stats` gives an instantaneous percentage, which cannot be turned into
        core-seconds after the fact."""
        out = sh(["docker", "exec", cont, "cat", "/sys/fs/cgroup/cpu.stat"],
                 check=False)
        m = re.search(r"usage_usec (\d+)", out)
        return round(int(m.group(1)) / 1e6, 1) if m else None

    # -- sweep ------------------------------------------------------------
    def sweep(self):
        for sim in self.args.sim:
            for n in self.args.players:
                try:
                    self.cell(sim, n)
                except Exception as exc:                       # noqa: BLE001
                    runner.log(f"s{sim}p{n}: FAILED {type(exc).__name__}: {exc}")
        (self.dir / "summary.json").write_text(json.dumps(self.rows, indent=2))
        self.table()

    def table(self):
        hdr = ("| sim | players | ticks/s | loaded chunks | monsters | endermen | "
               "animals | RSS GiB | CPU s | chunk-ticks/core-s | mob-ticks/core-s |")
        print(hdr)
        print("|" + "---|" * 11)
        for r in self.rows:
            print(f"| {r['sim']} | {r['players']} | {r['ticks_per_second']} | "
                  f"{r['loaded_chunks']} | {r['monsters']} | {r['endermen']} | "
                  f"{r['animals']} | {r['rss_gib']} | {r['cpu_seconds']} | "
                  f"{r.get('spawning_chunk_ticks_per_core_second')} | "
                  f"{r.get('mob_ticks_per_core_second')} |")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--sim", default="4,6,10",
                    type=lambda v: [int(x) for x in v.split(",")])
    ap.add_argument("--players", default="1,2,4,8",
                    type=lambda v: [int(x) for x in v.split(",")])
    ap.add_argument("--ticks", type=int, default=24000)
    ap.add_argument("--seed", type=int, default=3795784043239602249,
                    help="world seed; default is the villager-trap hunt's world")
    ap.add_argument("--rng-seed", type=int, default=1451989592335860511)
    ap.add_argument("--memory", default="2G")
    ap.add_argument("--spread", type=int, default=384,
                    help="blocks between fake players (>= 300 so their radius-8 "
                         "spawn trackers barely overlap)")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    Bench(args).sweep()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
