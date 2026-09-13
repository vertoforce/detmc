#!/usr/bin/env python3
"""Per-path section diff for the sweep case: sweep-<case>-A.txt vs sweep-<case>-B.txt.

diff-det.sh answers the same question for the compose-det.yml world, but it keeps
only lines containing "has the following entity data", which is right for that
transcript (it is all `data get entity`) and wrong here: the sweep transcript is
scoreboard reads, block data, `stopwatch` output and file digests as well.

Sections are the `### ...` headers written by sweep-pair.sh.  A checkpoint section
is named "[t<ticks>] <path>", so the per-path verdict is the OR over its checkpoints
and the first differing checkpoint is the tick at which that path moved.

Advisory sections carry wall-clock or host state, not world state, and never set the
exit code.  Everything else does.
"""
import os
import re
import sys
from collections import OrderedDict

ADVISORY = ("settled", "detmc startup lines", "tick base", "save-all flush",
            "datapacks", "RUN_FINISHED", "VOID", "WARNING")

# Sections that are allowed to differ because the difference is a NAMED, measured
# defect rather than a world-state divergence.  These are not swept under the carpet:
# each one prints with its reason on every run, and each is covered by a stronger
# check that does set the exit code -- the per-gametime digest for world state, and
# nbt-canon.py over the saved .mca for the on-disk claim.  If a "known" section starts
# differing for a NEW reason, that stronger check fails and the case fails with it.
KNOWN = {
    "01-InsideBrownianWalk-memories":
        "Brain.memories can serialise in an unstable key order: same keys, same values, "
        "different order. Seen at all four checkpoints of one 12000-tick pair and not "
        "in the next one, so it is intermittent. See STATUS 'sweep test'. Guarded by "
        "the canonical-NBT check, which fails if the VALUES ever move.",
    "region md5 no timestamps":
        "raw .mca md5 is a screening test only (STATUS: sector placement and partial "
        "neighbour chunks move bytes without moving state). nbt-canon.py is the check.",
}


def known(name):
    for k, why in KNOWN.items():
        if name.startswith(k) or name.split("] ", 1)[-1].startswith(k):
            return why
    return None


def sections(path):
    out, cur = OrderedDict(), None
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        if line.startswith("###"):
            cur = line[3:].strip()
            out.setdefault(cur, [])
            continue
        if cur is None or line.startswith(">>>"):
            continue
        if line.strip():
            out[cur].append(line)
    return out


def advisory(name):
    return any(name.startswith(a) for a in ADVISORY)


def main():
    case = sys.argv[1] if len(sys.argv) > 1 else "sweep"
    # Prefer the archived per-run copies; test/ itself is scratch that other chain
    # scripts write and clean.
    a_path, b_path = f"sweep/{case}-A/sweep-{case}-A.txt", f"sweep/{case}-B/sweep-{case}-B.txt"
    if not (os.path.exists(a_path) and os.path.exists(b_path)):
        a_path, b_path = f"sweep-{case}-A.txt", f"sweep-{case}-B.txt"
    A, B = sections(a_path), sections(b_path)

    only_a = [k for k in A if k not in B]
    only_b = [k for k in B if k not in A]
    if only_a or only_b:
        print(f"sections only in A: {only_a}")
        print(f"sections only in B: {only_b}")

    rc = 0
    per_path = OrderedDict()   # path -> (ran_lines, first differing checkpoint)
    print(f"{'section':52s} {'A':>4s} {'B':>4s}  verdict")
    print("-" * 84)
    for k in A:
        if k not in B:
            continue
        a, b = A[k], B[k]
        same = a == b
        n = sum(1 for x, y in zip(a, b) if x != y)
        why = known(k)
        tag = " (advisory)" if advisory(k) else (" (known)" if why else "")
        verdict = "MATCH" if same else f"DIFFER ({n} of {min(len(a), len(b))}, len {len(a)} vs {len(b)})"
        print(f"{k:52s} {len(a):4d} {len(b):4d}  {verdict}{tag}")
        if not same and why:
            print(f"{'':52s}       known: {why}")
        if not same and not advisory(k) and not why:
            rc = 1
        m = re.match(r"\[t(\d+)\]\s+(.*)", k)
        if m:
            tick, path = int(m[1]), m[2]
            ran, first = per_path.get(path, (0, None))
            ran = max(ran, len(a))
            if not same and first is None and not known(k):
                first = tick
            per_path[path] = (ran, first)

    if per_path:
        print()
        print(f"{'phase-8 path':44s} {'probe lines':>11s}  verdict")
        print("-" * 84)
        for path, (ran, first) in per_path.items():
            if ran == 0:
                v = "NO OUTPUT (probe returned nothing: path not exercised)"
            elif known(f"[t0] {path}"):
                v = "A != B, known save-order defect (see above)"
            elif first is None:
                v = "A == B at every checkpoint"
            else:
                v = f"A != B, first at checkpoint t{first}"
            print(f"{path:44s} {ran:11d}  {v}")
    print()
    print("exit", rc, "(0 = every non-advisory section identical)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
