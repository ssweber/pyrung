"""NOP rungs sharing a CLICK rung with ``.continued()`` rows (A1).

A NOP rung followed by a continued wire exports as one CLICK rung whose rows
end in ``NOP`` and in the continued instruction.  The importer used to group
those rows under the NOP's contacts, drop the NOP, and render the one
remaining instruction without its own contacts.  Neither round-trip validator
noticed, because both stopped checking at the analyzer.

The rule: a shape change on round trip is fine, a lost contact never is.  So
these tests compare logic (per-output condition, pins, snapshot grouping), not
CSV rows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click._topology import Series, make_compound
from pyrung.click.codegen import analyzer, shaping
from pyrung.click.codegen.analyzer import _analyze_rungs
from pyrung.click.codegen.parser import _parse_rows
from pyrung.click.ladder.types import LadderExportError
from pyrung.core import Or, Program, Rung, Timer
from pyrung.core.program import branch, call, copy, on_delay, out, subroutine
from tests.click.helpers import exec_with_source, strip_pyrung_boilerplate

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()


def _repro() -> None:
    with Rung(c[1]):
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _different_first_contact() -> None:
    with Rung(c[3]):
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _unconditional_nop() -> None:
    with Rung():
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _several_continued() -> None:
    with Rung(c[1]):
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])
    with Rung(c[1], c[3]).continued():
        copy(2, ds[2])


def _several_continued_same_contacts() -> None:
    with Rung(c[1]):
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])
    with Rung(c[1], c[2]).continued():
        copy(2, ds[2])


def _nop_between_outputs() -> None:
    with Rung(c[1]):
        out(y[1])
    with Rung(c[1]).continued():
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _continued_nop_after_output() -> None:
    with Rung(c[1]):
        copy(3, ds[3])
    with Rung(c[1], c[2]).continued():
        pass


def _continued_after_non_nop() -> None:
    with Rung(c[1]):
        copy(3, ds[3])
    with Rung(c[4], c[2]).continued():
        copy(1, ds[1])
    with Rung(c[4], c[3]).continued():
        pass


def _or_contacts() -> None:
    with Rung(Or(c[1], c[6])):
        pass
    with Rung(Or(c[1], c[6]), c[2]).continued():
        copy(1, ds[1])


def _stacked_nops() -> None:
    with Rung(c[1]):
        pass
    with Rung(c[1], c[2]).continued():
        pass
    with Rung(c[1], c[2], c[3]).continued():
        copy(1, ds[1])


def _branch_then_continued_nop() -> None:
    with Rung(c[1]):
        copy(1, ds[1])
        with branch(c[2]):
            copy(2, ds[2])
    with Rung(c[1], c[3]).continued():
        pass


def _timer_with_pin() -> None:
    with Rung(c[1]):
        pass
    with Rung(c[1], c[2]).continued():
        on_delay(Timer[1], preset=100).reset(c[7])


def _in_subroutine() -> None:
    with Rung(c[5]):
        call("Sub")
    with subroutine("Sub"):
        with Rung(c[1]):
            pass
        with Rung(c[1], c[2]).continued():
            copy(1, ds[1])


SHAPES: dict[str, Callable[[], None]] = {
    "repro": _repro,
    "different_first_contact": _different_first_contact,
    "unconditional_nop": _unconditional_nop,
    "several_continued": _several_continued,
    "several_continued_same_contacts": _several_continued_same_contacts,
    "nop_between_outputs": _nop_between_outputs,
    "continued_nop_after_output": _continued_nop_after_output,
    "continued_after_non_nop": _continued_after_non_nop,
    "or_contacts": _or_contacts,
    "stacked_nops": _stacked_nops,
    "branch_then_continued_nop": _branch_then_continued_nop,
    "timer_with_pin": _timer_with_pin,
    "in_subroutine": _in_subroutine,
}


def _tag_map() -> TagMap:
    return TagMap({Timer[1].Done: t[1], Timer[1].Acc: td[1]}, include_system=False)


def _build(shape: Callable[[], None]) -> Program:
    with Program() as logic:
        shape()
    return logic


def _logic(rows) -> list[tuple]:
    """Per-output (AF, full condition, pins) of exported rows, NOPs included."""
    outputs = []
    for rung in _analyze_rungs(_parse_rows(rows)):
        for instr in rung.instructions:
            parts = [p for p in (rung.condition_tree, instr.branch_tree) if p is not None]
            tree = make_compound(parts, Series) if parts else None
            outputs.append(
                (
                    instr.af_token,
                    shaping._tree_key(tree),
                    tuple((p.name, p.arg, shaping._tree_key(p.condition_tree)) for p in instr.pins),
                )
            )
    return outputs


def _bundle_logic(bundle) -> dict[str, list[tuple]]:
    scopes = {"main": _logic(bundle.main_rows)}
    for name, rows in bundle.subroutine_rows:
        scopes[name] = _logic(rows)
    return scopes


@pytest.mark.parametrize("name", list(SHAPES))
def test_round_trip_keeps_logic(name: str):
    original = pyrung_to_ladder(_build(SHAPES[name]), _tag_map())
    code = ladder_to_pyrung(original)
    ns: dict = {}
    exec_with_source(code, ns)
    reproduced = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert _bundle_logic(reproduced) == _bundle_logic(original), code


def test_repro_imports_as_source_program():
    code = strip_pyrung_boilerplate(ladder_to_pyrung(pyrung_to_ladder(_build(_repro), TagMap())))
    assert "with rung(C1):\n    pass" in code
    assert "with rung(C1, C2).continued():\n    copy(1, DS1)" in code


def _old_rung_blocks(rung):
    """The pre-fix emitter: drop NOPs, then render a lone output bare."""
    real = [i for i in rung.instructions if i.af_token != "NOP"]
    if len(real) == 1:
        return rung.condition_tree, [replace(real[0], branch_tree=None)]
    return _real_rung_blocks(rung)


_real_rung_blocks = shaping.rung_blocks


@pytest.fixture
def pre_fix_importer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analyzer, "_split_nop_outputs", lambda rung: [rung])
    monkeypatch.setattr(shaping, "rung_blocks", _old_rung_blocks)


@pytest.mark.parametrize("name", ["repro", "unconditional_nop", "stacked_nops", "in_subroutine"])
def test_export_validator_catches_lost_contact(name: str, pre_fix_importer: None):
    with pytest.raises(LadderExportError, match="would change the rung logic"):
        pyrung_to_ladder(_build(SHAPES[name]), _tag_map())


@pytest.mark.parametrize("name", ["repro", "unconditional_nop", "stacked_nops", "in_subroutine"])
def test_import_validator_catches_lost_contact(name: str, pre_fix_importer: None):
    bundle = pyrung_to_ladder(_build(SHAPES[name]), _tag_map(), validate=False)
    with pytest.raises(ValueError, match="should be copy\\(1,DS1\\) when AND\\(C1, C2"):
        ladder_to_pyrung(bundle)


def test_import_without_validate_skips_logic_check(pre_fix_importer: None):
    bundle = pyrung_to_ladder(_build(_repro), TagMap(), validate=False)
    ladder_to_pyrung(bundle, validate=False)


def test_validator_catches_snapshot_change(monkeypatch: pytest.MonkeyPatch):
    """Re-snapshotting a split piece reads different contacts: logic change."""
    real_split = analyzer._split_nop_outputs

    def fresh_snapshots(rung):
        pieces = real_split(rung)
        for piece in pieces:
            piece.is_continued = False
        return pieces

    monkeypatch.setattr(analyzer, "_split_nop_outputs", fresh_snapshots)
    with pytest.raises(LadderExportError, match="different condition snapshot"):
        pyrung_to_ladder(_build(_nop_between_outputs), TagMap())


def test_shape_change_is_not_a_logic_change():
    """Continued rows that come back as branches pass the check."""
    with Program() as logic:
        with Rung(c[1]):
            copy(1, ds[1])
        with Rung(c[1], c[2]).continued():
            copy(2, ds[2])
    code = strip_pyrung_boilerplate(ladder_to_pyrung(pyrung_to_ladder(logic, TagMap())))
    assert "with branch(C2):" in code
