#!/usr/bin/env python3
"""Ground-plane block patterns for the `pattern` detector.

Two patterns, both transcribed cell for cell from the Monte Carlo model so that a
hit in-game means the same thing as a hit in `montecarlo/summary.json`:

* `smiley_relaxed` -- two eyes and at least 3 of 10 mouth cells (sim.py:277-288).
  Measured in `hunt-wave1` as a near-daily event in this arena, so it is a score
  metric now rather than a detector.
* `smiley_strict`  -- the exact seven-cell shape, with the rest of the 5x5 window
  required to be clear (sim.py:226, matched by `pat('smiley', ...)` at sim.py:275
  with `forbid_rest` defaulted True at sim.py:247).  This is the detector.

Citations are `montecarlo/sim.py:<line>`:

    SM_EYES  = [(1, 1), (3, 1)]                                    sim.py:227
    SM_MOUTH = [(0, 3), (1, 3), (2, 3), (3, 3), (4, 3),
                (0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]            sim.py:228

    w = 5                                                          sim.py:278
    Y  = topy at SM_EYES[0]                                        sim.py:279-281
    m  = both eyes are `top_placed` and `topy == Y`                sim.py:282-284
    cnt= how many SM_MOUTH cells are `top_placed` and `topy == Y`  sim.py:285-287
    hit= m & (cnt >= 3)                                            sim.py:288

`top_placed` is "the topmost block of this column was placed by an enderman"
and `topy = hv - 1` is the y of that topmost block, so a cell matches when its
column's **top** block sits at exactly y == Y.

Two deliberate differences from the model, both in the direction of more hits:

* In-game there is no "who placed this" bit, so `top_placed` becomes "the column's
  top block is non-air".  The scenario keeps that honest by putting the pen floor
  one layer below the scanned levels and by spacing the seeded loose blocks 3
  apart: two loose blocks 2 apart in x with the same z cannot exist on a spacing-3
  grid, so the seeded layout can never satisfy the two eyes (asserted in
  `test_patterns.py::test_seeded_scatter_grid_never_matches`).
* The model scans every y; the datapack scans a fixed list of `levels` above the
  floor, because each level costs one command per anchor per tick.

Cell coordinates are `(dx, dz)`: index 0 is the x axis and index 1 is the z axis,
matching sim.py's `hv[x][z]` indexing (`h[cx * N + cz]`, sim.py:204).
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

Cell = Tuple[int, int]


@dataclass(frozen=True)
class Pattern:
    """`required` cells must all match; at least `min_any` of `any_of` must match.

    A cell "matches" when its column's topmost block is at exactly y == Y, where
    Y is the top y of the `anchor_cell` column (sim.py:279-281).
    """
    name: str
    size: int
    required: Sequence[Cell]
    any_of: Sequence[Cell] = field(default_factory=tuple)
    min_any: int = 0
    anchor_cell: Optional[Cell] = None   # defaults to required[0], as sim.py does
    forbid_rest: bool = False            # sim.py:258-264, `pat(forbid_rest=True)`

    @property
    def anchor(self) -> Cell:
        return self.anchor_cell or tuple(self.required[0])

    @property
    def forbidden(self) -> List[Cell]:
        """Cells that must NOT have their column top at Y.

        `sim.py:258-264`: with `forbid_rest` on, every cell of the size x size
        window that is not part of the shape is required to be clear at the scan
        level, so `m &= ~(top_placed & (topy == Y))`.  That is what separates the
        strict smiley from a window that merely contains one.
        """
        if not self.forbid_rest:
            return []
        req = {tuple(c) for c in self.required} | {tuple(c) for c in self.any_of}
        return [(dx, dz) for dx in range(self.size) for dz in range(self.size)
                if (dx, dz) not in req]


SM_EYES: List[Cell] = [(1, 1), (3, 1)]                       # sim.py:227
SM_MOUTH: List[Cell] = [(0, 3), (1, 3), (2, 3), (3, 3), (4, 3),
                        (0, 4), (1, 4), (2, 4), (3, 4), (4, 4)]  # sim.py:228

SMILEY_RELAXED = Pattern(name="smiley_relaxed", size=5,
                         required=SM_EYES, any_of=SM_MOUTH, min_any=3)

# The STRICT smiley, `sim.py:226` cell for cell:
#   SMILEY = ([(1, 1), (3, 1), (0, 3), (1, 4), (2, 4), (3, 4), (4, 3)], 5)
# two eyes and a five-cell curved mouth, matched by `pat('smiley', ...)` at
# sim.py:275 with `forbid_rest` left at its default True (sim.py:247).  So all
# seven cells must have their column top at Y (sim.py:250-253) AND the other 18
# cells of the 5x5 window must not (sim.py:258-264).  There is no `any_of` and no
# threshold: this is the exact shape or nothing.
SMILEY_CELLS: List[Cell] = [(1, 1), (3, 1), (0, 3), (1, 4),
                            (2, 4), (3, 4), (4, 3)]          # sim.py:226
SMILEY_STRICT = Pattern(name="smiley_strict", size=5,
                        required=SMILEY_CELLS, forbid_rest=True)

PATTERNS: Dict[str, Pattern] = {p.name: p for p in (SMILEY_RELAXED, SMILEY_STRICT)}


def get_pattern(spec) -> Pattern:
    """`spec` is a pattern name, or a dict describing one inline."""
    if isinstance(spec, str):
        if spec not in PATTERNS:
            raise ValueError(f"unknown pattern {spec!r}; known: {sorted(PATTERNS)}")
        return PATTERNS[spec]
    cells = lambda v: [(int(a), int(b)) for a, b in v]       # noqa: E731
    return Pattern(name=spec.get("name", "custom"), size=int(spec["size"]),
                   required=cells(spec["required"]),
                   any_of=cells(spec.get("any_of", [])),
                   min_any=int(spec.get("min_any", 0)),
                   anchor_cell=tuple(spec["anchor_cell"]) if spec.get("anchor_cell") else None,
                   forbid_rest=bool(spec.get("forbid_rest", False)))


# ------------------------------------------------------------------ reference

def match_tops(tops: Dict[Cell, Optional[int]], pattern: Pattern,
               area: Tuple[int, int, int, int],
               levels: Optional[Sequence[int]] = None) -> List[Tuple[int, int, int]]:
    """Reference matcher.  `tops[(x, z)]` is the y of that column's topmost block
    (None for an empty column).  Returns every `(x0, z0, Y)` that matches, where
    `(x0, z0)` is the low-x low-z corner of the size x size window.

    This is the semantics the generated datapack has to reproduce; the two are
    cross-checked in test_patterns.py.
    """
    x1, z1, x2, z2 = area
    xa, xb = min(x1, x2), max(x1, x2)
    za, zb = min(z1, z2), max(z1, z2)
    w = pattern.size
    ax, az = pattern.anchor
    out = []
    for x0 in range(xa, xb - w + 2):
        for z0 in range(za, zb - w + 2):
            y = tops.get((x0 + ax, z0 + az))
            if y is None or (levels is not None and y not in levels):
                continue
            if not all(tops.get((x0 + dx, z0 + dz)) == y for dx, dz in pattern.required):
                continue
            cnt = sum(1 for dx, dz in pattern.any_of if tops.get((x0 + dx, z0 + dz)) == y)
            if cnt < pattern.min_any:
                continue
            # sim.py:258-264: nothing else in the window may top out at Y
            if any(tops.get((x0 + dx, z0 + dz)) == y for dx, dz in pattern.forbidden):
                continue
            out.append((x0, z0, y))
    return out


def tops_from_blocks(blocks) -> Dict[Cell, int]:
    """`blocks` is an iterable of (x, y, z) non-air positions -> per-column top y."""
    tops: Dict[Cell, int] = {}
    for x, y, z in blocks:
        if tops.get((x, z), -1 << 30) < y:
            tops[(x, z)] = y
    return tops


# ------------------------------------------------------------- mcfunction gen
#
# Per anchor and per level the generated pack costs ONE command, because the
# `required` cells are folded into that command's condition chain and the
# expensive `any_of` count only runs for a window that already has both eyes.
# The count itself lives in a single shared function that works in coordinates
# relative to the window corner (`execute positioned <x0> <Y> <z0>`), so the pack
# has one mouth function instead of one per anchor.

AIR = "minecraft:air"


def _top_at(dx, dy, dz, air=AIR):
    """Condition pair for "this column's top block is at the scan level"."""
    return (f"unless block ~{dx} ~{dy} ~{dz} {air} "
            f"if block ~{dx} ~{dy + 1} ~{dz} {air}")


def count_function(pattern: Pattern, hit_fn: str, air: str = AIR,
                   counter: Optional[str] = None) -> str:
    """The shared second stage, in window-relative coordinates.

    It runs only for a window whose `required` cells already match, and it
    answers the two questions a window can still fail on:

    * how many `any_of` cells are at the scan level (`>= min_any` to pass), which
      is the relaxed smiley's `cnt >= 3` (sim.py:288);
    * how many `forbidden` cells are at the scan level (`== 0` to pass), which is
      `forbid_rest` (sim.py:258-264) and is what makes the strict smiley strict.

    A pattern with no `any_of` emits no counter for it, and likewise for
    `forbidden`, so the relaxed pattern's function is unchanged.
    """
    ctr = (counter or f"#c_{pattern.name}")[:40]
    ctrf = (("#f_" + ctr[3:]) if ctr.startswith("#c_") else ctr + "_f")[:40]
    forbidden = pattern.forbidden
    what = []
    if pattern.any_of:
        what.append(f"count the '{pattern.min_any} of {len(pattern.any_of)}' cells "
                    f"at the scan level")
    if forbidden:
        what.append(f"require the other {len(forbidden)} cells of the "
                    f"{pattern.size}x{pattern.size} window to be clear at it")
    lines = [f"# {pattern.name}: " + ", and ".join(what or ["no second stage"])]
    conds = []
    if pattern.any_of:
        lines.append(f"scoreboard players set {ctr} detmc 0")
        for dx, dz in pattern.any_of:
            lines.append(f"execute {_top_at(dx, 0, dz, air)} run "
                         f"scoreboard players add {ctr} detmc 1")
        conds.append(f"if score {ctr} detmc matches {pattern.min_any}..")
    if forbidden:
        lines.append(f"scoreboard players set {ctrf} detmc 0")
        for dx, dz in forbidden:
            lines.append(f"execute {_top_at(dx, 0, dz, air)} run "
                         f"scoreboard players add {ctrf} detmc 1")
        conds.append(f"if score {ctrf} detmc matches ..0")
    lines.append((f"execute {' '.join(conds)} run function {hit_fn}") if conds
                 else f"function {hit_fn}")
    return "\n".join(lines) + "\n"


def check_lines(pattern: Pattern, area: Tuple[int, int, int, int],
                levels: Sequence[int], hit_flag: str, count_fn: str,
                air: str = AIR) -> List[str]:
    """One line per (window, level): all `required` cells, then call the counter."""
    x1, z1, x2, z2 = area
    xa, xb = min(x1, x2), max(x1, x2)
    za, zb = min(z1, z2), max(z1, z2)
    w = pattern.size
    lines = [f"# {pattern.name}: {w}x{w} windows in [{xa},{za}]..[{xb},{zb}] "
             f"at y {list(levels)}; required {list(pattern.required)}, "
             f"{pattern.min_any} of {len(pattern.any_of)} in {list(pattern.any_of)}"]
    for y in levels:
        for x0 in range(xa, xb - w + 2):
            for z0 in range(za, zb - w + 2):
                conds = " ".join(_top_at(dx, 0, dz, air) for dx, dz in pattern.required)
                lines.append(f"execute if score {hit_flag} detmc matches 0 "
                             f"positioned {x0} {y} {z0} {conds} "
                             f"run function {count_fn}")
    return lines


def best_match_function(pattern: Pattern, name: str, air: str = AIR) -> str:
    """`detmc:score_<name>`: how close the window at the caller's position is.

    Window-relative, exactly like `count_function`, and called once per (window,
    level) by the generated `detmc:score`.  It keeps two running maxima, using
    `scoreboard players operation ... > ...` (verified on a live 26.2 server: `>`
    assigns the larger of the two, so it is a max accumulator):

      #s_<name>        the most cells of `required + any_of` that sit at the scan
                       level in any one window -- a pure "closest shape so far"
      #s_<name>_gated  the most `any_of` cells in a window whose `required` cells
                       ALL match, i.e. distance to `min_any` once the hard part of
                       the pattern is already there.  Starts at -1 and stays there
                       while no window has the required cells, which is a different
                       statement from "0 of the mouth".
    """
    tr, ta, tt = f"#tr_{name}", f"#ta_{name}", f"#tt_{name}"
    lines = [f"# {pattern.name}: partial match at the caller's window position",
             f"scoreboard players set {tr} detmc 0",
             f"scoreboard players set {ta} detmc 0"]
    for dx, dz in pattern.required:
        lines.append(f"execute {_top_at(dx, 0, dz, air)} run "
                     f"scoreboard players add {tr} detmc 1")
    for dx, dz in pattern.any_of:
        lines.append(f"execute {_top_at(dx, 0, dz, air)} run "
                     f"scoreboard players add {ta} detmc 1")
    lines += [f"scoreboard players operation {tt} detmc = {tr} detmc",
              f"scoreboard players operation {tt} detmc += {ta} detmc",
              f"scoreboard players operation #s_{name} detmc > {tt} detmc",
              f"execute if score {tr} detmc matches {len(pattern.required)}.. run "
              f"scoreboard players operation #s_{name}_gated detmc > {ta} detmc"]
    return "\n".join(lines) + "\n"
