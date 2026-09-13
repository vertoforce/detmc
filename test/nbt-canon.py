#!/usr/bin/env python3
"""Canonical dump of every chunk in one or more Anvil .mca files (region or entities).

usage: nbt-canon.py FILE.mca [FILE.mca ...]   -> prints one canonical line per chunk

Compound keys are sorted, so two files whose NBT differs only in map iteration order
produce the same output; any real value difference shows up in a plain `diff`.
"""
import struct, sys, zlib, gzip, io

def read_tag(buf, pos, tid):
    if tid == 0: return None, pos
    if tid == 1: return ('b', buf[pos]), pos + 1
    if tid == 2: return ('s', struct.unpack_from('>h', buf, pos)[0]), pos + 2
    if tid == 3: return ('i', struct.unpack_from('>i', buf, pos)[0]), pos + 4
    if tid == 4: return ('l', struct.unpack_from('>q', buf, pos)[0]), pos + 8
    if tid == 5: return ('f', struct.unpack_from('>I', buf, pos)[0]), pos + 4   # raw bits
    if tid == 6: return ('d', struct.unpack_from('>Q', buf, pos)[0]), pos + 8   # raw bits
    if tid == 7:
        n = struct.unpack_from('>i', buf, pos)[0]; pos += 4
        return ('B', buf[pos:pos + n].hex()), pos + n
    if tid == 8:
        n = struct.unpack_from('>H', buf, pos)[0]; pos += 2
        return ('S', buf[pos:pos + n].decode('utf-8', 'replace')), pos + n
    if tid == 9:
        et = buf[pos]; n = struct.unpack_from('>i', buf, pos + 1)[0]; pos += 5
        items = []
        for _ in range(n):
            v, pos = read_tag(buf, pos, et); items.append(v)
        return ('L', items), pos
    if tid == 10:
        d = {}
        while True:
            t = buf[pos]; pos += 1
            if t == 0: break
            n = struct.unpack_from('>H', buf, pos)[0]; pos += 2
            name = buf[pos:pos + n].decode('utf-8', 'replace'); pos += n
            v, pos = read_tag(buf, pos, t); d[name] = v
        return ('C', d), pos
    if tid == 11:
        n = struct.unpack_from('>i', buf, pos)[0]; pos += 4
        return ('I', list(struct.unpack_from('>%di' % n, buf, pos))), pos + 4 * n
    if tid == 12:
        n = struct.unpack_from('>i', buf, pos)[0]; pos += 4
        return ('J', list(struct.unpack_from('>%dq' % n, buf, pos))), pos + 8 * n
    raise ValueError('tag %d' % tid)

def canon(v):
    k, x = v
    if k == 'C': return '{' + ','.join('%s:%s' % (n, canon(x[n])) for n in sorted(x)) + '}'
    if k == 'L': return '[' + ','.join(canon(i) for i in x) + ']'
    return '%s%s' % (k, x)

def chunks(path):
    data = open(path, 'rb').read()
    if len(data) < 8192:
        return
    for i in range(1024):
        off, cnt = struct.unpack_from('>I', data, i * 4)[0] >> 8, data[i * 4 + 3]
        if off == 0: continue
        p = off * 4096
        length, comp = struct.unpack_from('>I', data, p)[0], data[p + 4]
        raw = data[p + 5:p + 4 + length]
        if comp == 2: raw = zlib.decompress(raw)
        elif comp == 1: raw = gzip.decompress(raw)
        elif comp == 3: pass
        else: raise ValueError('compression %d' % comp)
        # root: type byte, name, payload
        pos = 1; n = struct.unpack_from('>H', raw, pos)[0]; pos += 2 + n
        root, _ = read_tag(raw, pos, raw[0])
        yield i % 32, i // 32, root

for path in sys.argv[1:]:
    for cx, cz, root in chunks(path):
        print('%s chunk %d,%d %s' % (path.split('/')[-1], cx, cz, canon(root)))
