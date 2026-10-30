"""Math errors: the ``math_error`` group (subroutine ``Conf_MathError``).

CLICK's help: "SC46 _Math_Operation_Error: Data Registers used in the Math
formula have Invalid values.  SC50 is turned to ON, the PLC is put in Stop
Mode."  A CLICK does not do that (hardware round 1, 2026-10-29: all seven
errors below, one per run): the Math writes 0 over its destination, SC46 is on
for the rest of that scan, and the PLC stays in RUN, with SC46 and SC50 off
when the harness reads them afterwards.  No CLICK setting is known to change
this; record the firmware version with the next round.  pyrung matches the
hardware, so the group runs with every other group, last in
``suite.ALL_GROUPS``.

The case counts its own scans (``scans.py``) and runs one Math error every
other scan, each of the seven on its own scan 2, 4, ..., 14, so a flag left by
one is never read as the next one's.  Each stages its operand in DF401..DF407
(``memory.OWNED``), presets ``Result4`` to 99 and runs the Math into it; the
next rungs record, as bit k of Result2, that SC46 is on right after it, and as
bit k of Result1, that the Math wrote 0.  ``Fired`` goes up on scan 15, once
all seven have run.  What tells the models apart:

- Result1 = 127: every error writes 0.  A bit off: that Math skipped the write
  (99) or stored something else.
- Result2 = 127: every error raises SC46 for the rest of its scan.  A bit
  off: that one raised no error, or SC46 comes up only after the scan.
- SC11 = 1, SC50 = 0 (read by the harness at the end): the PLC kept running.
  A PLC that stops does so after the first error, scan 2: the case never
  fires, and the harness reads SC11 = 0 (and SC50 = 1 if it follows the help).
- SC46 = 0 at the end: no error is left standing.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, system
from pyrung.core.expression import acos, asin, log, log10, sqrt
from tests.conformance.memory import df
from tests.conformance.scans import count_scans, done, scans_for

#: Each Math error: the function, its operand, how CLICK's Math writes it.
ERRORS = [
    (sqrt, -4.0, "SQRT(-4.0)"),
    (log, 0.0, "LN(0.0)"),
    (log, -1.0, "LN(-1.0)"),
    (log10, 0.0, "LOG(0.0)"),
    (log10, -1.0, "LOG(-1.0)"),
    (asin, 1.5, "ASIN(1.5)"),
    (acos, 1.5, "ACOS(1.5)"),
]
#: Error k runs on scan 2k; the window ends with the last one.
WINDOW = 2 * len(ERRORS)
#: Every bit of Result1 and Result2 set.
ALL_SEVEN = (1 << len(ERRORS)) - 1


def prove_math_errors_do_not_stop(slot):
    math_error = system.fault.math_operation_error
    count_scans(slot, WINDOW)
    for k, (func, operand, _) in enumerate(ERRORS):
        on_scan = slot.Scratch == 2 * (k + 1)
        staged = df[401 + k]
        with Rung(slot.Cmd != 0, on_scan):
            copy(operand, staged)
            copy(99, slot.Result4)
            calc(func(staged), slot.Result4)
        with Rung(slot.Cmd != 0, on_scan, math_error):
            calc(slot.Result2 + (1 << k), slot.Result2)
        with Rung(slot.Cmd != 0, on_scan, slot.Result4 == 0):
            calc(slot.Result1 + (1 << k), slot.Result1)
    done(slot, WINDOW)


def init_math_errors_do_not_stop(slot):
    fill(0, df.select(401, 400 + len(ERRORS)))


group = Group(
    "math_error",
    [
        case(
            "A Math error does not stop the PLC: "
            + ", ".join(text for _, _, text in ERRORS)
            + ", one every other scan, each write 0 over the destination's 99 (Result1 = "
            "127, one bit each) and raise SC46 for the rest of their scan (Result2 = 127), "
            "and the PLC is still in RUN (SC11 = 1) with SC50 off and SC46 off at the end",
            ladder=prove_math_errors_do_not_stop,
            init=init_math_errors_do_not_stop,
            expect={"Result1": ALL_SEVEN, "Result2": ALL_SEVEN},
            expect_system={"SC11": 1, "SC50": 0, "SC46": 0},
            scans=scans_for(WINDOW),
        ),
    ],
)


def test_math_error():
    assert_all_passed(run(group))
