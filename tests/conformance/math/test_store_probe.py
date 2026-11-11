"""S6 store probes: the ``store_probe`` group (subroutine ``Conf_StoreProbe``).

The last group of part ``math`` (``suite.PARTS``): the editor may refuse a RAND bound form at paste.  What a RAND, a Math of NaN, a
Copy of an infinity stores and which fault flag follows, for the
stores the ``bob/stores`` arc chose without a reading.  Each case probes as the
``faults`` group does (an op once, on the first scan from 3 on which the flag
it reads is off just before it, a flag after an op read by running it again;
every case has a 40-scan window).  A random draw asserts its range, not a
value.  Expectations are what pyrung reads today, so a CLICK that reads
otherwise is an answer; ``non_finite_into_word`` and ``real_into_text_and_fill``
are what a CLICK read on 2026-11-05.

- ``rand_int_cuts`` (probe): what a RAND into an Int does with constant bounds
  past 16 bits.  A CLICK draws over the whole bounds, and the Int keeps the low
  16 bits of the draw, signed, with SC43 (2026-11-11).  RAND ( 65000 , 66000 )
  is -536..464 (Result1).  RAND ( 70000 , 4464 ) and RAND ( 70000 , 135536 )
  can be any Int (Result2, Result3), and reversed bounds are no error.  None
  raises SC46 (Result4 = 0).  Each draw past 32767 raises SC43 (ErrorCode bits
  1, 2 and 4).  The first and third always are, and the second is unless it
  draws 4464..32767, so ErrorCode is 7 or 5.  The CLICK drew 65284, 53040 and
  97521, which read -252, -12496 and 31985 with ErrorCode 7.  Bounds cut to 16
  bits, signed, read 0 for the second and third, equal after the cut, with
  SC46 (Result4 = 6) and no SC43.  A draw that clamps reads 32767 or -32768.
- ``rand_other_stores`` (probe): RAND ( 70000 , 70002 ) into a Dint is not cut,
  70000..70002 (Result1 = 0..2 over 70000), into a Word the exported hex
  bounds are 1170h..1172h, 4464..4466 (Result2), and into a Real 70000..70002
  (Result3 = 0..2 over 70000).  RAND ( 70000 , 70000 ) into a Dint is a math
  error, 0 (Result4; 32767 if it draws 70000).  Flags in ErrorCode: SC43 after
  the Dint (bit 1), the Word (bit 2), the Real (bit 4), and SC46 and SC43 after
  the equal bounds (bits 8 and 16).  Answered on a CLICK (2026-11-05): the Word's
  draw raises no SC43, only the equal bounds' SC46 (ErrorCode = 8).
- ``rand_tag_bounds`` (probe): two Dint tags holding 7 and 7 as bounds draw 7
  with no flag (Result1 = 7; 0 if equal tags are an error), randint ( df , df )
  + 5 with DF 3.0 reads 5 (Result2: a Real bound reads 0 and the 5 still
  lands, without a flag), and equal constants zero the whole calc, 0 (Result3)
  with SC46 (Result4 = 4).  RAND ( 7 , 7 ) + 5 reading 5 puts the 0 on the
  RAND alone.  Answered on a CLICK (2026-11-05): equal tags are a math error, 0
  with SC46, and RAND ( 7 , 7 ) + 5 is 5 with SC46 (Result4 = 5).
- ``inf_and_nan_to_text`` (probe): -Inf and NaN copied as text (Copy Option 4c),
  as ``faults/copy_inf_to_text`` does +Inf (``inf``, no flag).  A text register
  seeded ``Z``, then the copy, then each pair of TXT codes packed as 128 *
  first + second: -Inf reads ``-inf`` (45 * 128 + 105 = 5865, 110 * 128 + 102 =
  14182 in Result1, Result2), NaN reads ``nan`` and a NUL (110 * 128 + 97 =
  14177, 110 * 128 = 14080 in Result3, Result4), neither with SC43, and the
  NUL after ``-inf`` (TXT525) is the rest of ErrorCode: SC43 bits 1 and 2 plus 4 *
  its code, 0 in all.
- ``non_finite_into_word`` (probe): a Math of NaN into a Dint preset 99 writes 0
  (Result1 = 1; 2 is the preset left, 3 and 4 the Dint's limits, 5 any other)
  and a Copy of +Inf into a Word preset 99 writes 65535 (Result2 = 3; 1 is 0, 2
  preset, 4 is 32768) and of -Inf 32768 (Result3 = 4), none raising a flag
  (Result4 = 0: SC43 and SC46 after the Math are bits 1 and 2, SC43 after the
  Copies bits 4 and 8).  A Math of NaN or an infinity into a Word, and a Math
  of an infinity into one, are not probed: CLICK's Math takes Word operands in
  hex mode only, which a Real cannot enter (CLK_MATH_MODE_MIXED).
- ``real_into_text_and_fill`` (probe): a finite Real copied into a TXT as it is,
  66.7 over a seeded ``Z``, writes its digits, as ``to_text()`` does: the first
  character's code is 54 ('6', Result1; the number kept would read 0) and a
  plain Copy of the TXT into an Int reads the digit, 6 (Result2; the number
  would read 66, rounded 67, and a refused or failed copy leaves ``Z``, 90).
  The text is ten characters ("66.6999969" on a CLICK, a float32; pyrung's is
  "66.7000000"), so it starts at TXT851.  A Copy of 40000 into an Int clamps to
  32767 (Result3) and raises SC43 (bit 2 of Result4); the Real copy raises
  none (bit 1).

Not expressible on a CLICK, so not probed: a Copy whose source is an
expression (CLK_EXPR_ONLY_IN_MATH; the clamp of an expression is a Math, which
wraps), a Math of NaN or an infinity into a Word (hex mode), run_function and a
Modbus receive store (no instruction here reaches them).

The cases own C1661..C1700 and C1781..C1800, DD611..DD640, DF421..DF430, DH411..DH416,
TXT521..TXT545, TXT851..TXT865 and DS4499..DS4500 (``memory.OWNED``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, latch, math, out, reset, system, to_ascii, to_text
from pyrung.core.expression import randint
from tests.conformance.memory import c, dd, df, dh, ds, txt
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 40
SCANS = scans_for(WINDOW)
STAGE_SCAN = 2
SC43 = system.fault.out_of_range
SC46 = system.fault.math_operation_error
NUL = "\x00"

_BITS = [*range(1661, 1701), *range(1781, 1801)]
#: C bits per case (two a probe), in case order.
_SIZES = [12, 10, 6, 6, 4, 8, 4]
_FIRST = [sum(_SIZES[:i]) for i in range(len(_SIZES))]


def _case_bits(index):
    return _BITS[_FIRST[index] : _FIRST[index] + _SIZES[index]]


class Probes:
    """Hands out case *index*'s (now, taken) bit pairs, in order."""

    def __init__(self, index: int) -> None:
        self.free = iter(_case_bits(index))

    def __call__(
        self, slot: Any, op: Callable[[], None], flag: Any, field: Any, bit: int = 1
    ) -> None:
        now, taken = c[next(self.free)], c[next(self.free)]
        with Rung(slot.Scratch > STAGE_SCAN, slot.Scratch <= WINDOW, ~taken, ~flag):
            out(now)
            latch(taken)
        with Rung(now):
            op()
        with Rung(now, flag):
            math(field + bit, field)


def _stage(slot, *instructions):
    with Rung(slot.Scratch == STAGE_SCAN):
        for instruction in instructions:
            instruction()


def _end(slot):
    """The scans after the window: read the destinations back (idempotent).

    Not gated on ``Cmd`` or ``Fired``: ``Scratch`` is 0 while idle, and a compare
    contact costs 3 steps of the part's budget.
    """
    return (slot.Scratch > WINDOW,)


def _init(index, *, dds=(), dfs=(), dhs=(), txts=(), dss=()):
    def init(slot):
        for bit in _case_bits(index):
            reset(c[bit])
        for first, last in dds:
            fill(0, dd.select(first, last))
        for first, last in dss:
            fill(0, ds.select(first, last))
        for first, last in dfs:
            fill(0.0, df.select(first, last))
        for first, last in dhs:
            fill(0, dh.select(first, last))
        for first, last in txts:
            fill(NUL, txt.select(first, last))

    return init


def prove_rand_int_cuts(slot):
    probe = Probes(0)
    count_scans(slot, WINDOW)

    def op(dest, lo, hi):
        def run_op():
            copy(99, dest)
            math(randint(lo, hi), dest)

        return run_op

    ops = [
        (op(slot.Result1, 65000, 66000), 1),
        (op(slot.Result2, 70000, 4464), 2),
        (op(slot.Result3, 70000, 135536), 4),
    ]
    for run_op, bit in ops:
        probe(slot, run_op, SC46, slot.Result4, bit)
        probe(slot, run_op, SC43, slot.ErrorCode, bit)
    done(slot, WINDOW)


def prove_rand_other_stores(slot):
    probe = Probes(1)
    count_scans(slot, WINDOW)
    wide, word, real, equal = dd[611], dh[411], df[421], dd[612]

    def dint():
        copy(99, wide)
        math(randint(70000, 70002), wide)

    def word_op():
        copy(99, word)
        math(randint(70000, 70002), word)

    def real_op():
        copy(99.0, real)
        math(randint(70000, 70002), real)

    def equal_op():
        copy(99, equal)
        math(randint(70000, 70000), equal)

    probe(slot, dint, SC43, slot.ErrorCode, 1)
    probe(slot, word_op, SC43, slot.ErrorCode, 2)
    probe(slot, real_op, SC43, slot.ErrorCode, 4)
    probe(slot, equal_op, SC46, slot.ErrorCode, 8)
    probe(slot, equal_op, SC43, slot.ErrorCode, 16)
    with Rung(*_end(slot)):
        math(wide - 70000, slot.Result1)
        copy(word, slot.Result2)
        math(real - 70000, slot.Result3)
        copy(equal, slot.Result4)
    done(slot, WINDOW)


def prove_rand_tag_bounds(slot):
    probe = Probes(2)
    count_scans(slot, WINDOW)
    low, high, real = dd[613], dd[614], df[422]
    _stage(slot, lambda: copy(7, low), lambda: copy(7, high), lambda: copy(3.0, real))

    def tags():
        copy(99, slot.Result1)
        math(randint(low, high), slot.Result1)

    def real_bounds():
        copy(99, slot.Result2)
        math(randint(real, real) + 5, slot.Result2)

    def constants():
        copy(99, slot.Result3)
        math(randint(7, 7) + 5, slot.Result3)

    probe(slot, tags, SC46, slot.Result4, 1)
    probe(slot, real_bounds, SC46, slot.Result4, 2)
    probe(slot, constants, SC46, slot.Result4, 4)
    done(slot, WINDOW)


def _code_pair(first, second, into, scratch):
    """*into* = 128 * the code of text *first* + the code of text *second*."""
    copy(first, scratch[0], convert=to_ascii)
    copy(second, scratch[1], convert=to_ascii)
    math(scratch[0] * 128 + scratch[1], into)


def prove_inf_and_nan_to_text(slot):
    probe = Probes(4)
    count_scans(slot, WINDOW)
    minus_inf, nan, plus_inf = df[423], df[424], df[425]
    _stage(
        slot,
        lambda: copy(3.0e38, plus_inf),
        lambda: math(plus_inf * 10, plus_inf),
        lambda: math(plus_inf * -1, minus_inf),
        lambda: math(plus_inf - plus_inf, nan),
    )
    scratch = (dd[621], dd[622])
    flags, last = dd[626], dd[627]

    def minus_op():
        copy("Z", txt[521])
        copy(minus_inf, txt[521], convert=to_text())

    def nan_op():
        copy("Z", txt[531])
        copy(nan, txt[531], convert=to_text())

    probe(slot, minus_op, SC43, flags, 1)
    probe(slot, nan_op, SC43, flags, 2)
    with Rung(*_end(slot)):
        _code_pair(txt[521], txt[522], slot.Result1, scratch)
    with Rung(*_end(slot)):
        _code_pair(txt[523], txt[524], slot.Result2, scratch)
    with Rung(*_end(slot)):
        _code_pair(txt[531], txt[532], slot.Result3, scratch)
    with Rung(*_end(slot)):
        _code_pair(txt[533], txt[534], slot.Result4, scratch)
    with Rung(*_end(slot)):
        copy(txt[525], last, convert=to_ascii)
        math(flags + last * 4, slot.ErrorCode)
    done(slot, WINDOW)


def _class_of(value, readings, into, end):
    """*into* = the first reading whose value *value* equals, 5 if none."""
    with Rung(*end):
        copy(5, into)
    for number, reading in readings:
        with Rung(*end, value == number):
            copy(reading, into)


def prove_non_finite_into_word(slot):
    probe = Probes(5)
    count_scans(slot, WINDOW)
    nan, plus_inf, minus_inf = df[426], df[427], df[428]
    dint, word_up, word_down = dd[623], dh[412], dh[413]
    _stage(
        slot,
        lambda: copy(3.0e38, plus_inf),
        lambda: math(plus_inf * 10, plus_inf),
        lambda: math(plus_inf * -1, minus_inf),
        lambda: math(plus_inf - plus_inf, nan),
    )

    def nan_dint():
        copy(99, dint)
        math(nan, dint)

    def inf_up():
        copy(99, word_up)
        copy(plus_inf, word_up)

    def inf_down():
        copy(99, word_down)
        copy(minus_inf, word_down)

    probe(slot, nan_dint, SC43, slot.Result4, 1)
    probe(slot, nan_dint, SC46, slot.Result4, 2)
    probe(slot, inf_up, SC43, slot.Result4, 4)
    probe(slot, inf_down, SC43, slot.Result4, 8)
    end = _end(slot)
    dint_readings = ((0, 1), (99, 2), (2147483647, 3), (-2147483648, 4))
    word_readings = ((0, 1), (99, 2), (65535, 3), (32768, 4))
    _class_of(dint, dint_readings, slot.Result1, end)
    _class_of(word_up, word_readings, slot.Result2, end)
    _class_of(word_down, word_readings, slot.Result3, end)
    done(slot, WINDOW)


def prove_real_into_text_and_fill(slot):
    probe = Probes(6)
    count_scans(slot, WINDOW)
    real = df[429]
    _stage(slot, lambda: copy(66.7, real))

    def real_to_text():
        copy("Z", txt[851])
        copy(real, txt[851])

    def clamping_copy():
        copy(40000, ds[4499])

    probe(slot, real_to_text, SC43, slot.Result4, 1)
    probe(slot, clamping_copy, SC43, slot.Result4, 2)
    with Rung(*_end(slot)):
        copy(txt[851], slot.Result1, convert=to_ascii)
        copy(txt[851], slot.Result2)
        copy(ds[4499], slot.Result3)
    done(slot, WINDOW)


group = Group(
    "store_probe",
    [
        case(
            "RAND into an Int draws over the whole bounds, and the Int keeps the low 16 "
            "bits of the draw. RAND ( 65000 , 66000 ) is -536..464 (Result1). RAND ( 70000 "
            ", 4464 ) and RAND ( 70000 , 135536 ) can be any Int (Result2, Result3). None "
            "raises SC46 (Result4 = 0). A draw past 32767 raises SC43, so ErrorCode is 5 "
            "or 7.",
            ladder=prove_rand_int_cuts,
            init=_init(0),
            expect={
                "Result1": range(-536, 465),
                "Result2": range(-32768, 32768),
                "Result3": range(-32768, 32768),
                "Result4": 0,
                "ErrorCode": range(5, 8, 2),
            },
            scans=SCANS,
            probe="RAND into an Int: bounds kept whole, the draw wrapped",
        ),
        case(
            "RAND ( 70000 , 70002 ) is not cut for a Dint, a Word or a Real: a Dint draws "
            "70000..70002 (Result1 = 0..2 over 70000), a Word 4464..4466 (Result2), a Real "
            "70000..70002 (Result3 = 0..2); RAND ( 70000 , 70000 ) into a Dint is a math "
            "error, 0 (Result4), raising SC46 but not SC43, and the Word's draw raises no "
            "SC43 (ErrorCode = 8)",
            ladder=prove_rand_other_stores,
            init=_init(1, dds=[(611, 612)], dfs=[(421, 421)], dhs=[(411, 411)]),
            expect={
                "Result1": range(0, 3),
                "Result2": range(4464, 4467),
                "Result3": range(0, 3),
                "Result4": 0,
                "ErrorCode": 8,
            },
            scans=SCANS,
            probe="RAND into a Dint, a Word and a Real; equal bounds into a Dint",
        ),
        case(
            "RAND of two integer tags holding the same value is a math error, 0 (Result1) "
            "with SC46; randint ( df , df ) + 5 gives 5 with no flag (Result2); a math "
            "error zeroes only the RAND term, RAND ( 7 , 7 ) + 5 is 5 (Result3) with SC46 "
            "(Result4 = 5)",
            ladder=prove_rand_tag_bounds,
            init=_init(2, dds=[(613, 614)], dfs=[(422, 422)]),
            expect={"Result1": 0, "Result2": 5, "Result3": 5, "Result4": 5},
            scans=SCANS,
            probe="RAND with equal integer tags, a DF bound in a calc, equal constants in a calc",
        ),
        case(
            "-Inf and NaN copied as text write -inf and nan with no flag: the TXT codes "
            "packed 128 * first + second read 5865 and 14182 for -inf (Result1, Result2), "
            "14177 and 14080 for nan and a NUL (Result3, Result4), SC43 off (ErrorCode = 0)",
            ladder=prove_inf_and_nan_to_text,
            init=_init(4, dds=[(621, 622), (626, 627)], dfs=[(423, 425)], txts=[(521, 540)]),
            expect={
                "Result1": 5865,
                "Result2": 14182,
                "Result3": 14177,
                "Result4": 14080,
                "ErrorCode": 0,
            },
            scans=SCANS,
            probe="Copy of -Inf and NaN to text",
        ),
        case(
            "A Math of NaN into a Dint writes 0 (Result1 = 1), a Copy of +Inf into a Word "
            "writes 65535 (Result2 = 3) and of -Inf 32768 (Result3 = 4), none raising SC43 "
            "or SC46 (Result4 = 0)",
            ladder=prove_non_finite_into_word,
            init=_init(5, dds=[(623, 623)], dfs=[(426, 428)], dhs=[(412, 413)]),
            expect={"Result1": 1, "Result2": 3, "Result3": 4, "Result4": 0},
            scans=SCANS,
            probe="Math of NaN into a Dint, Copy of +/-Inf into a Word",
        ),
        case(
            "A finite Real copied into a TXT writes its digits: 66.7 reads text code 54, '6' "
            "(Result1), and 6 as a plain Copy into an Int (Result2); a Copy of 40000 into "
            "an Int clamps to 32767 (Result3) and raises SC43, the Real copy none (Result4 = 2)",
            ladder=prove_real_into_text_and_fill,
            init=_init(
                6, dfs=[(429, 429)], dhs=[(414, 415)], txts=[(851, 865)], dss=[(4499, 4499)]
            ),
            expect={"Result1": 54, "Result2": 6, "Result3": 32767, "Result4": 2},
            scans=SCANS,
            probe="finite Real copied into a TXT; Copy that clamps",
        ),
    ],
)


def test_store_probe():
    assert_all_passed(run(group))
