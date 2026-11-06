"""The order and width of a Real SUM: the ``real_sum`` group.

Part ``math`` (``suite.PARTS``).  CLICK's SUM over a DF range adds in some order at some
width.  pyrung adds plain left to right on every backend (core, kernel and
generated code); the ruling is plain left to right on all backends, and a
recording overrides the ruling.  Each case sums three orderings of one set into a
DF and reads each sum, truncated, into an Int.

The sets, as the candidates read them (first, second, third ordering):

================================  ==========  ==========  ==========
Candidate                         ``f32_row``  ``f64_row``  ``u`` (``sum_absorbs``)
================================  ==========  ==========  ==========
float32 left to right             0, 0, 1      0, 0, 1      0
float32 right to left             0, 1, 0      0, 1, 0      2
float32, smallest first           0, 0, 0      0, 0, 0      2
float32, largest first            1, 1, 1      1, 1, 1      0
double left to right              1, 1, 1      0, 0, 1      2
double right to left              1, 1, 1      0, 1, 0      2
compensated (Neumaier, fsum)      1, 1, 1      1, 1, 1      2
================================  ==========  ==========  ==========

- ``f32_row``: {1e8, 1, -1e8}, {1, 1e8, -1e8}, {1e8, -1e8, 1}.  At float32 1e8 absorbs
  the 1, so left to right is 0, 0, 1 and right to left 0, 1, 0.  At double the 1
  survives every order.  pyrung reads 1, 1, 1 on every backend.
- ``f64_row``: the same orderings of {1e30, 1, -1e30}, where double absorbs the 1 too.
  Only a compensated sum reads 1, 1, 1 at a width a CLICK could have.  pyrung reads
  0, 0, 1 on every backend, and the case expects a range and ``make test-conformance`` prints its reading on a PASS.
- ``sum_absorbs``: {16777216, 1, 1}, read as the sum less 16777216.  float32 left to right
  or largest first absorbs each 1 (0); anything else reads 2.  It alone tells float32
  largest first from compensated.  pyrung reads 2.
- ``math_keeps_the_one``: not a SUM: DF 1e8 + 1.0 - DF 1e8 in one Math (Result1), and
  DF 1e30 + 1.0 - DF 1e30 (Result2).  The intermediates are wider than a float32
  (``faults/real_intermediate_range`` read the range, not the precision), so 1, 0 at
  double, 0, 0 at float32, and 1, 1 wider than double.

The cases own DF70..DF100 and DF142..DF150.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math
from tests.conformance.memory import df
from tests.conformance.probe_kit import between, finish, once


def _orderings(slot, first, big, fields):
    """Stage and sum the three orderings of {big, 1, -big} from DF *first* on."""
    orderings = ((big, 1.0, -big), (1.0, big, -big), (big, -big, 1.0))
    address = first
    for values, field in zip(orderings, fields, strict=True):
        with Rung(*once(slot)):
            copy(values[0], df[address])
            copy(values[1], df[address + 1])
            copy(values[2], df[address + 2])
            math(df.select(address, address + 2).sum(), df[address + 3])
            math(df[address + 3], field)
        address += 4


def prove_f32_row(slot):
    _orderings(slot, 70, 1.0e8, (slot.Result1, slot.Result2, slot.Result3))
    finish(slot)


def init_f32_row(slot):
    fill(0.0, df.select(70, 81))


def prove_f64_row(slot):
    _orderings(slot, 82, 1.0e30, (slot.Result1, slot.Result2, slot.Result3))
    finish(slot)


def init_f64_row(slot):
    fill(0.0, df.select(82, 93))


def prove_sum_absorbs(slot):
    with Rung(*once(slot)):
        copy(16777216.0, df[94])
        copy(1.0, df[95])
        copy(1.0, df[96])
        math(df.select(94, 96).sum(), df[97])
        math(df[97] - 16777216.0, slot.Result1)
    finish(slot)


def init_sum_absorbs(slot):
    fill(0.0, df.select(94, 97))


def prove_math_keeps_the_one(slot):
    with Rung(*once(slot)):
        copy(1.0e8, df[142])
        copy(1.0e30, df[143])
        math(df[142] + 1.0 - df[142], slot.Result1)
        math(df[143] + 1.0 - df[143], slot.Result2)
    finish(slot)


def init_math_keeps_the_one(slot):
    fill(0.0, df.select(142, 143))


group = Group(
    "real_sum",
    [
        case(
            "SUM of a DF range keeps the 1 beside 1e8 in any order: {1e8, 1, -1e8}, {1, 1e8, "
            "-1e8} and {1e8, -1e8, 1} each read 1 (Result1..3)",
            ladder=prove_f32_row,
            init=init_f32_row,
            expect={"Result1": 1, "Result2": 1, "Result3": 1},
            probe="the order and width of a Real SUM (see the table)",
        ),
        case(
            "SUM of the same orderings of {1e30, 1, -1e30}: a double left to right reads 0, 0, "
            "1, compensated 1, 1, 1 (Result1..3)",
            ladder=prove_f64_row,
            init=init_f64_row,
            expect={"Result1": between(0, 1), "Result2": between(0, 1), "Result3": between(0, 1)},
            probe="a compensated SUM or a plain one (pyrung adds plain)",
        ),
        case(
            "SUM of {16777216, 1, 1} keeps both 1s: the sum less 16777216 is 2 (Result1), 0 if "
            "each is absorbed in turn",
            ladder=prove_sum_absorbs,
            init=init_sum_absorbs,
            expect={"Result1": 2},
            probe="float32 largest first against a compensated SUM",
        ),
        case(
            "A Real Math's intermediate keeps a double's precision: 1e8 + 1.0 - 1e8 is 1 "
            "(Result1) and 1e30 + 1.0 - 1e30 is 0 (Result2)",
            ladder=prove_math_keeps_the_one,
            init=init_math_keeps_the_one,
            expect={"Result1": 1, "Result2": 0},
            probe="the precision of a Real Math's intermediates, not only their range",
        ),
    ],
)


def test_real_sum():
    assert_all_passed(run(group))
