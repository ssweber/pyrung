"""Pack Copy of text with a decimal point into a DD: the ``pack_point`` group (part ``follow2_values``).

``text/pack_text_int_fraction`` read on a CLICK that "12.9" packed into an Int is 12, with no
SC43: the parse stops at the point.  CLICK's help (topic 059) says that "12.345" packed into a DD
is an error, "Period separator not allowed".  A CLICK stops at the point for a DD too, with no
SC43 (hardware, 2026-11-10), as pyrung does.

``point_into_dd``: each text is staged into TXT and packed into a DD preset to 99, on one rung,
with SC43 read by a coil on the rung right after it:

- Result1, "12.9" into a DD: 12, the parse stopping at the point.  99 would be the DD left alone,
  refused, and 13 a rounding.
- Result2, "12.345" into a DD (help 059's example): 12, as for Result1.
- ErrorCode: SC43 after "12.9" (bit 1) and after "12.345" (bit 2).  0: no error.  3 would be the
  refusal with SC43 that help 059 describes.
- Result3 and Result4, the control: "12" into a DD is 12 with no SC43 (Result4 = 0).

The case owns what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, out, pack_text, reset, system
from tests.conformance.memory import c, dd, txt
from tests.conformance.probe_kit import finish, once

SC43 = system.fault.out_of_range

#: The texts, the first TXT each is staged from, the DD each packs into, and the coil of its SC43.
TEXTS = (
    ("12.9", 301, dd[151], c[691]),
    ("12.345", 311, dd[152], c[692]),
    ("12", 321, dd[153], c[693]),
)


def prove_point_into_dd(slot: Any) -> None:
    for text, first, into, coil in TEXTS:
        with Rung(*once(slot)):
            copy(99, into)
            for offset, char in enumerate(text):
                copy(char, txt[first + offset])
            pack_text(txt.select(first, first + len(text) - 1), into)
        with Rung(*once(slot), SC43):
            out(coil)
    for (_text, _first, into, _coil), field in zip(
        TEXTS, (slot.Result1, slot.Result2, slot.Result3), strict=True
    ):
        with Rung(*once(slot)):
            copy(into, field)
    for (_text, _first, _into, coil), bit in zip(TEXTS[:2], (1, 2), strict=True):
        with Rung(*once(slot), coil):
            math(slot.ErrorCode + bit, slot.ErrorCode)
    with Rung(*once(slot), TEXTS[2][3]):
        copy(1, slot.Result4)
    finish(slot)


def init_point_into_dd(slot: Any) -> None:
    reset(c.select(691, 693))
    fill(0, dd.select(151, 153))
    fill("\x00", txt.select(301, 304))
    fill("\x00", txt.select(311, 316))
    fill("\x00", txt.select(321, 322))


group = Group(
    "pack_point",
    [
        case(
            'Pack Copy of "12.9" (Result1) and "12.345" (Result2) into a DD stops at the point '
            'and stores 12, with no SC43 after either (ErrorCode = 0). "12" packs 12 (Result3) '
            "with no SC43 (Result4 = 0)",
            ladder=prove_point_into_dd,
            init=init_point_into_dd,
            expect={"Result1": 12, "Result2": 12, "Result3": 12, "Result4": 0, "ErrorCode": 0},
        ),
    ],
)


def test_pack_point():
    assert_all_passed(run(group))
