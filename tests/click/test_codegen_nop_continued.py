"""NOP shapes CLICK will not hold.

A NOP sits alone on a bare wire in CLICK.  CLICK refuses a contact in front of
NOP (C0003, "There is no Coil instruction in the rung"), and deletes a NOP row
that shares its rung with another row, which is what an empty rung next to a
``.continued()`` rung exports as.  The exporter refuses both rather than write
a rung CLICK would reject or quietly change.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.ladder.types import LadderExportError
from pyrung.core import Or, Program, Rung, Timer
from pyrung.core.program import branch, call, copy, on_delay, out, subroutine
from tests.click.helpers import strip_pyrung_boilerplate

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

CONDITIONS = "empty rung with conditions"
SHARED = "deletes a NOP that shares its rung"


def _contact_nop() -> None:
    with Rung(c[1]):
        pass


def _contact_nop_then_continued() -> None:
    with Rung(c[1]):
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _or_contacts() -> None:
    with Rung(Or(c[1], c[6])):
        pass
    with Rung(Or(c[1], c[6]), c[2]).continued():
        copy(1, ds[1])


def _continued_contact_nop_after_output() -> None:
    with Rung(c[1]):
        copy(3, ds[3])
    with Rung(c[1], c[2]).continued():
        pass


def _branch_then_continued_contact_nop() -> None:
    with Rung(c[1]):
        copy(1, ds[1])
        with branch(c[2]):
            copy(2, ds[2])
    with Rung(c[1], c[3]).continued():
        pass


def _contact_nop_in_subroutine() -> None:
    with Rung(c[5]):
        call("Sub")
    with subroutine("Sub"):
        with Rung(c[1]):
            pass
        with Rung(c[1], c[2]).continued():
            copy(1, ds[1])


def _nop_then_continued() -> None:
    with Rung():
        pass
    with Rung(c[1], c[2]).continued():
        copy(1, ds[1])


def _nop_then_continued_timer() -> None:
    with Rung():
        pass
    with Rung(c[1], c[2]).continued():
        on_delay(Timer[1], preset=100).reset(c[7])


def _continued_nop_after_output() -> None:
    with Rung(c[1]):
        out(y[1])
    with Rung().continued():
        pass


def _nop_then_continued_in_subroutine() -> None:
    with Rung(c[5]):
        call("Sub")
    with subroutine("Sub"):
        with Rung():
            pass
        with Rung(c[1]).continued():
            copy(1, ds[1])


REFUSED: dict[str, tuple[Callable[[], None], str]] = {
    "contact_nop": (_contact_nop, CONDITIONS),
    "contact_nop_then_continued": (_contact_nop_then_continued, CONDITIONS),
    "or_contacts": (_or_contacts, CONDITIONS),
    "continued_contact_nop_after_output": (_continued_contact_nop_after_output, CONDITIONS),
    "branch_then_continued_contact_nop": (_branch_then_continued_contact_nop, CONDITIONS),
    "contact_nop_in_subroutine": (_contact_nop_in_subroutine, CONDITIONS),
    "nop_then_continued": (_nop_then_continued, SHARED),
    "nop_then_continued_timer": (_nop_then_continued_timer, SHARED),
    "continued_nop_after_output": (_continued_nop_after_output, SHARED),
    "nop_then_continued_in_subroutine": (_nop_then_continued_in_subroutine, SHARED),
}


def _tag_map() -> TagMap:
    return TagMap({Timer[1].Done: t[1], Timer[1].Acc: td[1]}, include_system=False)


def _build(shape: Callable[[], None]) -> Program:
    with Program() as logic:
        shape()
    return logic


@pytest.mark.parametrize("name", list(REFUSED))
def test_export_refuses(name: str):
    shape, match = REFUSED[name]
    with pytest.raises(LadderExportError, match=match):
        pyrung_to_ladder(_build(shape), _tag_map())


@pytest.mark.parametrize("name", list(REFUSED))
def test_export_refuses_without_validate(name: str):
    shape, match = REFUSED[name]
    with pytest.raises(LadderExportError, match=match):
        pyrung_to_ladder(_build(shape), _tag_map(), validate=False)


def test_bare_nop_between_rungs_exports():
    """A bare NOP with normal rungs on either side is its own CLICK rung."""
    with Program() as logic:
        with Rung(c[1]):
            copy(1, ds[1])
        with Rung():
            pass
        with Rung(c[2]):
            copy(2, ds[2])
    code = strip_pyrung_boilerplate(ladder_to_pyrung(pyrung_to_ladder(logic, TagMap())))
    assert "with rung():\n    pass" in code


def test_shape_change_is_not_a_logic_change():
    """Continued rows that come back as branches pass the check."""
    with Program() as logic:
        with Rung(c[1]):
            copy(1, ds[1])
        with Rung(c[1], c[2]).continued():
            copy(2, ds[2])
    code = strip_pyrung_boilerplate(ladder_to_pyrung(pyrung_to_ladder(logic, TagMap())))
    assert "with branch(C2):" in code
