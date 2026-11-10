"""Output-bus drawings: one rung per topology, exported and pinned to a golden CSV.

The golden is the drawing the CLICK keeps.  To check it, paste the export into CLICK, write it
to the PLC, save, and compare the CSV ClickNick writes for the saved project (``ckp_check``).
A DIFF means the CLICK redraws that topology.  Then copy the saved project's ``main.csv`` over
``ladder_topology_golden/main.csv``, so the test pins what the CLICK keeps.

Export the project for Guided Paste (``main.csv`` and ``nicknames.csv``) with::

    uv run python tests/click/test_ladder_topology.py [out_dir]    # default build/ladder-topology
"""

from __future__ import annotations

import sys
from pathlib import Path

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.core import Counter, Or, Program, Rung, Timer, branch, count_up, on_delay, out

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

GOLDEN = Path(__file__).with_name("ladder_topology_golden")

TallyA = Counter.clone("TallyA")
TallyB = Counter.clone("TallyB")
WaitA = Timer.clone("WaitA")
WaitB = Timer.clone("WaitB")


def build_program() -> tuple[Program, TagMap]:
    """One rung per topology: plain outputs, Or rows, branches, and a box under an Or."""
    with Program(strict=False) as logic:
        # An output bus of 2, 3 and 4 outputs.
        for n in (2, 3, 4):
            with Rung(x[1]):
                for k in range(n):
                    out(c[10 * n + k])
        # An Or of 2, 3 and 4 rows into 2 and 3 outputs.
        for rows in (2, 3, 4):
            for n in (2, 3):
                with Rung(x[1], Or(*[x[1 + k] for k in range(1, rows + 1)])):
                    for k in range(n):
                        out(c[100 + 10 * rows + 3 * n + k])
        # Branches with contacts of their own on the bus.
        with Rung(x[1]):
            out(c[201])
            with branch(x[2]):
                out(c[202])
            with branch(x[3]):
                out(c[203])
            out(c[204])
        # A branch inside a branch.
        with Rung(x[1]):
            out(c[211])
            with branch(x[2]):
                out(c[212])
                with branch(x[3]):
                    out(c[213])
            out(c[214])
        # An Or on a branch in front of the outputs.
        with Rung(x[1]):
            out(c[221])
            with branch(Or(x[2], x[3], x[4])):
                out(c[222])
                out(c[223])
        # A timer (no pins) beside a plain output, then a counter with a reset pin, after an Or.
        for rows, wait in ((2, WaitA), (3, WaitB)):
            with Rung(x[1], Or(*[x[1 + k] for k in range(1, rows + 1)])):
                out(c[230 + rows])
                on_delay(wait, preset=250)
        for rows, tally in ((2, TallyA), (3, TallyB)):
            with Rung(x[1], Or(*[x[1 + k] for k in range(1, rows + 1)])):
                count_up(tally, preset=5).reset(x[8])
    mapping = TagMap(
        {
            TallyA.Done: ct[2],
            TallyA.Acc: ctd[2],
            TallyB.Done: ct[3],
            TallyB.Acc: ctd[3],
            WaitA.Done: t[2],
            WaitA.Acc: td[2],
            WaitB.Done: t[3],
            WaitB.Acc: td[3],
        },
        include_system=False,
    )
    return logic, mapping


def export(out_dir: Path) -> None:
    logic, mapping = build_program()
    pyrung_to_ladder(logic, mapping).write(out_dir)
    mapping.to_nickname_file(out_dir / "nicknames.csv")


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def test_the_export_equals_the_golden(tmp_path: Path) -> None:
    export(tmp_path)
    assert _read(tmp_path / "main.csv") == _read(GOLDEN / "main.csv")


def test_the_importer_reads_the_golden_as_the_export() -> None:
    logic, mapping = build_program()
    bundle = pyrung_to_ladder(logic, mapping)
    assert ladder_to_pyrung(GOLDEN / "main.csv") == ladder_to_pyrung(bundle)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "build/ladder-topology")
    export(target)
    print(f"Wrote {target}")
