#!/bin/bash
# Compare the local run (out-hostA) against the host B run (out-hostB).
# Same checks run-pair.sh ends with, plus the per-tick digest and canonical NBT.
cd "$(dirname "$0")"
A=${1:-out-hostA}; B=${2:-out-hostB}
echo "########## 1. diff-det.sh, section by section (this is what run-pair.sh calls) ##########"
cp $A/result-A.txt result-A.txt; cp $B/result-B.txt result-B.txt
./diff-det.sh result; echo "diff-det.sh exit=$?"
echo
echo "########## 2. per-tick digest: first divergent gametime ##########"
python3 compare-dg.py $A/dg-A.txt $B/dg-B.txt; echo "compare-dg exit=$?"
echo
echo "########## 3. windowed entity trace (gt 0:3 and 2190:2210) ##########"
python3 analyze-ew.py $A/ew-A.txt $B/ew-B.txt 2>&1 | head -20
echo
echo "########## 4. region md5, timestamp-blind (test/region-md5.py) ##########"
python3 region-md5.py $A > /tmp/xm-rmd5-A.txt; python3 region-md5.py $B > /tmp/xm-rmd5-B.txt
echo "A: $(wc -l < /tmp/xm-rmd5-A.txt) files   B: $(wc -l < /tmp/xm-rmd5-B.txt) files"
if diff -q /tmp/xm-rmd5-A.txt /tmp/xm-rmd5-B.txt >/dev/null; then
  echo "region md5 (timestamp-blind): MATCH, all files"
else
  echo "region md5 (timestamp-blind): DIFFER"
  diff /tmp/xm-rmd5-A.txt /tmp/xm-rmd5-B.txt
fi
echo
echo "########## 5. entities/*.mca bytes and canonical NBT ##########"
echo "A entities: $(cat $A/entities/*.mca 2>/dev/null | wc -c) B bytes in $(ls $A/entities/ 2>/dev/null | wc -l) files"
echo "B entities: $(cat $B/entities/*.mca 2>/dev/null | wc -c) B bytes in $(ls $B/entities/ 2>/dev/null | wc -l) files"
python3 nbt-canon.py $A/entities/*.mca 2>/dev/null | sort > /tmp/xm-ent-A.txt
python3 nbt-canon.py $B/entities/*.mca 2>/dev/null | sort > /tmp/xm-ent-B.txt
echo "canonical chunk lines: A $(wc -l < /tmp/xm-ent-A.txt)  B $(wc -l < /tmp/xm-ent-B.txt)"
if diff -q /tmp/xm-ent-A.txt /tmp/xm-ent-B.txt >/dev/null; then echo "entities canonical NBT: IDENTICAL"
else echo "entities canonical NBT: DIFFER"; diff /tmp/xm-ent-A.txt /tmp/xm-ent-B.txt | cut -c1-200 | head -20; fi
echo
echo "########## 6. region/*.mca canonical NBT ##########"
python3 nbt-canon.py $A/region/*.mca 2>/dev/null > /tmp/xm-reg-A.txt
python3 nbt-canon.py $B/region/*.mca 2>/dev/null > /tmp/xm-reg-B.txt
echo "canonical chunk lines: A $(wc -l < /tmp/xm-reg-A.txt)  B $(wc -l < /tmp/xm-reg-B.txt)"
python3 - /tmp/xm-reg-A.txt /tmp/xm-reg-B.txt <<'PY'
import sys
def load(p):
    d = {}
    for line in open(p):
        f, _, cx, rest = line.split(' ', 3)
        d[(f, cx)] = rest
    return d
a, b = load(sys.argv[1]), load(sys.argv[2])
shared = set(a) & set(b)
bad = sorted(k for k in shared if a[k] != b[k])
print(f"  chunks in both: {len(shared)}; identical: {len(shared)-len(bad)}; differing: {len(bad)}")
print(f"  chunks only in A: {len(set(a)-set(b))}")
print(f"  chunks only in B: {len(set(b)-set(a))}")
for k in sorted(set(a)-set(b))[:6]: print(f"    only A: {k}")
for k in sorted(set(b)-set(a))[:6]: print(f"    only B: {k}")
for k in bad[:6]: print(f"    differs: {k}")
print("region canonical NBT: IDENTICAL" if not bad and set(a)==set(b)
      else ("region canonical NBT: all shared chunks identical, chunk SET differs"
            if not bad else "region canonical NBT: DIFFER"))
PY
echo
echo "########## 7. server.properties ##########"
if diff -q $A/server.properties.txt $B/server.properties.txt >/dev/null; then echo "server.properties (rcon.password line removed): IDENTICAL"
else echo "server.properties: DIFFER"; diff $A/server.properties.txt $B/server.properties.txt; fi
echo
echo "########## 8. machines ##########"
paste -d'\n' /dev/null /dev/null >/dev/null
echo "--- A (local) ---"; cat $A/machine.txt
echo "--- B (host B) ---"; cat $B/machine.txt
