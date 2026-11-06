"""A box with pins ends its rung, in pyrung as on a CLICK (C000A).

CLICK takes all inputs (pins) of a box instruction from the same branch, the
rail: "All inputs of the box instruction must originate from the same branch."
A counter, retentive timer, shift register or drum whose input splits off a
node another output also hangs from can be drawn but does not compile.  So the
program build refuses a box with pins beside any other output or branch of its
rung; what comes before it goes in a rung above, what follows it in a
``.continued()`` rung (a further wire of the CLICK rung) or a rung of its own.

A pyrung rung and its CLICK rung are then the same drawing, row for row, both
ways: export writes each rung as it stands, and import gives each wire of a
CLICK box rung back as a rung of its own.  A box with no pins (a TON) may share
the wire.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.core import PLC, Counter, Or, Program, Rung, Timer
from pyrung.core.program import (
    branch,
    call,
    count_up,
    event_drum,
    forloop,
    latch,
    on_delay,
    out,
    reset,
    shift,
    subroutine,
)
from tests.click.helpers import exec_with_source

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

ENDS_ITS_RUNG = "has pins, so it ends its rung"


def _drum() -> None:
    event_drum(
        outputs=[c[3]], events=[c[2]], pattern=[[1]], current_step=ds[1], completion_flag=c[1]
    ).reset(c[4])


# --- Shapes the build refuses: a box with pins shares its rung --------------


def _coil_before_drum() -> None:
    with Rung(x[1]):
        latch(c[3])
        _drum()


def _coil_before_rton() -> None:
    with Rung(x[1]):
        out(y[1])
        on_delay(Timer[1], preset=100, unit="Tms").reset(x[2])


def _counter_in_branch_beside_coil() -> None:
    with Rung(x[1]):
        with branch(x[2]):
            latch(c[1])
        with branch(x[3]):
            count_up(Counter[1], preset=10).reset(x[4])


def _shift_in_nested_branch() -> None:
    with Rung(x[1]):
        out(y[1])
        with branch(x[2]):
            with branch(x[3]):
                shift(c.select(11, 18)).clock(x[4]).reset(x[5])


def _outputs_below_box() -> None:
    with Rung(x[1]):
        with branch(x[6]):
            count_up(Counter[1], preset=3).reset(x[4])
        out(y[2])


def _box_in_continued_rung_beside_coil() -> None:
    with Rung(x[1]):
        out(y[1])
    with Rung(x[2]).continued():
        latch(c[9])
        with branch(x[7]):
            count_up(Counter[1], preset=3).reset(x[4])


def _trunk_written_before_box() -> None:
    with Rung(c[5]):
        reset(c[5])
        with branch(x[2]):
            count_up(Counter[1], preset=10).reset(x[4])


def _box_in_forloop_body_rung_beside_coil() -> None:
    with Rung(x[3]):
        with forloop(3):
            with Rung(x[1]):
                out(y[1])
                shift(c.select(11, 18)).clock(x[4]).reset(x[5])


REFUSED: dict[str, Callable[[], None]] = {
    "coil_before_drum": _coil_before_drum,
    "coil_before_rton": _coil_before_rton,
    "counter_in_branch_beside_coil": _counter_in_branch_beside_coil,
    "shift_in_nested_branch": _shift_in_nested_branch,
    "outputs_below_box": _outputs_below_box,
    "box_in_continued_rung_beside_coil": _box_in_continued_rung_beside_coil,
    "trunk_written_before_box": _trunk_written_before_box,
    "box_in_forloop_body_rung_beside_coil": _box_in_forloop_body_rung_beside_coil,
}


# --- The same programs written as CLICK draws them ---------------------------


def _drum_below_coil() -> None:
    with Rung(x[1]):
        latch(c[3])
    with Rung(x[1]).continued():
        _drum()


def _rton_below_coil() -> None:
    with Rung(x[1]):
        out(y[1])
    with Rung(x[1]).continued():
        on_delay(Timer[1], preset=100, unit="Tms").reset(x[2])


def _counter_below_coil() -> None:
    with Rung(x[1], x[2]):
        latch(c[1])
    with Rung(x[1], x[3]).continued():
        count_up(Counter[1], preset=10).reset(x[4])


def _shift_below_coil() -> None:
    with Rung(x[1]):
        out(y[1])
    with Rung(x[1], x[2], x[3]).continued():
        shift(c.select(11, 18)).clock(x[4]).reset(x[5])


def _wires_below_box() -> None:
    with Rung(x[1], x[6]):
        count_up(Counter[1], preset=3).reset(x[4])
    with Rung(x[1]).continued():
        out(y[2])
        with branch(Counter[1].Done):
            out(y[3])


def _box_on_a_continued_wire() -> None:
    with Rung(x[1]):
        out(y[1])
    with Rung(x[2]).continued():
        latch(c[9])
    with Rung(x[2], x[7]).continued():
        count_up(Counter[1], preset=3).reset(x[4])
    with Rung(x[2]).continued():
        out(y[2])


def _box_reads_the_wire_above() -> None:
    # The box's wire reads C5 where it stands, after the reset above.
    with Rung(c[5]):
        reset(c[5])
    with Rung(c[5], x[2]).continued():
        count_up(Counter[1], preset=10).reset(x[4])


def _two_boxes() -> None:
    with Rung(x[1], x[2]):
        count_up(Counter[1], preset=3).reset(x[4])
    with Rung(x[1]).continued():
        out(y[1])
    with Rung(x[1], x[3]).continued():
        shift(c.select(11, 18)).clock(x[4]).reset(x[5])
    with Rung(x[1]).continued():
        out(y[2])


def _no_trunk_contact() -> None:
    with Rung():
        out(y[1])
    with Rung(x[5]).continued():
        count_up(Counter[1], preset=3).reset(x[4])
    with Rung().continued():
        out(y[2])


def _box_in_a_subroutine() -> None:
    with Rung():
        call("Sub")
    with subroutine("Sub"):
        with Rung(c[5]):
            reset(c[5])
        with Rung(c[5], x[2]).continued():
            count_up(Counter[1], preset=10).reset(x[4])


def _box_alone_then_continued() -> None:
    with Rung(Or(x[1], x[2])):
        count_up(Counter[1], preset=10).down(x[3]).reset(x[4])
    with Rung(x[5]).continued():
        latch(c[1])


def _ton_beside_coil() -> None:
    with Rung(x[1]):
        out(y[1])
        on_delay(Timer[1], preset=100, unit="Tms")


WRITTEN: dict[str, Callable[[], None]] = {
    "drum_below_coil": _drum_below_coil,
    "rton_below_coil": _rton_below_coil,
    "counter_below_coil": _counter_below_coil,
    "shift_below_coil": _shift_below_coil,
    "wires_below_box": _wires_below_box,
    "box_on_a_continued_wire": _box_on_a_continued_wire,
    "box_reads_the_wire_above": _box_reads_the_wire_above,
    "two_boxes": _two_boxes,
    "no_trunk_contact": _no_trunk_contact,
    "box_in_a_subroutine": _box_in_a_subroutine,
    "box_alone_then_continued": _box_alone_then_continued,
    "ton_beside_coil": _ton_beside_coil,
}


def _box_in_its_only_branch() -> None:
    with Rung(x[1]):
        with branch(x[2]):
            count_up(Counter[1], preset=10).reset(x[4])


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


def _build(shape: Callable[[], None]) -> Program:
    with Program() as logic:
        shape()
    return logic


def _rungs(rows: tuple[tuple[str, ...], ...]) -> list[list[tuple[str, ...]]]:
    """Rows grouped into CLICK rungs (a marker starts one), header and end() dropped."""
    rungs: list[list[tuple[str, ...]]] = []
    for row in rows[1:]:
        if row[0].startswith("R"):
            rungs.append([])
        rungs[-1].append(row)
    return rungs[:-1]


def _reimport(program: Program, tag_map: TagMap) -> tuple[Program, TagMap]:
    code = ladder_to_pyrung(pyrung_to_ladder(program, tag_map))
    ns = exec_with_source(code, {})
    reimported, mapping = ns["logic"], ns["mapping"]
    assert isinstance(reimported, Program) and isinstance(mapping, TagMap)
    return reimported, mapping


def _shape(rungs: list) -> list[tuple]:
    """Each rung's drawing: continued or not, its contact count, and its items."""

    def items(rung) -> tuple:
        return tuple(
            ("branch", len(item._conditions) - item._branch_condition_start, items(item))
            if isinstance(item, type(rung))
            else type(item).__name__
            for item in rung._execution_items
        )

    return [(rung._use_prior_snapshot, len(rung._conditions), items(rung)) for rung in rungs]


_BOXES = ("count_up(", "on_delay(", "shift(", "event_drum(")


def _readings(program: Program, tag_map: TagMap, seed: int) -> list[dict[str, object]]:
    """Each scan's tags, by CLICK address, under a random input schedule."""
    names = {slot.logical_name: slot.hardware_address for slot in tag_map.mapped_slots()}
    rng = random.Random(seed)
    plc = PLC(program, dt=0.01)
    seen: list[dict[str, object]] = []
    for _ in range(60):
        plc.patch({f"X00{i}": rng.random() < 0.5 for i in range(1, 8)})
        plc.step()
        tags = plc.current_state.tags
        seen.append({names.get(name, name): value for name, value in tags.items()})
    return seen


@pytest.mark.parametrize("name", list(REFUSED))
def test_build_refuses_a_box_that_shares_its_rung(name: str):
    with pytest.raises(RuntimeError, match=ENDS_ITS_RUNG) as info:
        _build(REFUSED[name])
    message = str(info.value)
    assert "`with Rung(...).continued():`" in message
    assert message.isascii()


@pytest.mark.parametrize("name", [n for n in WRITTEN if n != "ton_beside_coil"])
def test_box_starts_its_own_click_wire(name: str):
    """The box is the first output of its wire; the next row with an output starts a wire."""
    bundle = pyrung_to_ladder(_build(WRITTEN[name]), _tag_map())
    scopes = [bundle.main_rows, *(rows for _name, rows in bundle.subroutine_rows)]
    rows = [row for scope in scopes for row in scope[1:]]
    boxes = [index for index, row in enumerate(rows) if row[-1].startswith(_BOXES)]
    assert boxes
    for index in boxes:
        after = index + 1
        while after < len(rows) and rows[after][-1].startswith("."):
            after += 1
        if after < len(rows) and not rows[after][0]:
            # A continued wire starts from the rail, not from the box's wire.
            assert rows[after][1], rows[after]
    # Column A is the power rail: nothing branches there.
    assert all(row[1] not in {"T", "|"} and not row[1].startswith("T:") for row in rows)


@pytest.mark.parametrize("name", list(WRITTEN) + ["box_in_its_only_branch"])
def test_round_trip_keeps_the_readings(name: str):
    shape = WRITTEN.get(name) or _box_in_its_only_branch
    program = _build(shape)
    tag_map = _tag_map()
    reimported, mapping = _reimport(program, tag_map)
    for seed in range(3):
        before = _readings(program, tag_map, seed)
        after = _readings(reimported, mapping, seed)
        shared = sorted(set(before[0]) & set(after[0]))
        assert any(key.startswith(("Y", "C")) for key in shared)
        for scan, (left, right) in enumerate(zip(before, after, strict=True)):
            assert {k: left[k] for k in shared} == {k: right[k] for k in shared}, (seed, scan)


@pytest.mark.parametrize("name", list(WRITTEN))
def test_round_trip_gives_back_the_rungs_written(name: str):
    """pyrung -> CLICK -> pyrung gives back each rung as written, and the rows
    it exports to are the rows it came from."""
    program = _build(WRITTEN[name])
    tag_map = _tag_map()
    bundle = pyrung_to_ladder(program, tag_map)
    reimported, mapping = _reimport(program, tag_map)
    assert _shape(reimported.rungs) == _shape(program.rungs)
    for sub, rungs in program.subroutines.items():
        assert _shape(reimported.subroutines[sub]) == _shape(rungs)
    again = pyrung_to_ladder(reimported, mapping)
    assert again.main_rows == bundle.main_rows
    assert again.subroutine_rows == bundle.subroutine_rows


def test_rton_below_coil_exports_as_one_rung_of_two_wires():
    bundle = pyrung_to_ladder(_build(_rton_below_coil), _tag_map())
    trimmed = [(row[0], row[1], row[-1]) for rung in _rungs(bundle.main_rows) for row in rung]
    assert trimmed == [
        ("R", "X001", "out(Y001)"),
        ("", "X001", "on_delay(T1,TD1,preset=100,unit=Tms)"),
        ("", "X002", ".reset()"),
    ]


def test_wires_below_the_box_continue_its_rung():
    bundle = pyrung_to_ladder(_build(_wires_below_box), _tag_map())
    markers = [(row[0], row[-1]) for row in bundle.main_rows[1:-1] if row[-1]]
    assert markers == [
        ("R", "count_up(CT1,CTD1,preset=3)"),
        ("", ".reset()"),
        ("", "out(Y002)"),
        ("", "out(Y003)"),
    ]


def test_box_in_forloop_body_is_a_body_rung_of_its_own():
    def shape() -> None:
        with Rung(x[3]):
            with forloop(3):
                with Rung(x[1]):
                    out(y[1])
                with Rung(x[1]):
                    shift(c.select(11, 18)).clock(x[4]).reset(x[5])

    bundle = pyrung_to_ladder(_build(shape), _tag_map())
    assert [(row[0], row[1], row[-1]) for row in bundle.main_rows[1:-1]] == [
        ("R", "X003", "for(3)"),
        ("R", "X001", "out(Y001)"),
        ("R", "X001", "shift(C11..C18)"),
        ("", "X004", ".clock()"),
        ("", "X005", ".reset()"),
        ("R", "-", "next()"),
    ]


def test_import_gives_each_wire_of_a_box_rung_a_rung_of_its_own():
    """CLICK -> pyrung -> CLICK: a box rung whose wires share their first contact
    comes back as the box's rung and one continued() rung per other wire."""
    header = ("marker", *[f"col{i}" for i in range(31)], "AF")

    def row(marker: str, *cells: str, af: str) -> tuple[str, ...]:
        filled = list(cells) + ["-"] * (31 - len(cells))
        return (marker, *filled, af)

    rows = (
        header,
        row("R", "X001", af="latch(C1)"),
        row("", "X001", af="count_up(CT1,CTD1,preset=3)"),
        row("", "X004", af=".reset()"),
        row("", "X001", af="out(Y001)"),
        row("R", "-", af="end()"),
    )
    bundle = pyrung_to_ladder(_build(_ton_beside_coil), _tag_map())
    assert len(bundle.main_rows[0]) == len(header)
    code = ladder_to_pyrung(dataclasses.replace(bundle, main_rows=rows))
    body = code[code.index("with Program") :]
    assert "with rung(X001):\n        latch(C1)" in body
    assert "with rung(X001).continued():\n        count_up(" in body
    assert body.count(".continued()") == 2
    ns = exec_with_source(code, {})
    again = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert again.main_rows[1:] == rows[1:]
