"""Monte Carlo of Minecraft 26.2 enderman block-moving on a flat 64x64 slab.

Every rule and constant is cited in PARAMS.md (class:line in a local decompile
of the 26.2 server jar).  Anything not derivable is marked GUESS there.
"""
import argparse, json, math, os, random, sys, time
import numpy as np

N = 64                      # slab is N x N columns
NN = N * N
BASE = 1                    # flat slab: one block at y=0, walkable surface y=1
YEAR = 365 * 24000          # ticks in an in-game year
DAY = 24000

# ---- derived rates, per GOAL-SELECTOR tick (= every 2 game ticks) -------------
P_TAKE = 1.0 / 10.0         # reducedTickDelay(20)      EnderMan.java:590
P_LEAVE = 1.0 / 1000.0      # reducedTickDelay(2000)    EnderMan.java:446
P_STROLL = 1.0 / 60.0       # reducedTickDelay(120)     RandomStrollGoal.java:47
SPEED = 0.3                 # blocks/tick, GUESS from MOVEMENT_SPEED 0.3
STROLL_R = 10               # LandRandomPos.getPos(mob, 10, 7)
P_DAY_TP = 0.04             # (1.0-0.4)*2/30            EnderMan.java:247
MIX_TICKS = 600             # fast-forward shortcut, validated by --mix inf


class World:
    def __init__(self, rng, loose, roofed, rain_frac, villager):
        self.rng = rng
        self.h = bytearray([BASE]) * NN          # solid blocks in the column
        self.p = bytearray(NN)                   # how many of them are enderman-placed
        self.d = bytearray(NN)                   # original surface blocks removed (holes)
        # seed `loose` single blocks (equivalently: 1-block terrain relief), GUESS
        for c in rng.sample(range(NN), loose):
            self.h[c] += 1
            self.p[c] += 1
        self.hv = np.frombuffer(self.h, dtype=np.uint8).reshape(N, N)
        self.pv = np.frombuffer(self.p, dtype=np.uint8).reshape(N, N)
        self.dv = np.frombuffer(self.d, dtype=np.uint8).reshape(N, N)
        self.roofed = roofed
        self.rain_frac = rain_frac
        # enderman
        self.ix = rng.randrange(N); self.iz = rng.randrange(N)
        self.x = self.ix + 0.5; self.z = self.iz + 0.5
        self.tx = None                            # stroll target
        self.carrying = False
        # villager (GUESS: random walk, 1 move / 40 ticks)
        self.villager = villager
        self.vx = rng.randrange(N); self.vz = rng.randrange(N)
        # counters
        self.takes = 0; self.places = 0
        self.trap_close = 0; self.trap_close_villager = 0
        self.trap2_close = 0; self.trap2_close_villager = 0
        self.deep_pit = 0

    # ---------------- movement -------------------------------------------------
    def relocate(self):
        """EnderMan.teleport(): +-32 blocks, treated as uniform on the slab."""
        self.ix = self.rng.randrange(N); self.iz = self.rng.randrange(N)
        self.x = self.ix + 0.5; self.z = self.iz + 0.5
        self.tx = None

    def advance(self, ticks, uniform_mode):
        """Advance the enderman `ticks` game ticks."""
        if uniform_mode:
            # a daylight/rain teleport every ~25 ticks; anything longer is uniform
            if ticks >= 1 and self.rng.random() < 1.0 - (1.0 - P_DAY_TP) ** min(ticks, 400):
                self.relocate()
                return
        if ticks > MIX_TICKS:
            self.relocate()
            return
        rng = self.rng; h = self.h
        left = ticks
        while left > 0:
            if self.tx is None:
                # geometric wait for RandomStrollGoal.canUse, in goal-selector ticks
                w = 2 * int(math.log(1.0 - rng.random()) / math.log(1.0 - P_STROLL))
                if w >= left:
                    return
                left -= w
                # first valid of 10 uniform offsets (see PARAMS.md, RandomPos.java:99)
                for _ in range(10):
                    tx = self.ix + rng.randint(-STROLL_R, STROLL_R)
                    tz = self.iz + rng.randint(-STROLL_R, STROLL_R)
                    if 0 <= tx < N and 0 <= tz < N:
                        self.tx = tx + 0.5; self.tz = tz + 0.5
                        break
                else:
                    return
            dx = self.tx - self.x; dz = self.tz - self.z
            d = math.hypot(dx, dz)
            if d < 1e-6:
                self.tx = None
                continue
            step = min(1.0, d, left * SPEED)      # advance in <=1 block chunks
            nx = self.x + dx / d * step; nz = self.z + dz / d * step
            ni = int(nx); nj = int(nz)
            if ni != self.ix or nj != self.iz:
                # STEP_HEIGHT 1.0 (EnderMan.java:119): can climb at most +1
                if h[ni * N + nj] > h[self.ix * N + self.iz] + 1:
                    self.tx = None
                    left -= step / SPEED
                    continue
                self.ix = ni; self.iz = nj
            self.x = nx; self.z = nz
            left -= step / SPEED
            if step >= d - 1e-9:
                self.tx = None

    def advance_villager(self, ticks):
        if not self.villager:
            return
        k = ticks // 40
        if k <= 0:
            return
        if k <= 8:
            for _ in range(k):
                dx, dz = self.rng.choice(((1, 0), (-1, 0), (0, 1), (0, -1)))
                nx = self.vx + dx; nz = self.vz + dz
                if 0 <= nx < N and 0 <= nz < N and \
                   self.h[nx * N + nz] <= self.h[self.vx * N + self.vz] + 1:
                    self.vx = nx; self.vz = nz
        else:
            # GUESS: diffusive approximation of a k-step walk, reflected at the edges
            s = math.sqrt(k / 2.0)
            self.vx = _reflect(int(round(self.vx + self.rng.gauss(0, s))))
            self.vz = _reflect(int(round(self.vz + self.rng.gauss(0, s))))

    # ---------------- goals ----------------------------------------------------
    def try_take(self):
        """EnderMan.java:594-612."""
        rng = self.rng; h = self.h
        fy = h[self.ix * N + self.iz]                    # getY() of a standing mob
        xt = math.floor(self.x - 2.0 + rng.random() * 4.0)
        zt = math.floor(self.z - 2.0 + rng.random() * 4.0)
        yt = fy + int(rng.random() * 3.0)
        if not (0 <= xt < N and 0 <= zt < N):
            return False
        c = xt * N + zt
        if h[c] - 1 != yt:                               # only the top block is exposed
            return False
        # clear-ray requirement: level.clip(...) must hit `pos` first
        if not self._clear(self.ix + 0.5, self.iz + 0.5, xt + 0.5, zt + 0.5, yt, c):
            return False
        h[c] -= 1
        if self.p[c] > 0:
            self.p[c] -= 1
        else:
            self.d[c] += 1                               # took an ORIGINAL surface block
        if h[c] < BASE:
            self.deep_pit += 1                           # must never happen
        self.carrying = True
        self.takes += 1
        return True

    def _clear(self, ax, az, bx, bz, yt, target):
        h = self.h
        dx = bx - ax; dz = bz - az
        d = math.hypot(dx, dz)
        steps = int(d * 4) + 1
        for s in range(1, steps):
            t = s / steps
            ci = int(ax + dx * t); cj = int(az + dz * t)
            c = ci * N + cj
            if c == target:
                continue
            if h[c] > yt:
                return False
        return True

    def try_leave(self):
        """EnderMan.java:450-481."""
        rng = self.rng; h = self.h
        fy = h[self.ix * N + self.iz]
        xt = math.floor(self.x - 1.0 + rng.random() * 2.0)
        zt = math.floor(self.z - 1.0 + rng.random() * 2.0)
        yt = fy + int(rng.random() * 2.0)
        if not (0 <= xt < N and 0 <= zt < N):
            return False
        c = xt * N + zt
        if h[c] != yt:              # target air + below solid full block
            return False
        if yt < 1:
            return False
        if self.villager and xt == self.vx and zt == self.vz and \
           yt in (h[c], h[c] + 1):
            return False            # getEntities(...).isEmpty()
        h[c] += 1
        self.p[c] += 1
        self.places += 1
        self.carrying = False
        self._check_trap(xt, zt)
        return True

    def _check_trap(self, xt, zt):
        """A 1x1 cell whose 4 horizontal neighbours just became placed blocks."""
        h = self.h; p = self.p
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            cx = xt + dx; cz = zt + dz
            if not (0 <= cx < N and 0 <= cz < N):
                continue
            y = h[cx * N + cz]
            ok = True
            for ex, ez in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx = cx + ex; nz = cz + ez
                if not (0 <= nx < N and 0 <= nz < N):
                    ok = False; break
                c = nx * N + nz
                if not (p[c] > 0 and h[c] - 1 == y):
                    ok = False; break
            if ok:
                self.trap_close += 1
                if self.villager and cx == self.vx and cz == self.vz:
                    self.trap_close_villager += 1


def _reflect(v):
    while v < 0 or v >= N:
        if v < 0:
            v = -v - 1
        if v >= N:
            v = 2 * N - 1 - v
    return v


# ---------------- pattern scanning (numpy, once per in-game day) ---------------
SMILEY = ([(1, 1), (3, 1), (0, 3), (1, 4), (2, 4), (3, 4), (4, 3)], 5)
SM_EYES = [(1, 1), (3, 1)]
SM_MOUTH = [(0, 3), (1, 3), (2, 3), (3, 3), (4, 3), (0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]
LSHAPE = ([(0, 0), (0, 1), (0, 2), (1, 2), (2, 2)], 3)
TSHAPE = ([(0, 0), (1, 0), (2, 0), (1, 1), (1, 2)], 3)
SQ2 = ([(0, 0), (0, 1), (1, 0), (1, 1)], 2)
SQ3 = ([(a, b) for a in range(3) for b in range(3)], 3)
RING3 = ([(a, b) for a in range(3) for b in range(3) if (a, b) != (1, 1)], 3)


def _win(mask, w, di, dj):
    return mask[di:di + mask.shape[0] - w + 1, dj:dj + mask.shape[1] - w + 1]


def masks(world):
    hv = world.hv.astype(np.int16)
    pv = world.pv
    top_placed = pv > 0
    topy = hv - 1
    out = {}

    def pat(name, cells, w, forbid_rest=True, hole=None):
        ai, aj = cells[0]
        Y = _win(topy, w, ai, aj)
        m = None
        for (di, dj) in cells:
            c = _win(top_placed, w, di, dj) & (_win(topy, w, di, dj) == Y)
            m = c if m is None else (m & c)
        if hole is not None:
            for (di, dj) in hole:
                m &= ~(_win(top_placed, w, di, dj) & (_win(topy, w, di, dj) == Y))
                m &= _win(hv, w, di, dj) <= Y            # cell at height Y is air
        if forbid_rest:
            req = set(cells) | set(hole or [])
            for di in range(w):
                for dj in range(w):
                    if (di, dj) in req:
                        continue
                    m &= ~(_win(top_placed, w, di, dj) & (_win(topy, w, di, dj) == Y))
        out[name] = m

    for k in (3, 4, 5):
        out['pillar%d' % k] = pv >= k
    out['stack2'] = pv >= 2
    pat('sq2', SQ2[0], SQ2[1], forbid_rest=False)
    pat('sq3', SQ3[0], SQ3[1], forbid_rest=False)
    pat('ring3', RING3[0], RING3[1], forbid_rest=False, hole=[(1, 1)])
    pat('L', LSHAPE[0], LSHAPE[1])
    pat('T', TSHAPE[0], TSHAPE[1])
    pat('smiley', SMILEY[0], SMILEY[1])

    # relaxed smiley: eyes + >=3 placed cells in rows 3-4 at the same height
    w = 5
    ai, aj = SM_EYES[0]
    Y = _win(topy, w, ai, aj)
    m = None
    for (di, dj) in SM_EYES:
        c = _win(top_placed, w, di, dj) & (_win(topy, w, di, dj) == Y)
        m = c if m is None else (m & c)
    cnt = np.zeros(m.shape, dtype=np.int8)
    for (di, dj) in SM_MOUTH:
        cnt += (_win(top_placed, w, di, dj) & (_win(topy, w, di, dj) == Y)).astype(np.int8)
    out['smiley_relaxed'] = m & (cnt >= 3)

    # villager-trap proxies
    nb = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    core = np.ones((N - 2, N - 2), dtype=bool)
    Yc = hv[1:-1, 1:-1]
    for (di, dj) in nb:
        sl = (slice(1 + di, N - 1 + di), slice(1 + dj, N - 1 + dj))
        core &= top_placed[sl] & (topy[sl] == Yc)
    out['trap1'] = core
    # 2x1 enclosure: cells (i,j),(i,j+1) open at the same floor height Y, and the
    # 6 cells around that domino all carry a placed block whose top is at Y.
    # (core[:,:-1] & core[:,1:] is self-contradictory: each cell would have to be
    #  both the open cell and its neighbour's wall.)
    def W2(arr, di, dj):
        return arr[1 + di:N - 2 + di, 1 + dj:N - 3 + dj]
    Ya = W2(hv, 0, 0)
    m2 = (W2(hv, 0, 1) == Ya)
    for (di, dj) in ((-1, 0), (-1, 1), (1, 0), (1, 1), (0, -1), (0, 2)):
        m2 = m2 & W2(top_placed, di, dj) & (W2(topy, di, dj) == Ya)
    out['trap2'] = m2
    # ---- hole and union ("either") variants, height-agnostic 2D mark sets ----
    holes = world.dv > 0
    for tag, mk in (('hole', holes), ('either', holes | top_placed)):
        def pat2(name, cells, w, forbid_rest=True, hole_cells=None, _mk=mk, _tag=tag):
            m = None
            for (di, dj) in cells:
                c = _win(_mk, w, di, dj)
                m = c if m is None else (m & c)
            for (di, dj) in (hole_cells or []):
                m = m & ~_win(_mk, w, di, dj)
            if forbid_rest:
                req = set(cells) | set(hole_cells or [])
                for di in range(w):
                    for dj in range(w):
                        if (di, dj) not in req:
                            m = m & ~_win(_mk, w, di, dj)
            out[_tag + '_' + name] = m
        pat2('sq2', SQ2[0], SQ2[1], forbid_rest=False)
        pat2('sq3', SQ3[0], SQ3[1], forbid_rest=False)
        pat2('ring3', RING3[0], RING3[1], forbid_rest=False, hole_cells=[(1, 1)])
        pat2('L', LSHAPE[0], LSHAPE[1])
        pat2('T', TSHAPE[0], TSHAPE[1])
        pat2('smiley', SMILEY[0], SMILEY[1])
        e = _win(mk, 5, 1, 1) & _win(mk, 5, 3, 1)
        cnt2 = np.zeros(e.shape, dtype=np.int8)
        for (di, dj) in SM_MOUTH:
            cnt2 += _win(mk, 5, di, dj).astype(np.int8)
        out[tag + '_smiley_relaxed'] = e & (cnt2 >= 3)

    # pits (structurally impossible on a flat base -- measured, not assumed)
    out['pit1'] = hv < BASE
    out['pit_depth2'] = world.dv >= 2
    p3 = holes
    m = np.ones((N - 2, N - 2), dtype=bool)
    for di in range(3):
        for dj in range(3):
            m &= p3[di:di + N - 2, dj:dj + N - 2]
    out['pit3x3'] = m
    return out


def run_one(seed, years, loose, roofed, rain_frac, villager, scan_every_days=1):
    rng = random.Random(seed)
    w = World(rng, loose, roofed, rain_frac, villager)
    total_ticks = int(years * YEAR)
    t = 0
    next_scan = DAY * scan_every_days
    prev = masks(w)
    counts = {k: 0 for k in prev}
    occ_sum = 0.0; occ_n = 0
    hist = np.zeros(16, dtype=np.int64)
    rain_today = rng.random() < rain_frac
    while t < total_ticks:
        day_t = t % DAY
        is_day = (not roofed) and day_t < DAY // 2
        uniform_mode = (not roofed) and (is_day or rain_today)
        if not w.carrying:
            # take attempts: 1/10 per goal tick; advance in ~1 block sub-steps
            sub = 4                     # ticks per sub-step (~1.2 blocks at 0.3 b/t)
            w.advance(sub, uniform_mode)
            w.advance_villager(sub)
            t += sub
            for _ in range(sub // 2):
                if rng.random() < P_TAKE:
                    if w.try_take():
                        break
        else:
            # leave attempts: 1/1000 per goal tick -> fast-forward the wait
            g = 1 + int(math.log(1.0 - rng.random()) / math.log(1.0 - P_LEAVE))
            dt = 2 * g
            w.advance(dt, uniform_mode)
            w.advance_villager(dt)
            t += dt
            w.try_leave()
        if t >= next_scan:
            cur = masks(w)
            for k in cur:
                counts[k] += int(np.count_nonzero(cur[k] & ~prev[k]))
            prev = cur
            occ_sum += float(np.count_nonzero(w.pv > 0)) / NN; occ_n += 1
            hb = np.bincount(w.pv.ravel(), minlength=16)[:16]
            hist += hb
            next_scan += DAY * scan_every_days
            if (t // DAY) % 1 == 0:
                rain_today = rng.random() < rain_frac
    res = {k: counts[k] for k in counts}
    res['_years'] = years
    res['_takes'] = w.takes
    res['_places'] = w.places
    res['_trap_close'] = w.trap_close
    res['_trap_close_villager'] = w.trap_close_villager
    res['_deep_pit'] = w.deep_pit
    res['_min_h'] = int(w.hv.min())
    res['_occ'] = occ_sum / max(occ_n, 1)
    res['_hist'] = hist.tolist()
    return res


def set_mix(v):
    global MIX_TICKS
    MIX_TICKS = v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=float, default=1.0)
    ap.add_argument('--reps', type=int, default=1)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--loose', type=int, default=64)
    ap.add_argument('--roofed', action='store_true')
    ap.add_argument('--rain', type=float, default=0.10)
    ap.add_argument('--villager', action='store_true')
    ap.add_argument('--mix', type=float, default=MIX_TICKS)
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    set_mix(a.mix)
    t0 = time.time()
    agg = None
    per_rep = []
    for r in range(a.reps):
        res = run_one(a.seed * 100003 + r, a.years, a.loose, a.roofed, a.rain, a.villager)
        per_rep.append(res)
        if agg is None:
            agg = {k: (v if not isinstance(v, list) else list(v)) for k, v in res.items()}
        else:
            for k, v in res.items():
                if isinstance(v, list):
                    agg[k] = [x + y for x, y in zip(agg[k], v)]
                else:
                    agg[k] += v
    agg['_wall'] = time.time() - t0
    agg['_cfg'] = vars(a)
    agg['_per_rep'] = [{k: v for k, v in r.items() if not k.startswith('_hist')} for r in per_rep]
    out = a.out or ('results/run_%d.json' % a.seed)
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'w') as f:
        json.dump(agg, f)
    print(json.dumps({k: v for k, v in agg.items() if not k.startswith('_per')})[:900])


if __name__ == '__main__':
    main()
