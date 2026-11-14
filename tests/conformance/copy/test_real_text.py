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
- 66.7: "66.6999969", 10, then 11..20 of the span say what the round's '0' was.

pyrung writes the digits as ``to_text()`` does, from the Real's 64-bit value.  That gives CLICK's text
where the float32 and float64 digits agree, and "123456.7000000" and "66.7000000" where they
do not.  A CLICK read every case the same way on 2026-11-06 and 2026-11-09, and each case
expects the 10 characters it read (``CASES``).  The CLICK wrote "123456.7031250" for 123456.7
and left the TXTs past the digits alone, with no NUL after them.  pyrung's digits for 123456.7
differ, so ``six_digits_first`` and ``six_digits_past`` are pending ``F32``.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, math, to_ascii
from tests.conformance.memory import df, txt
from tests.conformance.probe_kit import once

#: The case's value, the window of its span it reads (0 the first 10 TXT, 1 the next 10), the
#: id, and the 10 characters a CLICK read there.  Each case's DF is DF21 on, and its span TXT101
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
FIELDS = ("Result1", "Result2", "Result3", "Result4", "ErrorCode")
SPAN = 20
WINDOW = 10
FIRST_DF = 21
FIRST_TXT = 101
BLANK = "\x00"


def _span(index: int) -> range:
    first = FIRST_TXT + SPAN * index
    return range(first, first + SPAN)


def _ladder(index: int, value: float, window: int) -> Any:
    real, span = df[FIRST_DF + index], _span(index)

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


def _init(index: int) -> Any:
    span = _span(index)

    def init(slot: Any) -> None:
        fill(BLANK, txt.select(span.start, span.stop - 1))
        copy(0.0, df[FIRST_DF + index])

    return init


def _sentence(value: float, window: int, read: str) -> str:
    shown = {1.5: "1.5", -0.25: "-0.25", 0.0: "0.0", 123456.7: "123456.7", 1.0e10: "1.0E+10"}
    shown[66.7] = "66.7"
    where = "TXT 1..10" if window == 0 else "TXT 11..20"
    return (
        f"A Copy of DF {shown[value]} into a TXT writes its float32 digits with 7 decimals and "
        f"leaves the TXTs after them alone, so {where} of a span filled with 'Z' read '{read}' "
        f"(ASCII codes, two a field, 128 * first + second)"
    )


def _codes(read: str) -> dict[str, int]:
    """The fields that hold *read*, two characters a field."""
    return {f: 128 * ord(read[2 * k]) + ord(read[2 * k + 1]) for k, f in enumerate(FIELDS)}


group = Group(
    "real_text",
    [
        case(
            _sentence(value, window, read),
            ladder=_ladder(index, value, window),
            init=_init(index),
            id=name,
            expect=_codes(read),
            pending=PENDING.get(name),
        )
        for index, (value, window, name, read) in enumerate(CASES)
    ],
)


def test_real_text():
    assert_all_passed(run(group))
