"""A one-shot Search against a Search into a DD: the ``search_more`` group (part ``follow_values``).

``coil_range/ranges_and_search`` ran the only one-shot Search of the suite, and the only Search
whose result is a DD, and on a CLICK it stored nothing: DD77 read 0, its found bit off, and no -1
(a miss) written.  One case cannot say which of the two did it.  The fix step ``search-oneshot``
(8 in the ``conform-fix`` arc) waits on this.

``oneshot_against_dd_result``: four Searches, each rung true from scan 2 on, as in ``coil_range``.
The one-shots search DH11..DH12 for 9, which is at DH11 on scan 2 and moves to DH12 on scan 3; the
others search DH13..DH14, 9 at DH13 throughout.  Read on scan 6:

- Result1, a one-shot into a DD (``coil_range``'s form): 11 for the first hit kept, 12 for a search
  again on a later scan, 0 for nothing stored, -1 for a miss.
- Result2, a one-shot into a DS: the same readings.
- Result3, a plain Search into a DD: 13, or 0 for nothing stored.
- Result4, a plain Search into a DS, the control: 13 (``coil_range`` Result4 read this form).
- ErrorCode, the found bits: 1, 2, 4 and 8 in the same order.

pyrung reads 11, 11, 13, 13, 15.  A DD result that is never stored reads 0 in Result1 and Result3;
a one-shot that never runs reads 0 in Result1 and Result2.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, reset, search
from tests.conformance.memory import c, dd, dh, ds
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

READ_SCAN = 6


def prove_oneshot_against_dd_result(slot: Any) -> None:
    count_scans(slot)
    moving, fixed = dh.select(11, 12), dh.select(13, 14)
    found = [c[n] for n in range(201, 205)]
    results = [dd[41], ds[3916], dd[42], ds[3917]]
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
    fill(0, dd.select(41, 42))
    fill(0, ds.select(3916, 3917))


group = Group(
    "search_more",
    [
        case(
            "A one-shot Search into a DD (Result1) or a DS (Result2) keeps its first hit, 11, "
            "and a plain Search into a DD (Result3) or a DS (Result4) finds 13; each found bit "
            "is on (ErrorCode = 15)",
            ladder=prove_oneshot_against_dd_result,
            init=init_oneshot_against_dd_result,
            expect={
                "Result1": between(-1, 12),
                "Result2": between(-1, 12),
                "Result3": between(-1, 13),
                "Result4": 13,
                "ErrorCode": between(0, 15),
            },
            scans=scans_for(),
            probe="what keeps a Search from storing: one-shot or a DD result (8)",
        ),
    ],
)


def test_search_more():
    assert_all_passed(run(group))
