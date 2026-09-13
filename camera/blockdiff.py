#!/usr/bin/env python3
"""Count blocks per chunk section so we can tell whether anything actually changed."""
import sys, os, struct, zlib, gzip, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nbtdump import parse


def chunks(region_dir):
    for fn in sorted(os.listdir(region_dir)):
        p = fn.split(".")
        if len(p) != 4 or p[0] != "r" or p[3] != "mca":
            continue
        rx, rz = int(p[1]), int(p[2])
        with open(os.path.join(region_dir, fn), "rb") as fh:
            hdr = fh.read(4096)
            for i in range(1024):
                loc = struct.unpack_from(">I", hdr, i * 4)[0]
                off = loc >> 8
                if not off:
                    continue
                fh.seek(off * 4096)
                ln = struct.unpack(">I", fh.read(4))[0]
                comp = fh.read(1)[0]
                raw = fh.read(ln - 1)
                data = zlib.decompress(raw) if comp == 2 else gzip.decompress(raw) if comp == 1 else raw
                yield (rx * 32 + i % 32, rz * 32 + i // 32), parse(data)


def counts(region_dir, only=None):
    """Per-chunk multiset of palette entries. Palette membership changes as soon
    as a block type appears or disappears, which is what enderman griefing does."""
    out = {}
    for pos, ch in chunks(region_dir):
        if only and pos not in only:
            continue
        c = collections.Counter()
        for s in ch.get("sections", []):
            for e in s.get("block_states", {}).get("palette", []):
                c[e["Name"]] += 1
        out[pos] = c
    return out


if __name__ == "__main__":
    a, b = sys.argv[1], sys.argv[2]
    only = None
    if len(sys.argv) > 3:  # "x1,z1 x2,z2" bounding box of chunk coords
        x1, z1 = map(int, sys.argv[3].split(","))
        x2, z2 = map(int, sys.argv[4].split(","))
        only = {(x, z) for x in range(x1, x2 + 1) for z in range(z1, z2 + 1)}
    ca, cb = counts(a, only), counts(b, only)
    changed = 0
    for pos in sorted(set(ca) | set(cb)):
        d = collections.Counter(cb.get(pos, {}))
        d.subtract(ca.get(pos, {}))
        d = {k: v for k, v in d.items() if v}
        if d:
            changed += 1
            print(f"chunk {pos}: {d}")
    print(f"{changed} chunks changed (of {len(set(ca) | set(cb))})")


def section_blocks(section):
    """Decode one 16x16x16 section into a list of 4096 block names.

    Post-1.16 packing: entries are bit-packed into longs and never straddle a
    long boundary, so each long holds floor(64 / bits) entries.
    """
    bs = section.get("block_states", {})
    palette = [e["Name"] for e in bs.get("palette", [])]
    if len(palette) <= 1:
        return palette * 4096 if palette else ["minecraft:air"] * 4096
    data = bs.get("data") or []
    bits = max(4, (len(palette) - 1).bit_length())
    per_long = 64 // bits
    mask = (1 << bits) - 1
    out = []
    for word in data:
        w = word & 0xFFFFFFFFFFFFFFFF
        for k in range(per_long):
            if len(out) >= 4096:
                break
            out.append(palette[(w >> (k * bits)) & mask])
    return out + ["minecraft:air"] * (4096 - len(out))


def blocks_at_y(region_dir, y, only=None):
    """{(x, z): block name} for one horizontal slice of the world."""
    sec_y, inner_y = y >> 4, y & 15
    out = {}
    for (cx, cz), ch in chunks(region_dir):
        if only and (cx, cz) not in only:
            continue
        for s in ch.get("sections", []):
            if s.get("Y") != sec_y:
                continue
            blocks = section_blocks(s)
            for i in range(256):
                out[(cx * 16 + i % 16, cz * 16 + i // 16)] = blocks[inner_y * 256 + i]
            break
    return out
