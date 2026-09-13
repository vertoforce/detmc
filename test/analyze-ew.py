#!/usr/bin/env python3
"""Diff two [detmc-ew] windowed entity traces. usage: analyze-ew.py ew-A.txt ew-B.txt
Row: gt=<gt> uuid|type|id|x,y,z|mx,my,mz|yrot,xrot|tickCount  (doubles/floats as raw bits)."""
import sys, struct, collections
def d(bits): return struct.unpack('<d', struct.pack('<q', int(bits)))[0]
def f(bits): return struct.unpack('<f', struct.pack('<i', int(bits)))[0]
def load(p):
    rows = collections.defaultdict(dict)
    for line in open(p):
        line = line.strip()
        if not line.startswith('gt='): continue
        gt, row = line.split(' ', 1)
        gt = int(gt[3:])
        parts = row.split('|')
        rows[gt][parts[0]] = parts
    return rows
A, B = load(sys.argv[1]), load(sys.argv[2])
gts = sorted(set(A) & set(B))
print(f"window gametimes common: {gts[0] if gts else None}..{gts[-1] if gts else None} ({len(gts)})")
first = None
for gt in gts:
    if A[gt] != B[gt]:
        first = gt; break
if first is None:
    print("IDENTICAL over the window"); sys.exit(0)
print(f"FIRST DIFFERING GAMETIME {first}")
names = ['uuid','type','id','pos','motion','rot','tickCount']
for gt in gts:
    if gt < first: continue
    diffs = []
    for u in sorted(set(A[gt]) | set(B[gt])):
        a, b = A[gt].get(u), B[gt].get(u)
        if a == b: continue
        if a is None or b is None:
            diffs.append((u, 'missing in ' + ('A' if a is None else 'B'))); continue
        fields = [names[i] for i in range(len(names)) if a[i] != b[i]]
        detail = []
        if 'pos' in fields:
            pa = [d(x) for x in a[3].split(',')]; pb = [d(x) for x in b[3].split(',')]
            detail.append(f"pos A={pa} B={pb} dpos={[x-y for x,y in zip(pa,pb)]}")
        if 'motion' in fields:
            ma = [d(x) for x in a[4].split(',')]; mb = [d(x) for x in b[4].split(',')]
            detail.append(f"motion A={ma} B={mb}")
        if 'rot' in fields:
            ra = [f(x) for x in a[5].split(',')]; rb = [f(x) for x in b[5].split(',')]
            detail.append(f"rot A={ra} B={rb}")
        diffs.append((u, f"{a[1]} id={a[2]} fields={fields} " + '; '.join(detail)))
    print(f"gt={gt}: {len(diffs)} differing entities")
    for u, s in diffs[:12]:
        print(f"   {u} {s}")
    if gt >= first + 2: break
