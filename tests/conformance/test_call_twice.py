"""A subroutine called twice a scan: the ``call_twice`` group (subroutine ``Conf_CallTwice``).

The main program calls ``Conf_CallTwice`` twice every scan (``Group(calls=2)``),
two call rungs one after the other, so every rung of the group runs twice a
scan.  CLICK has no nested calls, so this is the only way a case can run a
subroutine more than once in a scan.  The cases own C701..C750
(``memory.OWNED``).

A case counts calls, not scans: ``count_scans`` adds 1 to ``Scratch`` on each
call, so call 2k-1 and call 2k are the two calls of scan k.  The window is 9
calls, so ``firings`` lists the calls an edge contact passed power on as one
digit each.

- ``rise_once_per_scan`` (probe): C701 is on for calls 5 and 6 (scan 3), its
  out rung between a rise and a fall contact above it and another pair below.
  Each contact keeps a memory of its own, updated on each call (adopted, the
  CLICK model of the coils group): above the writer the rise fires on call 6
  and the fall on call 8, below it on calls 5 and 7 (6, 5, 8, 7), as
  pyrung's do.  Edge contacts that compare the bit with its value at the end
  of the previous scan (pyrung before) see the same edge again on the second
  call of a scan: 6, 56, 8, 78.  One memory per bit updated by every read: 0,
  5, 0, 7.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, fall, out, rise
from tests.conformance.memory import c
from tests.conformance.scans import count_scans, done, firings, scans_for

#: Calls a case records (single digits for ``firings``).
CALLS = 9


def prove_rise_once_per_scan(slot):
    bit = c[701]
    count_scans(slot, CALLS)
    firings(rise(bit), slot, slot.Result1, window=CALLS)
    firings(fall(bit), slot, slot.Result3, window=CALLS)
    with Rung(slot.Cmd != 0, slot.Scratch >= 5, slot.Scratch <= 6):
        out(bit)
    firings(rise(bit), slot, slot.Result2, window=CALLS)
    firings(fall(bit), slot, slot.Result4, window=CALLS)
    done(slot, CALLS)


group = Group(
    "call_twice",
    [
        case(
            "An edge contact in a subroutine called twice a scan fires once per edge, on "
            "the call that first sees it: a bit on for calls 5 and 6 (scan 3) rises on call "
            "6 (Result1) and falls on call 8 (Result3) above its out rung, and rises on call "
            "5 (Result2) and falls on call 7 (Result4) below it",
            ladder=prove_rise_once_per_scan,
            expect={"Result1": 6, "Result2": 5, "Result3": 8, "Result4": 7},
            scans=scans_for(CALLS),
            probe="rise() in a subroutine called twice in one scan",
        ),
    ],
    calls=2,
)


def test_call_twice():
    assert_all_passed(run(group))
