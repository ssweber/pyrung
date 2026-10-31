"""A FOR whose count is a Real: the ``for_count`` group (subroutine ``Conf_ForCount``).

Part H of the CLICK project (``suite.PARTS``), a project of its own: a CLICK
that reads a non-finite count as a huge one loops until its watchdog stops
the PLC, and that must not cost any other part its answers.  A CLICK that
refuses a DF count at paste never fires the case.

- ``non_finite_count`` (probe): one FOR each, counted by its body into
  DD601..DD603, over DF496 = 3.0 (Result1), DF497 = +Inf (3.0E+38 * 10;
  Result2, the count copied into an Int, so clamped to 32767, and Result3,
  the count / 32768) and DF498 = NaN (+Inf - +Inf; Result4, clamped).
  Adopted (pyrung): a Real count is converted as a Copy converts, and a
  non-finite one runs no iteration: 3, 0, 0, 0.  A count clamped to the
  unsigned 16-bit range reads 65535 iterations for +Inf (Result2 = 32767,
  Result3 = 1); one clamped to an Int, 32767 (32767, 0).  A count that is
  the Real's 32 bits read as an integer (3.0 is 1077936128) loops until the
  watchdog stops the PLC, in Result1 already.
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
        calc(dd[602] / 32768, slot.Result3)
        copy(dd[603], slot.Result4)
        copy(1, slot.Fired)


def init_non_finite_count(slot):
    fill(0, dd.select(601, 603))
    fill(0.0, df.select(496, 498))


group = Group(
    "for_count",
    [
        case(
            "A FOR converts a Real count as a Copy does and runs no iteration for a "
            "non-finite one: a count of 3.0 runs 3 (Result1), +Inf none (Result2 = 0, "
            "Result3 = 0) and NaN none (Result4 = 0)",
            ladder=prove_non_finite_count,
            init=init_non_finite_count,
            expect={"Result1": 3, "Result2": 0, "Result3": 0, "Result4": 0},
            probe="FOR with a Real count, finite, +Inf and NaN",
        ),
    ],
)


def test_for_count():
    assert_all_passed(run(group))
