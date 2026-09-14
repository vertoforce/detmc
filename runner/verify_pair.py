#!/usr/bin/env python3
"""Two containers, one parent checkpoint, one reseed: do they agree?

`branch.py replay-leg` answers "is this leg reproducible" by comparing a replay with
the leg's own recorded checkpoint, and it runs the leg's recorded length.  This runs
the same leg TWICE from the same parent and compares the two arms with each other,
for a length you choose and with whatever JVM flags you want on both of them.  That is
the shape every determinism question about a resumed node has needed:

    # date a divergence to a tick, then name the call site that caused it
    python3 runner/verify_pair.py --run-id probe --ticks 1200 \\
        --opts '-Ddetmc.traceIo=true -Ddetmc.traceDraws=8736001:8737201'

Server logs are kept under `runs/<run-id>/serverlogs/<arm>-latest.log`, which the
harness would otherwise delete with the data directory, because the per-tick digest and
the draw trace only exist there.

Defaults point at `hunt-villager-wave1`'s `g181n0`, the natural-village checkpoint the
2026-09-13 `AcquirePoi` root-cause used.  The control path is the default: no fake
player, because a player is one more thing that has to be identical.
"""
import argparse, json, os, shutil, sys, time, types
from pathlib import Path

RUNNER = Path(__file__).resolve().parent
PROJECT = RUNNER.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-id", required=True, help="runs/<run-id>, must not exist yet")
    ap.add_argument("--ticks", type=int, default=1200, help="segment length (default 1200)")
    ap.add_argument("--opts", default="", help="extra JVM flags, given to BOTH arms")
    ap.add_argument("--source-run", default="hunt-villager-wave1")
    ap.add_argument("--parent", default="g181n0")
    ap.add_argument("--reseed", type=int, default=548001651)
    ap.add_argument("--resume-at", type=int, default=8736001, help="parent checkpoint gametime")
    ap.add_argument("--base-rng-seed", type=int, default=1451989592335860511)
    ap.add_argument("--world-seed", default="3795784043239602249")
    ap.add_argument("--generation", type=int, default=182)
    ap.add_argument("--scenario", default=str(RUNNER / "scenarios" / "enderman_villager_trap.yaml"))
    ap.add_argument("--jar", default=str(PROJECT / "fabric" / "build" / "libs" / "detmc-0.1.0.jar"))
    ap.add_argument("--fake-player", action="store_true",
                    help="re-spawn the scenario's fake players (default: control path, none)")
    args = ap.parse_args()

    os.environ["DETMC_EXTRA_OPTS"] = args.opts
    sys.path.insert(0, str(RUNNER))
    import run as runner                      # noqa: E402
    import branch                             # noqa: E402

    if not args.fake_player:
        runner.Replica.spawn_fake_players = lambda self, v, **kw: []

    src = RUNNER / "runs" / args.source_run
    saved = RUNNER / "runs" / args.run_id / "serverlogs"

    br_args = types.SimpleNamespace(
        run_id=args.run_id, segment_ticks=args.ticks, dump_at="", locate_at="",
        frames_at="", memory=None, jar=args.jar, concurrency=1, keep=False,
        start_timeout=900, stop_on_hit=False, replicas=1, generations=1, keep_top=1,
        children=1, rng_seed_offset=0, seeds=args.world_seed, reseed="on",
        scenario=args.scenario, prefilter_timeout=3600, resume=None, resume_from=None,
        keep_data=False)

    scn = runner.load_scenario(args.scenario)
    br = branch.BranchRun(scn, br_args, runner.resolve_seeds(scn, br_args))

    # The data directory carries the only copy of the server log, and run_wave deletes
    # it.  Copy the log out on the way past.
    saved.mkdir(parents=True, exist_ok=True)
    rmtree = branch.shutil.rmtree

    def keep_log(path, **kw):
        p = Path(path)
        lg = p / "logs" / "latest.log"
        if lg.exists():
            shutil.copy(lg, saved / f"{p.name}-latest.log")
        rmtree(path, **kw)

    branch.shutil.rmtree = keep_log

    br.stage_mods()
    if not (br.dir / "checkpoints" / args.parent).exists():
        shutil.copytree(src / "checkpoints" / args.parent, br.dir / "checkpoints" / args.parent)
    if (src / "template").exists() and not br.template.exists():
        shutil.copytree(src / "template", br.template, symlinks=True)
    (br.dir / "manifest.json").write_text(json.dumps({
        "run_id": args.run_id, "mode": "verify-pair", "source_run": args.source_run,
        "parent": args.parent, "resume_at_gametime": args.resume_at,
        "reseed": args.reseed, "segment_ticks": args.ticks,
        "base_rng_seed": args.base_rng_seed, "jvm_opts": args.opts,
        "fake_player": args.fake_player, "image": runner.IMAGE,
        "mod_sha256": br.jar_sha, "world": br.world,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2, default=str))

    made = []
    for i in (0, 1):
        node = branch.Node(f"v{i}", args.generation, br.next_ordinal(), args.base_rng_seed,
                           parent=args.parent, reseed=args.reseed, schedule=[],
                           resume_gametime=args.resume_at)
        t0 = time.time()
        br.run_wave([node], f"pair-{node.id}")
        if node.error:
            print(f"NODE {node.id} FAILED: {node.error}", flush=True)
            return 1
        print(f"=== {node.id}: {time.time() - t0:.0f}s wall  gametime={node.final_gametime}",
              flush=True)
        print("scores:", json.dumps(node.scores), flush=True)
        made.append(node)

    a = br.dir / "checkpoints" / made[0].id
    b = br.dir / "checkpoints" / made[1].id
    verd = branch.verify_checkpoint(a, b, (), made[0].id, made[1].id, show=True)
    bad = sorted(k for k, x in verd.items() if not x.startswith("MATCH"))
    print(f"\n=== {args.ticks}-tick pair verdict ===", flush=True)
    print(json.dumps(verd, indent=1), flush=True)
    print("ALL CRITERIA MATCH" if not bad else f"DIFFERS on {bad}", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
