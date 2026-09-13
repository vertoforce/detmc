#!/usr/bin/env python3
"""md5 of every .mca under a world dir with the region header's timestamp table blanked.

RegionFile.java:156  getTimestamp() -> (int)(Util.getEpochMillis() / 1000L)
RegionFile.java:278,309  timestamps.put(offsetIndex, getTimestamp())

Bytes 0..4095 are the sector offset table (deterministic once write order is) and
bytes 4096..8191 are one wall-clock second per chunk, rewritten on every save. So a
raw md5sum of a region file can never match between two runs taken minutes apart,
whatever the world contains. Blank the second 4 KiB and compare the rest.
"""
import hashlib, pathlib, sys

root = pathlib.Path(sys.argv[1])
for path in sorted(root.rglob("*.mca")):
    data = bytearray(path.read_bytes())
    if len(data) >= 8192:
        data[4096:8192] = b"\0" * 4096
    print(hashlib.md5(bytes(data)).hexdigest(), path.relative_to(root))
