"""Pin rows with OR branches and padding rows between them (A5).

CLICK allows OR branches and padding rows between the pin rows of any
multi-pin instruction.  What a pin reads comes from the wiring into its row;
the label belongs to the nearest instruction row above it, and labels must
follow the instruction's fixed pin order.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.codegen.analyzer import _analyze_single_rung
from pyrung.click.codegen.models import _RawRung
from pyrung.click.codegen.shaping import emitted_logic_mismatch
from pyrung.click.ladder.types import LadderBundle
from pyrung.core import Counter, Or, Program, Rung, Timer
from pyrung.core.program import (
    count_down,
    count_up,
    event_drum,
    on_delay,
    shift,
    time_drum,
    time_event_drum,
)
from tests.click.helpers import round_trip_logic

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_WIDTH = 33  # marker + 31 condition columns + AF


def _tag_map() -> TagMap:
    return TagMap(
        {
            Timer[1].Done: t[1],
            Timer[1].Acc: td[1],
            Counter[1].Done: ct[1],
            Counter[1].Acc: ctd[1],
        },
        include_system=False,
    )


def _event_drum():
    return event_drum(
        outputs=[c[11], c[12]],
        events=[c[13], c[14]],
        pattern=[[1, 0], [0, 1]],
        current_step=ds[1],
        completion_flag=c[15],
    )


def _time_drum():
    return time_drum(
        outputs=[c[11], c[12]],
        presets=[100, 200],
        pattern=[[1, 0], [0, 1]],
        current_step=ds[1],
        accumulator=td[2],
        completion_flag=c[15],
    )


def _time_event_drum():
    return time_event_drum(
        outputs=[c[11], c[12]],
        presets=[100, None],
        events=[c[13], c[14]],
        pattern=[[1, 0], [0, 1]],
        current_step=ds[1],
        accumulator=td[2],
        completion_flag=c[15],
    )


def _on_delay_reset_or() -> None:
    with Rung(c[1]):
        on_delay(Timer[1], preset=100).reset(Or(c[6], c[7]))


def _count_up_down_or_then_reset() -> None:
    with Rung(c[1]):
        count_up(Counter[1], 5).down(Or(c[4], c[5])).reset(c[6])


def _count_up_both_or() -> None:
    with Rung(c[1]):
        count_up(Counter[1], 5).down(Or(c[4], c[5])).reset(Or(c[6], c[7]))


def _count_down_reset_or() -> None:
    with Rung(c[1]):
        count_down(Counter[1], 5).reset(Or(c[6], c[7]))


def _shift_clock_or() -> None:
    with Rung(c[1]):
        shift(c.select(21, 28)).clock(Or(c[4], c[5])).reset(c[6])


def _shift_both_or() -> None:
    with Rung(c[1]):
        shift(c.select(21, 28)).clock(Or(c[4], c[5])).reset(Or(c[6], c[7]))


def _event_drum_first_pin_or() -> None:
    with Rung(c[1]):
        _event_drum().reset(Or(c[6], c[7])).jog(c[8])


def _event_drum_middle_pin_or() -> None:
    with Rung(c[1]):
        _event_drum().reset(c[6]).jump(Or(c[7], c[9]), step=ds[2]).jog(c[8])


def _event_drum_all_pins_or() -> None:
    with Rung(c[1]):
        _event_drum().reset(Or(c[6], c[7])).jump(Or(c[2], c[3]), step=ds[2]).jog(Or(c[8], c[9]))


def _time_drum_middle_pin_or() -> None:
    with Rung(c[1]):
        _time_drum().reset(c[6]).jump(Or(c[7], c[9]), step=ds[2]).jog(c[8])


def _time_event_drum_first_pin_or() -> None:
    with Rung(c[1]):
        _time_event_drum().reset(Or(c[6], c[7])).jog(c[8])


SHAPES: dict[str, Callable[[], None]] = {
    "on_delay_reset_or": _on_delay_reset_or,
    "count_up_down_or_then_reset": _count_up_down_or_then_reset,
    "count_up_both_or": _count_up_both_or,
    "count_down_reset_or": _count_down_reset_or,
    "shift_clock_or": _shift_clock_or,
    "shift_both_or": _shift_both_or,
    "event_drum_first_pin_or": _event_drum_first_pin_or,
    "event_drum_middle_pin_or": _event_drum_middle_pin_or,
    "event_drum_all_pins_or": _event_drum_all_pins_or,
    "time_drum_middle_pin_or": _time_drum_middle_pin_or,
    "time_event_drum_first_pin_or": _time_event_drum_first_pin_or,
}


@pytest.mark.parametrize("name", list(SHAPES))
def test_or_pin_rows_round_trip(name: str):
    with Program() as logic:
        SHAPES[name]()
    code, before, after = round_trip_logic(logic, _tag_map())
    assert after == before, code
    # Every pin keeps its OR (two-branch condition) through the round trip.
    pins = [pin for _af, _tree, instr_pins in before["main"] for pin in instr_pins]
    assert pins and any(isinstance(tree, tuple) and tree[0] == "OR" for _n, _a, tree in pins)


def test_middle_pin_or_reads_back_as_written():
    with Program() as logic:
        _event_drum_middle_pin_or()
    code = ladder_to_pyrung(pyrung_to_ladder(logic, _tag_map()))
    assert ".reset(C6).jump(Or(C7, C9), step=DS2).jog(C8)" in code


def test_logic_check_covers_pins():
    with Program() as logic:
        _event_drum_all_pins_or()
    rows = pyrung_to_ladder(logic, _tag_map()).main_rows
    from pyrung.click.codegen.analyzer import _analyze_rungs
    from pyrung.click.codegen.parser import _parse_rows

    rungs = _analyze_rungs(_parse_rows(rows))
    assert emitted_logic_mismatch(rungs) is None
    # Drop the jog pin's OR branch from what would be emitted: caught.
    drum = next(i for r in rungs for i in r.instructions if i.pins)
    drum.pins[-1].condition_tree = drum.pins[-1].condition_tree.children[0]
    mismatch = emitted_logic_mismatch(rungs)
    assert mismatch is not None and ".jog() when OR(C8, C9)" in mismatch


# ---------------------------------------------------------------------------
# Hand-drawn layouts
# ---------------------------------------------------------------------------


def _row(marker: str, contact: str | None, af: str = "", *, tee: bool = False) -> list[str]:
    """A row with *contact* in column A, wired across to the AF column."""
    row = [""] * _WIDTH
    row[0] = marker
    if contact is not None:
        row[1] = contact
        row[2] = "T" if tee else "-"
        for col in range(3, _WIDTH - 1):
            row[col] = "-"
    row[-1] = af
    return row


def _or_branch(contact: str) -> list[str]:
    """The lower row of an OR: *contact* in column A, joining the tee above."""
    row = [""] * _WIDTH
    row[1] = contact
    return row


_DRUM = (
    "event_drum(outputs=[C11,C12],events=[C13,C14],pattern=[[1,0],[0,1]],"
    "current_step=DS1,completion_flag=C15)"
)


def _drum_rows(*middle: list[str]) -> list[list[str]]:
    return [_row("R", "C1", _DRUM), *middle]


def test_padding_rows_between_pins():
    rung = _RawRung(
        comment_lines=[],
        rows=_drum_rows(
            _row("", "C6", ".reset()"),
            [""] * _WIDTH,
            [""] * _WIDTH,
            _row("", "C7", ".jump(2)"),
            [""] * _WIDTH,
            _row("", "C8", ".jog()"),
        ),
    )
    result = _analyze_single_rung(rung, validate=True)
    pins = result.instructions[0].pins
    assert [(p.name, p.arg) for p in pins] == [("reset", ""), ("jump", "2"), ("jog", "")]
    assert [p.conditions for p in pins] == [["C6"], ["C7"], ["C8"]]


def test_or_on_middle_pin_between_pins():
    rung = _RawRung(
        comment_lines=[],
        rows=_drum_rows(
            _row("", "C6", ".reset()"),
            _row("", "C7", ".jump(2)", tee=True),
            _or_branch("C9"),
            _row("", "C8", ".jog()"),
        ),
    )
    result = _analyze_single_rung(rung, validate=True)
    pins = {p.name: p for p in result.instructions[0].pins}
    assert sorted(leaf.label for leaf in pins["jump"].condition_tree.children) == ["C7", "C9"]
    assert pins["jog"].conditions == ["C8"]


def test_bundle_with_or_between_pins_imports():
    header = ("marker", *[f"c{i}" for i in range(_WIDTH - 2)], "AF")
    rows = _drum_rows(
        _row("", "C6", ".reset()", tee=True),
        _or_branch("C7"),
        [""] * _WIDTH,
        _row("", "C8", ".jog()"),
    )
    bundle = LadderBundle(main_rows=(header, *(tuple(r) for r in rows)), subroutine_rows=())
    code = ladder_to_pyrung(bundle)
    assert ".reset(Or(C6, C7)).jog(C8)" in code


def test_pins_out_of_order_rejected():
    rung = _RawRung(
        comment_lines=[],
        rows=_drum_rows(_row("", "C8", ".jog()"), _row("", "C6", ".reset()")),
    )
    with pytest.raises(ValueError, match=r"\.reset\(\) on row 2 is out of order"):
        _analyze_single_rung(rung)


def test_repeated_pin_rejected():
    rung = _RawRung(
        comment_lines=[],
        rows=_drum_rows(_row("", "C6", ".reset()"), _row("", "C7", ".reset()")),
    )
    with pytest.raises(ValueError, match="out of order"):
        _analyze_single_rung(rung)


def test_foreign_pin_rejected():
    rung = _RawRung(
        comment_lines=[],
        rows=[_row("R", "C1", "on_delay(T1,TD1,preset=100,unit=Tms)"), _row("", "C8", ".jog()")],
    )
    with pytest.raises(ValueError, match=r"\.jog\(\) on row 1 is not a pin of on_delay"):
        _analyze_single_rung(rung)


def test_unclaimed_row_after_last_pin_rejected():
    """A row after the last pin that wires into nothing is a dropped contact."""
    stray = [""] * _WIDTH
    stray[1] = "C9"
    rung = _RawRung(
        comment_lines=[],
        rows=_drum_rows(_row("", "C6", ".reset()"), _row("", "C8", ".jog()"), stray),
    )
    with pytest.raises(ValueError, match=r"drops condition\(s\).*C9"):
        _analyze_single_rung(rung, validate=True)
