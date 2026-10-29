"""Step-cost probe: one CLICK project, one instruction per rung.

The costs in ``pyrung.click.ladder.steps`` come from this project.  Load it in
CLICK, read the program's step count, then delete rungs from the bottom up and
read the count after each deletion.  Each difference is the cost of the rung
deleted.  The first rungs are calibration: a bare coil, contacts in series and
parallel, comments, so later rungs' costs split into "X1 contact" and "the
instruction".

    uv run python devtools/click_step_probe.py build/step_probe

writes main.csv, subroutines/, nicknames.csv (Guided Paste) and SHEET.md, the
sheet to fill in.  A new probe round adds rungs here and its readings below.

    uv run python devtools/click_step_probe.py --check

compares the estimator with the readings of 2026-10-29 (CLICK Programming
Software, the PC-side count of "Write Project into PLC").
"""

from __future__ import annotations

import argparse
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pyrung.click import ClickBlocks, TagMap
from pyrung.click.ladder import LadderBundle, pyrung_to_ladder
from pyrung.click.ladder.steps import (
    MAIN_BASE,
    SUBROUTINE_BASE,
    _rung_cost,
    _rungs,
    estimate_steps,
)
from pyrung.core import (
    Or,
    Program,
    Rung,
    Tag,
    blockcopy,
    branch,
    calc,
    call,
    comment,
    copy,
    count_down,
    count_up,
    event_drum,
    fall,
    fill,
    flasher,
    forloop,
    immediate,
    latch,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_words,
    reset,
    return_early,
    rise,
    search,
    shift,
    subroutine,
    time_drum,
    toggle,
    unpack_to_bits,
    unpack_to_words,
)

b = ClickBlocks()
x, y, c, ds, dd, df, dh, td = b.x, b.y, b.c, b.ds, b.dd, b.df, b.dh, b.td


@dataclass(frozen=True)
class ClickTimer:
    """Timer ``n`` as CLICK addresses it: T<n> and TD<n>."""

    n: int

    @property
    def Done(self) -> Tag:
        return b.t[self.n]

    @property
    def Acc(self) -> Tag:
        return b.td[self.n]


@dataclass(frozen=True)
class ClickCounter:
    """Counter ``n`` as CLICK addresses it: CT<n> and CTD<n>."""

    n: int

    @property
    def Done(self) -> Tag:
        return b.ct[self.n]

    @property
    def Acc(self) -> Tag:
        return b.ctd[self.n]


#: (label, rung builder), in rung order.  A builder may emit more than one
#: rung (For/Next); the user deletes all of them at once.
PROBES: list[tuple[str, Callable[[], None]]] = []


def probe(label: str):
    def register(fn: Callable[[], None]) -> Callable[[], None]:
        PROBES.append((label, fn))
        return fn

    return register


# --- calibration -----------------------------------------------------------


@probe("X1 -> OUT Y1 (baseline rung)")
def _():
    with Rung(x[1]):
        out(y[1])


@probe("X1 -> OUT Y2 (same again: is it linear?)")
def _():
    with Rung(x[1]):
        out(y[2])


@probe("no contact -> OUT Y3 (always-on rail)")
def _():
    with Rung():
        out(y[3])


@probe("X1,X2 -> OUT (2 contacts in series)")
def _():
    with Rung(x[1], x[2]):
        out(y[4])


@probe("X1,X2,X3,X4 -> OUT (4 in series)")
def _():
    with Rung(x[1], x[2], x[3], x[4]):
        out(y[5])


@probe("~X1 -> OUT (NC contact)")
def _():
    with Rung(~x[1]):
        out(y[6])


@probe("X1 OR X2 -> OUT (2 in parallel)")
def _():
    with Rung(Or(x[1], x[2])):
        out(y[7])


@probe("X1 OR X2 OR X3 -> OUT (3 in parallel)")
def _():
    with Rung(Or(x[1], x[2], x[3])):
        out(y[8])


@probe("X1 -> OUT, OUT (2 coils on one rung)")
def _():
    with Rung(x[1]):
        out(c[1])
        out(c[2])


@probe("X1 -> OUT, OUT, OUT, OUT (4 coils)")
def _():
    with Rung(x[1]):
        out(c[3])
        out(c[4])
        out(c[5])
        out(c[6])


@probe("X1 -> branch(X2) OUT (branch with its own contact)")
def _():
    with Rung(x[1]):
        out(c[7])
        with branch(x[2]):
            out(c[8])


@probe("X1 -> OUT C9, short comment on the rung")
def _():
    comment("Step probe.")
    with Rung(x[1]):
        out(c[9])


@probe("X1 -> OUT C10, ~400-char comment on the rung")
def _():
    comment("Step probe, long comment. " * 16)
    with Rung(x[1]):
        out(c[10])


# --- contacts --------------------------------------------------------------


@probe("rise(X1) -> OUT (edge contact)")
def _():
    with Rung(rise(x[1])):
        out(c[11])


@probe("fall(X1) -> OUT (edge contact)")
def _():
    with Rung(fall(x[1])):
        out(c[12])


@probe("immediate(X1) -> OUT (immediate contact)")
def _():
    with Rung(immediate(x[1])):
        out(c[13])


@probe("DS1 == 5 -> OUT (compare, constant)")
def _():
    with Rung(ds[1] == 5):
        out(c[14])


@probe("DS1 == DS2 -> OUT (compare, register)")
def _():
    with Rung(ds[1] == ds[2]):
        out(c[15])


@probe("DS1 > 5 -> OUT (compare >)")
def _():
    with Rung(ds[1] > 5):
        out(c[16])


@probe("DD1 > 100000 -> OUT (compare, DINT constant)")
def _():
    with Rung(dd[1] > 100000):
        out(c[17])


@probe("DF1 > 1.5 -> OUT (compare, REAL)")
def _():
    with Rung(df[1] > 1.5):
        out(c[18])


# --- coils -----------------------------------------------------------------


@probe("X1 -> SET C21")
def _():
    with Rung(x[1]):
        latch(c[21])


@probe("X1 -> RST C21")
def _():
    with Rung(x[1]):
        reset(c[21])


@probe("X1 -> OUT immediate(Y9)")
def _():
    with Rung(x[1]):
        out(immediate(y[9]))


@probe("X1 -> RST C31..C40 (range)")
def _():
    with Rung(x[1]):
        reset(c.select(31, 40))


@probe("X1 -> toggle C22 (Out, Toggle)")
def _():
    with Rung(x[1]):
        toggle(c[22])


@probe("X1 -> flasher C23 (Out, Flasher, T11/T12)")
def _():
    with Rung(x[1]):
        flasher(c[23], on=(ClickTimer(11), 500), off=(ClickTimer(12), 500))


# --- timers and counters ---------------------------------------------------


@probe("X1 -> TON T1 1000 ms")
def _():
    with Rung(x[1]):
        on_delay(ClickTimer(1), 1000)


@probe("X1 -> TON T2, preset DS3 (register)")
def _():
    with Rung(x[1]):
        on_delay(ClickTimer(2), ds[3])


@probe("X1 -> TOF T3 1000 ms")
def _():
    with Rung(x[1]):
        off_delay(ClickTimer(3), 1000)


@probe("X1 -> RTO T4 1000 ms, reset X2 (retentive)")
def _():
    with Rung(x[1]):
        on_delay(ClickTimer(4), 1000).reset(x[2])


@probe("X1 -> CTU CT1 10, reset X2")
def _():
    with Rung(x[1]):
        count_up(ClickCounter(1), 10).reset(x[2])


@probe("X1 -> CTD CT2 10, reset X2")
def _():
    with Rung(x[1]):
        count_down(ClickCounter(2), 10).reset(x[2])


@probe("X1 -> CTUD CT3 10, down X3, reset X2")
def _():
    with Rung(x[1]):
        count_up(ClickCounter(3), 10).down(x[3]).reset(x[2])


# --- copy family -----------------------------------------------------------


@probe("X1 -> copy 5 -> DS10")
def _():
    with Rung(x[1]):
        copy(5, ds[10])


@probe("X1 -> copy DS11 -> DS12")
def _():
    with Rung(x[1]):
        copy(ds[11], ds[12])


@probe("X1 -> copy 5 -> DS13, oneshot")
def _():
    with Rung(x[1]):
        copy(5, ds[13], oneshot=True)


@probe("X1 -> copy 1.5 -> DF10")
def _():
    with Rung(x[1]):
        copy(1.5, df[10])


@probe("X1 -> copy 5 -> DS14, then copy 6 -> DS15 (2 copies)")
def _():
    with Rung(x[1]):
        copy(5, ds[14])
        copy(6, ds[15])


@probe("X1 -> blockcopy DS101..DS110 -> DS111..DS120")
def _():
    with Rung(x[1]):
        blockcopy(ds.select(101, 110), ds.select(111, 120))


@probe("X1 -> fill 0 -> DS121..DS130")
def _():
    with Rung(x[1]):
        fill(0, ds.select(121, 130))


@probe("X1 -> pack_bits C101..C116 -> DS20")
def _():
    with Rung(x[1]):
        pack_bits(c.select(101, 116), ds[20])


@probe("X1 -> unpack_to_bits DS21 -> C121..C136")
def _():
    with Rung(x[1]):
        unpack_to_bits(ds[21], c.select(121, 136))


@probe("X1 -> pack_words DH1..DH2 -> DD10")
def _():
    with Rung(x[1]):
        pack_words(dh.select(1, 2), dd[10])


@probe("X1 -> unpack_to_words DD11 -> DH3..DH4")
def _():
    with Rung(x[1]):
        unpack_to_words(dd[11], dh.select(3, 4))


# --- math ------------------------------------------------------------------


@probe("X1 -> calc DS30 + DS31 -> DS32")
def _():
    with Rung(x[1]):
        calc(ds[30] + ds[31], ds[32])


@probe("X1 -> calc (DS33*DS34 + DS35) / DS36 -> DS37 (longer formula)")
def _():
    with Rung(x[1]):
        calc((ds[33] * ds[34] + ds[35]) / ds[36], ds[37])


@probe("X1 -> calc DF1 * DF2 -> DF3 (REAL)")
def _():
    with Rung(x[1]):
        calc(df[1] * df[2], df[3])


@probe("X1 -> calc DF1 * DF2 -> DF4, oneshot")
def _():
    with Rung(x[1]):
        calc(df[1] * df[2], df[4], oneshot=True)


@probe("rise(X1) -> calc DF1*1.8 + 32.0 -> DF5 (community '10 steps' case)")
def _():
    with Rung(rise(x[1])):
        calc(df[1] * 1.8 + 32.0, df[5])


@probe("X1 -> calc DH5 AND DH6 -> DH7 (hex math)")
def _():
    with Rung(x[1]):
        calc(dh[5] & dh[6], dh[7])


# --- search, shift, drum ---------------------------------------------------


@probe("X1 -> search DS201..DS210 == 7 -> DS40, found C41")
def _():
    with Rung(x[1]):
        search(ds.select(201, 210) == 7, result=ds[40], found=c[41])


@probe("X1 -> shift C201..C208, clock X2, reset X3")
def _():
    with Rung(x[1]):
        shift(c.select(201, 208)).clock(x[2]).reset(x[3])


@probe("X1 -> event drum, 2 outputs x 3 steps, reset X2")
def _():
    with Rung(x[1]):
        event_drum(
            outputs=[c[301], c[302]],
            events=[c[303], c[304], c[305]],
            pattern=[[1, 0], [0, 1], [1, 1]],
            current_step=ds[50],
            completion_flag=c[306],
        ).reset(x[2])


@probe("X1 -> time drum, 2 outputs x 3 steps, reset X2")
def _():
    with Rung(x[1]):
        time_drum(
            outputs=[c[311], c[312]],
            presets=[100, 100, 100],
            unit="ms",
            pattern=[[1, 0], [0, 1], [1, 1]],
            current_step=ds[51],
            accumulator=td[21],
            completion_flag=c[316],
        ).reset(x[2])


# --- program control (multi-rung groups last) ------------------------------


@probe("X1 -> call Probe_Sub")
def _():
    with Rung(x[1]):
        call(probe_sub)


@probe("For 3 / body copy / Next (3 rungs: delete all three together)")
def _():
    with Rung(x[1]):
        with forloop(3):
            copy(1, ds[60])


@subroutine("Probe_Sub")
def probe_sub():
    with Rung(x[1]):
        out(c[401])
    with Rung(x[2]):
        return_early()


def build() -> tuple[Program, TagMap]:
    with Program(strict=False) as program:
        for _label, fn in PROBES:
            fn()
    return program, TagMap([])


#: Step counts read on 2026-10-29: the whole project, then after each deletion
#: of the sheet's main rows (For/Next first, R1 last), leaving main's END.
MAIN_READINGS = (
    290, 280, 278, 262, 246, 241, 234, 228, 221, 215, 209, 199, 193, 188, 182,
    177, 171, 166, 160, 148, 143, 138, 133, 128, 123, 119, 115, 111, 108, 105,
    102, 97, 95, 92, 90, 88, 86, 82, 78, 74, 70, 66, 64, 62, 60, 58, 56, 50,
    40, 34, 30, 27, 25, 20, 17, 15, 13, 11,
)  # fmt: skip
#: Then Probe_Sub: rung 2 deleted, rung 1 deleted, only its RET left (which
#: CLICK keeps), and with the subroutine itself deleted, main's END alone.
SUB_READINGS = (9, 7, 7, 3)

SHEET_HEAD = """# Step probe sheet

1. Guided Paste the folder (main.csv, subroutines/Probe_Sub.csv).
2. Read the step count ("Write Project into PLC" shows the PC side).  That is row 0.
3. Delete main's rungs from the bottom up, one row of this table at a time, and
   read the count after each.  Multi-rung rows (For/Next) go in one deletion.
   Leave the END rung.
4. With main down to END: delete Probe_Sub's rungs one at a time, then the
   subroutine itself.

| # | rungs | deleted | steps after |
|---|-------|---------|-------------|
| 0 | | (nothing: whole project) | |
"""


def _probe_rungs(bundle: LadderBundle) -> list[list[int]]:
    """Each probe's 1-based main rung numbers, matched by source line."""
    found = []
    for _label, fn in PROBES:
        lines, start = inspect.getsourcelines(fn)
        end = start + len(lines) - 1
        found.append(
            [
                entry.rung
                for entry in bundle.main_sources
                if any(start <= (src.source_line or 0) <= end for src in entry.sources)
            ]
        )
    return found


def _span(numbers: list[int]) -> str:
    if not numbers:
        return "?"
    return f"R{numbers[0]}" if len(numbers) == 1 else f"R{numbers[0]}-R{numbers[-1]}"


def write(out_dir: Path) -> None:
    program, tag_map = build()
    bundle = pyrung_to_ladder(program, tag_map, index=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle.write(out_dir)
    tag_map.to_nickname_file(out_dir / "nicknames.csv")

    rows = [SHEET_HEAD]
    n = 1
    for (label, _fn), rungs in reversed(list(zip(PROBES, _probe_rungs(bundle), strict=True))):
        rows.append(f"| {n} | {_span(rungs)} | main: {label} | |\n")
        n += 1
    sub_rungs = len(dict(bundle.subroutine_rows)["Probe_Sub"]) - 1  # Less the header.
    for i in range(sub_rungs, 0, -1):
        rows.append(f"| {n} | R{i} | Probe_Sub rung {i} | |\n")
        n += 1
    rows.append(f"| {n} | | Probe_Sub itself (now empty) | |\n")
    (out_dir / "SHEET.md").write_text("".join(rows), encoding="utf-8")
    print(f"Wrote {out_dir}: {len(PROBES)} probes, sheet in SHEET.md")


def check() -> int:
    """Compare the estimator with every reading; return the number of misses."""
    program, tag_map = build()
    bundle = pyrung_to_ladder(program, tag_map, index=True)
    main_rungs = list(_rungs(bundle.main_rows[1:]))
    misses = 0

    def compare(label: str, estimated: int, measured: int) -> None:
        nonlocal misses
        if estimated != measured:
            misses += 1
            print(f"  {label}: estimated {estimated}, measured {measured}")

    deltas = [a - b for a, b in zip(MAIN_READINGS, MAIN_READINGS[1:], strict=False)]
    pairs = list(zip(PROBES, _probe_rungs(bundle), strict=True))
    for ((label, _fn), rungs), measured in zip(reversed(pairs), deltas, strict=True):
        estimated = sum(_rung_cost(main_rungs[r - 1], set()) for r in rungs)
        compare(label, estimated, measured)
    compare("main with only END", MAIN_BASE, SUB_READINGS[-1])
    compare("Probe_Sub with only RET", SUBROUTINE_BASE, SUB_READINGS[-2] - SUB_READINGS[-1])
    compare("whole project", estimate_steps(bundle).total, MAIN_READINGS[0])
    print(f"{misses} miss(es) against {len(deltas) + 3} readings")
    return misses


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir", nargs="?", default="build/step_probe")
    parser.add_argument("--check", action="store_true", help="compare with the readings")
    args = parser.parse_args(argv)
    if args.check:
        check()
        return 0
    write(Path(args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
