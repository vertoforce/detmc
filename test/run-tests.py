#!/usr/bin/env python3
"""run-tests.py -- the detmc determinism test runner.

One entry point for every determinism check. A case is a directory under
test/cases/ holding a case.yaml; see test/cases/README.md for the format.

What a run does, per case:

  1. builds the mod jar once per invocation (docker-gradle, no host JDK)
  2. for each replica, in turn, never two at once:
       asks runner/memgate.py for a pair's worth of host memory
       starts one container from a generated compose file
       freezes ticks, sets the world up, waits for chunk loading to settle
       advances to each checkpoint by exact gametime, dumping every entity
       flushes the save and hashes the region files
  3. compares the replicas and prints PASS or FAIL with the first divergent
     gametime

Replicas run one at a time because a pair of 2 GB servers plus JVM overhead is
about 5 GB of a host that also runs the search fleet, and on 2026-09-12 an
ungated launch took the whole box down with an OOM. The gate still asks for the
full 5 GB before the first replica starts, so a case never gets halfway through
and then blocks.

Usage:
  test/run-tests.sh --list
  test/run-tests.sh --case baseline-24k
  test/run-tests.sh --all --junit results.xml
"""

import argparse
import gzip
import hashlib
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
import xml.etree.ElementTree as ET

import yaml

HERE = pathlib.Path(__file__).resolve().parent          # test/
REPO = HERE.parent
CASES = HERE / "cases"
RUNS = HERE / ".runs"
sys.path.insert(0, str(REPO / "runner"))
import memgate                                          # noqa: E402

# A pair of 2 GB servers plus JVM overhead. Asked for before every launch even
# though replicas run one at a time: a case that cannot afford its second half
# should wait at the start, not after twenty minutes of work.
PAIR_NEED_MB = 5000

# Ticks left for "tick step" to place exactly. "tick sprint" ends by returning the
# server to its previous rate, so there is a free-running window between the sprint
# finishing and the freeze landing, bounded by the poll interval. Everything inside
# the margin is stepped while frozen, which is exact.
SPRINT_MARGIN = 200

DEFAULTS = {
    "kind": "pair",
    "expect": "match",
    "timeout_s": 5400,
    "world": {
        "seed": 12345,
        "spawn_mobs": True,
        "view_distance": 4,
        "simulation_distance": 4,
        "memory": "2G",
    },
    "jar": {
        "seed": 1,
        "flags": "",
        "trace_io": False,
        "mod": True,
    },
}

KINDS = ("pair", "resume-pair", "external")
EXPECTS = ("match", "diverge", "resume-match")


# ---------------------------------------------------------------- case loading

def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_case(name):
    path = CASES / name / "case.yaml"
    if not path.exists():
        raise SystemExit(f"no such case: {name} (expected {path})")
    raw = yaml.safe_load(path.read_text()) or {}
    # A case that brings its own compose file brings its own driver: the generic
    # runner cannot know what that world needs. Such a case is external, and its
    # script defaults to test/<name>-pair.sh, called with the case name.
    if "kind" not in raw and ("compose" in raw or "script" in raw
                              or (HERE / f"{name}-pair.sh").exists()):
        raw = dict(raw, kind="external",
                   script=raw.get("script", f"{name}-pair.sh"))
    case = _merge(DEFAULTS, raw)
    case["name"] = raw.get("name", name)
    case["dir"] = CASES / name
    case["args"] = [str(a) for a in (raw.get("args") or [name])]
    if case["name"] != name:
        raise SystemExit(f"{path}: name '{case['name']}' does not match directory '{name}'")
    if case["kind"] not in KINDS:
        raise SystemExit(f"{path}: kind must be one of {KINDS}")
    if case["expect"] not in EXPECTS:
        raise SystemExit(f"{path}: expect must be one of {EXPECTS}")

    if case["kind"] == "external":
        if not case.get("script"):
            raise SystemExit(f"{path}: kind external needs a script")
        return case

    case["ticks"] = int(case.get("ticks") or 0)
    cps = [int(c) for c in (case.get("checkpoints") or [case["ticks"]])]
    if sorted(cps) != cps or cps[-1] != case["ticks"]:
        raise SystemExit(f"{path}: checkpoints must ascend and end at ticks ({case['ticks']})")
    case["checkpoints"] = cps

    reps = case.get("replicas") or {"A": {}, "B": {}}
    case["replicas"] = {k: (v or {}) for k, v in reps.items()}
    labels = list(case["replicas"])
    if len(labels) < 2:
        raise SystemExit(f"{path}: need at least two replicas to compare")

    if not case.get("compare"):
        case["compare"] = [{"a": labels[0], "b": labels[1], "expect": case["expect"]}]
    for c in case["compare"]:
        c.setdefault("expect", case["expect"])
        for side in ("a", "b"):
            if c[side] not in case["replicas"]:
                raise SystemExit(f"{path}: compare names unknown replica {c[side]}")

    if case.get("reseed_at") is not None and int(case["reseed_at"]) not in cps:
        raise SystemExit(f"{path}: reseed_at must be one of the checkpoints")
    if case["kind"] == "resume-pair":
        if not case.get("resume_at"):
            raise SystemExit(f"{path}: kind resume-pair needs resume_at")
        if int(case["resume_at"]) >= case["ticks"]:
            raise SystemExit(f"{path}: resume_at must be before ticks")
    return case


def all_cases():
    return sorted(p.name for p in CASES.iterdir() if (p / "case.yaml").exists())


# ------------------------------------------------------------------ docker I/O

def sh(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return subprocess.run(cmd, **kw)


def logs(ctr, tail=None, since=None):
    cmd = ["docker", "logs"]
    if tail:
        cmd += ["--tail", str(tail)]
    if since:
        cmd += ["--since", str(since)]
    cmd.append(ctr)
    p = sh(cmd)
    return (p.stdout or "") + (p.stderr or "")


def rcon(ctr, cmd, timeout=180):
    """One rcon command. A busy server can leave the reply hanging (save-all
    flush does exactly that), so a timeout is an outcome, not an error."""
    try:
        p = sh(["docker", "exec", ctr, "rcon-cli", cmd], timeout=timeout)
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return "<rcon timed out>"


# ------------------------------------------------------------- compose writing

def compose_for(case, label, rundir):
    """Generate a one-service compose file for this replica.

    compose-det.yml stays the single source of image pin, log driver and the
    environment every replica shares; this only overrides what the case names.
    RCON_PASSWORD is left as the literal ${RCON_PASSWORD} and resolved by docker
    compose from test/.env, so the password is never read into this process."""
    base = yaml.safe_load((HERE / "compose-det.yml").read_text())
    svc = dict(base["services"]["mcA"])

    rep = case["replicas"][label]
    world = _merge(case["world"], rep.get("world"))
    jar = _merge(case["jar"], rep.get("jar"))

    env = {}
    for item in svc.get("environment", []):
        k, _, v = str(item).partition("=")
        env[k] = v
    env["SEED"] = str(world["seed"])
    env["MEMORY"] = str(world["memory"])
    env["VIEW_DISTANCE"] = str(world["view_distance"])
    env["SIMULATION_DISTANCE"] = str(world["simulation_distance"])
    env["SPAWN_MONSTERS"] = "true" if world["spawn_mobs"] else "false"

    opts = ["-Dmax.bg.threads=1"]
    if jar["mod"]:
        opts += [f"-Ddetmc.seed={jar['seed']}", "-Ddetmc.freezeOnStart=true"]
        if jar["trace_io"]:
            opts.append("-Ddetmc.traceIo=true")
    if jar["flags"]:
        opts.append(str(jar["flags"]))
    if rep.get("jvm_add"):
        opts.append(str(rep["jvm_add"]))
    env["JVM_OPTS"] = " ".join(opts)

    mods = "../../mods" if jar["mod"] else "./nomods"
    (rundir / "nomods").mkdir(exist_ok=True)
    svc.update({
        "container_name": f"mc-det-{label}",
        "environment": env,
        "volumes": [f"./data-{label}:/data", f"{mods}:/data/mods"],
    })
    doc = {"services": {f"mc{label}": svc}}
    path = rundir / f"compose-{label}.yml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path, world, jar


def compose(path, *args):
    return sh(["docker", "compose", "-f", str(path.relative_to(HERE)),
               "--env-file", ".env", *args], cwd=HERE)


# --------------------------------------------------------------- driving a run

class Replica:
    """One server: start it, set the world up, walk it to each checkpoint."""

    def __init__(self, case, label, rundir, log):
        self.case, self.label, self.rundir, self.log = case, label, rundir, log
        self.ctr = f"mc-det-{label}"
        self.out = []
        self.deadline = time.time() + case["timeout_s"]

    def say(self, msg):
        self.log(f"  [{self.label}] {msg}")

    def section(self, title):
        self.out.append(f"### {title}")

    def rc(self, cmd):
        self.out.append(f">>> {cmd}")
        reply = rcon(self.ctr, cmd)
        self.out.append(reply)
        return reply

    def write(self, path):
        path.write_text("\n".join(self.out) + "\n", encoding="utf-8", errors="replace")

    def expired(self):
        return time.time() > self.deadline

    # -- lifecycle ---------------------------------------------------------

    def wait_started(self, boots=1):
        """Block until the server is accepting commands. After a restart the log
        still holds the first boot's 'Done (', so count rather than match."""
        while not self.expired():
            text = logs(self.ctr)
            if text.count("Done (") >= boots:
                return True
            if "Minecraft server failed" in text:
                self.section(f"CONTAINER_FAILED {self.ctr}")
                return False
            time.sleep(0.5)
        self.section(f"TIMEOUT waiting for {self.ctr} to start")
        return False

    def gametime(self):
        nums = re.findall(r"\d+", self.rc_quiet("time query gametime"))
        return int(nums[-1]) if nums else -1

    def rc_quiet(self, cmd):
        return rcon(self.ctr, cmd)

    def settle(self):
        """Chunk loading keeps running while ticks are frozen, so the entity set
        is still growing right after a forceload. Wait until the entity list stops
        changing, or two replicas get dumped at different points of one sequence.
        Thirty stable polls: a three-second lull mid-forceload is common, because
        chunk generation is serial under detmc."""
        prev, same = None, 0
        for i in range(900):
            now = hashlib.md5(
                self.rc_quiet("execute as @e run data get entity @s Pos").encode()).hexdigest()
            same = same + 1 if now == prev else 0
            if same >= 30:
                break
            prev = now
            time.sleep(0.5)
        self.section(f"settled after {i + 1} polls")

    def dump(self, tag):
        for title, cmd in (
            ("enderman Pos", "execute as @e[type=enderman] run data get entity @s Pos"),
            ("zombie Pos", "execute as @e[type=zombie] run data get entity @s Pos"),
            ("all entities UUID", "execute as @e run data get entity @s UUID"),
            ("all entities id", "execute as @e run data get entity @s id"),
            ("all entities Motion", "execute as @e run data get entity @s Motion"),
            ("enderman carriedBlockState",
             "execute as @e[type=enderman] run data get entity @s carriedBlockState"),
        ):
            self.section(f"[{tag}] {title}")
            self.rc(cmd)

    # -- advancing ticks ---------------------------------------------------

    def sprint(self, ticks):
        """Run `ticks` ticks as fast as the server will go, then freeze.

        The end of the sprint is read from gametime over rcon, not from a
        "Sprint completed" line in the container log. Measured 2026-09-12: with
        -Ddetmc.traceIo=true the server writes hundreds of lines per tick, so the
        completion line falls out of any bounded log tail within one poll, the wait
        never ends and the server free-runs. One 600-tick checkpoint landed on
        gametime 2344 that way. Gametime is read from the world itself."""
        target = self.gametime() + ticks
        self.rc_quiet("tick unfreeze")
        self.rc_quiet(f"tick sprint {ticks}")
        while not self.expired():
            if self.gametime() >= target:
                break
            time.sleep(0.5)
        self.rc_quiet("tick freeze")

    def goto(self, target):
        """Land on exactly `target` gametime.

        Sprint to short of it, freeze, then close the gap with 'tick step', which
        runs exactly N ticks while frozen and stays frozen. Without the margin the
        two free-running windows around a sprint give the replicas a different
        number of gravity ticks: measured, two runs at nominal t1 held endermen
        with Motion y -0.447 and -0.652. That was the harness, not the mod."""
        need = target - self.gametime()
        if need > SPRINT_MARGIN:
            self.sprint(need - SPRINT_MARGIN)
        need = target - self.gametime()
        if need > 0:
            # One step only: re-issuing while frozenTicksToRun is counting down
            # overwrites the counter and overshoots.
            self.rc_quiet(f"tick step {need}")
            prev, stalls = -1, 0
            while not self.expired():
                now = self.gametime()
                if now >= target:
                    break
                stalls = stalls + 1 if now == prev else 0
                if stalls >= 20:       # 10 s of no progress: the step ended early
                    break
                prev = now
                time.sleep(0.5)
        now = self.gametime()
        if now != target:
            self.section(f"WARNING: wanted gametime {target}, landed on {now}")

    # -- the whole run -----------------------------------------------------

    def setup_world(self, world, datadir):
        self.section("detmc startup lines")
        for line in logs(self.ctr).splitlines():
            if "detmc" in line.lower() or re.search(r"Loading .* mods", line):
                self.out.append(line)
        self.rc("tick freeze")
        self.rc("time query gametime")
        self.rc("gamerule advance_time false")
        self.rc("time set midnight")
        self.rc("weather clear 1000000")
        self.rc(f"gamerule spawn_mobs {'true' if world['spawn_mobs'] else 'false'}")
        cmds = self.case_commands()
        # @settle splits the setup. Everything before it is world shape -- forceload,
        # gamerules -- and the wait that follows lets chunk loading finish. Everything
        # after it, typically summons, then lands in the same order on every replica.
        # Without that split a summon can interleave with a chunk-driven entity add at
        # a different point in each run, which is a divergence the harness created.
        if "@settle" not in cmds:
            cmds = cmds + ["@settle"]
        for cmd in cmds:
            if cmd == "@settle":
                self.settle()
                self.section("pre-existing entities")
                self.rc("execute as @e run data get entity @s Pos")
            else:
                self.rc(cmd)
        self.install_function(datadir)

    def install_function(self, datadir):
        """An optional setup.mcfunction, for setup that a flat command list cannot
        express. Written into the world after the first boot, when the world dir
        exists, then loaded with /reload."""
        src = self.case["dir"] / "setup.mcfunction"
        if not src.exists():
            return
        pack = datadir / "world" / "datapacks" / "detmc-test"
        fn = pack / "data" / "detmc" / "function"
        fn.mkdir(parents=True, exist_ok=True)
        (pack / "pack.mcmeta").write_text(
            '{"pack": {"pack_format": 88, "description": "detmc test setup"}}\n')
        (fn / "setup.mcfunction").write_text(src.read_text())
        self.rc("reload")
        self.section("setup.mcfunction")
        self.rc("function detmc:setup")

    def case_commands(self):
        path = self.case["dir"] / "setup.commands"
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.replace("{{label}}", self.label)
                           .replace("{{seed}}", str(self.case["world"]["seed"])))
        return out

    def save_and_hash(self, datadir, prefix):
        """Flush the save, then hash every saved chunk's payload.

        Not a hash of the .mca files. Measured over four pairs (STATUS, "no chunk
        has ever differed in content"), a file-level hash is sensitive to two
        things that are not world state: the wall-clock timestamp table, and where
        inside the file a chunk landed, which follows the order chunks were
        written in. One 1,000-tick pair failed on exactly that, with all 1,874
        chunks byte-identical. Chunk payloads are the state."""
        since = int(time.time()) - 1
        self.rc("save-all flush")
        for _ in range(600):
            if logs(self.ctr, tail=400, since=since).count("saveEverything done") > 0:
                break
            time.sleep(0.5)
        time.sleep(1)
        world = datadir / "world" / "dimensions" / "minecraft" / "overworld"
        self.section(CHUNKS)
        self.out += chunk_payloads(world)
        self.section("detmc-rng.properties after save")
        sidecar = datadir / "world" / "detmc-rng.properties"
        if sidecar.exists():
            self.out += sorted(l for l in sidecar.read_text().splitlines()
                               if l and not l.startswith("#"))
        for tag, marker in (("dg", "[detmc-dg]"), ("io", "[detmc-io]")):
            keep = [l.split(marker, 1)[1].strip()
                    for l in logs(self.ctr).splitlines() if marker in l]
            (self.rundir / f"{tag}-{prefix}-{self.label}.txt").write_text("\n".join(keep) + "\n")
        self.section("RUN_FINISHED")


CHUNKS = "chunk payloads"


def chunk_payloads(world_dir):
    """One line per saved chunk: `<dir>/<file> <x>,<z> <sha1-12>`.

    The payload is the decompressed chunk bytes with no NBT parsing, so two saves
    holding different blocks always differ here. Sector offsets are deliberately
    left out: a byte-identical chunk that landed elsewhere in the file is not a
    divergence."""
    out = []
    if not world_dir.exists():
        return ["<no world dir>"]
    for path in sorted(world_dir.rglob("*.mca")):
        data = path.read_bytes()
        if len(data) < 8192:
            continue                       # region file with nothing written yet
        for i in range(1024):
            off = struct.unpack_from(">I", data, i * 4)[0] >> 8
            if off == 0 or (off + 1) * 4096 > len(data):
                continue
            length = struct.unpack_from(">I", data, off * 4096)[0]
            comp = data[off * 4096 + 4]
            blob = data[off * 4096 + 5: off * 4096 + 4 + length]
            try:
                if comp == 2:
                    blob = zlib.decompress(blob)
                elif comp == 1:
                    blob = gzip.decompress(blob)
            except Exception:
                pass                       # hash what is on disk, whatever it is
            out.append(f"{path.parent.name}/{path.name} {i % 32},{i // 32} "
                       f"{hashlib.sha1(blob).hexdigest()[:12]}")
    return sorted(out)


# -------------------------------------------------------------- the case runner

def run_replica(case, label, rundir, log):
    """Drive one replica start to finish. Returns the artifact prefix map."""
    path, world, jar = compose_for(case, label, rundir)
    datadir = rundir / f"data-{label}"
    shutil.rmtree(datadir, ignore_errors=True)
    datadir.mkdir(parents=True)

    log(f"  [{label}] memgate: asking for {PAIR_NEED_MB} MB")
    try:
        memgate.acquire(PAIR_NEED_MB, timeout_s=case["timeout_s"])
    except TimeoutError as exc:
        log(f"  [{label}] memgate timed out: {exc}")
        return False
    sh(["docker", "rm", "-f", f"mc-det-{label}"])   # a stale container from an
    compose(path, "up", "-d")                      # interrupted earlier run
    r = Replica(case, label, rundir, log)
    try:
        if not r.wait_started():
            r.write(rundir / f"result-{label}.txt")
            return False
        r.setup_world(world, datadir)
        base = r.gametime()
        r.section(f"tick base {base}")
        log(f"  [{label}] setup done at gametime {base}")

        reseed_at = case.get("reseed_at")
        reseed_val = case["replicas"][label].get("reseed")
        resume_at = case.get("resume_at") if case["kind"] == "resume-pair" else None

        first_leg = [c for c in case["checkpoints"] if resume_at is None or c <= resume_at]
        if resume_at and resume_at not in first_leg:
            first_leg.append(resume_at)
        for cp in sorted(set(first_leg)):
            r.goto(base + cp)
            r.section(f"[t{cp}] gametime")
            r.rc("time query gametime")
            if reseed_at is not None and cp == reseed_at:
                r.dump(f"t{cp}-pre-reseed")
                r.section(f"[t{cp}] rng before reseed")
                r.rc("detmc rng")
                r.rc(f"detmc reseed {reseed_val}")
                r.section(f"[t{cp}] rng after reseed")
                r.rc("detmc rng")
            else:
                r.dump(f"t{cp}")
            log(f"  [{label}] checkpoint t{cp} done")

        r.save_and_hash(datadir, "pre" if resume_at else "run")
        r.write(rundir / (f"pre-{label}.txt" if resume_at else f"result-{label}.txt"))

        if not resume_at:
            return True

        # -- replay from save -------------------------------------------------
        # Stop and restart from the replica's own save, then carry on. No setup
        # commands: forceload, gamerules, time and weather are in the save, and
        # re-issuing them would be work the uninterrupted timeline never did.
        log(f"  [{label}] stopping at gametime {base + resume_at}, restarting from the save")
        sh(["docker", "stop", "-t", "180", r.ctr])
        sh(["docker", "start", r.ctr])
        r2 = Replica(case, label, rundir, log)
        r2.out = []
        if not r2.wait_started(boots=2):
            r2.write(rundir / f"resume-{label}.txt")
            return False
        r2.section("detmc restart lines")
        r2.out += [l for l in logs(r2.ctr).splitlines()
                   if re.search(r"detmc.*(restored|master seed|world seed|no detmc-rng)",
                                l, re.I)][-6:]
        r2.section(f"resumed gametime {r2.gametime()}")
        for cp in [c for c in case["checkpoints"] if c > resume_at]:
            r2.goto(base + cp)
            r2.section(f"[t{cp}] gametime")
            r2.rc("time query gametime")
            r2.dump(f"t{cp}")
            log(f"  [{label}] resumed checkpoint t{cp} done")
        r2.save_and_hash(datadir, "resume")
        r2.write(rundir / f"resume-{label}.txt")
        return True
    finally:
        compose(path, "down")


# ------------------------------------------------------------------ comparison

def sections(path):
    out, order, cur = {}, [], None
    if not path.exists():
        return out, order
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("###"):
            cur = line[3:].strip()
            if cur not in out:
                out[cur] = []
                order.append(cur)
            continue
        if cur is None or line.startswith(">>>"):
            continue
        out[cur].append(line)
    return out, order


def clean(key, lines):
    """Entity dumps are rcon echoes; keep the data lines and drop empties. Hash
    and rng sections are compared verbatim."""
    if (key == CHUNKS or "detmc-rng.properties" in key
            or "rng " in key or key.endswith("gametime")):
        # The gametime reply is compared verbatim on purpose: if one replica lands a
        # tick short of the checkpoint, the dumps that follow are not comparable and
        # the run should say so rather than quietly compare two different moments.
        return [l for l in lines if l.strip()]
    return [l for l in lines if l.strip() and "has the following entity data" in l]


def first_divergent_gametime(rundir, prefix, a, b):
    """The per-tick digest stream dates a divergence to one gametime. Only
    written when the case sets trace_io."""
    fa = rundir / f"dg-{prefix}-{a}.txt"
    fb = rundir / f"dg-{prefix}-{b}.txt"
    if not (fa.exists() and fb.exists()):
        return None
    # Keyed on gametime, never on line number. The `t=` counter in the trace counts
    # every server tick including the frozen ones the harness spends issuing setup
    # commands, so two replicas reach the same gametime after a different number of
    # lines: 2,301 against 1,989 on one 1,000-tick pair whose worlds were identical.
    def by_gametime(path):
        out = {}
        for line in path.read_text(errors="replace").splitlines():
            m = re.search(r"gt=(\d+).*?\bh=(\w+)", line)
            if m:
                out[int(m.group(1))] = m.group(2)    # last digest at that gametime
        return out

    a, b = by_gametime(fa), by_gametime(fb)
    shared = sorted(set(a) & set(b))
    if not shared:
        return None
    for gt in shared:
        if a[gt] != b[gt]:
            return gt
    return None


def compare(case, rundir, a, b, report):
    """True when the two replicas are identical everywhere compared."""
    prefix = "resume" if case["kind"] == "resume-pair" else "result"
    fa = rundir / f"{prefix}-{a}.txt"
    fb = rundir / f"{prefix}-{b}.txt"
    A, order = sections(fa)
    B, _ = sections(fb)
    if not order or not B:
        report.append(f"    no output from {a} or {b}: the run did not finish")
        return False, None
    same_everywhere, first_bad = True, None
    for k in order:
        if k not in B:
            continue
        x, y = clean(k, A[k]), clean(k, B[k])
        if k == CHUNKS:
            # Compare the chunks both saves hold. A chunk present in only one save
            # is not a divergence: a chunk unloaded while still EMPTY is never
            # written, one that got further is, and which of the two happens to a
            # neighbour outside the forceload follows shutdown timing. Measured
            # over four pairs, no chunk has ever differed in content while
            # presence differed by up to 34 chunks per file.
            xa = dict((l.rsplit(" ", 1)[0], l.rsplit(" ", 1)[1]) for l in x)
            yb = dict((l.rsplit(" ", 1)[0], l.rsplit(" ", 1)[1]) for l in y)
            shared = sorted(set(xa) & set(yb))
            bad = [c for c in shared if xa[c] != yb[c]]
            only = len(set(xa) ^ set(yb))
            report.append(f"    {k}: {len(shared)} chunks in both, {len(bad)} differ"
                          + (f", {only} present in only one save (not state)" if only else ""))
            if bad:
                same_everywhere = False
                report.append(f"    DIFFER {k}: first is {bad[0]}")
            continue
        if x == y:
            continue
        same_everywhere = False
        n = sum(1 for p, q in zip(x, y) if p != q)
        report.append(f"    DIFFER {k}: {n} of {min(len(x), len(y))} lines "
                      f"(len {len(x)} vs {len(y)})")
        m = re.match(r"\[t(\d+)", k)
        if m and first_bad is None:
            first_bad = int(m.group(1))
    gt = first_divergent_gametime(rundir, "resume" if case["kind"] == "resume-pair" else "run", a, b)
    if gt is None and first_bad is not None:
        gt = first_bad
    return same_everywhere, gt


# ------------------------------------------------------------------- the driver

def build_jar(log):
    log("building the mod jar (docker-gradle)")
    p = sh([str(REPO / "docker-gradle.sh"), ":fabric:build"])
    if p.returncode != 0:
        raise SystemExit((p.stdout or "") + (p.stderr or ""))
    mods = HERE / "mods"
    mods.mkdir(exist_ok=True)
    for old in mods.glob("*.jar"):
        old.unlink()
    jars = sorted((REPO / "fabric" / "build" / "libs").glob("detmc-*.jar"))
    if not jars:
        raise SystemExit("no detmc jar was built")
    shutil.copy(jars[-1], mods / jars[-1].name)
    log(f"jar: {jars[-1].name}")


def ensure_env():
    """test/.env holds only RCON_PASSWORD and is gitignored, so a fresh clone or
    a CI runner has none. Generate one and never read it back."""
    env = HERE / ".env"
    if not env.exists():
        import secrets
        env.write_text(f"RCON_PASSWORD={secrets.token_hex(16)}\n")
        env.chmod(0o600)


def run_case(name, log, compare_only=False):
    case = load_case(name)
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    report = []

    if case["kind"] == "external":
        log(f"  external driver: {case['script']} {' '.join(case['args'])}")
        p = subprocess.run(["bash", case["script"], *case["args"]], cwd=HERE)
        ok = p.returncode == 0
        return ok, time.time() - t0, [f"    external script exit {p.returncode}"]

    if not compare_only:
        for label in case["replicas"]:
            if not run_replica(case, label, rundir, log):
                return False, time.time() - t0, [f"    replica {label} did not finish"]

    ok = True
    for c in case["compare"]:
        identical, gt = compare(case, rundir, c["a"], c["b"], report)
        want = c["expect"]
        good = identical if want in ("match", "resume-match") else not identical
        where = "" if gt is None else f", first divergent gametime {gt}"
        report.append(f"    {c['a']} vs {c['b']}: expected {want}, got "
                      f"{'identical' if identical else 'different'}{where}"
                      f"  -> {'ok' if good else 'WRONG'}")
        ok = ok and good
    return ok, time.time() - t0, report


def junit(path, results):
    suite = ET.Element("testsuite", name="detmc-determinism",
                       tests=str(len(results)),
                       failures=str(sum(1 for r in results if not r["ok"])),
                       time=f"{sum(r['secs'] for r in results):.1f}")
    for r in results:
        tc = ET.SubElement(suite, "testcase", classname="determinism",
                           name=r["name"], time=f"{r['secs']:.1f}")
        if not r["ok"]:
            ET.SubElement(tc, "failure", message="determinism check failed").text = \
                "\n".join(r["report"])
        else:
            ET.SubElement(tc, "system-out").text = "\n".join(r["report"])
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="detmc determinism test runner")
    ap.add_argument("--list", action="store_true", help="print the cases and exit")
    ap.add_argument("--case", action="append", default=[], help="run one case, repeatable")
    ap.add_argument("--all", action="store_true", help="run every case")
    ap.add_argument("--junit", metavar="FILE", help="write a JUnit XML report")
    ap.add_argument("--no-build", action="store_true", help="use the jar already in test/mods")
    ap.add_argument("--compare-only", action="store_true",
                    help="re-print a verdict from the artifacts of an earlier run, "
                         "starting no servers")
    args = ap.parse_args(argv)

    if args.list:
        for name in all_cases():
            c = load_case(name)
            print(f"{name:20s} {c['kind']:12s} expect {c['expect']:13s} "
                  f"{(c.get('summary') or '').strip().splitlines()[0] if c.get('summary') else ''}")
        return 0

    names = args.case or (all_cases() if args.all else [])
    if not names:
        ap.error("give --case NAME, --all or --list")

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    ensure_env()
    RUNS.mkdir(exist_ok=True)
    if not (args.no_build or args.compare_only):
        build_jar(log)

    results = []
    for name in names:
        log(f"=== {name} ===")
        ok, secs, report = run_case(name, log, compare_only=args.compare_only)
        results.append({"name": name, "ok": ok, "secs": secs, "report": report})
        for line in report:
            print(line)
        print(f"{'PASS' if ok else 'FAIL'}  {name}  ({secs / 60:.1f} min)", flush=True)

    print()
    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['name']:20s} {r['secs'] / 60:6.1f} min")
    if args.junit:
        junit(args.junit, results)
        print(f"junit: {args.junit}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
