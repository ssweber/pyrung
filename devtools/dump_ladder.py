"""Dump wild ladder patterns as CSV for visual review."""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from pyrung.click import TagMap, c, ct, ctd, pyrung_to_ladder, x, y
from pyrung.core import Bool, Dint, Program, Rung, any_of
from pyrung.core.program import all_of, branch, count_up, latch, out

OUT = Path(tempfile.mkdtemp(prefix="ladder_"))


def _dump(label: str, bundle):
    safe = label.replace(" ", "_").replace(",", "").replace(":", "")[:60]
    d = OUT / safe
    d.mkdir(parents=True, exist_ok=True)
    bundle.write(d)
    print(f"\n== {label} ==")
    print((d / "main.csv").read_text(encoding="utf-8"), end="")


# --- Tags ---
A = Bool("A")
B = Bool("B")
C = Bool("C")
D = Bool("D")
E = Bool("E")
F = Bool("F")
G = Bool("G")
Mode = Bool("Mode")
Mode2 = Bool("Mode2")
Y1 = Bool("Y1")
Y2 = Bool("Y2")
Y3 = Bool("Y3")
Y4 = Bool("Y4")

base_map = {
    A: x[1],
    B: x[2],
    C: x[3],
    D: x[4],
    E: x[5],
    F: x[6],
    G: x[7],
    Mode: c[1],
    Mode2: c[2],
    Y1: y[1],
    Y2: y[2],
    Y3: y[3],
    Y4: y[4],
}

# ── Case 1: Deep AND chain + branch ──
with Program() as logic:
    with Rung(A, B, C, D):
        out(Y1)
        with branch(E):
            out(Y2)
        with branch(F):
            out(Y3)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 1: A,B,C,D + branch(E) + branch(F)", pyrung_to_ladder(logic, mapping))

# ── Case 2: Deep AND + branch with its own condition ──
with Program() as logic:
    with Rung(A, B, C):
        out(Y1)
        with branch(D, E):
            out(Y2)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 2: A,B,C + branch(D,E)", pyrung_to_ladder(logic, mapping))

# ── Case 3: Single condition + many branches ──
with Program() as logic:
    with Rung(A):
        out(Y1)
        with branch(B):
            out(Y2)
        with branch(C):
            out(Y3)
        with branch(D):
            out(Y4)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 3: A + branch(B) + branch(C) + branch(D)", pyrung_to_ladder(logic, mapping))

# ── Case 4: Branch after multiple outputs ──
with Program() as logic:
    with Rung(A):
        out(Y1)
        latch(Y2)
        with branch(Mode):
            out(Y3)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 4: A out+latch + branch(Mode)", pyrung_to_ladder(logic, mapping))

# ── Case 5: Branch sandwiched between instructions ──
with Program() as logic:
    with Rung(A, B):
        out(Y1)
        with branch(Mode):
            out(Y2)
        out(Y3)
        with branch(Mode2):
            out(Y4)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 5: A,B out branch out branch", pyrung_to_ladder(logic, mapping))

# ── Case 6: any_of inside all_of ──
with Program() as logic:
    with Rung(all_of(A, any_of(B, C), D)):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 6: all_of(A, any_of(B,C), D)", pyrung_to_ladder(logic, mapping))

# ── Case 7: any_of(all_of, all_of) ──
with Program() as logic:
    with Rung(any_of(all_of(A, B), all_of(C, D))):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 7: any_of(all_of(A,B), all_of(C,D))", pyrung_to_ladder(logic, mapping))

# ── Case 8: any_of with mixed leaf/chain ──
with Program() as logic:
    with Rung(any_of(A, B, C, all_of(D, E, F))):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 8: any_of(A, B, C, all_of(D,E,F))", pyrung_to_ladder(logic, mapping))

# ── Case 9: Deep AND then OR ──
with Program() as logic:
    with Rung(A, B, C, D, any_of(E, F, G)):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 9: A,B,C,D,any_of(E,F,G)", pyrung_to_ladder(logic, mapping))

# ── Case 10: nested any_of ──
with Program() as logic:
    with Rung(any_of(any_of(A, B), any_of(C, D))):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 10: any_of(any_of(A,B), any_of(C,D))", pyrung_to_ladder(logic, mapping))

# ── Case 11: negated OR branches ──
with Program() as logic:
    with Rung(A, any_of(~B, C, ~D)):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 11: A, any_of(~B, C, ~D)", pyrung_to_ladder(logic, mapping))

# ── Case 12: branch with pin rows ──
Done = Bool("Done")
Acc = Dint("Acc")
ResetCond = Bool("ResetCond")

with Program() as logic:
    with Rung(A, B):
        out(Y1)
        with branch(Mode):
            count_up(Done, Acc, preset=10).reset(ResetCond)

mapping = TagMap(
    {**base_map, Done: ct[1], Acc: ctd[1], ResetCond: x[8]},
    include_system=False,
)
_dump("Case 12: A,B out + branch(Mode) count_up.reset", pyrung_to_ladder(logic, mapping))

# ── Case 13: asymmetric OR widths ──
with Program() as logic:
    with Rung(any_of(A, all_of(B, C, D))):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 13: any_of(A, all_of(B,C,D))", pyrung_to_ladder(logic, mapping))

# ── Case 14: ORs sandwiching ANDs ──
with Program() as logic:
    with Rung(any_of(A, B), E, any_of(C, D), F):
        out(Y1)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 14: any_of(A,B), E, any_of(C,D), F", pyrung_to_ladder(logic, mapping))

# ── Case 15: OR + branch ──
with Program() as logic:
    with Rung(any_of(A, B)):
        out(Y1)
        with branch(Mode):
            out(Y2)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 15: any_of(A,B) + out + branch(Mode)", pyrung_to_ladder(logic, mapping))

# ── Case 16: 3-OR + branch ──
with Program() as logic:
    with Rung(any_of(A, B, C)):
        out(Y1)
        with branch(Mode):
            out(Y2)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 16: any_of(A,B,C) + out + branch(Mode)", pyrung_to_ladder(logic, mapping))

# ── Case 17: OR + two branches ──
with Program() as logic:
    with Rung(any_of(A, B)):
        out(Y1)
        with branch(Mode):
            out(Y2)
        with branch(Mode2):
            out(Y3)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 17: any_of(A,B) + out + branch(M1) + branch(M2)", pyrung_to_ladder(logic, mapping))

# ── Case 18: OR + branch + trailing out ──
with Program() as logic:
    with Rung(any_of(A, B)):
        out(Y1)
        with branch(Mode):
            out(Y2)
        out(Y3)

mapping = TagMap({**base_map}, include_system=False)
_dump("Case 18: any_of(A,B) + out + branch(Mode) + out", pyrung_to_ladder(logic, mapping))

# ── Case 19: OR + branch-first (no leading instruction) ──
with Program() as logic:
    with Rung(any_of(A, B)):
        with branch(Mode):
            out(Y1)
        with branch(Mode2):
            out(Y2)

mapping = TagMap({**base_map}, include_system=False)
_dump(
    "Case 19: any_of(A,B) + branch(M1) + branch(M2) [no leading out]",
    pyrung_to_ladder(logic, mapping),
)

print(f"\nCSVs written to: {OUT}")
