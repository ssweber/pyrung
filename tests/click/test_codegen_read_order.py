"""The importer reads each contact where the CLICK rung does.

CLICK solves a rung row by row: a contact cell is read on the row of the first
output its wire runs through, and two cells with the same label are two reads.
Grouping outputs under one ``with rung(...)`` or ``with branch(...)`` reads the
shared contacts once, at the group's first output, so the importer groups by
label only where no output in between writes what the contact reads.  The
exporter's factoring keeps the same rule.  Each case pins the generated shape
and runs it against a hand-written program with the CLICK rung's meaning.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.core import PLC, Program, Rung
from pyrung.core.program import branch, out, reset
from tests.click.helpers import exec_with_source

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_HEADER = ["marker", *(chr(65 + i) for i in range(26)), "AA", "AB", "AC", "AD", "AE", "AF"]


def _row(marker: str, cells: list[str], af: str) -> list[str]:
    return [marker, *cells, *["-"] * (31 - len(cells)), af]


def _rail_rows_write_c5() -> None:
    with Rung(c[5]):
        reset(c[5])
    with Rung(c[5]).continued():
        out(y[1])


def _trunk_then_written_contact() -> None:
    with Rung(x[1], c[5]):
        reset(c[5])
    with Rung(x[1], c[5], x[2]).continued():
        out(y[1])


def _teed_cells_write_c5() -> None:
    with Rung(x[1]):
        with branch(c[5]):
            reset(c[5])
        with branch(c[5], x[2]):
            out(y[1])


def _teed_cells_no_write() -> None:
    with Rung(x[1]):
        with branch(c[5]):
            reset(c[6])
        with branch(c[5], x[2]):
            out(y[1])


def _rail_rows_no_write() -> None:
    with Rung(c[5]):
        reset(c[6])
    with Rung(c[5]).continued():
        out(y[1])


def _coil_writes_trunk_contact() -> None:
    with Rung(x[1], c[5]):
        out(c[1])
    with Rung(c[1], x[2]).continued():
        out(y[1])


# name: (rows, the CLICK rung's meaning, text the generated code holds)
CASES: dict[str, tuple[list[list[str]], Callable[[], None], str]] = {
    "rail_rows_write_c5": (
        [_row("R", ["C5"], "reset(C5)"), _row("", ["C5"], "out(Y001)")],
        _rail_rows_write_c5,
        "with rung(C5).continued():\n        out(Y001)",
    ),
    "trunk_then_written_contact": (
        [_row("R", ["X001", "C5"], "reset(C5)"), _row("", ["X001", "C5", "X002"], "out(Y001)")],
        _trunk_then_written_contact,
        "with branch(C5, X002):\n            out(Y001)",
    ),
    "teed_cells_write_c5": (
        [_row("R", ["X001", "T:C5"], "reset(C5)"), _row("", ["", "C5", "X002"], "out(Y001)")],
        _teed_cells_write_c5,
        "with branch(C5, X002):\n            out(Y001)",
    ),
    "teed_cells_no_write": (
        [_row("R", ["X001", "T:C5"], "reset(C6)"), _row("", ["", "C5", "X002"], "out(Y001)")],
        _teed_cells_no_write,
        "with rung(X001, C5):\n        reset(C6)",
    ),
    "rail_rows_no_write": (
        [_row("R", ["C5"], "reset(C6)"), _row("", ["C5"], "out(Y001)")],
        _rail_rows_no_write,
        "with rung(C5):\n        reset(C6)\n        out(Y001)",
    ),
    "coil_writes_trunk_contact": (
        [_row("R", ["X001", "C5"], "out(C1)"), _row("", ["C1", "X002"], "out(Y001)")],
        _coil_writes_trunk_contact,
        "with rung(C1, X002).continued():",
    ),
}


def _readings(program: Program, names: dict[str, str], seed: int) -> list[dict[str, object]]:
    rng = random.Random(seed)
    plc = PLC(program, dt=0.01)
    seen: list[dict[str, object]] = []
    for _ in range(40):
        plc.patch(
            {
                names.get(address, address): rng.random() < 0.5
                for address in ("X001", "X002", "C5", "C6")
            }
        )
        plc.step()
        by_address = {address: name for name, address in names.items()}
        seen.append(
            {
                by_address.get(name, name): value
                for name, value in plc.current_state.tags.items()
                if by_address.get(name, name) in {"C1", "C5", "C6", "Y001"}
            }
        )
    return seen


def _write_csv(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows([_HEADER, *rows, _row("R", [], "end()")])
    return path


@pytest.mark.parametrize("name", list(CASES))
def test_import_reads_each_contact_where_click_does(name: str, tmp_path: Path):
    rows, meaning, shape = CASES[name]
    code = ladder_to_pyrung(_write_csv(tmp_path / "main.csv", rows))
    assert shape in code, code
    ns = exec_with_source(code, {})
    imported, mapping = ns["logic"], ns["mapping"]
    assert isinstance(mapping, TagMap)
    names = {slot.hardware_address: slot.logical_name for slot in mapping.mapped_slots()}
    with Program() as reference:
        meaning()
    for seed in range(3):
        expected = _readings(reference, {}, seed)
        assert _readings(imported, names, seed) == expected, seed


def test_export_keeps_a_written_contact_out_of_the_shared_prefix():
    with Program() as logic:
        with Rung(x[1]):
            with branch(c[5]):
                reset(c[5])
            with branch(c[5], x[2]):
                out(y[1])
    bundle = pyrung_to_ladder(logic, TagMap(include_system=False))
    cells = [(row[0], row[1:4], row[-1]) for row in bundle.main_rows[1:-1]]
    # C5 is drawn twice, so the second row reads it after the reset, as the source does.
    assert cells == [
        ("R", ("X001", "T:C5", "-"), "reset(C5)"),
        ("", ("", "C5", "X002"), "out(Y001)"),
    ]
    code = ladder_to_pyrung(bundle)
    assert "with branch(C5, X002):" in code
