#!/usr/bin/env python3
"""First divergent gametime over the per-tick [detmc-dg] digest, for two runs.

Same comparison as test/analyze-probe.py (which hardcodes test/dg-A.txt and
test/dg-B.txt); this one takes paths so it can compare a local run against one
copied back from another machine.

tickCount is not comparable across runs: ticks also advance while the server is
frozen and the two runs sit frozen for a different number of ticks, so every line
is re-keyed on gametime, last tick at that gametime winning, exactly as
analyze-probe.py does.

Reported twice:
  digest    n= and h=   (the per-tick entity digest: uuid, type, id, pos, motion,
                         rot, tickCount, entity RNG draws+state)
  levelRng  the level RNG's draw count and state
  full      the whole dg line minus t=. ADVISORY ONLY: it also carries clockTasks,
            a count of server-queue tasks run in that tick, which differs even
            between two same-host runs whose world state is identical
            (measured: phase7/M24kA vs phase7/M24kB differ at gametime 4).
            The exit code ignores it.
"""
import re, sys

def load(path):
    dg, rng, full = {}, {}, {}
    for line in open(path):
        line = line.strip()
        m = re.match(r"t=(-?\d+) gt=(\d+) n=(\d+) h=(\w+)", line)
        if not m:
            continue
        gt = int(m[2])
        dg[gt] = (int(m[3]), m[4])
        r = re.search(r"levelRng=(\S+)", line)
        rng[gt] = r[1] if r else None
        full[gt] = line.split(' ', 1)[1]
    return dg, rng, full

pa, pb = sys.argv[1], sys.argv[2]
A, rA, fA = load(pa)
B, rB, fB = load(pb)
gts = sorted(set(A) & set(B))
print(f"{pa}: {len(A)} gametimes   {pb}: {len(B)} gametimes   common {len(gts)}"
      + (f" (range {gts[0]}..{gts[-1]})" if gts else ""))
if set(A) != set(B):
    onlyA, onlyB = sorted(set(A) - set(B)), sorted(set(B) - set(A))
    print(f"  gametimes only in A: {len(onlyA)} {onlyA[:8]}")
    print(f"  gametimes only in B: {len(onlyB)} {onlyB[:8]}")

rc = 0
for name, a, b, advisory in (("digest (n,h)", A, B, False),
                             ("levelRng draws,state", rA, rB, False),
                             ("full dg line (advisory: clockTasks is a server-queue counter,\n              not world state, and differs between same-host matching runs)",
                              fA, fB, True)):
    first = next((gt for gt in gts if a[gt] != b[gt]), None)
    if first is None:
        print(f"{name}: IDENTICAL at every common gametime ({len(gts)})")
    else:
        if not advisory:
            rc = 1
        print(f"{name}: FIRST DIVERGENCE at gametime {first}")
        for gt in range(max(gts[0], first - 2), min(gts[-1], first + 2) + 1):
            mark = "  <== first differing gametime" if gt == first else ""
            print(f"  gt={gt:6d}  A {a[gt]}")
            print(f"  gt={gt:6d}  B {b[gt]}{mark}")
if gts:
    print(f"last common gametime {gts[-1]}: A {fA[gts[-1]]}")
    print(f"last common gametime {gts[-1]}: B {fB[gts[-1]]}")
sys.exit(rc)
