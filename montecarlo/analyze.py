"""Aggregate shards, Poisson 90% CIs, analytic extrapolation for zero-count patterns."""
import glob, json, math, os, sys
from collections import defaultdict

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results')


def gammap(a, x):
    if x < 0 or a <= 0:
        return 0.0
    if x < a + 1.0:
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(1000):
            ap += 1.0; d *= x / ap; s += d
            if abs(d) < abs(s) * 1e-14:
                break
        return s * math.exp(-x + a * math.log(x) - math.lgamma(a))
    b, c = x + 1.0 - a, 1e300
    d = 1.0 / b; h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0; d = an * d + b
        if abs(d) < 1e-300: d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300: c = 1e-300
        d = 1.0 / d; de = d * c; h *= de
        if abs(de - 1.0) < 1e-14:
            break
    return 1.0 - math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2q(p, k):          # quantile of chi2_k by bisection
    lo, hi = 0.0, max(10.0, k * 10.0)
    while gammap(k / 2.0, hi / 2.0) < p:
        hi *= 2
    for _ in range(200):
        m = (lo + hi) / 2
        if gammap(k / 2.0, m / 2.0) < p:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def poisson_ci(c, T, alpha=0.10):
    """Garwood exact 90% CI for a rate, c counts over T exposure."""
    lo = 0.0 if c == 0 else chi2q(alpha / 2, 2 * c) / 2 / T
    hi = chi2q(1 - alpha / 2, 2 * (c + 1)) / 2 / T
    return lo, hi


def load():
    cfgs = defaultdict(lambda: {'counts': defaultdict(int), 'years': 0.0,
                                'takes': 0, 'places': 0, 'occ': [], 'hist': [0] * 16,
                                'trap_close': 0, 'trap_v': 0, 'deep': 0, 'minh': 99,
                                'wall': 0.0, 'shards': 0})
    for f in sorted(glob.glob(os.path.join(D, '*.json'))):
        name = os.path.basename(f)[:-5]
        if name == 'smoke':
            continue
        cfg = name.rsplit('_', 1)[0]
        d = json.load(open(f))
        a = cfgs[cfg]
        for k, v in d.items():
            if not k.startswith('_'):
                a['counts'][k] += v
        a['years'] += d['_years'] * d['_cfg']['reps']
        a['takes'] += d['_takes']; a['places'] += d['_places']
        a['occ'].append(d['_occ']); a['trap_close'] += d['_trap_close']
        a['trap_v'] += d['_trap_close_villager']; a['deep'] += d['_deep_pit']
        a['minh'] = min(a['minh'], d['_min_h']); a['wall'] += d['_wall']
        a['hist'] = [x + y for x, y in zip(a['hist'], d['_hist'])]
        a['shards'] += 1
        a['cfg'] = d['_cfg']
    return cfgs


# pattern geometry: (required marks k, forbidden cells m, window anchors W)
GEO = {
    'sq2': (4, 0, 63 * 63), 'sq3': (9, 0, 62 * 62), 'ring3': (8, 1, 62 * 62),
    'L': (5, 4, 62 * 62), 'T': (5, 4, 62 * 62),
    'smiley': (7, 18, 60 * 60), 'smiley_relaxed': (5, 0, 60 * 60),
    'trap1': (4, 0, 62 * 62), 'trap2': (6, 0, 62 * 61),
}


def analytic(name, p, places_yr, takes_yr, nn=4096):
    """Rate of first appearance per enderman-year under a product-form mark field."""
    base = name.split('_', 1)[1] if name.startswith(('hole_', 'either_')) else name
    if base not in GEO:
        return None
    k, m, W = GEO[base]
    if p <= 0:
        return 0.0
    add = places_yr / nn                      # per-column marking rate
    rem = takes_yr / nn / p if p > 0 else 0    # per-marked-column unmarking rate
    r = W * (k * p ** (k - 1) * (1 - p) ** m * add +
             m * p ** k * (1 - p) ** (m - 1) * rem)
    return r


def main():
    cfgs = load()
    out = {}
    for cfg, a in sorted(cfgs.items()):
        Y = a['years']
        p = sum(a['occ']) / len(a['occ'])
        places_yr = a['places'] / Y
        takes_yr = a['takes'] / Y
        rows = {}
        for k in sorted(a['counts']):
            c = a['counts'][k]
            lo, hi = poisson_ci(c, Y)
            rows[k] = {'n': c, 'rate': c / Y, 'lo': lo, 'hi': hi,
                       'analytic': analytic(k, p, places_yr, takes_yr)}
        out[cfg] = {'years': Y, 'shards': a['shards'], 'occ': p,
                    'places_per_yr': places_yr, 'takes_per_yr': takes_yr,
                    'trap_close_per_yr': a['trap_close'] / Y,
                    'trap_close_villager': a['trap_v'],
                    'villager_colocation_rate': a['trap_v'] / Y,
                    'deep_pit': a['deep'], 'min_h': a['minh'],
                    'stack_hist': a['hist'], 'wall_core_s': a['wall'],
                    'rows': rows, 'cfg': a.get('cfg')}
    json.dump(out, open(os.path.join(D, '..', 'summary.json'), 'w'), indent=1)
    for cfg, s in out.items():
        print('== %s  years=%.0f occ=%.4f places/yr=%.1f takes/yr=%.1f deep_pit=%d min_h=%d'
              % (cfg, s['years'], s['occ'], s['places_per_yr'], s['takes_per_yr'],
                 s['deep_pit'], s['min_h']))
        for k, r in s['rows'].items():
            if r['n'] or (r['analytic'] or 0) > 0:
                print('   %-24s n=%-7d rate=%.4g  [%.3g, %.3g]  analytic=%s'
                      % (k, r['n'], r['rate'], r['lo'], r['hi'],
                         ('%.3g' % r['analytic']) if r['analytic'] is not None else '-'))


if __name__ == '__main__':
    main()
