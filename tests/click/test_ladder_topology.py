"""Output-bus drawings: one rung per topology, exported and pinned to a golden CSV.

CLICK's syntax check redraws a pasted rung, so only a project saved after the check shows what
the CLICK keeps.  The golden holds two kinds of rung:

- Rungs R1 to R22 (out(C1) to the counters) are copied from the CSV ClickNick wrote for the
  CLICK's saved project of 2026-11-12.  They are CLICK evidence.
- The rungs after them (out(C52) on), marked ``R`` without a number, are pyrung's drawing.  They
  await the next paste and syntax check.

To check a change, paste the export into CLICK and run the syntax check.  Then write it to the
PLC, save, and compare the CSV ClickNick writes for the saved project (``ckp_check``).  A DIFF
means the CLICK redraws that topology.  Then copy the saved project's ``main.csv`` over
``ladder_topology_golden/main.csv``, so the test pins what the CLICK keeps.

Export the project for Guided Paste (``main.csv`` and ``nicknames.csv``) with::

    uv run python tests/click/test_ladder_topology.py [out_dir]    # default build/ladder-topology
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder._exporter import _LadderExporter
from pyrung.core import And, Counter, Or, Program, Rung, Timer, branch, count_up, on_delay, out

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

GOLDEN = Path(__file__).with_name("ladder_topology_golden")

TallyA = Counter.clone("TallyA")
TallyB = Counter.clone("TallyB")
WaitA = Timer.clone("WaitA")
WaitB = Timer.clone("WaitB")


def build_program() -> tuple[Program, TagMap]:
    """One rung per topology: plain outputs, Or rows, branches, and a box under an Or."""
    coil = iter(range(1, 400))

    def outputs(n: int) -> None:
        for _ in range(n):
            out(c[next(coil)])

    def rows(count: int, first: int = 2):
        return Or(*[x[first + k] for k in range(count)])

    with Program(strict=False) as logic:
        # An output bus of 2, 3 and 4 outputs.
        for n in (2, 3, 4):
            with Rung(x[1]):
                outputs(n)
        # An Or of 2 rows into 2 and 3 outputs, then 3 rows into 2, 3 and 4, 4 rows into 2 and 3,
        # and 5 rows into 2.
        for count, counts in ((2, (2, 3)), (3, (2, 3, 4)), (4, (2, 3)), (5, (2,))):
            for n in counts:
                with Rung(x[1], rows(count)):
                    outputs(n)
        # And rows inside the Or.
        with Rung(x[1], Or(And(x[2], x[3]), And(x[4], x[5]), x[6])):
            outputs(2)
        # A nested Or that flattens to 4 rows.
        with Rung(x[1], Or(Or(x[2], x[3]), Or(x[4], x[5]))):
            outputs(2)
        # An Or followed by a contact, and two Ors in a row.
        with Rung(x[1], rows(3), x[6]):
            outputs(2)
        with Rung(x[1], rows(3), rows(3, first=5)):
            outputs(2)
        # Branches with contacts of their own on the bus.
        with Rung(x[1]):
            outputs(1)
            with branch(x[2]):
                outputs(1)
            with branch(x[3]):
                outputs(1)
            outputs(1)
        # A branch inside a branch.
        with Rung(x[1]):
            outputs(1)
            with branch(x[2]):
                outputs(1)
                with branch(x[3]):
                    outputs(1)
            outputs(1)
        # An Or on a branch in front of the outputs.
        with Rung(x[1]):
            outputs(1)
            with branch(rows(3)):
                outputs(2)
        # A timer (no pins) beside a plain output, then a counter with a reset pin, after an Or.
        for count, wait in ((2, WaitA), (3, WaitB)):
            with Rung(x[1], rows(count)):
                outputs(1)
                on_delay(wait, preset=250)
        for count, tally in ((2, TallyA), (3, TallyB)):
            with Rung(x[1], rows(count)):
                count_up(tally, preset=5).reset(x[8])
        # The rungs above were saved by the CLICK.  The rungs below await the next paste.
        # An Or nested in an Or, with one output.
        with Rung(x[1], Or(Or(x[2], x[3]), x[4])):
            outputs(1)
        # Ors in series after a contact, with one output: 2 rows then 2, 2 then 3, 3 then 2,
        # three Ors, and an And row in the second Or.
        for first, second in ((2, 2), (2, 3), (3, 2)):
            with Rung(x[1], rows(first), rows(second, first=2 + first)):
                outputs(1)
        with Rung(x[1], rows(2), rows(2, first=4), rows(2, first=6)):
            outputs(1)
        with Rung(x[1], rows(2), Or(And(x[4], x[5]), x[6])):
            outputs(1)
        # Ors in series on the left rail, with one and two outputs.
        for n in (1, 2):
            with Rung(rows(2, first=1), rows(2, first=3)):
                outputs(n)
        # An Or of 2 rows into 5 and 6 outputs.
        for n in (5, 6):
            with Rung(x[1], rows(2)):
                outputs(n)
        # An Or of 2 rows on a branch in front of the outputs.
        with Rung(x[1]):
            outputs(1)
            with branch(rows(2)):
                outputs(2)
        # An Or followed by a branch between two outputs, and two Ors in series on a branch.
        with Rung(x[1], rows(3)):
            outputs(1)
            with branch(x[5]):
                outputs(1)
            outputs(1)
        with Rung(x[1]):
            outputs(1)
            with branch(rows(2), rows(2, first=4)):
                outputs(1)
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
    # The CLICK numbers its rungs (R1, R2, ...) where pyrung writes R.
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    return re.sub(r"^R\d+,", "R,", text, flags=re.MULTILINE)


def test_the_export_equals_the_golden(tmp_path: Path) -> None:
    export(tmp_path)
    assert _read(tmp_path / "main.csv") == _read(GOLDEN / "main.csv")


def test_the_importer_reads_the_golden_as_the_programs_rungs() -> None:
    """Each rung of the golden reads back with the program's own conditions and outputs."""
    logic, mapping = build_program()
    exporter = _LadderExporter(tag_map=mapping, program=logic)
    with (GOLDEN / "main.csv").open(encoding="utf-8", newline="") as handle:
        rows = [tuple(row) for row in csv.reader(handle)]
    rungs = rows[1 : len(rows) - len(exporter._end_rung())]
    exporter._validate_scope_roundtrip(
        source_rungs=logic.rungs, rendered_rows=rungs, scope="main", subroutine_name=None
    )


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "build/ladder-topology")
    export(target)
    print(f"Wrote {target}")
