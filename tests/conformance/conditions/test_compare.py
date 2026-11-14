"""Compares across types, and a TXT against a 2-char literal.

An expectation is what a CLICK read on 2026-11-05.

``compare``, the last group of its part:

- ``word_and_wide_compare``: a Word is unsigned.  DH 0xFFFF > 7FFFh (Result1) reads 1, and 0 if
  the Word reads as signed -1.  DH 0xFFFF == FFFFh (Result2) reads 1, and 0 if the constant reads
  as -1.  A DS against a constant past its width, DS 5000 < 70000 (Result3), reads 1 for the whole
  constant and 0 for the constant cut to 4464.  CLICK's Compare dialog pairs a hex operand (DH, XD,
  YD, an h constant) only with another hex one, so no DH is compared with a DS here.  A CLICK
  has not read Results 1 and 2 yet. Result3 is as read.  Text order is not asked: CLICK's
  Compare dialog offers only = and not-equal on text.
- ``char_two_char_literal``: a TXT compared with a 2-char literal.  R1: TXT "A", next "B" == "AB";
  R2: "A", next "C"; R3: "B"; control R4: "A" == "A" = 1.  A char that is not two: 0, 0, 0, 1.
  First-char compare: 1, 1, 0, 1.  A window over two TXT: 1, 0, 0, 1, CLICK's reading and
  pyrung's.  A refusal at paste would have been an answer too, which is why this case comes
  last.

The cases own what ``memory.OWNED`` lists under this group.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    copy,
    fill,
)
from tests.conformance.memory import dh, ds, txt
from tests.conformance.probe_kit import finish, once


def prove_word_and_wide_compare(slot):
    with Rung(*once(slot)):
        copy(0xFFFF, dh[70])
        copy(5000, ds[3925])
    with Rung(*once(slot), dh[70] > 0x7FFF):
        copy(1, slot.Result1)
    with Rung(*once(slot), dh[70] == 0xFFFF):
        copy(1, slot.Result2)
    with Rung(*once(slot), ds[3925] < 70000):
        copy(1, slot.Result3)
    finish(slot)


def init_word_and_wide_compare(slot):
    copy(0, dh[70])
    copy(0, ds[3925])


def prove_char_two_char_literal(slot):
    with Rung(*once(slot)):
        copy("A", txt[771])
        copy("B", txt[772])
        copy("A", txt[773])
        copy("C", txt[774])
        copy("B", txt[775])
        copy("A", txt[776])
    with Rung(*once(slot), txt[771] == "AB"):
        copy(1, slot.Result1)
    with Rung(*once(slot), txt[773] == "AB"):
        copy(1, slot.Result2)
    with Rung(*once(slot), txt[775] == "AB"):
        copy(1, slot.Result3)
    with Rung(*once(slot), txt[776] == "A"):
        copy(1, slot.Result4)
    finish(slot)


def init_char_two_char_literal(slot):
    fill("\x00", txt.select(771, 776))


group = Group(
    "compare",
    [
        case(
            "A Word compares as unsigned: DH 0xFFFF > 7FFFh reads 1 (Result1), DH 0xFFFF == FFFFh "
            "reads 1 (Result2), and DS 5000 < 70000 reads 1 (Result3)",
            ladder=prove_word_and_wide_compare,
            init=init_word_and_wide_compare,
            expect={"Result1": 1, "Result2": 1, "Result3": 1},
            probe="a Word's h constants; a constant past an Int",
        ),
        case(
            "A TXT compared with a 2-char literal compares it and the next TXT: TXT A, next B == "
            "AB reads 1 (Result1), A, next C reads 0 (Result2), B reads 0 (Result3), control A "
            "== A reads 1 (Result4)",
            ladder=prove_char_two_char_literal,
            init=init_char_two_char_literal,
            expect={"Result1": 1, "Result2": 0, "Result3": 0, "Result4": 1},
            probe="CHAR compare with a 2-char literal; a refusal at paste is an answer",
        ),
    ],
)


def test_compare():
    assert_all_passed(run(group))
