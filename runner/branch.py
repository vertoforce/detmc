#!/usr/bin/env python3
"""branch.py -- a go-explore style branching search on top of run.py.

`run.py` runs N independent replicas to the end of a tick budget.  That is the
right shape for measuring a rate and the wrong shape for FINDING something rare:
every replica spends its whole budget on whatever its own seed happened to do,
including the 90% of them that are going nowhere.

This drives the same scenario as a beam search over saved worlds instead:

    generation 0   N replicas from the base world, one `-Ddetmc.seed` each
    checkpoint     freeze, `function detmc:score`, read the progress metrics
    select         keep the top K worlds (a full world save copy each)
    branch         M children per kept world: resume the copy, `/detmc reseed <s>`
    prune          everything else is deleted
    repeat         G generations, or until a detector fires

A branch is therefore described entirely by

    base rng seed  +  [(gametime, reseed), (gametime, reseed), ...]

where every gametime is a RESUME POINT: the tick at which that world was saved,
restarted from the save and reseeded.  That is not a cosmetic detail.  Vanilla
serialises no `RandomSource` draw position, so a reloaded world can never be
bit-identical to one that was never interrupted (STATUS.md, "Persistence"); a
schedule written as "reseed at tick T of one continuous run" would not replay.
Written as resume points it does, because the replay performs the same
save/restart/reseed at the same ticks.

Usage
    branch.py search scenarios/enderman_hunt.yaml --replicas 4 --generations 2 \
        --segment-ticks 3000 --keep-top 2 --children 2 --concurrency 10
    branch.py replay runs/<run>/lineage.jsonl g2n0 --run-id <run>-replay
    branch.py compare runs/<a>/dumps/g2n0.txt runs/<b>/dumps/g2n0.txt
"""
import argparse
import gzip
import hashlib
import json
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

import yaml

import memgate
import run as runner
from run import log, sh

RUNNER = Path(__file__).resolve().parent

# Measured on this box (runner/README.md, "RAM is the binding constraint"):
# a replica's container RSS tracks the heap setting, 1.46-1.61 GiB at MEMORY=1G.
RSS_PER_REPLICA_GIB = 1.5


def parse_gametimes(spec):
    """`1000,2000` or `2338000:2352000:350` (inclusive) -> sorted ints.

    The range form exists because a time-lapse wants 40 evenly spaced stops and
    typing them out is 300 characters of place for a typo to hide in."""
    out = set()
    for tok in (spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            a, b, step = (int(x) for x in tok.split(":"))
            if step <= 0:
                raise SystemExit(f"--frames-at {tok}: step must be positive")
            out.update(range(a, b + 1, step))
        else:
            out.add(int(tok))
    return sorted(out)


# --------------------------------------------------------------------------- node

class Node:
    """One world in the search: a base seed plus the resume points that made it."""

    def __init__(self, nid, gen, ordinal, rng_seed, parent=None, reseed=None,
                 schedule=(), resume_gametime=None):
        self.id = nid
        self.gen = gen
        self.ordinal = ordinal
        self.rng_seed = rng_seed            # -Ddetmc.seed, constant along a lineage
        self.parent = parent                # parent Node id, or None for generation 0
        self.reseed = reseed                # the `/detmc reseed` argument at resume
        self.schedule = list(schedule)      # [{"gametime": G, "reseed": S}, ...]
        self.resume_gametime = resume_gametime
        self.abs_target = None              # replay: the leg's recorded end gametime
        self.base_gametime = None
        self.target_gametime = None
        self.final_gametime = None
        self.scores, self.rank, self.hits = {}, 0, {}
        self.where = {}                     # hit coordinates, when a stop caught them
        self.frames = {}                    # gametime -> the world copy written there
        self.error = None
        self.timings = {}
        self.fake_players = []              # what Replica.spawn_fake_players did here

    def child_schedule(self, gametime, reseed):
        return self.schedule + [{"gametime": int(gametime), "reseed": int(reseed)}]

    def record(self, extra=None):
        rec = {"node": self.id, "generation": self.gen, "parent": self.parent,
               "base_rng_seed": self.rng_seed, "reseed": self.reseed,
               "base_gametime": self.base_gametime,
               "resume_at_gametime": self.resume_gametime,
               "schedule": self.schedule, "final_gametime": self.final_gametime,
               "rank": self.rank, "scores": self.scores, "hits": self.hits,
               "where": self.where,
               "fake_players": self.fake_players,
               "error": str(self.error) if self.error else None,
               "timings": self.timings}
        if self.frames:
            rec["frames"] = self.frames
        rec.update(extra or {})
        return rec


def last_usable_generation(recs, has_checkpoint):
    """-> (generation, its usable records), or (None, []).

    The last FULLY USABLE generation is the highest one with at least 2 records
    that finished without an error and whose checkpoint is still on disk -- which
    after a normal generation is exactly the `--keep-top` worlds, because the
    search prunes the rest as soon as it has ranked them.  Two is the floor: a
    beam of one is a single lineage with no branching left.

    `has_checkpoint(node_id) -> bool` is passed in so the same rule answers the
    question for this run's directory (`--resume`) and for somebody else's
    (`--resume-from`, which has to know what to copy before the run dir exists).
    """
    by_gen = {}
    for r in recs:
        by_gen.setdefault(int(r.get("generation") or 0), []).append(r)
    for gen in sorted(by_gen, reverse=True):
        ok = [r for r in by_gen[gen]
              if not r.get("error") and r.get("rank") is not None
              and has_checkpoint(r["node"])]
        if len(ok) >= 2:
            return gen, ok
    return None, []


def clone_for_resume(src_id, dst_id):
    """Seed runs/<dst_id> from runs/<src_id> so `--resume` can continue it there.

    `--resume` appends to the run it continues.  That is the wrong thing when the
    source run is a RESULT: hunt-br-wave1 holds the only two observations of the
    3-high pillar, and a continuation with a different detector set would bury
    them under 130 more generations of a different experiment in the same
    lineage.jsonl.  So the continuation gets its own run id, and this copies the
    four things a resume actually reads:

      lineage.jsonl   the schedules, so a wave-2 node still replays from tick 0
      the checkpoints of the last usable generation (the new beam's parents)
      template/       the server install, hard-linked into every node's /data;
                      without it all 8 containers re-download Fabric every wave
      manifest.json   as manifest-source.json, provenance only

    The source run is never written to -- not even a hard link into its template,
    which a container could in principle write through.
    """
    src, dst = RUNNER / "runs" / src_id, RUNNER / "runs" / dst_id
    if not (src / "lineage.jsonl").exists():
        raise SystemExit(f"--resume-from {src_id}: no {src / 'lineage.jsonl'}")
    # An empty dir holding nothing but a log is fine, and is exactly what the
    # documented launch line leaves behind: `mkdir -p runs/<id>` then
    # `> runs/<id>/driver.log` both run before this does.  Anything else means the
    # run id is already in use, and clobbering a run dir is not recoverable.
    inuse = sorted(p.name for p in dst.glob("*")) if dst.exists() else []
    inuse = [n for n in inuse if not n.endswith(".log")]
    if inuse:
        raise SystemExit(f"--resume-from: {dst} already exists and holds {inuse}; "
                         f"pick another --run-id")
    recs = [json.loads(l) for l in (src / "lineage.jsonl").read_text().splitlines()
            if l.strip()]
    gen, ok = last_usable_generation(
        recs, lambda n: (src / "checkpoints" / n / "meta.json").exists())
    if gen is None:
        raise SystemExit(f"--resume-from {src_id}: no generation has 2 usable "
                         f"checkpoints left; nothing to continue from")
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "checkpoints").mkdir(exist_ok=True)
    shutil.copy(src / "lineage.jsonl", dst / "lineage.jsonl")
    for name, as_name in (("params.json", "params.json"),
                          ("manifest.json", "manifest-source.json")):
        if (src / name).exists():
            shutil.copy(src / name, dst / as_name)
    for r in ok:
        shutil.copytree(src / "checkpoints" / r["node"], dst / "checkpoints" / r["node"])
    if (src / "template").exists():
        # a real copy, not `cp -al`: a hard link would let a wave-2 container
        # write through into the run this one is supposed to leave alone
        sh(["cp", "-a", str(src / "template"), str(dst / "template")], timeout=900)
    (dst / "RESUMED_FROM.json").write_text(json.dumps({
        "source_run": src_id, "source_dir": str(src),
        "continued_from_generation": gen,
        "checkpoints_copied": [r["node"] for r in ok],
        "lineage_records_copied": len(recs),
        "copied": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n")
    log(f"--resume-from {src_id}: copied {len(recs)} lineage records and the "
        f"{len(ok)} generation-{gen} checkpoints ({[r['node'] for r in ok]}) into "
        f"{dst}; {src_id} is untouched")
    return dst


def child_seed(ordinal):
    """A child's reseed argument.  Derived from the child's own ordinal, which is
    assigned in a fixed creation order, so a search is reproducible from its own
    arguments; the value is written into the lineage regardless, because the
    lineage -- not this function -- is what a replay reads."""
    return ordinal * 1000003 + 7


# ------------------------------------------------------------------------ driver

class BranchRun:
    """Stands in for `run.Run` so `run.Replica` works unchanged: the replica only
    ever touches `.dir`, `.scn`, `.args` and `.run_id`."""

    def __init__(self, scn, args, seeds):
        self.scn = scn
        self.args = args
        self.run_id = args.run_id or f"{scn['name']}-branch-{time.strftime('%Y%m%d-%H%M%S')}"
        self.dir = RUNNER / "runs" / self.run_id
        self.world = seeds[0]
        self.world_seed = int(self.world["world_seed"])
        for sub in ("work", "checkpoints", "logs", "dumps", "compose"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.lineage = self.dir / "lineage.jsonl"
        self.segment = args.segment_ticks or scn["run"]["segment_ticks"]
        self.dump_at = sorted(int(x) for x in (args.dump_at or "").split(",") if x.strip())
        self.locate_at = sorted(int(x) for x in
                                (getattr(args, "locate_at", "") or "").split(",")
                                if x.strip())
        self.frames_at = parse_gametimes(getattr(args, "frames_at", ""))
        self.dump_at = sorted(set(self.dump_at) | set(self.locate_at)
                              | set(self.frames_at))
        self.ordinal = 0
        self.nodes = {}
        self.mods = self.dir / "mods"
        self.template = self.dir / "template"
        if args.memory:
            scn["server"]["memory"] = args.memory
        # The checkpoint IS the world copy, so the snapshot/render path is off:
        # nothing here needs a second copy of the same region files.
        scn["snapshots"]["enabled"] = False
        scn["snapshots"]["render"] = False

    # -- plumbing
    def next_ordinal(self):
        self.ordinal += 1
        return self.ordinal

    def stage_mods(self):
        self.mods.mkdir(exist_ok=True)
        jars = sorted((runner.PROJECT / "fabric" / "build" / "libs").glob("detmc-*.jar"))
        if self.args.jar:
            jars = [Path(self.args.jar)]
        if not jars:
            raise RuntimeError("no detmc jar; build one with ./docker-gradle.sh :fabric:build")
        for old in self.mods.glob("*.jar"):
            old.unlink()
        shutil.copy(jars[-1], self.mods / jars[-1].name)
        if self.scn["setup"]["fake_players"]:
            if not runner.CARPET_JAR.exists():
                raise RuntimeError(f"{runner.CARPET_JAR} missing; run ./fetch-carpet.sh")
            shutil.copy(runner.CARPET_JAR, self.mods / runner.CARPET_JAR.name)
        self.jar_sha = sh(["sha256sum", str(self.mods / jars[-1].name)]).split()[0]
        log(f"mod jar {jars[-1].name} sha256 {self.jar_sha[:16]}")
        (self.dir / ".env").write_text(f"RCON_PASSWORD={secrets.token_hex(16)}\n")

    def replica_for(self, node):
        r = runner.Replica(self, node.ordinal, self.world_seed, node.rng_seed)
        r.name = node.id
        r.container = f"mc-run-{self.run_id}-{node.id}"
        r.data = self.dir / "work" / node.id
        r.logf = self.dir / "logs" / f"{node.id}.log"
        r.node = node
        return r

    def node_need_mb(self):
        """Host cost of one node: its heap plus JVM/container overhead."""
        return memgate.job_need_mb(self.scn["server"].get("memory"))

    def compose(self, path, *args, check=True, need_mb=None):
        # Every container start in this file goes through here, so the shared host
        # memory gate does too.  2026-09-12 15:27: 8 hunt nodes + 3 Chunky renders
        # + a 4G pair launched against no shared budget and the kernel OOM-killed
        # a server and system daemons.  `down`/`rm` free memory, so only `up` waits.
        # Default need is one node; a whole-wave `up` passes the wave's total.
        if args and args[0] == "up":
            memgate.acquire(need_mb or self.node_need_mb())
        # Project name per run.  Compose defaults the project to the compose
        # directory name, so two runs shared project "compose" and a service
        # named after the node; on 2026-09-13 05:38 hunt-villager-wave1 `up`
        # for its g154n0 recreated hunt-br-wave2's g154n0 container, which
        # dropped three wave-2 nodes.
        project = "detmc-" + re.sub(r"[^a-z0-9_-]", "-", self.run_id.lower())
        return sh(["docker", "compose", "-p", project, "-f", str(path),
                   "--env-file", str(self.dir / ".env")] + list(args),
                  check=check, timeout=900)

    # -- data dirs
    def prepare_dir(self, node):
        """Generation 0 gets an empty /data.  Every later node gets its parent's
        saved world copied in, plus hard links to the already-downloaded server
        install so a 30-container search does not re-download Fabric 30 times."""
        dst = self.dir / "work" / node.id
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True)
        if node.parent is None:
            return
        src = self.dir / "checkpoints" / node.parent / "world"
        if not src.exists():
            raise RuntimeError(f"{node.id}: no checkpoint at {src}")
        # A checkpoint is copied out from under a running server, so it can carry a
        # half-written sidecar (measured: hunt-br-wave1 g44n4, below).  Checking it
        # HERE names the real culprit -- the parent -- instead of letting the child
        # die 90 s later on a missing scoreboard objective.
        bad = runner.bad_nbt_files(src)
        if bad:
            msg = (f"{node.id}: parent checkpoint {node.parent} has corrupt saved "
                   f"data: {'; '.join(bad)}")
            if any(b.startswith("level.dat") for b in bad):
                raise RuntimeError(msg)
            log(f"WARNING {msg} -- booting anyway: the server rebuilds these and "
                f"Replica.ensure_detmc_loaded puts the detmc objective back")
        shutil.copytree(src, dst / "world")
        (dst / "world" / "session.lock").unlink(missing_ok=True)
        self.refresh_detector_pack(node, dst / "world")
        if self.template.exists():
            # libraries/ and versions/ are write-once jars; hard links are safe and
            # free.  Everything itzg rewrites (server.properties, eula.txt, the
            # *.json caches) is deliberately NOT linked -- a hard link there would
            # let one container edit every other node's copy.
            for sub in ("libraries", "versions"):
                if (self.template / sub).exists():
                    sh(["cp", "-al", str(self.template / sub), str(dst / sub)])
            for jar in self.template.glob("*.jar"):
                shutil.copy(jar, dst / jar.name)

    def refresh_detector_pack(self, node, world):
        """Regenerate the detector datapack inside a copied world before it boots.

        A resumed world brings its parent's `datapacks/detmc_detector` with it, and
        `Replica.resume` deliberately never reloads (a reload needs a tick, and a
        branch's resume gametime must stay exactly the parent's).  So without this,
        the pack a lineage was born with is the pack it keeps for the whole search
        and no fix to `run.detector_pack` could ever reach it -- which is exactly
        how hunt-br-wave1 would have carried the `#armed` bug through a resume.
        Writing the files before `docker compose up` means the server reads the
        current generator's output at startup, from the same scenario and the same
        `vars` the parent recorded, so nothing else about the node changes.
        """
        meta = self.dir / "checkpoints" / node.parent / "meta.json"
        if not meta.exists():
            return
        v = (json.loads(meta.read_text()) or {}).get("vars")
        if not v:
            return
        files = runner.detector_pack(self.scn, v)
        if not files:                       # custom_datapack / custom_scarpet
            return
        pack = world / "datapacks" / "detmc_detector"
        shutil.rmtree(pack, ignore_errors=True)
        for rel, text in files.items():
            f = pack / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

    def capture_template(self, node):
        if self.template.exists():
            return
        src = self.dir / "work" / node.id
        self.template.mkdir(parents=True, exist_ok=True)
        for sub in ("libraries", "versions"):
            if (src / sub).exists():
                sh(["cp", "-al", str(src / sub), str(self.template / sub)])
        for jar in src.glob("*.jar"):
            shutil.copy(jar, self.template / jar.name)
        log(f"template captured from {node.id} "
            f"({sh(['du', '-sh', str(self.template)]).split()[0]})")

    # -- one wave of concurrent servers
    def run_wave(self, nodes, label):
        """Boot, set up and advance one wave of nodes.

        A node that fails is DROPPED, not fatal.  Until hunt-br-wave1 generation 45
        (2026-09-12) any single failure raised out of here and killed a search that
        had 44 clean generations behind it; the caller now decides, and only stops
        when a generation has fewer than 2 survivors left.  Each failure gets one
        bounded retry first: a fresh copy of the parent checkpoint into a fresh
        container, which is the same remedy `wait_started` already had for the
        itzg image's flaky first start.
        """
        comp = self.dir / "compose" / f"{label}.yml"
        services, reps = {}, {}
        for node in nodes:
            try:
                self.prepare_dir(node)
            except Exception as exc:                   # noqa: BLE001 - reported
                node.error = exc
                log(f"{node.id}: FAILED before boot: {exc}")
                continue
            r = self.replica_for(node)
            reps[node.id] = r
            services[node.id] = runner.service_spec(
                self.scn, r.container, f"../work/{node.id}", self.world_seed,
                node.rng_seed, "../mods")
        if not services:
            log(f"{label}: no node could even be prepared")
            self.record_wave(nodes)
            return
        comp.write_text(yaml.safe_dump({"services": services}, sort_keys=False))
        t0 = time.time()
        self.compose(comp, "up", "-d",
                     need_mb=len(services) * self.node_need_mb())
        try:
            for node in [n for n in nodes if n.id in reps]:
                self._boot_one(comp, reps[node.id], node)
            boot = time.time() - t0
            self._parallel(self._prepare_one,
                           [n for n in nodes if n.id in reps and not n.error], reps)
            # one bounded retry per failed node, from a clean data dir
            retry = [n for n in nodes if n.error and n.id in reps]
            for node in retry:
                self._reboot_one(comp, reps[node.id], node)
            self._parallel(self._prepare_one, [n for n in retry if not n.error], reps)
            if nodes[0].parent is None and not nodes[0].error:
                self.capture_template(nodes[0])
            setup = time.time() - t0 - boot
            live = [n for n in nodes if n.id in reps and not n.error]
            log(f"{label}: boot {boot:.1f}s, setup/resume {setup:.1f}s for "
                f"{len(live)} of {len(nodes)} nodes")
            self._parallel(self._advance_one, live, reps)
            for node in nodes:
                node.timings["boot_seconds"] = round(boot, 1)
                node.timings["setup_seconds"] = round(setup, 1)
            dead = [n for n in nodes if n.error]
            if dead:
                log(f"{label}: DROPPING {len(dead)} of {len(nodes)} nodes: "
                    + "; ".join(f"{n.id}: {n.error}" for n in dead))
        finally:
            if not self.args.keep:
                self.compose(comp, "down", check=False)
                # The checkpoint holds everything a child needs, and a live /data
                # is ~100 MB (measured: 822 MB for 8 nodes), so a long search would
                # otherwise fill the disk with dead server installs.
                for node in nodes:
                    if not node.error:      # a failed node keeps its /data to look at
                        shutil.rmtree(self.dir / "work" / node.id, ignore_errors=True)
        self.record_wave(nodes)

    def record_wave(self, nodes):
        for node in nodes:
            with self.lineage.open("a") as fh:
                fh.write(json.dumps(node.record({
                    "checkpoint": str((self.dir / "checkpoints" / node.id).relative_to(self.dir)),
                    "dump": str((self.dir / "dumps" / f"{node.id}.txt").relative_to(self.dir)),
                })) + "\n")

    def _parallel(self, fn, nodes, reps):
        threads = [threading.Thread(target=fn, args=(reps[n.id], n), daemon=True)
                   for n in nodes]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

    def _boot_one(self, comp, r, node):
        try:
            try:
                r.wait_started(timeout=self.args.start_timeout)
            except (RuntimeError, TimeoutError) as exc:
                # same retry run.py needs: the itzg image's first-start download
                # fails often enough to matter
                log(f"{node.id}: {exc}; retrying once from a clean data dir")
                self.compose(comp, "rm", "-sf", node.id, check=False)
                self.prepare_dir(node)
                self.compose(comp, "up", "-d", node.id)
                r.wait_started(timeout=self.args.start_timeout)
            log(f"{node.id}: started (world {self.world_seed}, "
                f"-Ddetmc.seed={node.rng_seed}, parent {node.parent})")
        except Exception as exc:                       # noqa: BLE001 - reported
            node.error = exc
            log(f"{node.id}: FAILED to boot: {exc}")

    def _reboot_one(self, comp, r, node):
        """Second chance for a node that booted but could not be set up/resumed."""
        log(f"{node.id}: retrying once after: {node.error}")
        node.error = None
        try:
            self.compose(comp, "rm", "-sf", node.id, check=False)
            self.prepare_dir(node)
            self.compose(comp, "up", "-d", node.id)
            r.wait_started(timeout=self.args.start_timeout)
            log(f"{node.id}: rebooted for the retry")
        except Exception as exc:                       # noqa: BLE001 - reported
            node.error = exc
            log(f"{node.id}: FAILED on the retry boot: {exc}")

    def _prepare_one(self, r, node):
        """Generation 0 is `setup`, every later generation is `resume`.

        Both arms must leave the world with the scenario's fake players ON it.
        Until 2026-09-13 only the `setup` arm did: `Replica.resume` never put them
        back, Carpet does not restore them from the save, and natural spawning
        needs a non-spectator player within 128 blocks -- so every generation after
        0 ticked an empty village.  `hunt-villager-wave1` scored `endermen` 0 in 548
        of its 549 node-generations and ran 1.4x faster for it.  The spawn now
        happens inside `Replica.resume` (frozen, before the reseed, so the reseed
        stays the only difference between siblings) and raises if `list` does not
        show the player, which drops the node here rather than reporting an empty
        world as progress.
        """
        try:
            if node.parent is None:
                r.setup(self.world)
                node.base_gametime = r.gametime()
                node.vars = r.vars
                node.fake_players = r.fake_players_spawned
                # `run.reseed` from the scenario is a run.py feature and would fire
                # inside a segment; a branch's only reseeds are its resume points.
                r.reseeds = []
            else:
                meta = json.loads((self.dir / "checkpoints" / node.parent
                                   / "meta.json").read_text())
                r.resume(meta["vars"], node.resume_gametime, node.reseed)
                node.base_gametime = meta["base_gametime"]
                node.vars = meta["vars"]
                node.resummoned = r.resummoned
                node.restored_blocks = r.restored_blocks
                node.fake_players = r.fake_players_spawned
        except Exception as exc:                       # noqa: BLE001 - reported above
            node.error = exc
            log(f"{node.id}: FAILED: {exc}")

    def locate(self, r, target):
        """`Replica.locate_hits` at this stop, for every detector, fired or not.

        `locate_hits` normally scans only the detectors whose flag is set, which is
        the right default (a column_stack scan is 625 rcon round trips, ~107 s
        measured).  It is the wrong rule when the question is "where WAS it": the
        `#gt_pillar3` gametime is the only thing hunt-br-wave1 kept about either
        pillar, and a shape that has already been taken apart fires no flag at the
        stop where it still stood.  `--locate-at <gametime>` lifts the filter for
        one stop, so the predicate is re-tested over the whole arena there.
        """
        saved = dict(r.hits)
        if target in self.locate_at:
            r.hits = {d["name"]: {"hit": 1}
                      for d in runner.det_list(self.scn) if d["kind"] != "none"}
        try:
            return r.locate_hits()
        finally:
            r.hits = saved

    def _advance_one(self, r, node):
        try:
            start = r.gametime()
            # a search leg runs a segment; a replay leg runs to the absolute
            # gametime the lineage recorded, which is the same thing when the
            # replay is faithful and an immediate, loud failure when it is not
            node.target_gametime = node.abs_target or (start + self.segment)
            dumps = self.dir / "dumps" / f"{node.id}.txt"
            stops = sorted({g for g in self.dump_at if start < g < node.target_gametime}
                           | {node.target_gametime})
            # A frame at the leg's own start gametime has no stop to hang off:
            # `stops` is strictly after `start`, and the resume already left the
            # tick loop frozen there.  Take it before the clock starts so the copy
            # is not counted as ticking.
            if start in self.frames_at:
                self.frame(r, node, start)
            t0 = time.time()
            ticked = 0
            for target in stops:
                r.goto(target)
                ticked = target - start
                d = r.read_detector()
                if d:
                    fresh = [name for name, sub in d["per_detector"].items()
                             if sub["hit"] and name not in node.hits]
                    for name in fresh:
                        sub = d["per_detector"][name]
                        node.hits[name] = sub["hit_gametime"]
                        r.hits[name] = sub          # what locate_hits reads
                        log(f"{node.id}: HIT {name} at gametime {sub['hit_gametime']}")
                    # WHERE, not just when.  The gametime alone is what made
                    # hunt-br-wave1's g48n5 pillar unrecoverable: the checkpoint was
                    # taken 1941 ticks after the hit and no longer contained it, so
                    # `#gt_pillar3` was the only surviving evidence that it ever
                    # stood.  `locate_hits` re-tests the detector's own predicate
                    # over the arena, so it only finds anything if the shape is
                    # still standing AT THIS STOP -- put a `--locate-at` on the
                    # hit gametime and the coordinates come back.
                    if fresh or target in self.locate_at:
                        try:
                            node.where.setdefault(f"t{target}", self.locate(r, target))
                            log(f"{node.id}: where at t{target} "
                                f"{json.dumps(node.where[f't{target}'])}")
                        except Exception as exc:       # noqa: BLE001 - reported
                            log(f"{node.id}: could not locate the hit: {exc}")
                if target in self.frames_at:
                    self.frame(r, node, target)
                if target in self.dump_at or target == node.target_gametime:
                    r.entity_dump(dumps, f"t{target}")
            # The detector is only trustworthy if it was armed for the WHOLE
            # segment, and "no hit" from a disarmed node means nothing at all.
            # This is not hypothetical: on its first run this read caught that
            # every resumed segment of hunt-br-wave1 had ticked with `#armed` 0,
            # because the `minecraft:load` tag fires on the first TICKED tick of a
            # resumed world -- after `Replica.resume` armed it -- and the generated
            # `detmc:load` used to set `#armed` 0 unconditionally.  One rcon read
            # per node per segment, against 352 of 360 nodes watched by nothing.
            armed = r.score("#armed")
            if armed != 1:
                raise RuntimeError(f"#armed was {armed} at the end of the segment, "
                                   f"so the detector was not armed throughout")
            node.timings["tick_seconds"] = round(time.time() - t0, 1)
            node.timings["ticks_per_second"] = (
                round(ticked / max(time.time() - t0, 1e-9), 1))
            node.final_gametime = r.reached
            vals, rank, secs = r.read_scores()
            node.scores, node.rank = vals, rank
            node.timings["score_seconds"] = round(secs, 2)
            log(f"{node.id}: gametime {node.final_gametime} rank {rank} "
                f"scores {json.dumps(vals)} (score fn {secs:.2f}s)")
            t1 = time.time()
            save = r.save_world()
            self.checkpoint(r, node)
            node.timings["save_seconds"] = round(save, 1)
            node.timings["checkpoint_seconds"] = round(time.time() - t1, 1)
        except Exception as exc:                       # noqa: BLE001 - reported above
            node.error = exc
            log(f"{node.id}: FAILED while ticking: {exc}")

    def frame(self, r, node, gametime):
        """One time-lapse frame: save, then copy the world out to
        `frames/<gametime>/` while the tick loop is still frozen at this stop.

        `camera/snap.sh` wants a save root, and `camera/README.md` names the three
        rules: copy after a save with the ticks frozen, never copy
        `session.lock`, and copy `dimensions/**/entities/*.mca` -- Chunky reads
        living mobs only from `entities`, never from `region`, so a frame without
        it renders an empty arena whatever the renderer does.

        This is `checkpoint()` minus the NBT re-verification and plus `entities/`:
        a frame is a render input, not a world the search will ever resume, so a
        half-written `scoreboard.dat` in it would only cost one frame.  The cost
        of the stop itself is the measured-neutral mid-segment freeze (README, "A
        mid-segment stop does not change the leg"); `entity_bytes` is recorded
        per frame because a jar that does not persist entities writes 0 there and
        the frame is silently mobless.
        """
        dst = self.dir / "frames" / str(gametime)
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True)
        t0 = time.time()
        save = r.save_world()
        src = r.data / "world"
        shutil.copy(src / "level.dat", dst / "level.dat")
        ent = 0
        for dim in sorted((src / "dimensions").glob("*/*")):
            for kind in ("region", "entities"):
                if not (dim / kind).is_dir():
                    continue
                out = dst / dim.relative_to(src) / kind
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(dim / kind, out)
                if kind == "entities":
                    ent += sum(f.stat().st_size for f in out.rglob("*.mca"))
        size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
        node.frames[str(gametime)] = {"dir": str(dst.relative_to(self.dir)),
                                      "bytes": size, "entity_bytes": ent,
                                      "save_seconds": round(save, 2),
                                      "seconds": round(time.time() - t0, 2)}
        log(f"{node.id}: frame t{gametime} -> {dst.relative_to(self.dir)} "
            f"({size / 1e6:.1f} MB, entities {ent} bytes, {time.time() - t0:.1f}s)")

    def checkpoint(self, r, node):
        """Copy the saved world out from under the still-frozen, still-running
        server.  Not after `docker compose down`: measured in test/, compose's 10 s
        stop timeout SIGKILLs one of these servers before its shutdown save
        finishes, so the on-disk world after a teardown is the previous autosave."""
        dst = self.dir / "checkpoints" / node.id
        attempts = 3
        for attempt in range(1, attempts + 1):
            shutil.rmtree(dst, ignore_errors=True)
            dst.mkdir(parents=True)
            shutil.copytree(r.data / "world", dst / "world")
            (dst / "world" / "session.lock").unlink(missing_ok=True)
            # `save_world` waits on detmc's own sidecar, which is NOT the last thing
            # a save writes: measured on hunt-br-wave1, 2 of 360 checkpoints came
            # out with a truncated `data/minecraft/scoreboard.dat` and killed the
            # whole search one generation later.  The files are a few hundred bytes,
            # so verify the copy and take it again rather than find out on a resume.
            bad = runner.bad_nbt_files(dst / "world")
            if not bad:
                break
            if attempt == attempts:
                raise RuntimeError(f"{node.id}: checkpoint still half-written after "
                                   f"{attempts} copies: {'; '.join(bad)}")
            log(f"{node.id}: checkpoint copy {attempt} caught a half-written save "
                f"({'; '.join(bad)}); saving again and re-copying")
            time.sleep(1)
            r.save_world()
        size = sum(f.stat().st_size for f in (dst / "world").rglob("*") if f.is_file())
        (dst / "meta.json").write_text(json.dumps({
            "node": node.id, "gametime": node.final_gametime,
            "base_gametime": node.base_gametime, "vars": node.vars,
            "scores": node.scores, "rank": node.rank, "hits": node.hits,
            "where": node.where,
            "schedule": node.schedule, "base_rng_seed": node.rng_seed,
            "fake_players": node.fake_players,
            "world_seed": self.world_seed, "bytes": size}, indent=2))
        log(f"{node.id}: checkpoint {dst.name} ({size / 1e6:.1f} MB) "
            f"at gametime {node.final_gametime}")

    def waves(self, nodes):
        c = max(1, self.args.concurrency)
        # --concurrency is what the operator asked for; this is what the host can
        # pay for right now.  A narrower wave costs wall clock, not nodes: the
        # generation still runs every node, in more waves.
        per = self.node_need_mb()
        fits = memgate.budget_nodes(per, cap=c)
        if fits < c:
            log(f"memory budget: capping wave width {c} -> {fits} "
                f"({per} MB/node, MemAvailable {memgate.mem_available_mb()} MB, "
                f"{memgate.DEFAULT_RESERVE_MB} MB host reserve)")
            c = fits
        return [nodes[i:i + c] for i in range(0, len(nodes), c)]

    # -- the search
    def write_manifest(self):
        man = {"run_id": self.run_id, "mode": "search", "scenario": self.scn["name"],
               "scenario_file": str(Path(self.args.scenario).resolve()),
               "image": runner.IMAGE, "mc_version": runner.MC_VERSION,
               "mod_sha256": self.jar_sha, "world": self.world,
               "segment_ticks": self.segment, "generations": self.args.generations,
               "replicas": self.args.replicas, "keep_top": self.args.keep_top,
               "children": self.args.children, "concurrency": self.args.concurrency,
               "dump_at": self.dump_at, "frames_at": self.frames_at,
               "resolved_scenario": self.scn,
               "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        if getattr(self.args, "resume", None):
            man["resumed"] = True
        if getattr(self.args, "resume_from", None):
            man["resumed_from"] = self.args.resume_from
        # An existing manifest.json is the record of how THIS run dir was first
        # launched, so a resume never overwrites it.  A run dir that does not have
        # one yet -- a fresh run, or a `--resume-from` clone -- gets one, because
        # `branch.py replay` reads the scenario file out of it.
        out = (self.dir / f"manifest-resume-{time.strftime('%Y%m%d-%H%M%S')}.json"
               if (self.dir / "manifest.json").exists() else self.dir / "manifest.json")
        out.write_text(json.dumps(man, indent=2, default=str))

    def write_params(self):
        """Persist every argument `--resume` has to reproduce.

        A resumed search MUST run with the same segment length, beam shape and jar
        as the run it continues, and "the same" cannot mean "whatever the operator
        retypes".  manifest.json carries most of it but not `--jar` or `--memory`,
        which is exactly what hunt-br-wave1 needed."""
        keys = ("replicas", "generations", "keep_top", "children", "rng_seed_offset",
                "stop_on_hit", "segment_ticks", "memory", "concurrency", "dump_at",
                "frames_at", "start_timeout", "jar", "reseed")
        params = {k: getattr(self.args, k, None) for k in keys}
        params.update({
            "run_id": self.run_id,
            "scenario": str(Path(self.args.scenario).resolve()),
            "segment_ticks": self.segment,
            "seeds": self.args.seeds or str(self.world_seed),
            "world_seed": self.world_seed,
            "mod_sha256": getattr(self, "jar_sha", None),
            "written": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        (self.dir / "params.json").write_text(json.dumps(params, indent=2, default=str))

    def initial_nodes(self):
        """-> (nodes to run, the generation they are)."""
        if getattr(self.args, "resume", None):
            return self.resume_nodes()
        rs = self.scn["run"]["rng_seeds"] or list(range(1, self.args.replicas + 1))
        nodes = []
        for i in range(self.args.replicas):
            o = self.next_ordinal()
            nodes.append(Node(f"g0n{i}", 0, o,
                              int(rs[i % len(rs)]) + self.args.rng_seed_offset))
        return nodes, 0

    def resume_nodes(self):
        """Rebuild the beam from lineage.jsonl plus the checkpoints still on disk.

        The last FULLY USABLE generation is the highest one with at least 2 records
        that finished without an error and whose checkpoint directory is still
        there -- which after a normal generation is exactly the `--keep-top` worlds,
        because the search prunes the rest as soon as it has ranked them.  Those
        become the parents, and the search carries on at the next generation with
        the same ordinal counter, so child reseeds continue the same sequence
        instead of colliding with ones already in the lineage.
        """
        recs = [json.loads(l) for l in self.lineage.read_text().splitlines() if l.strip()]
        if not recs:
            raise RuntimeError(f"{self.lineage} is empty; nothing to resume")
        # every node ever created wrote a record, and a child's reseed IS
        # child_seed(ordinal), so the counter is recoverable exactly
        self.ordinal = max([len(recs)] +
                           [(int(r["reseed"]) - 7) // 1000003
                            for r in recs if r.get("reseed")])
        gen, ok = last_usable_generation(
            recs, lambda n: (self.dir / "checkpoints" / n / "meta.json").exists())
        if gen is None:
            raise RuntimeError(
                f"{self.run_id}: no generation in {self.lineage} has 2 usable "
                f"checkpoints left; nothing to resume from")
        stale = [r["node"] for r in recs if int(r.get("generation") or 0) > gen]
        if stale:
            log(f"WARNING lineage.jsonl already has records past generation {gen} "
                f"({stale}); they are superseded and stay in the file")
        parents = []
        for r in ok:
            n = Node(r["node"], gen, 0, int(r["base_rng_seed"]),
                     parent=r.get("parent"), reseed=r.get("reseed"),
                     schedule=r.get("schedule") or [],
                     resume_gametime=r.get("resume_at_gametime"))
            n.rank = r.get("rank") or 0
            n.scores, n.hits = r.get("scores") or {}, r.get("hits") or {}
            # Rank the inherited parents with THIS run's `score.rank`, not with
            # the one the record was written under.  A continuation may be run to
            # look for something else (hunt-br-wave2 ranks strict-smiley progress
            # first, hunt-br-wave1 ranked the 3-stack first), and the very first
            # selection a resumed search makes is `keep-top` over these parents;
            # using the stale number would pick that beam by the old objective.
            # The metrics themselves are re-measured, so this is only about the
            # one generation that is read off disk rather than run.
            if n.scores:
                try:
                    fresh = runner.rank_of(self.scn, n.scores)
                except Exception as exc:               # noqa: BLE001 - reported
                    log(f"WARNING {n.id}: could not re-rank from the recorded "
                        f"scores ({exc}); keeping the recorded rank {n.rank}")
                else:
                    if fresh != n.rank:
                        log(f"{n.id}: rank {n.rank} -> {fresh} under this run's "
                            f"score.rank")
                    n.rank = fresh
            n.base_gametime, n.final_gametime = r.get("base_gametime"), r.get("final_gametime")
            parents.append(n)
        ranked = sorted(parents, key=lambda n: (-n.rank, n.id))
        log(f"resuming {self.run_id} at generation {gen + 1}: "
            f"{len(recs)} records, last complete generation {gen}, parents "
            + ", ".join(f"{n.id}={n.rank}@{n.final_gametime}" for n in ranked)
            + f", next ordinal {self.ordinal + 1}")
        return self.spawn_children(ranked, gen), gen + 1

    def spawn_children(self, ranked, gen):
        """Keep the top K of `ranked`, prune the rest, return generation gen+1.

        The wave width is held at the width the search was launched with: if a
        generation lost nodes, the best parents get the spare children rather than
        letting the beam shrink for the rest of the run."""
        keep = ranked[:self.args.keep_top]
        pruned = ranked[self.args.keep_top:]
        log(f"keeping {[n.id for n in keep]}, pruning {[n.id for n in pruned]}")
        for n in pruned:
            shutil.rmtree(self.dir / "checkpoints" / n.id, ignore_errors=True)
        counts = {p.id: self.args.children for p in keep}
        width = max(self.args.replicas, self.args.keep_top * self.args.children)
        i = 0
        while sum(counts.values()) < width and keep:
            counts[keep[i % len(keep)].id] += 1
            i += 1
        nxt = []
        for p in keep:
            for _ in range(counts[p.id]):
                o = self.next_ordinal()
                seed = child_seed(o)
                nxt.append(Node(f"g{gen + 1}n{len(nxt)}", gen + 1, o, p.rng_seed,
                                parent=p.id, reseed=seed,
                                schedule=p.child_schedule(p.final_gametime, seed),
                                resume_gametime=p.final_gametime))
        return nxt

    def search(self):
        self.mode = "search"
        # t0 before the try: write_done runs from `finally`, including on a
        # failure inside stage_mods(), and still needs an elapsed time
        t0 = time.time()
        status, error = 0, None
        try:
            self.stage_mods()
            self.write_manifest()
            self.write_params()
            nodes, start_gen = self.initial_nodes()
            alive = []
            for gen in range(start_gen, self.args.generations):
                log(f"=== generation {gen}: {len(nodes)} nodes "
                    f"({[n.id for n in nodes]}) ===")
                for w, wave in enumerate(self.waves(nodes)):
                    self.run_wave(wave, f"g{gen}w{w}")
                alive = [n for n in nodes if not n.error]
                dead = [n for n in nodes if n.error]
                # A node failure is not a search failure: it costs the beam one
                # world, and the survivors carry on.  Two is the floor, because a
                # beam of one is a single lineage with no branching left.
                if len(alive) < 2:
                    raise RuntimeError(
                        f"generation {gen}: only {len(alive)} of {len(nodes)} nodes "
                        f"survived (" + "; ".join(f"{n.id}: {n.error}" for n in dead)
                        + "); a beam needs at least 2")
                if dead:
                    log(f"generation {gen}: continuing with {len(alive)} of "
                        f"{len(nodes)} nodes; dropped {[n.id for n in dead]}")
                ranked = sorted(alive, key=lambda n: (-n.rank, n.id))
                log(f"generation {gen} ranking: " +
                    ", ".join(f"{n.id}={n.rank}" for n in ranked))
                hit = [n for n in alive if n.hits]
                if hit and self.args.stop_on_hit:
                    log(f"DETECTOR HIT on {[n.id for n in hit]}; stopping the search")
                    break
                if gen == self.args.generations - 1:
                    break
                nodes = self.spawn_children(ranked, gen)
            self.write_summary(alive, time.time() - t0)
        except BaseException as exc:                   # noqa: BLE001 - re-raised
            status, error = 1, f"{type(exc).__name__}: {exc}"
            raise
        finally:
            # The marker is written in `finally`, so a detached search leaves one
            # either way: clean finish, crash or Ctrl-C.
            self.write_done(status, error, t0)
        return 0

    def write_summary(self, leaves, seconds):
        recs = [json.loads(l) for l in self.lineage.read_text().splitlines() if l.strip()]
        best = sorted(recs, key=lambda r: (-(r["rank"] or 0), r["node"]))
        out = {"run_id": self.run_id, "wall_seconds": round(seconds, 1),
               "world_seed": self.world_seed, "segment_ticks": self.segment,
               "generations_run": max(r["generation"] for r in recs),
               "nodes": len(recs),
               "hits": {r["node"]: r["hits"] for r in recs if r["hits"]},
               "leaves": [n.id for n in leaves],
               "best": best[0] if best else None, "lineage": recs}
        (self.dir / "lineage.json").write_text(json.dumps(out, indent=2))
        log(f"search done in {seconds:.1f}s over {len(recs)} nodes; "
            f"best {out['best']['node'] if out['best'] else None} "
            f"rank {out['best']['rank'] if out['best'] else None}")
        log(f"lineage: {self.dir / 'lineage.json'}")

    def write_done(self, status, error, t0):
        """Write `runs/<run_id>/DONE`, the run's completion marker.

        Same contract as `run.Run.write_done`: a detached driver
        (`nohup branch.py search ... &`) is otherwise only observable by tailing
        its log, and "the log stopped moving" does not distinguish a finished
        search from a killed one.  This file appears exactly once, from the
        `finally` block of `search`/`replay`, so it covers the crash and Ctrl-C
        paths as well: `status` 0 means the body ran to the end, 1 means it
        raised and `error` says what.  `lineage.json` still carries the full
        per-node record.

        Everything is read defensively.  `finally` can run before `stage_mods`
        has set `jar_sha` and before `lineage.jsonl` exists, and a marker that
        raised on its way out would replace the real exception with a useless
        one.  A failure while summarising is reported as `summary_error`, not
        swallowed.
        """
        d = Path(getattr(self, "dir", "."))
        args = getattr(self, "args", None)
        done = {"run_id": getattr(self, "run_id", None),
                "mode": (getattr(self, "mode", None)
                         or getattr(args, "cmd", None) or "search"),
                "scenario": None,
                "status": status, "error": error,
                "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "wall_seconds": round(time.time() - (t0 or time.time()), 1)}
        try:
            scn = getattr(self, "scn", None)
            if isinstance(scn, dict):
                done["scenario"] = scn.get("name")
            recs, bad = [], 0
            lin = Path(getattr(self, "lineage", d / "lineage.jsonl"))
            if lin.exists():
                for line in lin.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        bad += 1      # a kill mid-append leaves a partial line
            hits = {r.get("node"): r["hits"] for r in recs if r.get("hits")}
            best = sorted(recs, key=lambda r: (-(r.get("rank") or 0), str(r.get("node"))))
            done.update({
                "generations_requested": getattr(args, "generations", None),
                "generations_run": (max((r.get("generation") or 0) for r in recs)
                                    if recs else 0),
                "nodes_completed": len(recs),
                "segment_ticks": getattr(self, "segment", None),
                "replicas": getattr(args, "replicas", None),
                "keep_top": getattr(args, "keep_top", None),
                "children": getattr(args, "children", None),
                "concurrency": getattr(args, "concurrency", None),
                "world_seed": getattr(self, "world_seed", None),
                "mod_sha256": getattr(self, "jar_sha", None),
                "hits": hits,
                "hits_total": len(hits),
                "best": ({"node": best[0].get("node"), "rank": best[0].get("rank"),
                          "final_gametime": best[0].get("final_gametime")}
                         if best else None),
                "lineage": str(d / "lineage.json")})
            if bad:
                done["lineage_bad_lines"] = bad
        except Exception as exc:                       # noqa: BLE001 - reported
            done["summary_error"] = f"{type(exc).__name__}: {exc}"
            log(f"WARNING could not summarise the lineage for DONE: {exc}")
        try:
            (d / "DONE").write_text(json.dumps(done, indent=2, default=str) + "\n")
        except Exception as exc:                       # noqa: BLE001 - reported
            log(f"WARNING could not write {d / 'DONE'}: {exc}")
            return
        log(f"wrote {d / 'DONE'}: status {status}, "
            f"{done.get('hits_total', 0)} of {done.get('nodes_completed', 0)} "
            f"nodes hit")

    # -- replay
    def replay(self, rec):
        """Re-run ONE lineage from scratch: fresh world, the recorded base seed,
        then the recorded (gametime, reseed) resume points in order."""
        self.mode = "replay"
        t0 = time.time()
        status, error = 0, None
        try:
            self.stage_mods()
            (self.dir / "manifest.json").write_text(json.dumps({
                "run_id": self.run_id, "mode": "replay", "replayed": rec["node"],
                "schedule": rec["schedule"], "base_rng_seed": rec["base_rng_seed"],
                "final_gametime": rec["final_gametime"], "world": self.world,
                "image": runner.IMAGE, "mod_sha256": self.jar_sha,
                "dump_at": self.dump_at, "frames_at": self.frames_at,
                "segment_ticks": self.segment,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2, default=str))
            # one leg per resume point, plus the leg that ends at the leaf's own
            # gametime.  Leg 0 is a fresh world; every later leg resumes leg i-1's
            # checkpoint and issues the recorded reseed at the recorded gametime.
            targets = [s["gametime"] for s in rec["schedule"]] + [rec["final_gametime"]]
            prev, node = None, None
            for i, target in enumerate(targets):
                node = Node(f"{rec['node']}-r{i}", i, self.next_ordinal(),
                            rec["base_rng_seed"],
                            parent=(prev.id if prev else None),
                            reseed=(rec["schedule"][i - 1]["reseed"] if i else None),
                            schedule=rec["schedule"][:i],
                            resume_gametime=(rec["schedule"][i - 1]["gametime"] if i else None))
                node.abs_target = int(target)
                log(f"replay leg {i}: -> gametime {target}"
                    + (f", resume at {node.resume_gametime} reseed {node.reseed}" if i else
                       f", fresh world, -Ddetmc.seed={node.rng_seed}"))
                self.run_wave([node], f"replay-{node.id}")
                if node.error:
                    raise RuntimeError(f"replay failed at leg {i}: {node.error}")
                prev = node
            log(f"replay finished at gametime {node.final_gametime} in "
                f"{time.time() - t0:.1f}s; dump {self.dir / 'dumps' / (node.id + '.txt')}")
            self.write_summary([node], time.time() - t0)
        except BaseException as exc:                   # noqa: BLE001 - re-raised
            status, error = 1, f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.write_done(status, error, t0)
        return node

    # -- one-leg replay
    def replay_leg(self, rec, src, repeat, columns):
        """Re-run ONE leg of a lineage and compare it with the leg's own checkpoint.

        `replay` re-derives a whole lineage from tick 0: for g48n5 that is 49 legs
        and ~2.5 h, 48 of them spent re-deriving a parent world that is already on
        disk as `checkpoints/g47n7`.  When the question is "is THIS segment
        reproducible", that is 2.5 h of answering a different question.

        The two are different claims and both are worth having:

          replay-leg  the leg is a function of (parent world, resume gametime,
                      reseed).  ~3 min.  Says nothing about how the parent arose.
          replay      the whole lineage is a function of (world seed, base rng
                      seed, schedule).  Hours.  Subsumes the first.

        The leg is set up exactly the way `search` sets a child up, because it is
        the same code: a copy of the parent checkpoint into a fresh `/data`,
        `refresh_detector_pack`, `Replica.resume(vars, resume_gametime, reseed)`
        -- re-summon, restore material, reseed, arm -- then `_advance_one` to the
        recorded end gametime.  The only thing added is the comparison afterwards.
        """
        self.mode = "replay-leg"
        t0 = time.time()
        status, error, results = 0, None, []
        try:
            self.stage_mods()
            parent = rec.get("parent")
            if not parent:
                raise SystemExit(f"{rec['node']} is a generation-0 node: there is no "
                                 f"parent checkpoint to boot from.  Use `replay`")
            psrc, nsrc = src / "checkpoints" / parent, src / "checkpoints" / rec["node"]
            for d in (psrc, nsrc):
                if not (d / "world").exists():
                    raise SystemExit(f"no checkpoint at {d}; this mode needs both the "
                                     f"parent (to boot) and the node (to compare)")
            # Copy, never link: the source run is a result and is opened read-only.
            if not (self.dir / "checkpoints" / parent).exists():
                shutil.copytree(psrc, self.dir / "checkpoints" / parent)
            if not self.template.exists() and (src / "template").exists():
                sh(["cp", "-a", str(src / "template"), str(self.template)], timeout=900)
            (self.dir / "manifest.json").write_text(json.dumps({
                "run_id": self.run_id, "mode": "replay-leg", "replayed": rec["node"],
                "source_run": src.name, "parent": parent,
                "resume_at_gametime": rec["resume_at_gametime"],
                "reseed": rec["reseed"], "final_gametime": rec["final_gametime"],
                "base_rng_seed": rec["base_rng_seed"], "repeat": repeat,
                "columns": list(columns), "world": self.world,
                "image": runner.IMAGE, "mod_sha256": self.jar_sha,
                "dump_at": self.dump_at, "frames_at": self.frames_at,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2, default=str))
            for i in range(repeat):
                node = Node(f"{rec['node']}-rl{i}", rec.get("generation") or 0,
                            self.next_ordinal(), rec["base_rng_seed"],
                            parent=parent, reseed=rec["reseed"],
                            schedule=rec.get("schedule") or [],
                            resume_gametime=rec["resume_at_gametime"])
                node.abs_target = int(rec["final_gametime"])
                log(f"leg replay {i + 1}/{repeat} of {rec['node']}: boot "
                    f"{parent} at gametime {node.resume_gametime}, reseed "
                    f"{node.reseed}, run to {node.abs_target}")
                w0 = time.time()
                self.run_wave([node], f"leg-{node.id}")
                wall = round(time.time() - w0, 1)
                if node.error:
                    log(f"leg replay {i + 1}: FAILED: {node.error}")
                    results.append({"run": i + 1, "node": node.id, "wall_seconds": wall,
                                    "error": str(node.error)})
                    status = 1
                    continue
                v = verify_checkpoint(nsrc, self.dir / "checkpoints" / node.id, columns,
                                      f"{src.name}/checkpoints/{rec['node']}",
                                      f"{self.run_id}/checkpoints/{node.id}")
                results.append({"run": i + 1, "node": node.id, "wall_seconds": wall,
                                "hits": node.hits, "verdicts": v,
                                "differs": sorted(k for k, x in v.items()
                                                  if not x.startswith("MATCH"))})
                log(f"leg replay {i + 1}: {wall}s, hits {json.dumps(node.hits)}, "
                    + ("ALL CRITERIA MATCH" if not results[-1]["differs"]
                       else f"DIFFERS on {results[-1]['differs']}"))
            (self.dir / "verify.json").write_text(json.dumps(
                {"node": rec["node"], "source_run": src.name, "parent": parent,
                 "expected_hits": rec.get("hits"), "runs": results},
                indent=2, default=str))
            keys = [k for r in results for k in (r.get("verdicts") or {})]
            keys = list(dict.fromkeys(keys))
            print(f"\n{rec['node']}: {repeat} independent replays of the leg "
                  f"{rec['resume_at_gametime']} -> {rec['final_gametime']}")
            print(f"{'criterion':36s}" + "".join(f"{'run ' + str(r['run']):>10s}"
                                                 for r in results))
            for k in keys:
                row = "".join(
                    f"{('MATCH' if (r.get('verdicts') or {}).get(k, '').startswith('MATCH') else 'DIFFER') if r.get('verdicts') else 'FAILED':>10s}"
                    for r in results)
                print(f"{k:36s}{row}")
            print(f"{'wall seconds':36s}"
                  + "".join(f"{r['wall_seconds']:>10.1f}" for r in results))
            print(flush=True)
            self.write_summary([], time.time() - t0)
        except BaseException as exc:                   # noqa: BLE001 - re-raised
            status, error = 1, f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.write_done(status, error, t0)
        return results



# ------------------------------------------------------------------- comparison

SECTION = re.compile(r"^###\s*(.*)$")


def sections(path):
    """The `### [label] field` sections test/run-reseed.sh writes."""
    out, order, cur = {}, [], None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = SECTION.match(line)
        if m:
            cur = m.group(1).strip()
            if cur not in out:
                out[cur] = []
                order.append(cur)
            continue
        if cur is None or line.startswith(">>>"):
            continue
        out[cur].append(line)
    return out, order


def compare(a, b):
    """Section-by-section diff of two entity dumps, and the first divergent
    gametime.  Same algorithm as test/diff-reseed.sh, transcribed here so this
    runs on runner/'s own dumps without writing anything into test/."""
    A, order = sections(a)
    B, _ = sections(b)
    clean = lambda ls: [l for l in ls if l.strip() and                 # noqa: E731
                        "has the following entity data" in l]
    first_bad, worst = None, 0
    print(f"{'section':38s} {'A':>5s} {'B':>5s}  verdict")
    print("-" * 80)
    for k in order:
        if k not in B:
            continue
        x, y = clean(A[k]), clean(B[k])
        same = x == y
        n = sum(1 for p, q in zip(x, y) if p != q)
        verdict = "MATCH" if same else (
            f"DIFFER ({n} of {min(len(x), len(y))} lines, len {len(x)} vs {len(y)})")
        print(f"{k:38s} {len(x):5d} {len(y):5d}  {verdict}")
        if not same:
            worst = 1
            m = re.match(r"\[t(\d+)", k)
            if m and first_bad is None:
                first_bad = (int(m.group(1)), k)
    print()
    if first_bad is None:
        print(f"{Path(a).name} vs {Path(b).name}: MATCH at every checkpoint")
    else:
        print(f"{Path(a).name} vs {Path(b).name}: first divergent checkpoint = "
              f"gametime {first_bad[0]} ({first_bad[1]})")
    return worst


# ------------------------------------------------- checkpoint-level verification
#
# `compare` above diffs two ENTITY dumps.  That is the finest test there is while
# a server is up and it is useless afterwards, because a checkpoint is a world
# save and detmc writes no entity region files (README, "detmc never writes entity
# region files").  What survives a run is the world, its rng sidecar and the
# scoreboard, so that is what a replay has to be judged on.

REGION_MD5 = runner.PROJECT / "test" / "region-md5.py"
CAMERA = runner.PROJECT / "camera"


def region_md5(world):
    """{region file: md5} with the header's timestamp table blanked.

    Shells out to `test/region-md5.py` instead of reimplementing the rule.  Bytes
    4096..8191 of a region file are one wall-clock second per chunk, rewritten on
    every save, so a raw md5 of two identical worlds saved minutes apart never
    matches.  Every other determinism claim in this repo was measured with that
    script; a second copy of the rule here would be a second thing to keep right.
    """
    out = sh([sys.executable, str(REGION_MD5), str(world)], timeout=900)
    md5 = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 32:
            md5[parts[1]] = parts[0]
    return md5


def region_chunks_md5(world):
    """{`<region file>#<chunk index>`: md5 of the chunk's decompressed NBT}.

    `region_md5` hashes the whole file, which over-reports.  Measured 2026-09-12 on
    leg 9 of the g18n0 full replay: `r.0.-1.mca` differed from hunt-br-wave1's
    `g9n4` in 12,153 bytes, all of them in sectors 197-200, which **no chunk's
    offset entry points at**.  They are stale bytes left in free sectors by chunks
    that were later rewritten somewhere else, the file is the same length, and all
    54 live chunks are identical down to their compressed bytes.  A region file is
    a heap with a free list, so its dead space is a function of the save history,
    not of the world.  This hashes only what the game reads back.
    """
    out = {}
    for path in sorted(Path(world).rglob("*.mca")):
        if path.stat().st_size < 8192:
            continue
        rel = path.relative_to(world)
        with path.open("rb") as fh:
            hdr = fh.read(4096)
            for i in range(1024):
                loc = struct.unpack_from(">I", hdr, i * 4)[0]
                if not loc >> 8:
                    continue
                fh.seek((loc >> 8) * 4096)
                ln = struct.unpack(">I", fh.read(4))[0]
                comp = fh.read(1)[0]
                raw = fh.read(ln - 1)
                data = (zlib.decompress(raw) if comp == 2 else
                        gzip.decompress(raw) if comp == 1 else raw)
                out[f"{rel}#{i}"] = hashlib.md5(data).hexdigest()
    return out


def rng_props(world):
    """detmc-rng.properties as a dict, without the java.util.Properties header.

    Line 1 of a Properties file is `#<the current date>`, so the file can never
    compare equal byte for byte between two runs however identical the stream is.
    """
    p = Path(world) / "detmc-rng.properties"
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def overworld_region(world):
    """The overworld `region/` dir of a save.

    26.2 writes `world/dimensions/minecraft/overworld/region/`, not the
    `world/region/` every pre-1.21 tool assumes; both are accepted so this keeps
    working if the layout moves again."""
    world = Path(world)
    for rel in ("dimensions/minecraft/overworld/region", "region"):
        if (world / rel).is_dir():
            return world / rel
    raise FileNotFoundError(f"no overworld region dir under {world}")


def block_column(world, x, z, ys):
    """{y: block name} for one column, read straight out of the saved region file.

    A block column is the one criterion that says something about the EVENT rather
    than about the save as a whole: a pillar find is three specific blocks at three
    specific coordinates, and "the region md5 matched" does not tell a reader that
    the pillar was there.  `camera/blockdiff.py` already decodes the post-1.16
    bit-packed palette and the NBT under it, so those two functions are imported
    rather than copied; nothing here writes to camera/.  Its `chunks()` iterator is
    not used: it walks every chunk of every region file (and trips over the 0-byte
    `entities/*.mca` detmc leaves behind), where one column needs exactly one chunk.
    """
    if str(CAMERA) not in sys.path:
        sys.path.insert(0, str(CAMERA))
    import blockdiff                                    # noqa: PLC0415 - optional
    ys = list(ys)
    cx, cz = x >> 4, z >> 4
    path = overworld_region(world) / f"r.{cx >> 5}.{cz >> 5}.mca"
    sections = {}
    if path.exists() and path.stat().st_size >= 8192:
        with path.open("rb") as fh:
            hdr = fh.read(4096)
            loc = struct.unpack_from(">I", hdr, ((cz & 31) * 32 + (cx & 31)) * 4)[0]
            if loc >> 8:
                fh.seek((loc >> 8) * 4096)
                ln = struct.unpack(">I", fh.read(4))[0]
                comp = fh.read(1)[0]
                raw = fh.read(ln - 1)
                data = (zlib.decompress(raw) if comp == 2 else
                        gzip.decompress(raw) if comp == 1 else raw)
                want = {y >> 4 for y in ys}
                for sec in blockdiff.parse(data).get("sections", []):
                    if sec.get("Y") in want:
                        sections[sec["Y"]] = blockdiff.section_blocks(sec)
    i = (z & 15) * 16 + (x & 15)
    return {y: (sections[y >> 4][(y & 15) * 256 + i] if (y >> 4) in sections
                else "<chunk not saved>") for y in ys}


def parse_column(spec):
    """`x,z,y0,y1` -> (x, z, [y0..y1])."""
    x, z, y0, y1 = (int(v) for v in spec.split(","))
    return x, z, list(range(min(y0, y1), max(y0, y1) + 1))


def verify_checkpoint(a, b, columns=(), la="expected", lb="replay", show=True):
    """Compare two checkpoint dirs (`world/` + `meta.json`) criterion by criterion.

    -> {criterion: "MATCH" | "DIFFER: <the actual values>"}.  The differing values
    are always printed, never summarised away: a verdict with no diff under it is
    an assertion, not a measurement.
    """
    a, b = Path(a), Path(b)
    out, detail = {}, []
    load = lambda p: (json.loads((p / "meta.json").read_text())        # noqa: E731
                      if (p / "meta.json").exists() else {})
    ma, mb = load(a), load(b)

    def put(key, x, y):
        out[key] = "MATCH" if x == y else f"DIFFER: {la}={x!r} {lb}={y!r}"
        if x != y:
            detail.append(f"  {key}: {la} = {x!r}\n  {' ' * len(key)}  {lb} = {y!r}")

    put("final gametime", ma.get("gametime"), mb.get("gametime"))

    for key, field in (("score metrics", "scores"), ("detector hits", "hits")):
        xa, xb = ma.get(field) or {}, mb.get(field) or {}
        bad = {k: (xa.get(k), xb.get(k)) for k in sorted(set(xa) | set(xb))
               if xa.get(k) != xb.get(k)}
        out[key] = "MATCH" if not bad else f"DIFFER: {bad}"
        detail.append(f"  {key}: {la} = {json.dumps(xa, sort_keys=True)}\n"
                      f"  {' ' * len(key)}  {lb} = {json.dumps(xb, sort_keys=True)}")

    ra, rb = region_md5(a / "world"), region_md5(b / "world")
    bad = sorted(k for k in set(ra) | set(rb) if ra.get(k) != rb.get(k))
    out["region md5"] = ("MATCH" if not bad and ra
                         else f"DIFFER: {len(bad)} of {len(set(ra) | set(rb))} files")
    detail.append(f"  region md5: {len(ra)} files")
    for k in sorted(set(ra) | set(rb)):
        detail.append(f"    {'SAME  ' if ra.get(k) == rb.get(k) else 'DIFFER'} "
                      f"{k}  {ra.get(k)}  {rb.get(k)}")

    ca, cb = region_chunks_md5(a / "world"), region_chunks_md5(b / "world")
    bad = sorted(k for k in set(ca) | set(cb) if ca.get(k) != cb.get(k))
    out["region chunk payloads"] = (
        "MATCH" if not bad and ca
        else f"DIFFER: {len(bad)} of {len(set(ca) | set(cb))} chunks")
    detail.append(f"  region chunk payloads: {len(ca)} live chunks vs {len(cb)}")
    for k in bad[:20]:
        detail.append(f"    DIFFER {k}  {ca.get(k)}  {cb.get(k)}")

    pa, pb = rng_props(a / "world"), rng_props(b / "world")
    bad = {k: (pa.get(k), pb.get(k)) for k in sorted(set(pa) | set(pb))
           if pa.get(k) != pb.get(k)}
    out["detmc-rng.properties"] = "MATCH" if not bad and pa else f"DIFFER: {bad}"
    detail.append(f"  detmc-rng.properties: {la} = {json.dumps(pa, sort_keys=True)}\n"
                  f"                        {lb} = {json.dumps(pb, sort_keys=True)}")

    for spec in columns:
        x, z, ys = parse_column(spec)
        ca = block_column(a / "world", x, z, ys)
        cb = block_column(b / "world", x, z, ys)
        key = f"block column {x},{z} y{ys[0]}-{ys[-1]}"
        out[key] = "MATCH" if ca == cb else f"DIFFER: {[y for y in ys if ca[y] != cb[y]]}"
        for y in ys:
            detail.append(f"    {'SAME  ' if ca[y] == cb[y] else 'DIFFER'} "
                          f"({x},{y},{z})  {ca[y]}  {cb[y]}")

    if show:
        print(f"\n{la}\n  vs {lb}")
        print("-" * 78)
        for k, v in out.items():
            print(f"  {k:34s} {v}")
        print("\nvalues (never a verdict without the numbers under it):")
        print("\n".join(detail))
        print("-" * 78, flush=True)
    return out


# -------------------------------------------------------------------------- main

def add_common(ap):
    ap.add_argument("--run-id")
    ap.add_argument("--segment-ticks", type=int, help="ticks per generation")
    ap.add_argument("--memory", help="override server.memory (e.g. 1G)")
    ap.add_argument("--concurrency", type=int, default=10,
                    help="servers alive at once (default 10; ~1.5 GiB RSS each at "
                         "MEMORY=1G, measured -- see runner/README.md)")
    ap.add_argument("--dump-at", default="",
                    help="comma-separated absolute gametimes to dump entity "
                         "Pos/Motion/UUID/id/carriedBlockState at, on top of every "
                         "checkpoint; use the SAME list in a search and its replay")
    ap.add_argument("--locate-at", default="",
                    help="comma-separated absolute gametimes to re-test every "
                         "detector's predicate over the whole arena at, recording "
                         "the coordinates in `where`.  Implies --dump-at.  A "
                         "column_stack scan is one rcon round trip per column "
                         "(625 here, ~107 s measured), so this is per stop, not "
                         "per segment")
    ap.add_argument("--frames-at", default="",
                    help="comma-separated absolute gametimes, or START:STOP:STEP "
                         "(inclusive), to write a time-lapse frame at: freeze, "
                         "`save-all`, copy level.dat + every dimension's region/ "
                         "and entities/ to runs/<id>/frames/<gametime>/, then "
                         "carry on.  Implies --dump-at.  Render one with "
                         "`camera/snap.sh runs/<id>/frames/<gametime> <cam.json> "
                         "<out.png>`")
    ap.add_argument("--keep", action="store_true", help="leave containers up")
    ap.add_argument("--start-timeout", type=int, default=900)
    ap.add_argument("--jar", help="explicit detmc jar instead of fabric/build/libs")
    ap.add_argument("--seeds", help="comma-separated world seeds, skips the prefilter")
    ap.add_argument("--prefilter-timeout", type=int, default=3600)
    ap.add_argument("--reseed", choices=("auto", "on", "off"), default="on",
                    help="branching REQUIRES /detmc reseed, so this defaults to on")


# arguments `--resume` restores, and the flag each one is spelled with on the
# command line: an explicitly typed flag always beats the persisted value.
RESUMABLE = {"scenario": None, "replicas": "--replicas",
             "generations": "--generations", "keep_top": "--keep-top",
             "children": "--children", "rng_seed_offset": "--rng-seed-offset",
             "segment_ticks": "--segment-ticks", "memory": "--memory",
             "concurrency": "--concurrency", "dump_at": "--dump-at",
             "frames_at": "--frames-at",
             "start_timeout": "--start-timeout", "jar": "--jar",
             "seeds": "--seeds", "reseed": "--reseed"}


def apply_resume(args, argv=None):
    """Fill `args` from runs/<--resume>/params.json (or manifest.json)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    d = RUNNER / "runs" / args.resume
    if not (d / "lineage.jsonl").exists():
        raise SystemExit(f"--resume {args.resume}: no {d / 'lineage.jsonl'}")
    params = {}
    if (d / "params.json").exists():
        params = json.loads((d / "params.json").read_text())
    elif (d / "manifest.json").exists():
        m = json.loads((d / "manifest.json").read_text())
        params = {"scenario": m.get("scenario_file"), "replicas": m.get("replicas"),
                  "generations": m.get("generations"), "keep_top": m.get("keep_top"),
                  "children": m.get("children"),
                  "segment_ticks": m.get("segment_ticks"),
                  "concurrency": m.get("concurrency"),
                  "dump_at": ",".join(str(x) for x in (m.get("dump_at") or [])),
                  "seeds": str((m.get("world") or {}).get("world_seed") or "")}
        log(f"--resume {args.resume}: no params.json, read manifest.json instead "
            f"(a pre-params.json run: --jar and --memory are NOT in it)")
    else:
        raise SystemExit(f"--resume {args.resume}: neither params.json nor "
                         f"manifest.json in {d}")
    typed = {k for k, flag in RESUMABLE.items()
             if flag and any(a == flag or a.startswith(flag + "=") for a in argv)}
    if args.scenario:
        typed.add("scenario")
    restored = {}
    for k in RESUMABLE:
        v = params.get(k)
        if v in (None, "") or k in typed:
            continue
        setattr(args, k, v)
        restored[k] = v
    args.run_id = args.resume
    log(f"--resume {args.resume}: restored {restored}"
        + (f", command line keeps {sorted(typed)}" if typed else ""))
    # the old marker has to go, or `until [ -f DONE ]` returns the instant the
    # resumed search starts
    done = d / "DONE"
    if done.exists():
        done.rename(d / f"DONE.before-resume-{time.strftime('%Y%m%d-%H%M%S')}")
        log(f"moved the previous DONE aside")
    return args


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="run the branching search")
    s.add_argument("scenario", nargs="?",
                   help="scenario YAML; optional with --resume, which reads the "
                        "one the run was launched with")
    s.add_argument("--resume", metavar="RUN_ID",
                   help="continue runs/<RUN_ID> from its last complete generation, "
                        "with the parameters it was launched with (params.json, or "
                        "manifest.json for a run that predates it), appending to the "
                        "same lineage.jsonl.  Flags given on the command line still "
                        "win, which is how you raise --generations")
    s.add_argument("--resume-from", metavar="RUN_ID",
                   help="continue runs/<RUN_ID> in a NEW run dir (--run-id), by "
                        "copying its lineage, its last usable generation's "
                        "checkpoints and its server template across first.  Use "
                        "this instead of --resume when the source run is a result "
                        "worth keeping, or when the continuation changes the "
                        "scenario: the source run is never written to")
    s.add_argument("--replicas", type=int, default=4, help="generation-0 width N")
    s.add_argument("--generations", type=int, default=2,
               help="TOTAL generations including generation 0, so 2 means "
                    "one branching round")
    s.add_argument("--keep-top", type=int, default=2, help="beam width K")
    s.add_argument("--children", type=int, default=2, help="branches per kept world M")
    s.add_argument("--rng-seed-offset", type=int, default=0)
    s.add_argument("--stop-on-hit", action="store_true", default=True)
    s.add_argument("--no-stop-on-hit", dest="stop_on_hit", action="store_false")
    add_common(s)

    r = sub.add_parser("replay", help="re-run one leaf's schedule from scratch")
    r.add_argument("lineage", help="runs/<run>/lineage.jsonl")
    r.add_argument("node", help="node id to replay")
    r.add_argument("--scenario", help="override the scenario file (default: the "
                                      "one in the original run's manifest)")
    add_common(r)

    rl = sub.add_parser("replay-leg",
                        help="re-run ONE leg from its parent checkpoint and verify it")
    rl.add_argument("lineage", help="runs/<run>/lineage.jsonl")
    rl.add_argument("node", help="node id whose own segment is re-run")
    rl.add_argument("--repeat", type=int, default=1,
                    help="run the same leg N times, each in its own container, and "
                         "verify each (default 1).  Three is the useful number: one "
                         "run cannot distinguish `deterministic` from `lucky`")
    rl.add_argument("--column", action="append", default=[], metavar="X,Z,Y0,Y1",
                    help="compare this exact block column between the two "
                         "checkpoints; repeatable.  e.g. 89,-21,135,139 brackets "
                         "the g18n0 pillar with the floor below and the air above")
    rl.add_argument("--scenario", help="override the scenario file (default: the "
                                       "one in the source run's manifest)")
    add_common(rl)

    v = sub.add_parser("verify", help="compare two checkpoint dirs (world/ + meta.json)")
    v.add_argument("a", help="runs/<run>/checkpoints/<node>")
    v.add_argument("b")
    v.add_argument("--column", action="append", default=[], metavar="X,Z,Y0,Y1")

    c = sub.add_parser("compare", help="diff two entity dumps")
    c.add_argument("a")
    c.add_argument("b")

    args = ap.parse_args()
    if args.cmd == "compare":
        return compare(args.a, args.b)

    if args.cmd == "verify":
        verd = verify_checkpoint(args.a, args.b, args.column,
                                 str(Path(args.a)), str(Path(args.b)))
        bad = sorted(k for k, x in verd.items() if not x.startswith("MATCH"))
        print(("MATCH on every criterion" if not bad else f"DIFFERS on {bad}"))
        return 1 if bad else 0

    if args.cmd == "replay-leg":
        lin = Path(args.lineage).resolve()
        recs = [json.loads(l) for l in lin.read_text().splitlines() if l.strip()]
        match = [x for x in recs if x["node"] == args.node]
        if not match:
            raise SystemExit(f"no node {args.node} in {lin}")
        rec = match[-1]                     # a resumed run supersedes earlier records
        man = json.loads((lin.parent / "manifest.json").read_text())
        args.scenario = args.scenario or man["scenario_file"]
        args.replicas, args.generations = 1, 1
        args.keep_top, args.children, args.rng_seed_offset = 1, 1, 0
        args.stop_on_hit = False
        args.run_id = args.run_id or f"replay-leg-{args.node}"
        scn = runner.load_scenario(args.scenario)
        args.seeds = args.seeds or str(man["world"]["world_seed"])
        br = BranchRun(scn, args, runner.resolve_seeds(scn, args))
        log(f"replaying leg {rec['node']}: parent {rec['parent']}, resume at "
            f"gametime {rec['resume_at_gametime']}, reseed {rec['reseed']}, "
            f"target {rec['final_gametime']}, recorded hits {rec.get('hits')}")
        res = br.replay_leg(rec, lin.parent, max(1, args.repeat), args.column)
        return 0 if res and all(not r.get("differs") and not r.get("error")
                                for r in res) else 1

    if args.cmd == "replay":
        lin = Path(args.lineage).resolve()
        recs = [json.loads(l) for l in lin.read_text().splitlines() if l.strip()]
        match = [x for x in recs if x["node"] == args.node]
        if not match:
            raise SystemExit(f"no node {args.node} in {lin}; "
                             f"have {[x['node'] for x in recs]}")
        rec = match[-1]
        man = json.loads((lin.parent / "manifest.json").read_text())
        args.scenario = args.scenario or man["scenario_file"]
        args.replicas, args.generations = 1, len(rec["schedule"]) + 1
        args.keep_top, args.children, args.rng_seed_offset = 1, 1, 0
        args.stop_on_hit = False
        scn = runner.load_scenario(args.scenario)
        args.seeds = args.seeds or str(man["world"]["world_seed"])
        seeds = runner.resolve_seeds(scn, args)
        br = BranchRun(scn, args, seeds)
        log(f"replaying {rec['node']}: base seed {rec['base_rng_seed']}, "
            f"schedule {rec['schedule']}, final gametime {rec['final_gametime']}")
        br.replay(rec)
        return 0

    if getattr(args, "resume_from", None):
        if args.resume:
            raise SystemExit("--resume and --resume-from are mutually exclusive")
        if not args.run_id:
            raise SystemExit("--resume-from needs --run-id <new run id>; the whole "
                             "point is that the continuation is a separate run")
        clone_for_resume(args.resume_from, args.run_id)
        args.resume = args.run_id          # everything after this is a plain resume
    if args.resume:
        apply_resume(args)
    if not args.scenario:
        raise SystemExit("search needs a scenario file, or --resume <run-id>")
    scn = runner.load_scenario(args.scenario)
    seeds = runner.resolve_seeds(scn, args)
    br = BranchRun(scn, args, seeds)
    want = br.args.concurrency * RSS_PER_REPLICA_GIB
    free = int(re.search(r"Mem:\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)",
                         sh(["free", "-g"])).group(1))
    log(f"world seed {br.world_seed}; concurrency {args.concurrency} "
        f"(~{want:.0f} GiB of the {free} GiB available)")
    if want > free:
        log(f"WARNING concurrency {args.concurrency} wants ~{want:.0f} GiB but only "
            f"{free} GiB is available; lower --concurrency")
    return br.search()


if __name__ == "__main__":
    sys.exit(main())
