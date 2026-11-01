"""A FOR whose count is a Real: the ``for_count`` group, in-process only.

CLICK's editor takes a FOR count from a constant, DS, DD, TD, CTD or SD and
refuses a DF.  A pasted one stops the PLC: hardware (2026-11-01, as a project of
its own) ran its first FOR, over DF496 = 3.0, 545130 times (DD601) until a 10 s
watchdog raised error 0107, and never reached the +Inf and NaN ones.  That fits
the Real's 32 bits read whole, 1077936128 for 3.0.  So no CLICK run can pass
this, and the case holds what pyrung does with such a count.

- ``non_finite_count``: one FOR each, counted by its body into DD601..DD603,
  over DF496 = 3.0 (Result1), DF497 = +Inf (3.0E+38 * 10; Result2, the count
  copied into an Int, and Result3, the count / 4) and DF498 = NaN (+Inf - +Inf;
  Result4).  ``for_loop_iterations`` truncates a finite Real count and reads it
  as unsigned 16-bit, as an Int count is, and a non-finite one runs no iteration
  and raises SC43: 3, 0, 0, 0.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, forloop
from tests.conformance.memory import dd, df


def prove_non_finite_count(slot):
    finite, inf, nan = df[496], df[497], df[498]
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(3.0, finite)
        copy(3.0e38, inf)
        calc(inf * 10, inf)
        calc(inf - inf, nan)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(finite):
            calc(dd[601] + 1, dd[601])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(inf):
            calc(dd[602] + 1, dd[602])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        with forloop(nan):
            calc(dd[603] + 1, dd[603])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(dd[601], slot.Result1)
        copy(dd[602], slot.Result2)
        calc(dd[602] / 4, slot.Result3)
        copy(dd[603], slot.Result4)
        copy(1, slot.Fired)


def init_non_finite_count(slot):
    fill(0, dd.select(601, 603))
    fill(0.0, df.select(496, 498))


group = Group(
    "for_count",
    [
        case(
            "pyrung's FOR truncates a finite Real count to unsigned 16-bit and runs no "
            "iteration for a non-finite one: a count of 3.0 runs 3 (Result1), +Inf none "
            "(Result2 = 0, Result3 = 0) and NaN none (Result4 = 0)",
            ladder=prove_non_finite_count,
            init=init_non_finite_count,
            expect={"Result1": 3, "Result2": 0, "Result3": 0, "Result4": 0},
            in_process_only=(
                "CLICK's editor refuses a Real FOR count, and a pasted one stops the PLC on "
                "its watchdog (hardware 2026-11-01)"
            ),
        ),
    ],
)


def test_for_count():
    assert_all_passed(run(group))
