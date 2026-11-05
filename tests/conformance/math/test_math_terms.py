"""Follow-up Math probes: the ``math_terms`` group (part ``follow_values``).

What the 2026-11-05 round left unread in ``math_more``, for the fix step ``math-click`` (2 in the
``conform-fix`` arc).  A question's field holds every candidate (``probe_kit.between``), so a
pass answers nothing and a run on a CLICK records what it read.

- ``wide_values``: the two values ``math_more`` read as "neither listed".  ABS(-2147483648) into a
  DD preset 99 (``abs_and_precision`` ErrorCode): Result1 1 for itself, 2 for 2147483647, 3 for 0
  (as ABS(-32768) into an Int reads), 4 for 99 (no store), 0 for any other, and Result2 the DD /
  100000, which shows any other reading's size.  3000000000 / DD 1000 into a DD preset 99
  (``wide_constant_into_dint`` Result4): Result3 1 for 3000000, 2 for -1294967 (the constant's
  low 32 bits), 3 for 24 (its low 16 bits, 24064, as the rule for a wide Math constant predicts),
  4 for 99, and ErrorCode the DD copied into an Int (24, or 32767 for 3000000).  Result4 has SC43
  after the ABS (bit 1) and after the division (bit 2).  pyrung reads 1, -21474, 1, 1, 32767: the
  ABS wraps back to itself with SC43, and the constant is whole.
- ``error_terms``: is a math error's 0 put on its term or on the whole formula, outside RAND
  (``store_probe/rand_tag_bounds`` read RAND ( 7 , 7 ) + 5 as 5)?  DD 7 / DD 0 + 5 (Result1): 5
  for the term, 0 for the formula, 99 for no store.  (DD 7 / DD 0 + 1) * 5 (Result2): 5 for the
  term, 0 for the formula.  SQRT(DF -4.0) + 5 (Result3): 5 or 0.  DD 0 ^ DD 0 (Result4): 0
  (``power_edges`` read it), 99 for no store.  ErrorCode has the flags: SC40 after the first two
  (bits 1, 2), SC46 after the root (bit 4), and after 0 ^ 0 SC40 (bit 8), SC43 (bit 16) and SC46
  (bit 32): which flag 0 ^ 0 raises.  pyrung reads 0, 0, 0, 1, 7: the formula is 0, and 0 ^ 0 is 1
  with no flag.

SC46 stays on to the end of the scan, so the root and the power each run on the first scan whose
SC46 is off right before them, as ``probe_kit.GuardedProbes`` does; SC40 and SC43 follow the last
Math, so each is read on the rung right after.  After 0 ^ 0, SC43 is read by a coil first, since
the Math that reads SC40 settles it.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, latch, out, reset, system
from pyrung.core.expression import sqrt
from tests.conformance.memory import c, dd, df
from tests.conformance.probe_kit import between, class_of, finish, flag_bit, once
from tests.conformance.scans import count_scans, done, scans_for

SC40 = system.fault.division_error
SC43 = system.fault.out_of_range
SC46 = system.fault.math_operation_error


def prove_wide_values(slot: Any) -> None:
    low, thousand, scale, absolute, quotient = dd[51], dd[52], dd[53], dd[54], dd[55]
    with Rung(*once(slot)):
        copy(-2147483648, low)
        copy(1000, thousand)
        copy(100000, scale)
        copy(99, absolute)
        copy(99, quotient)
        calc(abs(low), absolute)
    flag_bit(slot, SC43, slot.Result4, 1)
    with Rung(*once(slot)):
        calc(3000000000 / thousand, quotient)
    flag_bit(slot, SC43, slot.Result4, 2)
    class_of(slot, absolute, [-2147483648, 2147483647, 0, 99], slot.Result1)
    class_of(slot, quotient, [3000000, -1294967, 24, 99], slot.Result3)
    with Rung(*once(slot)):
        calc(absolute / scale, slot.Result2)
        copy(quotient, slot.ErrorCode)
    finish(slot)


def init_wide_values(slot: Any) -> None:
    fill(0, dd.select(51, 55))


WINDOW = 8


def _guarded(slot: Any, now: Any, taken: Any) -> None:
    """*now* on, once, on the first scan from 2 whose SC46 is off right here."""
    with Rung(slot.Scratch > 1, slot.Scratch <= WINDOW, ~taken, ~SC46):
        out(now)
        latch(taken)


def prove_error_terms(slot: Any) -> None:
    seven, zero, minus = dd[61], dd[62], df[11]
    count_scans(slot, WINDOW)
    with Rung(slot.Scratch == 1):
        copy(7, seven)
        copy(0, zero)
        copy(-4.0, minus)
        copy(99, slot.Result1)
        copy(99, slot.Result2)
        copy(99, slot.Result3)
        copy(99, slot.Result4)
    with Rung(slot.Scratch == 2):
        calc(seven / zero + 5, slot.Result1)
    with Rung(slot.Scratch == 2, SC40):
        calc(slot.ErrorCode + 1, slot.ErrorCode)
    with Rung(slot.Scratch == 2):
        calc((seven / zero + 1) * 5, slot.Result2)
    with Rung(slot.Scratch == 2, SC40):
        calc(slot.ErrorCode + 2, slot.ErrorCode)
    root_now, root_taken, power_now, power_taken, power_sc43 = (c[n] for n in range(181, 186))
    _guarded(slot, root_now, root_taken)
    with Rung(root_now):
        calc(sqrt(minus) + 5, slot.Result3)
    with Rung(root_now, SC46):
        calc(slot.ErrorCode + 4, slot.ErrorCode)
    _guarded(slot, power_now, power_taken)
    with Rung(power_now):
        calc(zero**zero, slot.Result4)
    with Rung(power_now, SC43):
        out(power_sc43)
    with Rung(power_now, SC40):
        calc(slot.ErrorCode + 8, slot.ErrorCode)
    with Rung(power_now, SC46):
        calc(slot.ErrorCode + 32, slot.ErrorCode)
    with Rung(power_sc43):
        calc(slot.ErrorCode + 16, slot.ErrorCode)
    done(slot, WINDOW)


def init_error_terms(slot: Any) -> None:
    fill(0, dd.select(61, 62))
    copy(0.0, df[11])
    reset(c.select(181, 185))


group = Group(
    "math_terms",
    [
        case(
            "ABS(-2147483648) into a DD (Result1, Result2 = DD / 100000) and 3000000000 / DD 1000 "
            "into a DD (Result3, ErrorCode = the DD into an Int), with SC43 after each (Result4)",
            ladder=prove_wide_values,
            init=init_wide_values,
            expect={
                "Result1": between(0, 4),
                "Result2": between(-21475, 21475),
                "Result3": between(0, 4),
                "Result4": between(0, 3),
                "ErrorCode": between(-32768, 32767),
            },
            probe="ABS of the least DD; a constant past 32 bits over a DD (2)",
        ),
        case(
            "A math error's 0 in a larger formula: 7 / 0 + 5 (Result1), (7 / 0 + 1) * 5 "
            "(Result2), SQRT(-4.0) + 5 (Result3) read 5 if the term alone is 0 and 0 if the "
            "formula is; 0 ^ 0 (Result4); the flags each raises (ErrorCode)",
            ladder=prove_error_terms,
            init=init_error_terms,
            expect={
                "Result1": between(0, 99),
                "Result2": between(0, 99),
                "Result3": between(0, 99),
                "Result4": between(0, 99),
                "ErrorCode": between(0, 63),
            },
            scans=scans_for(WINDOW),
            probe="a math error zeroes its term or its formula; 0 ^ 0's flag (2)",
        ),
    ],
)


def test_math_terms():
    assert_all_passed(run(group))
