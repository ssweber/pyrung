"""Check the stores that a CLICK was read making in programs of their own: the ``plc_stores``
group (part ``follow4_reads``).

A CLICK C2-03CPU on firmware 4.00 ran each of these stores in a short program written for the
question, and pyrung was changed to match.  No case here has run on a CLICK yet.  Each case
does all its work on the scan it fires (``probe_kit.once``).  A coil reads SC43 on the rung
right after each store, so no later instruction can clear the flag first.

- ``fraction_into_word`` copies the constants 1.5, 2.5, -1.5 and 1.4 into DHs that hold 0, and
  copies each DH into a Result.  A CLICK drops the fraction toward zero and keeps the low 16
  bits, so the Results read 1, 2, -1 and 1.  A Word copied into an Int keeps its bits, so
  FFFFh reads -1.  Rounding half to even would read 2, 2, -2 and 1.  Rounding half away from
  zero would read 2, 3, -2 and 1.  Rounding down would read 1, 2, -2 and 1.  A refused Copy
  leaves 0.
- ``copy_non_finite_into_dd`` copies a DF holding NaN, then +Inf, then -Inf, into DDs preset
  to 99.  Result1..3 hold a class of each DD: 1 for -2147483648, 2 for 2147483647, 3 for 0,
  4 for 99 (not written) and 0 for any other value.  A CLICK reads 1, 2 and 1.  Result4 holds
  SC43 after each Copy as bits 1, 2 and 4, and a CLICK raises none (0).  ErrorCode is 1 when
  the NaN DF is neither >= 0 nor < 0, the control that shows it holds a NaN.
- ``math_non_finite_into_ints`` runs a Math of -Inf into a DS preset to 99 (Result1), and a
  Math of NaN and of -Inf into DDs preset to 99 (Result2 and Result3, classed as above).  A
  CLICK writes 0 each time, so 0, 3 and 3.  Result4 holds SC43 after each Math as bits 1, 2 and
  4.  A CLICK raises it for -Inf only, so 5.  ``faults/non_finite_math_into_int`` reads the
  other Math stores of NaN and +Inf.
- ``pack_text_past_range`` packs three texts into DFs preset to 9.0, with a coil on SC43
  after each.  "3.40282352E38" is past the Real range.  A CLICK stores +Inf with no SC43.
  Result1 is 1 when half the DF is still past 3.0E+38 (+Inf), 2 when the DF holds 9.0, and
  0 otherwise.  The largest finite Real halves to about 1.7E+38, so it reads 0.  A blank text
  raises SC43 and writes nothing (Result2 = 1 for 9.0, 2 for 0.0).  "1.5" then packs 1.5
  (Result3 = 1, 2 for 9.0) and turns SC43 off.  Result4 holds the three coils as bits 1, 2 and
  4, so a CLICK reads 2.

A blank text's SC43 also stays on into the next scan, until an instruction that runs without
an error turns it off.  No case here can read that.  Main's Copy of the layout hash runs at the
top of each scan, and a Copy turns SC43 off.  ``tests/core/test_click_text_semantics.py`` checks
pyrung's answer.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, out, pack_text, reset, system
from tests.conformance.memory import c, dd, df, dh, txt
from tests.conformance.probe_kit import class_of, finish, once

SC43 = system.fault.out_of_range
#: The classes of a DD: its least value, its greatest, 0, and the 99 it was preset to.
DD_CLASSES = (-2147483648, 2147483647, 0, 99)
#: Text just past the Real range.
PAST_RANGE = "3.40282352E38"


def _flag_bits(slot: Any, coils: tuple[Any, ...], into: Any) -> None:
    for bit, coil in zip((1, 2, 4), coils, strict=True):
        with Rung(*once(slot), coil):
            math(into + bit, into)


def _stage_non_finite(inf: Any, minus_inf: Any, nan: Any) -> None:
    copy(3.0e38, inf)
    math(inf * 10, inf)
    math(inf * -1, minus_inf)
    math(inf - inf, nan)


def prove_fraction_into_word(slot: Any) -> None:
    fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4)
    for value, address, field in zip((1.5, 2.5, -1.5, 1.4), range(131, 135), fields, strict=True):
        with Rung(*once(slot)):
            copy(value, dh[address])
            copy(dh[address], field)
    finish(slot)


def init_fraction_into_word(slot: Any) -> None:
    fill(0, dh.select(131, 134))


def prove_copy_non_finite_into_dd(slot: Any) -> None:
    inf, minus_inf, nan = df[121], df[122], df[123]
    dints = (dd[211], dd[212], dd[213])
    coils = (c[721], c[722], c[723])
    with Rung(*once(slot)):
        _stage_non_finite(inf, minus_inf, nan)
        fill(99, dd.select(211, 213))
    for source, dint, coil in zip((nan, inf, minus_inf), dints, coils, strict=True):
        with Rung(*once(slot)):
            copy(source, dint)
        with Rung(*once(slot), SC43):
            out(coil)
    for dint, field in zip(dints, (slot.Result1, slot.Result2, slot.Result3), strict=True):
        class_of(slot, dint, DD_CLASSES, field)
    _flag_bits(slot, coils, slot.Result4)
    with Rung(*once(slot)):
        copy(1, slot.ErrorCode)
    with Rung(*once(slot), nan >= 0):
        copy(0, slot.ErrorCode)
    with Rung(*once(slot), nan < 0):
        copy(0, slot.ErrorCode)
    finish(slot)


def init_copy_non_finite_into_dd(slot: Any) -> None:
    reset(c.select(721, 723))
    fill(0, dd.select(211, 213))
    fill(0.0, df.select(121, 123))


def prove_math_non_finite_into_ints(slot: Any) -> None:
    inf, minus_inf, nan = df[124], df[125], df[126]
    coils = (c[724], c[725], c[726])
    with Rung(*once(slot)):
        _stage_non_finite(inf, minus_inf, nan)
        copy(99, slot.Result1)
        fill(99, dd.select(214, 215))
    for source, dest, coil in zip(
        (minus_inf, nan, minus_inf), (slot.Result1, dd[214], dd[215]), coils, strict=True
    ):
        with Rung(*once(slot)):
            math(source, dest)
        with Rung(*once(slot), SC43):
            out(coil)
    class_of(slot, dd[214], DD_CLASSES, slot.Result2)
    class_of(slot, dd[215], DD_CLASSES, slot.Result3)
    _flag_bits(slot, coils, slot.Result4)
    finish(slot)


def init_math_non_finite_into_ints(slot: Any) -> None:
    reset(c.select(724, 726))
    fill(0, dd.select(214, 215))
    fill(0.0, df.select(124, 126))


def prove_pack_text_past_range(slot: Any) -> None:
    over, blank, good, half = df[127], df[128], df[129], df[130]
    coils = (c[727], c[728], c[729])
    # Each Copy and Fill takes two of a CLICK rung's 32 rows, so the 16 Copies and the Fill need two
    # rungs.
    with Rung(*once(slot)):
        for offset, char in enumerate(PAST_RANGE):
            copy(char, txt[411 + offset])
    with Rung(*once(slot)):
        fill(9.0, df.select(127, 129))
        for offset, char in enumerate("1.5"):
            copy(char, txt[441 + offset])
    for source, dest, coil in zip(
        (txt.select(411, 423), txt.select(431, 433), txt.select(441, 443)),
        (over, blank, good),
        coils,
        strict=True,
    ):
        with Rung(*once(slot)):
            pack_text(source, dest)
        with Rung(*once(slot), SC43):
            out(coil)
    with Rung(*once(slot)):
        math(over * 0.5, half)
    with Rung(*once(slot), half > 3.0e38):
        copy(1, slot.Result1)
    with Rung(*once(slot), over == 9.0):
        copy(2, slot.Result1)
    class_of(slot, blank, (9.0, 0.0), slot.Result2)
    class_of(slot, good, (1.5, 9.0), slot.Result3)
    _flag_bits(slot, coils, slot.Result4)
    finish(slot)


def init_pack_text_past_range(slot: Any) -> None:
    reset(c.select(727, 729))
    fill(0.0, df.select(127, 130))
    fill("\x00", txt.select(411, 423))
    fill("\x00", txt.select(431, 433))
    fill("\x00", txt.select(441, 443))


group = Group(
    "plc_stores",
    [
        case(
            "A Copy of a constant with a fraction into a DH drops the fraction toward zero and "
            "keeps 16 bits: 1.5 is 1 (Result1), 2.5 is 2 (Result2), -1.5 is FFFFh, which "
            "reads -1 (Result3), and 1.4 is 1 (Result4)",
            ladder=prove_fraction_into_word,
            init=init_fraction_into_word,
            expect={"Result1": 1, "Result2": 2, "Result3": -1, "Result4": 1},
        ),
        case(
            "A Copy of a DF into a DD stores -2147483648 for NaN (Result1 = 1), 2147483647 for "
            "+Inf (Result2 = 2) and -2147483648 for -Inf (Result3 = 1), with no SC43 after any "
            "(Result4 = 0). The NaN is neither >= 0 nor < 0 (ErrorCode = 1)",
            ladder=prove_copy_non_finite_into_dd,
            init=init_copy_non_finite_into_dd,
            expect={"Result1": 1, "Result2": 2, "Result3": 1, "Result4": 0, "ErrorCode": 1},
        ),
        case(
            "A Math of -Inf into a DS writes 0 (Result1). A Math of NaN (Result2 = 3) and of "
            "-Inf (Result3 = 3) into a DD writes 0. SC43 follows -Inf and not NaN (Result4 = 5)",
            ladder=prove_math_non_finite_into_ints,
            init=init_math_non_finite_into_ints,
            expect={"Result1": 0, "Result2": 3, "Result3": 3, "Result4": 5},
        ),
        case(
            'A pack_text of "3.40282352E38" into a DF stores +Inf (Result1 = 1) with no SC43. '
            'A blank text leaves the DF (Result2 = 1) and raises SC43. A pack_text of "1.5" '
            "stores 1.5 (Result3 = 1) and turns SC43 off (Result4 = 2)",
            ladder=prove_pack_text_past_range,
            init=init_pack_text_past_range,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 2},
        ),
    ],
)


def test_plc_stores():
    assert_all_passed(run(group))
