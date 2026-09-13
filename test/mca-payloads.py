#!/usr/bin/env python3
"""Hash each chunk's DECOMPRESSED payload bytes, with no NBT parsing at all.
Prints "<chunkX,Z> <sha1-12> <sectorOffset>" per chunk, so the same content in a
different place in the file is visible as an offset change and nothing else."""
import hashlib, struct, sys, zlib
for path in sys.argv[1:]:
    b = open(path, 'rb').read()
    rows = []
    for i in range(1024):
        off = struct.unpack_from('>I', b, i*4)[0] >> 8
        if off == 0: continue
        length = struct.unpack_from('>I', b, off*4096)[0]
        comp = b[off*4096+4]
        data = b[off*4096+5: off*4096+4+length]
        if comp == 2: raw = zlib.decompress(data)
        elif comp == 1:
            import gzip, io; raw = gzip.decompress(data)
        else: raw = data
        rows.append((f"{i%32},{i//32}", hashlib.sha1(raw).hexdigest()[:12], off))
    for r in sorted(rows):
        print(f"{r[0]} {r[1]} sector={r[2]}")
