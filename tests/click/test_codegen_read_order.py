"""The importer reads each contact where the CLICK rung does, row for row.

CLICK solves a rung row by row: a contact cell is read on the row of the first
output its wire runs through, and two cells with the same label are two reads.
The importer groups outputs under one ``with rung(...)`` or ``with branch(...)``
only where the drawing has one cell teed off to them, and gives each further
wire of a CLICK rung back as a ``.continued()`` rung, so the generated code
draws as the rows do and reads each contact where they do.  The exporter's
label factoring keeps a written contact out of a shared node.  Each case pins
the generated shape and runs it against a hand-written program with the CLICK
rung's meaning.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click._topology import Series, make_compound
from pyrung.click.codegen.analyzer import _analyze_rungs
from pyrung.click.codegen.parser import _parse_rows
from pyrung.click.codegen.shaping import emitted_logic_mismatch
from pyrung.core import PLC, Or, Program, Rung
from pyrung.core.program import branch, out, reset
from tests.click.helpers import exec_with_source, join_continued, rung_drawing

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


def _teed_cell_then_rail_row() -> None:
    with Rung(c[5]):
        reset(c[5])
        out(y[1])
    with Rung(c[5]).continued():
        out(y[2])


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
        "with rung(X001, C5, X002).continued():\n        out(Y001)",
    ),
    "teed_cells_write_c5": (
        [_row("R", ["X001", "T:C5"], "reset(C5)"), _row("", ["", "C5", "X002"], "out(Y001)")],
        _teed_cells_write_c5,
        "with branch(C5, X002):\n            out(Y001)",
    ),
    "teed_cells_no_write": (
        [_row("R", ["X001", "T:C5"], "reset(C6)"), _row("", ["", "C5", "X002"], "out(Y001)")],
        _teed_cells_no_write,
        "with branch(C5, X002):\n            out(Y001)",
    ),
    "rail_rows_no_write": (
        [_row("R", ["C5"], "reset(C6)"), _row("", ["C5"], "out(Y001)")],
        _rail_rows_no_write,
        "with rung(C5).continued():\n        out(Y001)",
    ),
    "coil_writes_trunk_contact": (
        [_row("R", ["X001", "C5"], "out(C1)"), _row("", ["C1", "X002"], "out(Y001)")],
        _coil_writes_trunk_contact,
        "with rung(C1, X002).continued():",
    ),
    # Y001 hangs off the C5 cell the reset reads, so it reads C5 as the rung
    # started; only the rail row below reads C5 again, after the reset.
    "teed_cell_then_rail_row": (
        [
            _row("R", ["C5", "T"], "reset(C5)"),
            _row("", ["", "-"], "out(Y001)"),
            _row("", ["C5"], "out(Y002)"),
        ],
        _teed_cell_then_rail_row,
        "with rung(C5):\n        reset(C5)\n        out(Y001)\n"
        "    with rung(C5).continued():\n        out(Y002)",
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
                if by_address.get(name, name) in {"C1", "C5", "C6", "Y001", "Y002"}
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


def test_round_trip_keeps_outputs_teed_off_one_cell_together():
    with Program() as logic:
        _teed_cell_then_rail_row()
    bundle = pyrung_to_ladder(logic, TagMap(include_system=False))
    cells = [(row[0], row[1:3], row[-1]) for row in bundle.main_rows[1:-1]]
    assert cells == [
        ("R", ("C5", "T"), "reset(C5)"),
        ("", ("", "-"), "out(Y001)"),
        ("R", ("C5", "-"), "out(Y002)"),
    ]
    ns = exec_with_source(ladder_to_pyrung(bundle), {})
    names = {slot.hardware_address: slot.logical_name for slot in ns["mapping"].mapped_slots()}
    for seed in range(3):
        assert _readings(ns["logic"], names, seed) == _readings(logic, {}, seed), seed


def _coils_on_one_contact() -> None:
    with Rung(x[1]):
        out(y[1])
        out(y[2])


def _unconditional_trunk_branches() -> None:
    with Rung():
        with branch(x[1]):
            out(y[1])
        with branch(x[2]):
            out(y[2])


def _nested_branches_under_an_or() -> None:
    with Rung(Or(x[1], x[2]), x[3]):
        out(y[1])
        with branch(x[4]):
            out(y[2])
            with branch(x[5]):
                out(y[3])
        out(y[4])


def _three_wires() -> None:
    with Rung(x[1]):
        out(y[1])
    with Rung(x[1], x[2]).continued():
        out(y[2])
    with Rung().continued():
        out(y[3])


WRITTEN: dict[str, Callable[[], None]] = {
    "coils_on_one_contact": _coils_on_one_contact,
    "unconditional_trunk_branches": _unconditional_trunk_branches,
    "nested_branches_under_an_or": _nested_branches_under_an_or,
    "three_wires": _three_wires,
    "teed_cells_write_c5": _teed_cells_write_c5,
    "teed_cell_then_rail_row": _teed_cell_then_rail_row,
    "trunk_then_written_contact": _trunk_then_written_contact,
    "coil_writes_trunk_contact": _coil_writes_trunk_contact,
}


@pytest.mark.parametrize("name", list(WRITTEN))
def test_round_trip_gives_back_the_code_written(name: str):
    """pyrung -> CLICK -> pyrung -> CLICK: the rungs as the export draws them, and the same rows."""
    with Program() as logic:
        WRITTEN[name]()
    bundle = pyrung_to_ladder(logic, TagMap(include_system=False))
    ns = exec_with_source(ladder_to_pyrung(bundle), {})
    assert rung_drawing(ns["logic"].rungs) == rung_drawing(logic.rungs, as_exported=True)
    assert pyrung_to_ladder(ns["logic"], ns["mapping"]).main_rows == bundle.main_rows


@pytest.mark.parametrize(
    "name", [name for name in WRITTEN if name != "unconditional_trunk_branches"]
)
def test_one_click_rung_of_several_wires_imports_as_the_code_written(name: str):
    with Program() as logic:
        WRITTEN[name]()
    bundle = join_continued(pyrung_to_ladder(logic, TagMap(include_system=False)), logic)
    ns = exec_with_source(ladder_to_pyrung(bundle), {})
    assert rung_drawing(ns["logic"].rungs) == rung_drawing(logic.rungs)


def _analyzed(rows: list[list[str]]):
    return _analyze_rungs(_parse_rows([_HEADER, *rows, _row("R", [], "end()")]))


def test_logic_check_refuses_two_cells_folded_into_one():
    rungs = _analyzed(CASES["teed_cells_no_write"][0])
    assert emitted_logic_mismatch(rungs) is None
    # Read both outputs' C5 from the first cell, as grouping by label would.
    (rung,) = rungs
    first, second = rung.instructions
    rung.condition_tree = make_compound([rung.condition_tree, first.branch_tree], Series)
    first.branch_tree = None
    second.branch_tree = second.branch_tree.children[1]
    mismatch = emitted_logic_mismatch(rungs)
    assert mismatch is not None
    assert "another cell with the same label" in mismatch


def test_logic_check_refuses_an_output_moved_to_another_wire():
    rungs = _analyzed(CASES["rail_rows_no_write"][0])
    assert emitted_logic_mismatch(rungs) is None
    first, second = rungs
    assert second.is_continued
    second.is_continued = False
    mismatch = emitted_logic_mismatch(rungs)
    assert mismatch is not None and "different CLICK rung" in mismatch
    # Both outputs on the first wire, each with its own cell: another wire.
    second.is_continued = True
    (moved,) = second.instructions
    moved.branch_tree = second.condition_tree
    first.instructions[0].branch_tree = first.condition_tree
    first.condition_tree = None
    first.instructions.append(moved)
    second.instructions = []
    mismatch = emitted_logic_mismatch(rungs)
    assert mismatch is not None and "different wire" in mismatch


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
