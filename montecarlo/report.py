"""Final table: measured rates + calibrated analytic extrapolation for zero-count patterns."""
import json, math, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze as A

NN = 4096


def binpmf(n, k, p):
    return math.comb(n, k) * p ** k * (1 - p) ** (n - k)


def analytic(name, p, places_yr, takes_yr):
    base = name.split('_', 1)[1] if name.startswith(('hole_', 'either_')) else name
    add = places_yr / NN
    rem = takes_yr / NN / p if p > 0 else 0.0
    if base == 'smiley_relaxed':
        W = 60 * 60
        pm = sum(binpmf(10, j, p) for j in range(3, 11))
        return W * add * (2 * p * pm + p * p * 10 * binpmf(9, 2, p))
    if base not in A.GEO:
        return None
    k, m, W = A.GEO[base]
    if p <= 0:
        return 0.0
    return W * (k * p ** (k - 1) * (1 - p) ** m * add +
                m * p ** k * (1 - p) ** (m - 1) * rem)


def main():
    cfgs = A.load()
    S = {}
    for cfg, a in cfgs.items():
        Y = a['years']
        if Y == 0:
            continue
        p = sum(a['occ']) / len(a['occ'])
        S[cfg] = dict(Y=Y, p=p, places=a['places'] / Y, takes=a['takes'] / Y,
                      counts=dict(a['counts']), trap_v=a['trap_v'], deep=a['deep'],
                      minh=a['minh'], trapclose=a['trap_close'] / Y, hist=a['hist'])

    # calibration: measured / analytic in the dense run, where the dense run has >=3 counts
    cal = {}
    for dense in ('open512b', 'open512'):
        if dense not in S:
            continue
        d = S[dense]
        for k, n in d['counts'].items():
            an = analytic(k, d['p'], d['places'], d['takes'])
            if an and n >= 3 and k not in cal:
                cal[k] = (n / d['Y']) / an
    print('# calibration factors (measured/analytic at high density)')
    for k in sorted(cal):
        print('   %-22s %.2f' % (k, cal[k]))

    order = ['pillar3', 'pillar4', 'pillar5', 'sq2', 'sq3', 'ring3', 'smiley',
             'smiley_relaxed', 'L', 'T', 'trap1', 'trap2',
             'pit1', 'pit_depth2', 'pit3x3']
    for cfg in ('open64', 'roofed64', 'open8', 'open512b', 'open512', 'flat'):
        if cfg not in S:
            continue
        d = S[cfg]
        print('\n== %s  years=%.0f  occupancy=%.4f  places/yr=%.1f  takes/yr=%.1f'
              '  trap-closures/yr=%.4g  villager-in-cell=%d  deep_pit=%d  min_h=%d'
              % (cfg, d['Y'], d['p'], d['places'], d['takes'], d['trapclose'],
                 d['trap_v'], d['deep'], d['minh']))
        print('%-22s %8s %12s %22s %12s' % ('pattern', 'n', 'rate/ey', '90% CI', 'analytic'))
        for base in order:
            for pref in ('', 'hole_', 'either_'):
                k = pref + base
                if k not in d['counts']:
                    continue
                n = d['counts'][k]
                lo, hi = A.poisson_ci(n, d['Y'])
                an = analytic(k, d['p'], d['places'], d['takes'])
                anc = an * cal.get(k, cal.get(base, 1.0)) if an else an
                print('%-22s %8d %12.5g %22s %12s'
                      % (k, n, n / d['Y'], '[%.3g, %.3g]' % (lo, hi),
                         ('%.3g' % anc) if anc is not None else '-'))
        # pillar occupancy histogram -> stationary stack heights
        tot = sum(d['hist'])
        print('   stack-height occupancy:',
              ' '.join('%d:%.3g' % (i, d['hist'][i] / tot) for i in range(1, 8)
                       if d['hist'][i]))

    # headline table: years to first occurrence for 0.1 / 1 / 20 endermen
    hd = 'open64'
    if hd in S:
        d = S[hd]
        print('\n# %s: in-game years to first occurrence, and wall-clock days at '
              '10 in-game years/day' % hd)
        print('%-22s %10s %10s %10s %10s %10s %10s %10s'
              % ('pattern', 'rate/ey', 'y@0.1', 'y@1', 'y@20', 'd@0.1', 'd@1', 'd@20'))
        for base in order:
            k = base
            if k not in d['counts']:
                continue
            n = d['counts'][k]
            r = n / d['Y']
            if n < 3:
                an = analytic(k, d['p'], d['places'], d['takes'])
                r = (an * cal.get(k, 1.0)) if an else 0.0
            if r <= 0:
                print('%-22s %10s %10s %10s %10s %10s %10s %10s'
                      % (k, '0', 'never', 'never', 'never', '-', '-', '-'))
                continue
            ys = [1.0 / (r * pop) for pop in (0.1, 1.0, 20.0)]
            print('%-22s %10.4g %10.4g %10.4g %10.4g %10.4g %10.4g %10.4g'
                  % (k, r, ys[0], ys[1], ys[2], ys[0] / 10, ys[1] / 10, ys[2] / 10))


if __name__ == '__main__':
    main()
