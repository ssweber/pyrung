"""Pins that tee off the enable wire after shared contacts (box atomicity).

A CLICK box has its own rung (C000A: every pin of a box starts from the same
branch).  A pin's wire either runs from the rail with all its own contacts, or
tees off the box's input wire after some shared contacts and adds its own.  The
importer must keep the shared contacts in the pin's condition, and the
re-exported wiring must be the rail form of the same logic.
"""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, TagMap, ladder_to_pyrung, pyrung_to_ladder
from pyrung.click.ladder.types import LadderBundle
from pyrung.core import And, Counter, Program, Rung
from pyrung.core.program import (
    count_down,
    count_up,
    event_drum,
    shift,
    time_drum,
)
from tests.click.helpers import bundle_logic, exec_with_source

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_WIDTH = 33
_HEADER = ("marker", *[f"c{i}" for i in range(_WIDTH - 2)], "AF")
_TAG_MAP = TagMap({Counter[1].Done: ct[1], Counter[1].Acc: ctd[1]}, include_system=False)

# (box factory, pins as (method, own contact index) in pin order)
_SHARED = (c[1], c[2])  # shared parent contacts the pins tee off after
_ENABLE_TAIL = c[3]  # an enable-only contact past the tee


def _box(kind: str):
    if kind == "count_up":
        return count_up(Counter[1], 5)
    if kind == "count_down":
        return count_down(Counter[1], 5)
    if kind == "shift":
        return shift(c.select(21, 28))
    if kind == "event_drum":
        return event_drum(
            outputs=[c[11], c[12]],
            events=[c[13], c[14]],
            pattern=[[1, 0], [0, 1]],
            current_step=ds[1],
            completion_flag=c[15],
        )
    if kind == "time_drum":
        return time_drum(
            outputs=[c[11], c[12]],
            presets=[100, 200],
            pattern=[[1, 0], [0, 1]],
            current_step=ds[1],
            accumulator=td[2],
            completion_flag=c[15],
        )
    raise AssertionError(kind)


# kind -> pins in CLICK order: (name, own contact)
_PINS = {
    "count_up": [("down", c[5]), ("reset", c[6])],
    "count_down": [("reset", c[6])],
    "shift": [("clock", c[5]), ("reset", c[6])],
    "event_drum": [("reset", c[5]), ("jump", c[6]), ("jog", c[7])],
    "time_drum": [("reset", c[5]), ("jump", c[6]), ("jog", c[7])],
}


def _rail_program(kind: str) -> Program:
    """The box with every pin wired from the rail: the shared contacts copied in."""
    with Program(strict=False) as logic:
        with Rung(*_SHARED, _ENABLE_TAIL):
            box = _box(kind)
            for name, own in _PINS[kind]:
                cond = And(*_SHARED, own)
                if name == "jump":
                    box = box.jump(cond, step=ds[2])
                else:
                    box = getattr(box, name)(cond)
    return logic


def _dash(row: list[str], start: int) -> list[str]:
    for col in range(start, _WIDTH - 1):
        if not row[col]:
            row[col] = "-"
    return row


def _tee_rows(rail: LadderBundle, own_contacts: list[str]) -> list[tuple[str, ...]]:
    """Redraw the rail bundle with the pins teeing off after the shared contacts.

    The enable row keeps ``C1 C2`` then a tee; each pin row holds only its own
    contact in the tee's column, with the vertical wire continuing down.
    """
    body = [r for r in rail.main_rows[1:] if r[-1] != "end()"]
    box_token = body[0][-1]
    pin_tokens = [r[-1] for r in body[1:]]
    tee_col = 3
    first = [""] * _WIDTH
    first[0], first[1], first[2], first[3], first[4] = "R", "C1", "C2", "T", "C3"
    first[-1] = box_token
    rows = [tuple(_dash(first, tee_col))]
    for index, token in enumerate(pin_tokens):
        row = [""] * _WIDTH
        last = index == len(pin_tokens) - 1
        row[tee_col] = own_contacts[index] if last else f"T:{own_contacts[index]}"
        row[-1] = token
        rows.append(tuple(_dash(row, tee_col + 1)))
    rows.append(tuple(_dash(["R"] + [""] * (_WIDTH - 1), 1)[:-1]) + ("end()",))
    return rows


@pytest.mark.parametrize("kind", list(_PINS))
def test_tee_off_pins_import_with_shared_contacts_and_re_export_as_rail(kind: str):
    rail = pyrung_to_ladder(_rail_program(kind), _TAG_MAP)
    owns = [own.name for _n, own in _PINS[kind]]
    tee = LadderBundle(main_rows=(_HEADER, *_tee_rows(rail, owns)), subroutine_rows=())

    code = ladder_to_pyrung(tee)
    # Every pin keeps the shared parent contacts C1 and C2 ahead of its own.
    for name, own in _PINS[kind]:
        assert f".{name}(C1, C2, C{own.name[1:]}" in code, code

    ns: dict[str, object] = {}
    exec_with_source(code, ns)
    again = pyrung_to_ladder(ns["logic"], ns["mapping"])
    assert bundle_logic(again) == bundle_logic(rail), code
