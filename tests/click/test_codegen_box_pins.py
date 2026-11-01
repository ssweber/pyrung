"""A box with pins gets a CLICK rung of its own (CLICK C000A).

CLICK takes all inputs (pins) of a box instruction from the same branch, the
rail: "All inputs of the box instruction must originate from the same branch."
A counter, retentive timer, shift register or drum whose input splits off a
node another output also hangs from can be drawn but does not compile.  So the
exporter splits a pyrung rung at each such box: the outputs above it stay on
the first CLICK rung, the box starts a rung of its own, and the outputs below
it follow as continued rungs.  Every CLICK rung the split makes repeats the
contacts in front of its outputs, and CLICK solves row by row, as pyrung does,
so a repeated contact is read again where the new rung stands.  The export is
refused only where that second read could see another value: a write on the
same rung, in between, of a tag the contact reads.  A box with no pins (a TON)
may share the wire.
"""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.ladder.types import LadderExportError
from pyrung.core import PLC, Counter, Or, Program, Rung, Timer
from pyrung.core.program import (
    branch,
    call,
    copy,
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

C000A = r"all inputs \(pins\) of a box instruction must start from the same branch"


def _drum() -> None:
    event_drum(
        outputs=[c[3]], events=[c[2]], pattern=[[1]], current_step=ds[1], completion_flag=c[1]
    ).reset(c[4])


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
        out(y[1])
        with branch(x[6]):
            count_up(Counter[1], preset=3).reset(x[4])
        out(y[2])
        with branch(Counter[1].Done):
            out(y[3])


def _no_trunk_contact() -> None:
    with Rung():
        out(y[1])
        with branch(x[5]):
            count_up(Counter[1], preset=3).reset(x[4])
        out(y[2])


def _box_in_continued_rung() -> None:
    with Rung(x[1]):
        out(y[1])
    with Rung(x[2]).continued():
        latch(c[9])
        with branch(x[7]):
            count_up(Counter[1], preset=3).reset(x[4])
        out(y[2])


def _branch_reads_live() -> None:
    # The branches read C5 where they stand (after the reset and the latch);
    # the trunk contact X001 is written by nothing, so repeating it is safe.
    with Rung(x[1]):
        reset(c[5])
        with branch(c[5]):
            count_up(Counter[1], preset=3).reset(x[4])
        with branch(x[2]):
            latch(c[5])
        with branch(c[5]):
            out(y[3])


def _write_after_the_box() -> None:
    # The rung after the box re-reads C5 there; nothing between wrote it.
    with Rung(c[5]):
        with branch(x[2]):
            count_up(Counter[1], preset=3).reset(x[4])
        reset(c[5])
        out(y[1])


def _two_boxes() -> None:
    with Rung(x[1]):
        with branch(x[2]):
            count_up(Counter[1], preset=3).reset(x[4])
        out(y[1])
        with branch(x[3]):
            shift(c.select(11, 18)).clock(x[4]).reset(x[5])
        out(y[2])


SPLIT: dict[str, Callable[[], None]] = {
    "coil_before_drum": _coil_before_drum,
    "coil_before_rton": _coil_before_rton,
    "counter_in_branch_beside_coil": _counter_in_branch_beside_coil,
    "shift_in_nested_branch": _shift_in_nested_branch,
    "outputs_below_box": _outputs_below_box,
    "no_trunk_contact": _no_trunk_contact,
    "box_in_continued_rung": _box_in_continued_rung,
    "branch_reads_live": _branch_reads_live,
    "write_after_the_box": _write_after_the_box,
    "two_boxes": _two_boxes,
}


def _box_alone_then_continued() -> None:
    with Rung(Or(x[1], x[2])):
        count_up(Counter[1], preset=10).down(x[3]).reset(x[4])
    with Rung(x[5]).continued():
        latch(c[1])


def _ton_beside_coil() -> None:
    with Rung(x[1]):
        out(y[1])
        on_delay(Timer[1], preset=100, unit="Tms")


def _box_in_its_only_branch() -> None:
    with Rung(x[1]):
        with branch(x[2]):
            count_up(Counter[1], preset=10).reset(x[4])


ACCEPTED: dict[str, Callable[[], None]] = {
    "box_alone_then_continued": _box_alone_then_continued,
    "ton_beside_coil": _ton_beside_coil,
    "box_in_its_only_branch": _box_in_its_only_branch,
}


def _trunk_written_before_box() -> None:
    with Rung(c[5]):
        reset(c[5])
        with branch(x[2]):
            count_up(Counter[1], preset=10).reset(x[4])


def _box_writes_trunk_contact() -> None:
    with Rung(~Counter[1].Done):
        with branch(x[2]):
            count_up(Counter[1], preset=1).reset(x[4])
        out(y[1])


def _comparison_written_before_box() -> None:
    with Rung(ds[1] == 0):
        copy(1, ds[1])
        with branch(x[2]):
            count_up(Counter[1], preset=10).reset(x[4])


def _refused_in_subroutine() -> None:
    with Rung():
        call("Sub")
    with subroutine("Sub"):
        with Rung(c[5]):
            reset(c[5])
            with branch(x[2]):
                count_up(Counter[1], preset=10).reset(x[4])


REFUSED: dict[str, tuple[Callable[[], None], str]] = {
    "trunk_written_before_box": (_trunk_written_before_box, "C5"),
    "box_writes_trunk_contact": (_box_writes_trunk_contact, "Counter_Done (CT1)"),
    "comparison_written_before_box": (_comparison_written_before_box, "DS1"),
    "refused_in_subroutine": (_refused_in_subroutine, "C5"),
}


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


@pytest.mark.parametrize("name", list(SPLIT))
def test_box_starts_its_own_click_rung(name: str):
    """The box is the first output of a CLICK rung; rows below it are wires of their own."""
    bundle = pyrung_to_ladder(_build(SPLIT[name]), _tag_map())
    rows = bundle.main_rows[1:-1]
    boxes = [index for index, row in enumerate(rows) if row[-1].startswith(_BOXES)]
    assert boxes
    for index in boxes:
        assert rows[index][0].startswith("R"), rows[index]
        after = index + 1
        while after < len(rows) and rows[after][-1].startswith("."):
            after += 1
        if after < len(rows) and not rows[after][0]:
            # A continued wire starts from the rail, not from the box's wire.
            assert rows[after][1], rows[after]
    # Column A is the power rail: nothing branches there.
    assert all(row[1] not in {"T", "|"} and not row[1].startswith("T:") for row in rows)


@pytest.mark.parametrize("name", list(SPLIT) + list(ACCEPTED))
def test_split_round_trips_with_the_same_readings(name: str):
    shape = SPLIT.get(name) or ACCEPTED[name]
    program = _build(shape)
    tag_map = _tag_map()
    code = ladder_to_pyrung(pyrung_to_ladder(program, tag_map))
    ns = exec_with_source(code, {})
    reimported, mapping = ns["logic"], ns["mapping"]
    assert isinstance(reimported, Program) and isinstance(mapping, TagMap)
    for seed in range(3):
        before = _readings(program, tag_map, seed)
        after = _readings(reimported, mapping, seed)
        shared = sorted(set(before[0]) & set(after[0]))
        assert any(key.startswith(("Y", "C")) for key in shared)
        for scan, (left, right) in enumerate(zip(before, after, strict=True)):
            assert {k: left[k] for k in shared} == {k: right[k] for k in shared}, (seed, scan)


def test_rton_beside_coil_exports_as_two_rungs():
    bundle = pyrung_to_ladder(_build(_coil_before_rton), _tag_map())
    trimmed = [(row[0], row[1], row[-1]) for rung in _rungs(bundle.main_rows) for row in rung]
    assert trimmed == [
        ("R", "X001", "out(Y001)"),
        ("R", "X001", "on_delay(T1,TD1,preset=100,unit=Tms)"),
        ("", "X002", ".reset()"),
    ]


def test_outputs_below_the_box_continue_its_rung():
    bundle = pyrung_to_ladder(_build(_outputs_below_box), _tag_map())
    markers = [(row[0], row[-1]) for row in bundle.main_rows[1:-1] if row[-1]]
    assert markers == [
        ("R", "out(Y001)"),
        ("R", "count_up(CT1,CTD1,preset=3)"),
        ("", ".reset()"),
        ("", "out(Y002)"),
        ("", "out(Y003)"),
    ]


def test_box_in_forloop_body_gets_a_body_rung_of_its_own():
    def shape() -> None:
        with Rung(x[3]):
            with forloop(3):
                with Rung(x[1]):
                    out(y[1])
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


@pytest.mark.parametrize("name", list(ACCEPTED))
def test_export_accepts(name: str):
    pyrung_to_ladder(_build(ACCEPTED[name]), _tag_map())


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("name", list(REFUSED))
def test_export_refuses_a_split_that_rereads_a_written_tag(name: str, validate: bool):
    shape, tag = REFUSED[name]
    with pytest.raises(LadderExportError, match=C000A) as info:
        pyrung_to_ladder(_build(shape), _tag_map(), validate=validate)
    assert f"writes {tag}" in str(info.value)
    assert str(info.value).isascii()
