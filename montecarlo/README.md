# Enderman block-moving Monte Carlo (Minecraft 26.2)

`PARAMS.md` cites every constant as `class:line` in a local decompile of the 26.2 server jar.
`sim.py` runs one enderman on a 64x64 flat grass/dirt slab; `run_all.sh` fans out;
`analyze.py` / `report.py` aggregate (Garwood exact 90% Poisson CIs).
17,180 enderman-years total, 134,058 core-seconds.

## Headline result: a perfectly flat slab produces nothing, ever

`EndermanTakeBlockGoal.tick` picks `yt = floor(getY() + rnd*3.0)` (EnderMan.java:599) and a
standing mob's `getY()` is the top surface of the block under it, so the sampled y is always
**at or above the feet** and the surface block is always one below. Run `flat`
(100 enderman-years, loose = 0): **0 takes, 0 placements, 0 of every pattern.**
Consequences, all measured, not assumed: no holes ever (`min_h == 1` in every run), so every
`hole_*` and `either_*`-only pattern is exactly 0; pit depth >= 2 is 0 (`deep_pit == 0`);
3x3 pit at depth 1 is 0.

To get any dynamics the slab is seeded with `L` loose blocks (= 1-block relief). The enderman
never creates material, it only recycles it, so `L` fixes the stationary mark density `p`
(measured: L=8 -> p=0.0019, L=64 -> p=0.0154, L=512 -> p=0.114). `L` is a GUESS; every rate
below scales roughly as `p^(k-1)` in the number of marks `k`.

## Rates per enderman-year, headline config (open sky, L = 64, p = 0.0154, 10,000 ey)

| pattern | n | rate/ey | 90% CI | source |
|---|---|---|---|---|
| 2-stack | 139088 | 13.91 | 13.8-14.0 | measured |
| pillar h>=3 | 19 | 0.0207 | 0.0135-0.0303 | measured, MIX=inf run (see bias note) |
| pillar h>=4 | 3 | 1.4e-4 | <3.3e-3 | measured |
| pillar h>=5 | 0 | 0 | <3e-4 | measured |
| 2x2 same height | 104 | 0.0104 | 0.0088-0.0122 | measured |
| relaxed smiley | 191 | 0.0191 | 0.0169-0.0215 | measured |
| trap cell (1x1, 4 walls) | 122 | 0.0122 | 0.0104-0.0142 | measured |
| L | 0 | 1.8e-4 | <3e-4 | calibrated analytic |
| T | 0 | 1.8e-4 | <3e-4 | calibrated analytic |
| 2x1 enclosure | 0 | 1.9e-6 | <3e-4 | calibrated analytic |
| 3x3 ring | 0 | 1.8e-9 | <3e-4 | calibrated analytic |
| strict smiley | 0 | 1.2e-7 | <3e-4 | calibrated analytic |
| 3x3 filled | 0 | 2.7e-11 | <3e-4 | calibrated analytic |
| any hole pattern, pit d>=2, 3x3 pit | 0 | 0 | exact | structural |

Analytic = `W * (k*p^(k-1)*(1-p)^m*add + m*p^k*(1-p)^(m-1)*rem)`, calibrated by the
measured/analytic ratio in the L=512 run (factors 0.39-1.30) where that run had n >= 3.
At L=64 the calibrated analytic reproduces the measured 2x2, relaxed-smiley and trap-cell
rates to within 1.4x, so the extrapolated rows are good to roughly a factor of 3.

## Villager

A villager random-walking the same slab (1 move / 40 ticks) was inside the closing cell
**0 times out of 136 trap-cell closures in 10,000 enderman-years** (open64) and 0/~11 in every
other config. The closures are uniform over 4096 columns, so the co-location rate is
~1/4096 of the trap rate: **3e-6 per enderman-year**. A 1-high wall is also jumpable, so the
true "villager is trapped" rate is lower still.

## Roofed vs open

Roofing removes the daylight teleport (p=0.04/tick, EnderMan.java:247) and rain. Measured at
L=64: places/yr 985 (roofed) vs 1016 (open), pillar3 0.041 vs 0.046, 2x2 0.012 vs 0.010 --
i.e. **roofing does not change the rates measurably**, because the rate limiter is the
1/1000-per-goal-tick leave goal, not the enderman's position.

## Validation

* `mix600` vs `mixinf` (the >600-tick fast-forward vs full path simulation): every pattern
  agrees within CI (2-stack 13.91 vs 13.95) **except pillar h>=3, where the shortcut inflates
  the rate 2.2x** (0.0463 vs 0.0207, disjoint CIs). The table uses the MIX=inf value.
* `open512` had a self-contradictory 2x1 mask (`core[:,:-1] & core[:,1:]` requires a cell to be
  both the open cell and its own wall); fixed in `open512b`, which measures 0.0708/ey at p=0.114.

## Known simplifications and the direction they bias each rate

| simplification | affects | direction |
|---|---|---|
| Take only from a column's top block (no overhangs / floating blocks) | all | under |
| Ground speed 0.3 b/t taken straight from MOVEMENT_SPEED (real ~0.93x) | pillars | slightly over |
| Straight-line movement, no pathfinder; leg ends if a +2 step blocks it | pillars, 2x2 | over (more time near own builds) |
| Day = first 12000 ticks with brightness 1.0, no twilight ramp | all (open) | over (more teleports than reality) |
| Rain modelled as relocation with no damage; vanilla kills an unroofed enderman in ~20 s of rain | all (open) | strongly over |
| Daily scan only; patterns that form and are recycled inside one day are missed | all | under |
| Villager can jump 1 block, so a 1-high "trap" is not a trap | trap rates | strongly over |
| `L` (loose blocks) is a guess; rates scale as p^(k-1) | all | unknown, dominant |
| Pattern counted on new appearance (present today, absent yesterday) | all | n/a |

## In-game years to first occurrence, and per-server wall-clock days at 10 in-game years/day

Headline config (open sky, L = 64). Fleet of 30 servers: divide the days by 30.

| pattern | rate/ey | 0.1 endermen | 1 enderman | 20 endermen | days @0.1 | days @1 | days @20 |
|---|---|---|---|---|---|---|---|
| pillar h>=3 | 2.1e-2 | 483 y | 48 y | 2.4 y | 48 | 4.8 | 0.24 |
| 2x2 same height | 1.0e-2 | 962 y | 96 y | 4.8 y | 96 | 9.6 | 0.48 |
| relaxed smiley | 1.9e-2 | 524 y | 52 y | 2.6 y | 52 | 5.2 | 0.26 |
| trap cell (1x1) | 1.2e-2 | 820 y | 82 y | 4.1 y | 82 | 8.2 | 0.41 |
| pillar h>=4 | 1.4e-4 | 71 ky | 7.1 ky | 357 y | 7140 | 714 | 36 |
| L / T letter | 1.8e-4 | 56 ky | 5.6 ky | 278 y | 5560 | 556 | 28 |
| 2x1 enclosure | 1.9e-6 | 5.3 My | 526 ky | 26 ky | 5.3e5 | 5.3e4 | 2600 |
| villager inside a closing cell | 3e-6 | 3.3 My | 333 ky | 17 ky | 3.3e5 | 3.3e4 | 1700 |
| strict smiley | 1.2e-7 | 83 My | 8.3 My | 417 ky | 8.3e6 | 8.3e5 | 4.2e4 |
| 3x3 ring | 1.8e-9 | 5.6 Gy | 556 My | 28 My | 5.6e8 | 5.6e7 | 2.8e6 |
| 3x3 filled | 2.7e-11 | 370 Gy | 37 Gy | 1.9 Gy | 3.7e10 | 3.7e9 | 1.9e8 |
| any hole art, pit d>=2, 3x3 pit | 0 | never | never | never | - | - | - |

Ranking for a headless simulation: 2-stack >> pillar-3 ~ relaxed smiley ~ trap cell ~ 2x2
(days of fleet time with 20 endermen) >> pillar-4 ~ L/T (weeks-months) >> everything else
(out of reach). "Enderman traps a villager" is ~4 orders of magnitude harder than
"enderman art" at the 2x2/relaxed-smiley level, and the strict smiley is not reachable.
