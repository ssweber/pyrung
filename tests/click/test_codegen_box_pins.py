"""A box with pins shares its input wire with nothing (CLICK C000A).

CLICK takes all inputs (pins) of a box instruction from the same branch, the
rail: "All inputs of the box instruction must originate from the same branch."
A counter, retentive timer, shift register or drum whose input splits off a
node another output also hangs from can be drawn but does not compile, so the
exporter refuses it.  A box with no pins (a TON) may share the wire, and an
output below a box goes on a ``.continued()`` rung.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder.types import LadderExportError
from pyrung.core import Counter, Or, Program, Rung, Timer
from pyrung.core.program import (
    branch,
    count_up,
    event_drum,
    latch,
    on_delay,
    out,
    shift,
)

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


REFUSED: dict[str, Callable[[], None]] = {
    "coil_before_drum": _coil_before_drum,
    "coil_before_rton": _coil_before_rton,
    "counter_in_branch_beside_coil": _counter_in_branch_beside_coil,
    "shift_in_nested_branch": _shift_in_nested_branch,
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


@pytest.mark.parametrize("validate", [True, False])
@pytest.mark.parametrize("name", list(REFUSED))
def test_export_refuses(name: str, validate: bool):
    with pytest.raises(LadderExportError, match=C000A):
        pyrung_to_ladder(_build(REFUSED[name]), _tag_map(), validate=validate)


@pytest.mark.parametrize("name", list(ACCEPTED))
def test_export_accepts(name: str):
    pyrung_to_ladder(_build(ACCEPTED[name]), _tag_map())
