#!/usr/bin/env python3
"""Diff two probe runs.

tickCount is not comparable across runs: ticks also advance while the server is
frozen, and the two runs sit frozen for a different number of ticks during the
settle/summon phase. Every [detmc-dg] line carries both, so build a tick->gametime
map per run and re-key the [detmc-io] lines on gametime before diffing.
"""
import os, re, sys, collections

def load(label):
    d = os.path.dirname(os.path.abspath(__file__)) + "/"
    t2g, dg = {}, {}
    for line in open(d + f"dg-{label}.txt"):
        m = re.match(r"t=(-?\d+) gt=(\d+) n=(\d+) h=(\w+)", line.strip())
        if not m: continue
        t, gt, n, h = int(m[1]), int(m[2]), int(m[3]), m[4]
        t2g[t] = gt
        dg[gt] = (n, h)          # last tick at that gametime wins
    io = collections.defaultdict(list)
    for line in open(d + f"io-{label}.txt"):
        m = re.match(r"t=(-?\d+) (.*)", line.rstrip("\n"))
        if not m: continue
        t = int(m[1])
        io[t2g.get(t, -1)].append(m[2])
    return dg, io

A, ioA = load("A")
B, ioB = load("B")
gts = sorted(set(A) & set(B))
print(f"gametimes: A {len(A)} B {len(B)} common {len(gts)} "
      f"(range {gts[0]}..{gts[-1]})")

first = None
for gt in gts:
    if A[gt] != B[gt]:
        first = gt
        break
if first is None:
    print("digest: IDENTICAL at every common gametime")
else:
    print(f"\ndigest: FIRST DIVERGENCE at gametime {first}")
    for gt in range(max(gts[0], first - 2), min(gts[-1], first + 2) + 1):
        if gt in A and gt in B:
            print(f"  gt={gt:6d}  A n={A[gt][0]} h={A[gt][1]}   B n={B[gt][0]} h={B[gt][1]}"
                  + ("   <== first differing tick" if gt == first else ""))

# io events, keyed on gametime
allgt = sorted(set(ioA) | set(ioB))
firstio = None
for gt in allgt:
    if ioA.get(gt, []) != ioB.get(gt, []):
        firstio = gt
        break
print()
if firstio is None:
    print("io events: identical per gametime")
else:
    print(f"io events: FIRST DIVERGENCE at gametime {firstio}")
    a, b = ioA.get(firstio, []), ioB.get(firstio, [])
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else "<none>"
        y = b[i] if i < len(b) else "<none>"
        if x != y:
            print(f"  gt={firstio} line {i+1}")
            print(f"    A: {x[:160]}")
            print(f"    B: {y[:160]}")
            break

# Where the digest first diverges, what happened in the ticks just before?
if first is not None:
    print(f"\nio events in gametimes {first-3}..{first} :")
    for gt in range(first - 3, first + 1):
        a, b = ioA.get(gt, []), ioB.get(gt, [])
        ca = collections.Counter(x.split()[0] for x in a)
        cb = collections.Counter(x.split()[0] for x in b)
        print(f"  gt={gt:6d} A={dict(ca)}")
        print(f"  gt={gt:6d} B={dict(cb)}")
        sa = [x for x in a if not x.startswith("eagerHead")]
        sb = [x for x in b if not x.startswith("eagerHead")]
        if sa != sb:
            print(f"    A non-eagerHead: {sa[:8]}")
            print(f"    B non-eagerHead: {sb[:8]}")

# Cumulative event counts, to show drift building up
print("\ncumulative counts over the whole run:")
for name, io in (("A", ioA), ("B", ioB)):
    c = collections.Counter()
    for v in io.values():
        for x in v:
            c[x.split()[0]] += 1
    print(f"  {name}: {dict(sorted(c.items()))}")
