#!/usr/bin/env python3
"""Read one case.yaml and print shell assignments for sweep-pair.sh to eval.

The sweep cases are `kind: external` in the sense of test/cases/README.md: they bring
their own compose file and their own driver, because the generic runner cannot build
this world (it needs a flat level type, a second mod jar for the fake player, and a
per-tick function tag; none of those are in the schema).  Everything the schema DOES
define -- name, summary, kind, expect, ticks, checkpoints, world.*, jar.* -- is read
from here and used, so the case file is not a second, divergent dialect.

Fields under `self:` are the ones only this driver understands; they are documented in
test/cases/README.md under "Self-driven cases".

usage: sweep-case.py <case-name>        # prints KEY=value lines, safe to eval
"""
import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    import yaml
except ImportError:  # the runner venv has PyYAML; a bare python3 may not
    yaml = None


def flat_parse(text):
    """Enough YAML for these two files if PyYAML is missing: one level of nesting,
    scalars and flow lists. Nested keys come back as 'parent.child'."""
    out, parent = {}, None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.split("#")[0].rstrip() if " #" in raw else raw.rstrip()
        if ":" not in line:
            continue
        key, _, val = line.strip().partition(":")
        val = val.strip().strip('"').strip("'")
        if indent == 0:
            parent = key if not val else None
            if val:
                out[key] = val
        elif parent:
            out[f"{parent}.{key}"] = val
    return out


def load(name):
    path = HERE / "cases" / name / "case.yaml"
    text = path.read_text()
    if yaml is None:
        return flat_parse(text)
    doc = yaml.safe_load(text) or {}
    out = {}
    for k, v in doc.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{k}.{k2}"] = v2
        else:
            out[k] = v
    return out


def main():
    name = sys.argv[1]
    case = load(name)
    assets = case.get("inherit") or name
    base = load(assets) if assets != name else case

    def pick(key, default=""):
        v = case.get(key, base.get(key, default))
        return "" if v is None else v

    cps = pick("checkpoints") or [pick("ticks", 12000)]
    if isinstance(cps, str):
        cps = cps.strip("[]").replace(",", " ").split()
    flags = str(pick("jar.flags", ""))
    if str(pick("jar.trace_io", True)).lower() not in ("false", "0", "no"):
        # DetTrace.tickDigest() returns early without it, and that line is the whole
        # per-gametime comparison.
        flags = "-Ddetmc.traceIo=true " + flags

    vals = {
        "CASE_NAME": name,
        "ASSETS_CASE": assets,
        "EXPECT": pick("expect", "match"),
        "TICKS": pick("ticks", 12000),
        "CHECKPOINTS": " ".join(str(c) for c in cps),
        "CASE_FLAGS": flags.strip(),
        "WANT_PLAYER": str(pick("self.player", False)).lower(),
        "PACK_DIR": pick("self.pack", "pack"),
        "PACK_NAME": pick("self.pack_name", "detmc_sweep"),
        "PACK_NS": pick("self.pack_namespace", "detmc_sweep"),
        "PROBES": pick("self.probes", "probes.txt"),
        "SWEEP_SEED": pick("world.seed", 12345),
        "SWEEP_LEVEL_TYPE": pick("world.level_type", "minecraft:flat"),
        "SWEEP_MEMORY": pick("world.memory", "2G"),
        "SWEEP_VIEW": pick("world.view_distance", 4),
        "SWEEP_SIM": pick("world.simulation_distance", 4),
        "SWEEP_SPAWN_MOBS": str(pick("world.spawn_mobs", False)).lower(),
        "DETMC_SEED": pick("jar.seed", 1),
    }
    for k, v in vals.items():
        print(f"{k}={shlex.quote(str(v))}")


if __name__ == "__main__":
    main()
