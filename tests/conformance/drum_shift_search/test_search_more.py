"""A one-shot Search against a plain one: the ``search_more`` group (part ``follow_values``).

``coil_range/ranges_and_search`` ran the only one-shot Search of the suite, and on a CLICK it
stored nothing: DD77 read 0, its found bit off, and no -1 (a miss) written.  Its result was a DD,
a form CLICK's Search refuses (a Search takes a DS Result only), so the case now asks the
one-shot alone, with DS results.  The fix step ``search-oneshot`` (8 in the ``conform-fix`` arc)
waits on this.  The id says ``dd_result`` for the case's first form; it is kept so its readings
stay under it.

``oneshot_against_dd_result``: four Searches into a DS each, every rung true from scan 2 on, as
in ``coil_range``.  The one-shots search DH11..DH12 for 9, which is at DH11 on scan 2 and moves to
DH12 on scan 3; the others search DH13..DH14, 9 at DH13 throughout.  Read on scan 6:

- Result1 and Result2, a one-shot each: 11 for the first hit kept, 12 for a search again on a
  later scan, 0 for nothing stored, -1 for a miss.  The 2026-11-06 reading of the DS form was 11.
- Result3 and Result4, a plain Search each, the control: 13 (read the same day).
- ErrorCode, the found bits: 1, 2, 4 and 8 in the same order, 15.

pyrung reads 11, 11, 13, 13, 15.  A one-shot that never runs reads 0 in Result1 and Result2.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, reset, search
from tests.conformance.memory import c, dh, ds
from tests.conformance.scans import count_scans, done, scans_for

READ_SCAN = 6


def prove_oneshot_against_dd_result(slot: Any) -> None:
    count_scans(slot)
    moving, fixed = dh.select(11, 12), dh.select(13, 14)
    found = [c[n] for n in range(201, 205)]
    results = [ds[3931], ds[3916], ds[3932], ds[3917]]
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(9, dh[11])
        copy(9, dh[13])
    running = (slot.Cmd != 0, slot.Scratch >= 2)
    with Rung(*running):
        search(moving == 9, result=results[0], found=found[0], oneshot=True)
    with Rung(*running):
        search(moving == 9, result=results[1], found=found[1], oneshot=True)
    with Rung(*running):
        search(fixed == 9, result=results[2], found=found[2])
    with Rung(*running):
        search(fixed == 9, result=results[3], found=found[3])
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(0, dh[11])
        copy(9, dh[12])
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == READ_SCAN):
        for result, field in zip(results, fields, strict=True):
            copy(result, field)
    for k, bit in enumerate(found):
        with Rung(slot.Cmd != 0, slot.Scratch == READ_SCAN, bit):
            math(slot.ErrorCode + (1 << k), slot.ErrorCode)
    done(slot)


def init_oneshot_against_dd_result(slot: Any) -> None:
    reset(c.select(201, 204))
    fill(0, dh.select(11, 14))
    fill(0, ds.select(3916, 3917))
    fill(0, ds.select(3931, 3932))


group = Group(
    "search_more",
    [
        case(
            "A one-shot Search keeps its first hit, 11 (Result1, Result2), and a plain Search "
            "finds 13 (Result3, Result4); each found bit is on (ErrorCode = 15)",
            ladder=prove_oneshot_against_dd_result,
            init=init_oneshot_against_dd_result,
            expect={
                "Result1": 11,
                "Result2": 11,
                "Result3": 13,
                "Result4": 13,
                "ErrorCode": 15,
            },
            scans=scans_for(),
            probe="what a one-shot Search keeps (8)",
        ),
    ],
)


def test_search_more():
    assert_all_passed(run(group))
