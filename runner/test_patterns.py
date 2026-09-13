#!/usr/bin/env python3
"""Unit tests for the relaxed-smiley matcher and for the mcfunction it generates.

Two layers:

1. `match_tops` against synthetic block grids -- does the Python matcher agree
   with montecarlo/sim.py's `smiley_relaxed` definition (sim.py:277-288)?
2. a tiny interpreter for the generated `execute ... unless block ... if block`
   lines, run against the same grids -- does the DATAPACK compute the same
   predicate as the Python matcher?  This is what makes the in-game detector
   trustworthy without a 289-column hand check.

    .venv/bin/python -m pytest test_patterns.py -q
    .venv/bin/python test_patterns.py          # same tests, no pytest needed
"""
import re

import patterns
from patterns import (SMILEY_CELLS, SMILEY_RELAXED, SMILEY_STRICT, SM_EYES, SM_MOUTH,
                      match_tops, tops_from_blocks)

FLOOR_Y = 100          # the pen floor
Y = FLOOR_Y + 1        # the scan level, one above the floor
AREA = (0, 0, 16, 16)  # 17x17, the same shape as the hunt scenario's detector area
LEVELS = [Y, Y + 1]


def grid(cells, y=Y, floor=True):
    """(dx, dz) cells -> a set of non-air (x, y, z), plus a solid floor."""
    blocks = {(x, FLOOR_Y, z) for x in range(-2, 20) for z in range(-2, 20)} if floor else set()
    for c in cells:
        blocks.add((c[0], c[1] if len(c) > 2 else y, c[-1]))
    return blocks


def tops(blocks):
    return tops_from_blocks(blocks)


def matches(blocks, area=AREA, levels=LEVELS):
    return match_tops(tops(blocks), SMILEY_RELAXED, area, levels)


def smiley_at(x0, z0, mouth=SM_MOUTH[:3], y=Y):
    return [(x0 + dx, y, z0 + dz) for dx, dz in list(SM_EYES) + list(mouth)]


# ------------------------------------------------------------------ semantics

def test_full_smiley_matches():
    hits = matches(grid(smiley_at(4, 4)))
    assert hits == [(4, 4, Y)], hits


def test_translated_smiley_matches_at_its_corner():
    assert matches(grid(smiley_at(9, 2))) == [(9, 2, Y)]


def test_two_mouth_cells_is_not_enough():
    assert matches(grid(smiley_at(4, 4, mouth=SM_MOUTH[:2]))) == []


def test_exactly_three_mouth_cells_is_enough():
    assert len(matches(grid(smiley_at(4, 4, mouth=SM_MOUTH[:3])))) == 1


def test_any_three_of_the_ten_mouth_cells_count():
    for m in (SM_MOUTH[:3], SM_MOUTH[-3:], [SM_MOUTH[0], SM_MOUTH[4], SM_MOUTH[9]]):
        assert len(matches(grid(smiley_at(4, 4, mouth=m)))) == 1, m


def test_one_missing_eye_is_not_a_match():
    cells = smiley_at(4, 4)
    del cells[1]                                  # drop the (3,1) eye
    assert matches(grid(cells)) == []


def test_eyes_at_the_wrong_spacing_do_not_match():
    # eyes must be 2 apart in x at the same z (SM_EYES, sim.py:227)
    cells = [(4 + 1, Y, 4 + 1), (4 + 2, Y, 4 + 1)] + [(4 + dx, Y, 4 + dz) for dx, dz in SM_MOUTH[:3]]
    assert matches(grid(cells)) == []


def test_mixed_heights_do_not_match():
    """Every matched cell must have its top at the SAME Y (sim.py:283,286)."""
    cells = [(4 + dx, Y, 4 + dz) for dx, dz in SM_EYES]
    cells += [(4 + dx, Y + 1, 4 + dz) for dx, dz in SM_MOUTH[:3]]
    # the mouth blocks are floating one level up, so their column tops are at Y+1
    assert matches(grid(cells)) == []


def test_a_block_on_top_of_an_eye_breaks_the_match():
    """`top_placed` is the column's TOP block (sim.py:236-238), so a block above
    an eye moves that column's top off the scan level."""
    cells = smiley_at(4, 4)
    cells.append((4 + 1, Y + 1, 4 + 1))
    assert matches(grid(cells)) == []


def test_whole_second_level_smiley_matches_at_that_level():
    """A smiley built one level higher is still a hit, at Y+1."""
    hits = matches(grid(smiley_at(4, 4, y=Y + 1)))
    assert hits == [(4, 4, Y + 1)], hits


def test_solid_slab_matches_nowhere_because_no_column_top_is_isolated():
    """A completely filled scan level: every column top IS at Y, so this is a hit
    everywhere -- the reason the detector area must sit above the floor, not on
    it.  Asserted so the property is explicit rather than assumed."""
    solid = grid([(x, Y, z) for x in range(0, 17) for z in range(0, 17)])
    assert len(matches(solid)) == (17 - 5 + 1) ** 2


def test_seeded_scatter_grid_never_matches():
    """The scenario seeds loose blocks every 3 blocks.  The two eyes are 2 apart
    in x at the same z, and 2 % 3 != 0, so the seeded layout cannot produce an
    eye pair -- the detector cannot fire on the setup itself."""
    seeded = grid([(x, Y, z) for x in range(0, 17, 3) for z in range(0, 17, 3)])
    assert matches(seeded) == []


def test_floor_alone_never_matches():
    assert matches(grid([])) == []


# ------------------------------------------------- generated-datapack agreement

COND = re.compile(r"(unless|if) block ~(-?\d+) ~(-?\d+) ~(-?\d+) (\S+)")


SCORE_COND = re.compile(r"if score (\S+) detmc matches (-?\d*)\.\.(-?\d*)")
ADD_TO = re.compile(r"scoreboard players add (\S+) detmc 1")


def run_pack(blocks, pattern=SMILEY_RELAXED, area=AREA, levels=LEVELS):
    """Interpret the generated mcfunction lines against a block set.

    Understands exactly the forms run.py emits: an `execute ... positioned x y z
    <conds> run function <count>` line, and the count function's
    `execute <conds> run scoreboard players add <holder>` lines followed by one
    `execute if score <holder> detmc matches <range> ... run function <hit>`.
    Both counters are modelled, so this covers the strict pattern's
    "the other 18 cells must be clear" gate as well as the relaxed `>= 3`.
    """
    count_src = patterns.count_function(pattern, "detmc:hit")
    check_src = patterns.check_lines(pattern, area, levels, "#hit", "detmc:count")

    def conds_hold(line, px, py, pz):
        for kind, dx, dy, dz, block in COND.findall(line):
            solid = (px + int(dx), py + int(dy), pz + int(dz)) in blocks
            is_air = not solid
            if (kind == "if") != (is_air and block == "minecraft:air"):
                return False
        return True

    hits = []
    for line in check_src:
        if line.startswith("#"):
            continue
        m = re.search(r"positioned (-?\d+) (-?\d+) (-?\d+)", line)
        px, py, pz = (int(v) for v in m.groups())
        if not conds_hold(line, px, py, pz):
            continue
        scores, final = {}, None
        for cl in count_src.splitlines():
            if cl.startswith("#"):
                continue
            add = ADD_TO.search(cl)
            if add:
                scores.setdefault(add.group(1), 0)
                scores[add.group(1)] += 1 if conds_hold(cl, px, py, pz) else 0
            elif cl.startswith("scoreboard players set "):
                scores.setdefault(cl.split()[3], 0)
            elif "run function" in cl:
                final = cl
        ok = True
        for holder, lo, hi in SCORE_COND.findall(final or ""):
            v = scores.get(holder, 0)
            ok &= (lo == "" or v >= int(lo)) and (hi == "" or v <= int(hi))
        if ok:
            hits.append((px, pz, py))
    return hits


CASES = {
    "full smiley": grid(smiley_at(4, 4)),
    "translated": grid(smiley_at(9, 2)),
    "two mouth cells": grid(smiley_at(4, 4, mouth=SM_MOUTH[:2])),
    "three mouth cells": grid(smiley_at(4, 4, mouth=SM_MOUTH[:3])),
    "one eye": grid([c for i, c in enumerate(smiley_at(4, 4)) if i != 1]),
    "mixed heights": grid([(5, Y, 5), (7, Y, 5)] + [(4 + dx, Y + 1, 4 + dz)
                                                    for dx, dz in SM_MOUTH[:3]]),
    "second level": grid(smiley_at(4, 4, y=Y + 1)),
    "seeded scatter": grid([(x, Y, z) for x in range(0, 17, 3) for z in range(0, 17, 3)]),
    "floor only": grid([]),
    "two smileys": grid(smiley_at(2, 2) + smiley_at(10, 9)),
}


def test_generated_pack_agrees_with_the_python_matcher():
    for name, blocks in CASES.items():
        want = sorted(match_tops(tops(blocks), SMILEY_RELAXED, AREA, LEVELS))
        got = sorted(run_pack(blocks))
        assert got == want, f"{name}: pack {got} != matcher {want}"


def test_generated_pack_command_count():
    """One command per window per level, plus the shared counter."""
    lines = [l for l in patterns.check_lines(SMILEY_RELAXED, AREA, LEVELS, "#hit", "detmc:count")
             if not l.startswith("#")]
    assert len(lines) == (17 - 5 + 1) ** 2 * len(LEVELS) == 338
    adds = [l for l in patterns.count_function(SMILEY_RELAXED, "detmc:hit").splitlines()
            if "add" in l]
    assert len(adds) == len(SM_MOUTH) == 10


# ------------------------------------------------------- the STRICT smiley
#
# sim.py:226 + `pat('smiley', ...)` at sim.py:275 with forbid_rest on (sim.py:247,
# 258-264): the seven cells of SMILEY_CELLS top out at Y and NOTHING else in the
# 5x5 window does.  These tests are the bracket the relaxed pattern does not have:
# a relaxed-but-not-strict arrangement, and a strict arrangement with one cell too
# many, both of which must NOT match.


def strict_at(x0, z0, y=Y):
    return [(x0 + dx, y, z0 + dz) for dx, dz in SMILEY_CELLS]


def strict_matches(blocks, area=AREA, levels=LEVELS):
    return match_tops(tops(blocks), SMILEY_STRICT, area, levels)


def test_strict_smiley_matches_the_exact_shape():
    assert strict_matches(grid(strict_at(4, 4))) == [(4, 4, Y)]


def test_strict_smiley_is_the_seven_cells_of_sim_py():
    assert list(SMILEY_STRICT.required) == [(1, 1), (3, 1), (0, 3), (1, 4),
                                            (2, 4), (3, 4), (4, 3)]
    assert SMILEY_STRICT.size == 5 and SMILEY_STRICT.min_any == 0
    assert len(SMILEY_STRICT.forbidden) == 25 - 7


def test_relaxed_but_not_strict_does_not_match_strict():
    """The case that motivated the strict detector: two eyes and three mouth
    cells fire `smiley_relaxed` and must not fire `smiley_strict`."""
    blocks = grid(smiley_at(4, 4, mouth=SM_MOUTH[:3]))
    assert matches(blocks) == [(4, 4, Y)]        # relaxed fires
    assert strict_matches(blocks) == []          # strict does not


def test_one_extra_block_in_the_window_breaks_strict():
    """forbid_rest, sim.py:258-264.  (2, 2) is the window centre and is not part
    of the shape, so a block there at the scan level disqualifies the window."""
    assert strict_matches(grid(strict_at(4, 4) + [(4 + 2, Y, 4 + 2)])) == []


def test_a_missing_mouth_cell_breaks_strict():
    assert strict_matches(grid(strict_at(4, 4)[:-1])) == []


def test_strict_matches_on_the_second_level_too():
    assert strict_matches(grid(strict_at(4, 4, y=Y + 1))) == [(4, 4, Y + 1)]


def test_strict_never_matches_the_seeded_scatter_grid():
    seeded = grid([(x, Y, z) for x in range(0, 17, 3) for z in range(0, 17, 3)])
    assert strict_matches(seeded) == []


def test_a_block_outside_the_window_does_not_break_strict():
    """Only the 5x5 window is forbidden ground, not the whole arena."""
    assert strict_matches(grid(strict_at(4, 4) + [(4 + 6, Y, 4 + 2)])) == [(4, 4, Y)]


STRICT_CASES = {
    "exact strict": grid(strict_at(4, 4)),
    "strict translated": grid(strict_at(9, 2)),
    "strict + one extra cell": grid(strict_at(4, 4) + [(4 + 2, Y, 4 + 2)]),
    "relaxed but not strict": grid(smiley_at(4, 4, mouth=SM_MOUTH[:3])),
    "missing a mouth cell": grid(strict_at(4, 4)[:-1]),
    "second level": grid(strict_at(4, 4, y=Y + 1)),
    "seeded scatter": grid([(x, Y, z) for x in range(0, 17, 3) for z in range(0, 17, 3)]),
    "floor only": grid([]),
    "block outside the window": grid(strict_at(4, 4) + [(4 + 6, Y, 4 + 2)]),
}


def test_generated_strict_pack_agrees_with_the_python_matcher():
    for name, blocks in STRICT_CASES.items():
        want = sorted(match_tops(tops(blocks), SMILEY_STRICT, AREA, LEVELS))
        got = sorted(run_pack(blocks, SMILEY_STRICT))
        assert got == want, f"{name}: pack {got} != matcher {want}"


def test_strict_pack_costs_one_command_per_window_per_level():
    lines = [l for l in patterns.check_lines(SMILEY_STRICT, AREA, LEVELS,
                                             "#hit", "detmc:count")
             if not l.startswith("#")]
    assert len(lines) == (17 - 5 + 1) ** 2 * len(LEVELS) == 338
    adds = [l for l in patterns.count_function(SMILEY_STRICT, "detmc:hit").splitlines()
            if "add" in l]
    assert len(adds) == 18, len(adds)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                fails += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{fails} failure(s)")
    raise SystemExit(1 if fails else 0)
