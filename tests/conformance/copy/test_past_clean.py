"""A write past the DD bank, cleared first: the ``past_clean`` group (part ``follow_values``).

**It writes into a real CLICK's memory past the DD bank, at DD1531.**  Do not run it on a CLICK
whose memory matters; delete its call rung in main to leave it out.  It ends by writing 0 there
again, which ``faults/pointer_past_bank_lands`` (part ``system``) expects to read.  The simulator
and the P1AM code have no memory there.

``past_write/past_bank_write`` read 5, 5, 0 on a CLICK whose DD1531 already held 5, so it skipped
its write and answered nothing.  This case writes whatever is there.  The ruling on the copy guard
(``conform-fix`` arc, 1h) waits on it.

``clear_then_write``, through the pointer DS4226 = 1531 (``past_write``'s), each read into a field
preset to -7 on its scan, so -7 is a read that left its destination alone:

- ErrorCode: DD1531 before anything is written, as found (0 after the round's clear).
- Result1: after a Copy of 0 into it.
- Result2: after a Copy of 1234 into it: 1234 where the write lands, 0 where it is dropped.
- Result3: after a Copy of 0 again: 0 where it lands, 1234 where only the first write landed.
- Result4: SC43 (bit 1) and SC44 (bit 2) right after the write of 1234, latched by coils.

A CLICK read 0, 1234, 0, 0 and 0 on 2026-11-06 and 2026-11-09.  Each write lands, and no write
raises either flag.  pyrung drops the writes and reads 0, 0, 0, 0 and 0, so the case is pending
``PASTBANK``.  ErrorCode expects 0, what this case and ``modbus_raw`` leave there.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, latch, math, reset, system
from tests.conformance.copy.test_past_bank_write import ADDRESS, POINTER
from tests.conformance.memory import c, dd
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 10
SC43 = system.fault.out_of_range
SC44 = system.fault.address_error
UNREAD = -7


def _read(slot: Any, scan: int, into: Any) -> None:
    with Rung(slot.Scratch == scan):
        copy(UNREAD, into)
        copy(dd[POINTER], into)


def _write(slot: Any, scan: int, value: int) -> None:
    with Rung(slot.Scratch == scan):
        copy(value, dd[POINTER])


def prove_clear_then_write(slot: Any) -> None:
    raised43, raised44 = c[231], c[232]
    count_scans(slot, WINDOW)
    with Rung(slot.Scratch == 2):
        copy(ADDRESS, POINTER)
    _read(slot, 3, slot.ErrorCode)
    _write(slot, 4, 0)
    _read(slot, 5, slot.Result1)
    _write(slot, 6, 1234)
    with Rung(slot.Scratch == 6, SC43):
        latch(raised43)
    with Rung(slot.Scratch == 6, SC44):
        latch(raised44)
    _read(slot, 7, slot.Result2)
    _write(slot, 8, 0)
    _read(slot, 9, slot.Result3)
    with Rung(slot.Scratch == 9, raised43):
        math(slot.Result4 + 1, slot.Result4)
    with Rung(slot.Scratch == 9, raised44):
        math(slot.Result4 + 2, slot.Result4)
    done(slot, WINDOW)


def init_clear_then_write(slot: Any) -> None:
    reset(c.select(231, 232))
    copy(0, POINTER)


group = Group(
    "past_clean",
    [
        case(
            "WRITES PAST THE DD BANK on a CLICK, at DD1531, and clears it last.  Each write "
            "lands.  DD1531 reads 0 as found (ErrorCode) and 0 after a Copy of 0 (Result1).  It "
            "reads 1234 after a Copy of 1234 (Result2), and 0 after a Copy of 0 again "
            "(Result3).  No SC43 or SC44 follows the 1234 (Result4 = 0)",
            ladder=prove_clear_then_write,
            init=init_clear_then_write,
            expect={"Result1": 0, "Result2": 1234, "Result3": 0, "Result4": 0, "ErrorCode": 0},
            scans=scans_for(WINDOW),
            pending="PASTBANK",
        ),
    ],
)


def test_past_clean():
    assert_all_passed(run(group))
