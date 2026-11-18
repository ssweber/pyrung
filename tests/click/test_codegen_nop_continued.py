"""NOP shapes in the export and the import.

CLICK pastes and keeps a contact in front of a NOP, on a wire of its own or on a leg
beside a coil.  Its Syntax Check reports C0003, "There is no Coil instruction in the
rung", so the export writes the rung and ``TagMap.validate()`` reports ``CLK_NOP_NO_COIL``.
CLICK reports nothing for a NOP with no contact of its own, and drops its row.  The export
draws an empty rung as a CLICK rung of its own, next to a ``.continued()`` rung too.  The
import reads a NOP row beside another row.  A NOP behind a contact of its own comes back as
``nop()`` in a ``branch()``.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, nop, pyrung_to_ladder
from pyrung.core import Or, Program, Timer, rung
from pyrung.core.program import branch, call, copy, forloop, on_delay, out, subroutine
from tests.click.helpers import (
    bundle_logic,
    exec_with_source,
    join_continued,
    round_trip_logic,
    rows_logic,
    strip_pyrung_boilerplate,
)

pytestmark = pytest.mark.filterwarnings(r"ignore:\[CLK_NOP_NO_COIL\]")

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _contact_nop() -> None:
    with rung(c[1]):
        pass


def _contact_nop_then_continued() -> None:
    with rung(c[1]):
        pass
    with rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _or_contacts() -> None:
    with rung(Or(c[1], c[6])):
        pass
    with rung(Or(c[1], c[6]), c[2]).continued():
        copy(1, ds[1])


def _continued_contact_nop_after_output() -> None:
    with rung(c[1]):
        copy(3, ds[3])
    with rung(c[1], c[2]).continued():
        pass


def _branch_then_continued_contact_nop() -> None:
    with rung(c[1]):
        copy(1, ds[1])
        with branch(c[2]):
            copy(2, ds[2])
    with rung(c[1], c[3]).continued():
        pass


def _contact_nop_in_subroutine() -> None:
    with rung(c[5]):
        call("Sub")
    with subroutine("Sub"):
        with rung(c[1]):
            pass
        with rung(c[1], c[2]).continued():
            copy(1, ds[1])


def _nop_then_continued() -> None:
    with rung():
        pass
    with rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _nop_then_continued_timer() -> None:
    with rung():
        pass
    with rung(c[1], c[2]).continued():
        on_delay(Timer[1], preset=100).reset(c[7])


def _continued_nop_after_output() -> None:
    with rung(c[1]):
        out(y[1])
    with rung().continued():
        pass


def _nop_then_continued_in_subroutine() -> None:
    with rung(c[5]):
        call("Sub")
    with subroutine("Sub"):
        with rung():
            pass
        with rung(c[1]).continued():
            copy(1, ds[1])


CONTACT_NOPS: dict[str, Callable[[], None]] = {
    "contact_nop": _contact_nop,
    "contact_nop_then_continued": _contact_nop_then_continued,
    "or_contacts": _or_contacts,
    "continued_contact_nop_after_output": _continued_contact_nop_after_output,
    "branch_then_continued_contact_nop": _branch_then_continued_contact_nop,
    "contact_nop_in_subroutine": _contact_nop_in_subroutine,
}

# An empty rung beside a continued rung exports as a CLICK rung of its own.
NOP_ALONE: dict[str, Callable[[], None]] = {
    "nop_then_continued": _nop_then_continued,
    "nop_then_continued_timer": _nop_then_continued_timer,
    "continued_nop_after_output": _continued_nop_after_output,
    "nop_then_continued_in_subroutine": _nop_then_continued_in_subroutine,
}


def _tag_map() -> TagMap:
    return TagMap({Timer[1].Done: t[1], Timer[1].Acc: td[1]}, include_system=False)


def _build(shape: Callable[[], None]) -> Program:
    with Program() as logic:
        shape()
    return logic


def _nop_codes(logic: Program) -> list[str]:
    report = TagMap({}, include_system=False).validate(logic, mode="warn")
    return [f.code for f in (*report.errors, *report.warnings, *report.hints)]


@pytest.mark.parametrize("name", list(CONTACT_NOPS))
def test_export_writes_a_contact_in_front_of_a_nop(name: str):
    _code, before, after = round_trip_logic(_build(CONTACT_NOPS[name]), _tag_map())
    assert any(af == "NOP" for scope in before.values() for af, _cond, _pins in scope)
    assert after == before


def test_export_writes_the_nop_row_with_its_contact():
    with Program() as logic:
        with rung(c[1]):
            nop()
    rows = pyrung_to_ladder(logic, TagMap()).main_rows
    assert rows[1][1] == "C1"
    assert rows[1][-1] == "NOP"


def test_contact_nop_round_trips_to_the_same_program():
    with Program() as logic:
        with rung(c[1]):
            nop()
    bundle = pyrung_to_ladder(logic, TagMap())
    code = strip_pyrung_boilerplate(ladder_to_pyrung(bundle))
    assert "with rung(C1):\n    pass" in code


def test_check_reports_a_nop_behind_a_condition():
    with Program() as logic:
        with rung(c[1]):
            nop()
    assert _nop_codes(logic) == ["CLK_NOP_NO_COIL"]


def test_check_reports_contacts_in_parallel_in_front_of_a_nop():
    assert _nop_codes(_build(_or_contacts)) == ["CLK_NOP_NO_COIL"]


def test_check_reports_a_contact_nop_in_a_subroutine():
    assert _nop_codes(_build(_contact_nop_in_subroutine)) == ["CLK_NOP_NO_COIL"]


def test_check_reports_a_contact_nop_wire_below_a_coil():
    assert _nop_codes(_build(_continued_contact_nop_after_output)) == ["CLK_NOP_NO_COIL"]


def test_check_reports_a_contact_nop_wire_above_a_coil():
    assert _nop_codes(_build(_contact_nop_then_continued)) == ["CLK_NOP_NO_COIL"]


def test_check_reports_nothing_for_a_bare_nop_wire_beside_a_coil():
    """CLICK drops a NOP fed by a bare wire without an error."""
    assert _nop_codes(_build(_continued_nop_after_output)) == []
    assert _nop_codes(_build(_nop_then_continued)) == []


def test_check_reports_a_nop_in_a_branch_with_its_own_contact_beside_a_coil():
    # CLICK capture clicknick-wt/buildbot/jobs/nop-leg: C0003 with the NOP leg on top or bottom.
    with Program() as logic:
        with rung(c[1]):
            out(y[1])
            with branch(c[2]):
                nop()
    assert _nop_codes(logic) == ["CLK_NOP_NO_COIL"]


def test_check_reports_nothing_for_a_nop_branch_with_no_contact_beside_a_coil():
    # On the coil's net with no contact of its own, so CLICK reports nothing.
    with Program() as logic:
        with rung(c[1]):
            out(y[1])
            with branch():
                nop()
    assert _nop_codes(logic) == []


def test_check_reports_nothing_for_a_bare_nop():
    with Program() as logic:
        with rung():
            nop()
    assert _nop_codes(logic) == []


def test_export_warns_instead_of_refusing_a_contact_nop():
    with Program() as logic:
        with rung(c[1]):
            nop()
    with pytest.warns(UserWarning, match="CLK_NOP_NO_COIL"):
        pyrung_to_ladder(logic, TagMap(include_system=False))


@pytest.mark.parametrize("name", list(NOP_ALONE))
def test_a_nop_beside_a_continued_rung_is_a_click_rung_of_its_own(name: str):
    bundle = pyrung_to_ladder(_build(NOP_ALONE[name]), _tag_map())
    scopes = [bundle.main_rows, *(rows for _name, rows in bundle.subroutine_rows)]
    nops = [(scope, i) for scope in scopes for i, row in enumerate(scope) if row[-1] == "NOP"]
    assert nops
    for scope, index in nops:
        assert scope[index][0] == "R"
        assert index + 1 == len(scope) or scope[index + 1][0] == "R"
    ladder_to_pyrung(bundle)


def test_bare_nop_between_rungs_exports():
    """A bare NOP with normal rungs on either side is its own CLICK rung."""
    with Program() as logic:
        with rung(c[1]):
            copy(1, ds[1])
        with rung():
            pass
        with rung(c[2]):
            copy(2, ds[2])
    code = strip_pyrung_boilerplate(ladder_to_pyrung(pyrung_to_ladder(logic, TagMap())))
    assert "with rung():\n    pass" in code


def test_continued_rows_come_back_as_continued_rungs():
    """Each wire of a CLICK rung is a rung of its own, row for row."""
    with Program() as logic:
        with rung(c[1]):
            copy(1, ds[1])
        with rung(c[1], c[2]).continued():
            copy(2, ds[2])
    bundle = join_continued(pyrung_to_ladder(logic, TagMap()), logic)
    code = strip_pyrung_boilerplate(ladder_to_pyrung(bundle))
    assert "with rung(C1, C2).continued():\n    copy(2, DS2)" in code
    assert "branch(" not in code


FIXTURES = Path(__file__).parent / "fixtures_nop"


def _csv_rows(path: Path) -> tuple[tuple[str, ...], ...]:
    with path.open(newline="", encoding="utf-8") as f:
        return tuple(tuple(row) for row in csv.reader(f) if row and row[0] != "#")


@pytest.mark.parametrize("name", ["two_wires", "nop_leg"])
def test_import_reads_a_nop_beside_a_coil_and_exports_it_again(name: str):
    """CLICK captures (clicknick-wt buildbot jobs two-wires and nop-leg), kept by CLICK."""
    path = FIXTURES / f"{name}.csv"
    ns: dict[str, object] = {}
    exec_with_source(ladder_to_pyrung(path), ns)
    again = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert bundle_logic(again)["main"] == rows_logic(_csv_rows(path))
    assert [r[-1] for r in again.main_rows if r[-1] == "NOP"] == ["NOP", "NOP"]


def test_nop_leg_imports_as_nop_in_a_branch():
    code = strip_pyrung_boilerplate(ladder_to_pyrung(FIXTURES / "nop_leg.csv"))
    assert "with branch(C2):" + chr(10) + "        nop()" in code


def test_branch_nop_beside_a_coil_exports_and_round_trips():
    with Program() as logic:
        with rung(c[1]):
            out(y[1])
            with branch(c[2]):
                nop()
    _code, before, after = round_trip_logic(logic, TagMap(include_system=False))
    assert any(af == "NOP" for af, _cond, _pins in before["main"])
    assert after == before


def test_check_reports_a_nop_branch_under_a_contact_with_no_coil():
    with Program() as logic:
        with rung(c[1]):
            with branch(c[2]):
                nop()
    assert _nop_codes(logic) == ["CLK_NOP_NO_COIL"]


def test_check_reports_a_nop_branch_under_a_bare_rung():
    with Program() as logic:
        with rung():
            with branch(c[2]):
                nop()
    assert _nop_codes(logic) == ["CLK_NOP_NO_COIL"]


def test_check_reports_a_forloop_body_rung_with_a_contact_and_no_output():
    with Program() as logic:
        with rung(c[1]):
            with forloop(3):
                with rung(c[2]):
                    pass
    assert _nop_codes(logic) == ["CLK_NOP_NO_COIL"]


def test_check_reports_nothing_for_a_forloop_body_rung_with_no_contact():
    with Program() as logic:
        with rung(c[1]):
            with forloop(3):
                with rung():
                    pass
    assert _nop_codes(logic) == []


def _two_nop_legs() -> None:
    with rung(c[1]):
        with branch(c[2]):
            nop()
        with branch(c[3]):
            nop()


def test_two_nop_legs_with_no_coil_come_back_as_branches():
    code = strip_pyrung_boilerplate(
        ladder_to_pyrung(pyrung_to_ladder(_build(_two_nop_legs), TagMap()))
    )
    assert code.count("nop()") == 2
    assert "with branch(C2):" in code
    assert "with branch(C3):" in code
    _code, before, after = round_trip_logic(_build(_two_nop_legs), TagMap(include_system=False))
    assert after == before


def test_nop_wires_from_the_rail_come_back_as_branches(tmp_path: Path):
    """R,X001,NOP over ,X002,NOP comes back as a NOP in a branch for each contact."""
    header = ["marker", *(chr(65 + i) for i in range(26)), "AA", "AB", "AC", "AD", "AE", "AF"]
    rows = [
        header,
        ["R", "X001", *(["-"] * 30), "NOP"],
        ["", "X002", *(["-"] * 30), "NOP"],
    ]
    path = tmp_path / "main.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    code = ladder_to_pyrung(path)
    ns: dict[str, object] = {}
    exec_with_source(code, ns)
    again = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert bundle_logic(again)["main"] == rows_logic(tuple(tuple(r) for r in rows))
    assert code.count("nop()") == 2
    assert code.count("with branch(") == 2


def test_check_reports_nothing_for_a_nop_leg_the_export_factors_onto_the_coil_net():
    with Program() as logic:
        with rung():
            with branch(c[1]):
                out(y[1])
            with branch(c[1]):
                nop()
    assert _nop_codes(logic) == []


def test_check_reports_one_finding_for_two_nop_legs_in_a_rung():
    assert _nop_codes(_build(_two_nop_legs)) == ["CLK_NOP_NO_COIL"]
