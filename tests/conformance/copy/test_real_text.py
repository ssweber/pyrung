"""What a Copy of a Real into a TXT writes: the ``real_text`` group (part ``follow_values``).

``store_probe/real_into_text_and_fill`` read "66.6999969" from TXT541 for a DF of 66.7, then '0'
in TXT551..TXT555.  Those '0's were left over, since a CLICK leaves the TXTs past the digits alone.

Each case has a DF and 20 TXT of its own.  On one scan it fills the 20 with 'Z', copies its value
into the DF and the DF into the first TXT, and reads 10 of the 20: two characters a field, as ASCII
codes, ``128 * first + second`` (Result1 the first two, ..., ErrorCode the last two).  A case named
``..._first`` reads TXT 1..10 of its span, ``..._past`` TXT 11..20.  A 'Z' (90) is a TXT the Copy
left alone, a NUL (0) one it blanked.  "Real Numbering" with 7 decimals, as CLICK's Copy dialog
shows ("10000.0000000"), on the float32 value, writes:

- 1.5: "1.5000000", 9 characters.
- -0.25: "-0.2500000", 10.
- 0.0: "0.0000000", 9.
- 123456.7: "123456.7031250", 14 (float32 123456.703125).
- 1.0E+10: "10000000000.0000000", 19.  Exponential would be "1.0000000E+10", 13.
- 66.7: "66.6999969", 10, then 11..20 of the span stay as the init left them.

pyrung writes the digits as ``to_text()`` does, from the Real's 64-bit value.  That gives CLICK's text
where the float32 and float64 digits agree, and "123456.7000000" and "66.7000000" where they
do not.  Each case expects the 10 characters a CLICK writes (``CASES``).  A CLICK writes
"123456.7031250" for 123456.7 and leaves the TXTs past the digits alone, with no NUL after them.  pyrung's digits for 123456.7
differ, so ``six_digits_first`` and ``six_digits_past`` are pending ``F32``.

Nobody has read the text of a Real near 3.4E+38 off a CLICK (``LONGEST``).  The most negative
Real, -3.4028235E+38, writes the longest text of any Real in fixed notation.  These cases have a DF
and 50 TXT each.  ``most_negative_first`` reads TXT 1..10 of its span and ``most_negative_end`` TXT
41..50.  The candidate answers:

- The float32 digits, as for 123456.7.  "-340282346638528859811704183484516925440.0000000" is
  48 characters.  Its first ten read "-340282346" and its last ten ".0000000ZZ".  Each case
  expects these.
- pyrung's 64-bit digits.  "-340282349999999991754788743781432688640.0000000" is 48 characters.
  Its first ten read "-340282349" and its last ten ".0000000ZZ".  So ``most_negative_first``
  is pending ``F32``.
- Exponential.  "-3.4028235E+38" is 14 characters.  Its first ten read "-3.4028235" and TXT
  41..50 read "ZZZZZZZZZZ".
- No text at all.  Both windows read "ZZZZZZZZZZ".

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, to_ascii
from tests.conformance.memory import df, txt
from tests.conformance.probe_kit import once

#: The case's value, the window of its span it reads (0 the first 10 TXT, 1 the next 10), the
#: id, and the 10 characters a CLICK writes there.  Each case's DF is DF21 on, and its span TXT101
#: on, 20 a case.
CASES: list[tuple[float, int, str, str]] = [
    (1.5, 0, "one_and_a_half_first", "1.5000000Z"),
    (1.5, 1, "one_and_a_half_past", "ZZZZZZZZZZ"),
    (-0.25, 0, "minus_quarter_first", "-0.2500000"),
    (-0.25, 1, "minus_quarter_past", "ZZZZZZZZZZ"),
    (0.0, 0, "zero_first", "0.0000000Z"),
    (123456.7, 0, "six_digits_first", "123456.703"),
    (123456.7, 1, "six_digits_past", "1250ZZZZZZ"),
    (1.0e10, 0, "ten_billion_first", "1000000000"),
    (1.0e10, 1, "ten_billion_past", "0.0000000Z"),
    (66.7, 1, "round_value_past", "ZZZZZZZZZZ"),
]
#: pyrung writes 123456.7's digits from its 64-bit value, "123456.7000000".
PENDING = {"six_digits_first": "F32", "six_digits_past": "F32"}
#: The cases of the most negative Real, as in ``CASES``.  Each case's DF is DF33 on, and its
#: span TXT501 on, 50 a case.
MOST_NEGATIVE = -3.4028235e38
LONGEST: list[tuple[float, int, str, str]] = [
    (MOST_NEGATIVE, 0, "most_negative_first", "-340282346"),
    (MOST_NEGATIVE, 4, "most_negative_end", ".0000000ZZ"),
]
#: pyrung writes the most negative Real's digits from its 64-bit value, "-340282349...".
LONGEST_PENDING = {"most_negative_first": "F32"}
FIELDS = ("Result1", "Result2", "Result3", "Result4", "ErrorCode")
SPAN = 20
LONGEST_SPAN = 50
WINDOW = 10
FIRST_DF = 21
FIRST_TXT = 101
LONGEST_DF = 33
LONGEST_TXT = 501
BLANK = "\x00"


def _span(index: int) -> range:
    first = FIRST_TXT + SPAN * index
    return range(first, first + SPAN)


def _longest_span(index: int) -> range:
    first = LONGEST_TXT + LONGEST_SPAN * index
    return range(first, first + LONGEST_SPAN)


def _ladder(real: Any, span: range, value: float, window: int) -> Any:
    def ladder(slot: Any) -> None:
        with Rung(*once(slot)):
            copy(value, real)
            for start in range(span.start, span.stop, 8):
                copy("Z" * min(8, span.stop - start), txt[start])
            copy(real, txt[span.start])
        fields = (slot.Result1, slot.Result2, slot.Result3, slot.Result4, slot.ErrorCode)
        first = span.start + WINDOW * window
        with Rung(*once(slot)):
            for k, field in enumerate(fields):
                copy(txt[first + 2 * k], field, convert=to_ascii)
                copy(txt[first + 2 * k + 1], slot.Scratch, convert=to_ascii)
                math(field * 128 + slot.Scratch, field)
            copy(1, slot.Fired)

    return ladder


def _init(real: Any, span: range) -> Any:
    def init(slot: Any) -> None:
        fill(BLANK, txt.select(span.start, span.stop - 1))
        copy(0.0, real)

    return init


def _where(window: int) -> str:
    return f"TXT {WINDOW * window + 1}..{WINDOW * window + WINDOW}"


def _sentence(value: float, window: int, read: str) -> str:
    shown = {1.5: "1.5", -0.25: "-0.25", 0.0: "0.0", 123456.7: "123456.7", 1.0e10: "1.0E+10"}
    shown[66.7] = "66.7"
    where = _where(window)
    return (
        f"A Copy of DF {shown[value]} into a TXT writes its float32 digits with 7 decimals and "
        f"leaves the TXTs after them alone, so {where} of a span filled with 'Z' read '{read}' "
        f"(ASCII codes, two a field, 128 * first + second)"
    )


def _longest_sentence(window: int, read: str) -> str:
    return (
        "A Copy of DF -3.4028235E+38 into a TXT writes 48 characters in fixed notation and leaves "
        f"the TXTs after them alone, so {_where(window)} of a span filled with 'Z' read '{read}' "
        "(ASCII codes, two a field, 128 * first + second)"
    )


def _codes(read: str) -> dict[str, int]:
    """The fields that hold *read*, two characters a field."""
    return {f: 128 * ord(read[2 * k]) + ord(read[2 * k + 1]) for k, f in enumerate(FIELDS)}


group = Group(
    "real_text",
    [
        *(
            case(
                _sentence(value, window, read),
                ladder=_ladder(df[FIRST_DF + index], _span(index), value, window),
                init=_init(df[FIRST_DF + index], _span(index)),
                id=name,
                expect=_codes(read),
                pending=PENDING.get(name),
            )
            for index, (value, window, name, read) in enumerate(CASES)
        ),
        *(
            case(
                _longest_sentence(window, read),
                ladder=_ladder(df[LONGEST_DF + index], _longest_span(index), value, window),
                init=_init(df[LONGEST_DF + index], _longest_span(index)),
                id=name,
                expect=_codes(read),
                probe="the text of a Real near 3.4E+38: how long, which digits",
                pending=LONGEST_PENDING.get(name),
            )
            for index, (value, window, name, read) in enumerate(LONGEST)
        ),
    ],
)


def test_real_text():
    assert_all_passed(run(group))
