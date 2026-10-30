"""Text and copy converters: the ``text`` group (subroutine ``Conf_Text``).

The harness reads only slot registers, so every case stages its text in raw TXT
it owns (``memory.OWNED``: TXT1..TXT200 and TXT301..TXT400, staged
DS3901..DS3999, DD301..DD400, DH301..DH400) and reads characters back as codes
into its slot: ``copy(txt[n], Result, convert=to_ascii)``.  (A CLICK does serve
TXT over Modbus; a slot keeps every target's answer in one place.)  A TXT slot a converter should leave alone is seeded with
``Z`` first (code 90), so "untouched" reads 90 and "written" reads anything
else.  A destination a failing instruction should leave alone is seeded with a
nonzero value, so a write of 0 would show.  ``init`` puts the TXT, the staged
registers and the seeds back to their power-up value (TXT NUL, numbers 0).

The cases that raise SC43 (``to_value_rejects``, ``pack_text_word_hex``,
``pack_text_whitespace``) come last in the group: every case fires on the same
scan, so an earlier fault would still be on at a later case's flag probe.

The expectations are what a CLICK does (hardware round 1, 2026-10-29): TXT
is 8-bit (Copy Binary of 200 stores 200), Pack Copy into an INT or DINT clamps
a value past its range and stops at a decimal point ("12.5" is 12), and
Character Value of NUL writes 0.  A fault flag is read on the rung right after the
instruction it asks about: a CLICK flag may follow the last instruction (see
``test_system.py``), so anything between would confound it.  What each case
tells apart:

- ``value_vs_ascii``: Copy Character Value reads '5' as 5, Copy ASCII Code
  Value as 53.  A model with one text->number converter reads 5 or 53 in both.
- ``null_ascii``: an unwritten TXT holds NUL, and ASCII Code Value of NUL writes
  0 over the destination's 77.  A model that skips empty text leaves 77.
- ``to_value_rejects``: Character Value of a letter is a fault (SC43) and
  leaves the destination alone (77); of NUL it writes 0 over 66 (Result3) and,
  adopted, raises no fault (Result4 = 0).  pyrung writes 0 for NUL, but its
  fault flag stays on from the letter earlier in the scan (Result4 = 1), so the
  case stays ``pending="SYS"`` until a flag follows the last instruction.  A
  model that stores the code minus 48 reads 17 for the letter.
- ``to_text_suppress``: 123 converts to the three characters "123", the rest
  of the TXT range is untouched (90).  A model that pads to the Int's five
  digits reads "00123": 48 where this reads 49, and 50, 51 in the slots this
  leaves at 90.
- ``to_text_fixed``: Do not Suppress zero renders an Int as five digits "00042".
- ``to_text_fixed_negative``: a negative Int is a sign plus five digits
  "-00042", six characters.  A model that fits the sign inside five ("-0042")
  reads 50 in slot 5 and 90 in slot 6 where this reads 52 and 50.
- ``to_text_termination``: the termination code is one character after the
  digits, and code 0 is written (not skipped) as NUL.
- ``literal_fanout``: a literal string spreads across consecutive TXT, one
  character per slot, and stops at its length.
- ``to_binary``: Copy Binary of an Int writes one character, the low byte; a
  model that writes both bytes of the word also changes the next TXT (1 where
  this reads 90).  A byte above 127 is stored as is (200, Result3) and,
  adopted, raises no fault (Result4 = 0).
- ``pack_text_word_hex``: Pack Copy text into a WORD parses hex: "00FF" is 255
  and "0010" is 16, where a decimal parse refuses the first and reads 10 for
  the second; text that is not hex leaves the destination alone.
- ``pack_text_int``: into an INT parses signed decimal ("0042" is 42, not
  hex 66), clamps 123456 to 32767 (Result3), and reads "12.5" as 12 (Result4).
  The clamp raises the fault (ErrorCode = 1; hardware round 2, 2026-10-30, where no fault
  was adopted).  pyrung refuses both and leaves 5 and 9.
- ``pack_text_dint``: into a DINT parses signed decimal over 32 bits, and
  clamps 2147483648 to 2147483647 (Result3 = 1) with the fault (Result4 = 1;
  hardware round 2, 2026-10-30).  pyrung refuses it (Result3 = 0, Result4 = 1).
- ``pack_text_int_fraction`` (probe): "12.9" reads 12 if the parse stops at
  the point (adopted), 13 if it rounds, 99 (the preset) if refused (Result1);
  "12x" reads 12 if the parse stops at the first non-digit, 99 if refused
  (Result2).  Result3 and Result4 are SC43 after each.  A CLICK reads 12, 99,
  0, 1: "12.9" stops at the point with no fault, "12x" is refused with SC43
  (hardware round 2, 2026-10-30).
- ``pack_text_whitespace``: leading or trailing whitespace faults and leaves
  the destination alone unless ``allow_whitespace``, which trims it.  A model
  that always trims reads 12 in Result1.  Round 1 read Result2 = 0 with an
  in-range copy between the pack and the flag rung; read right after the pack,
  a refusal that faults reads 1 (adopted), one that does not reads 0.
- ``fill_blank_ascii``: Fill with empty text blanks a TXT to NUL, the one blank
  a TXT byte has, so ASCII Code Value of it writes 0 over the destination's 77
  and 66.  A model where Fill of empty text writes nothing reads the seed's 90;
  one with an empty state apart from NUL, which ASCII Code Value skips, leaves
  77 and 66 (pyrung did, before a blank TXT was stored as NUL).
- ``reset_blank_ascii`` (in-process only): ``reset()`` of a TXT stores the same
  NUL, so its ASCII code is 0 too.  CLICK's Reset coil takes bit memory only
  (X, Y, C, T, CT, SC), so this is pyrung's answer with nothing to compare.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import (
    Rung,
    copy,
    fill,
    pack_text,
    reset,
    system,
    to_ascii,
    to_binary,
    to_text,
    to_value,
)
from tests.conformance.memory import dd, dh, ds, txt

NUL = "\x00"


def _go(slot):
    """The rung condition every case's staging and reading shares."""
    return Rung(slot.Cmd != 0, slot.Fired == 0)


def _seed(first, text):
    """Write ``text`` into consecutive TXT from ``first``."""
    copy(text, txt[first])


def _code(n, dest):
    """Read TXT n back as its ASCII code."""
    copy(txt[n], dest, convert=to_ascii)


def _clear(first, last):
    fill(NUL, txt.select(first, last))


def _fault_flag(slot, dest):
    """Result = 1 if CLICK's out-of-range fault (SC43) is on this scan."""
    with Rung(slot.Cmd != 0, slot.Fired == 0, system.fault.out_of_range):
        copy(1, dest)


def _fired(slot):
    with _go(slot):
        copy(1, slot.Fired)


def prove_value_vs_ascii(slot):
    with _go(slot):
        _seed(1, "5")
        copy(txt[1], slot.Result1, convert=to_value)
        copy(txt[1], slot.Result2, convert=to_ascii)
        _seed(2, "A")
        _code(2, slot.Result3)
        copy(txt[2], slot.Result4, convert=to_ascii)
    _fired(slot)


def prove_null_ascii(slot):
    with _go(slot):
        copy(77, slot.Result1)
        copy(txt[11], slot.Result1, convert=to_ascii)
        copy(txt[11], slot.Result2, convert=to_ascii)
        copy(66, slot.Result3)
        _seed(12, " ")
        copy(txt[12], slot.Result3, convert=to_ascii)
    _fired(slot)


def prove_to_value_rejects(slot):
    with _go(slot):
        copy(77, slot.Result1)
        _seed(21, "A")
        copy(txt[21], slot.Result1, convert=to_value)
    _fault_flag(slot, slot.Result2)
    with _go(slot):
        copy(66, slot.Result3)
        copy(txt[22], slot.Result3, convert=to_value)
    _fault_flag(slot, slot.Result4)
    _fired(slot)


def prove_to_text_suppress(slot):
    with _go(slot):
        copy(123, ds[3901])
        _seed(31, "ZZZZZ")
        copy(ds[3901], txt[31], convert=to_text())
        _code(31, slot.Result1)
        _code(33, slot.Result2)
        _code(34, slot.Result3)
        _code(35, slot.Result4)
    _fired(slot)


def prove_to_text_fixed(slot):
    with _go(slot):
        copy(42, ds[3902])
        _seed(41, "ZZZZZZ")
        copy(ds[3902], txt[41], convert=to_text(suppress_zero=False))
        _code(41, slot.Result1)
        _code(44, slot.Result2)
        _code(45, slot.Result3)
        _code(46, slot.Result4)
    _fired(slot)


def prove_to_text_fixed_negative(slot):
    with _go(slot):
        copy(-42, ds[3903])
        _seed(51, "ZZZZZZZ")
        copy(ds[3903], txt[51], convert=to_text(suppress_zero=False))
        _code(51, slot.Result1)
        _code(52, slot.Result2)
        _code(55, slot.Result3)
        _code(57, slot.Result4)
    _fired(slot)


def prove_to_text_termination(slot):
    with _go(slot):
        copy(123, ds[3904])
        _seed(61, "ZZZZZ")
        copy(ds[3904], txt[61], convert=to_text(termination_code="$0D"))
        _code(63, slot.Result1)
        _code(64, slot.Result2)
        _code(65, slot.Result3)
        _seed(71, "ZZZZZ")
        copy(ds[3904], txt[71], convert=to_text(termination_code=0))
        _code(74, slot.Result4)
    _fired(slot)


def prove_literal_fanout(slot):
    with _go(slot):
        _seed(86, "Z")
        copy("HELLO", txt[81])
        _code(81, slot.Result1)
        _code(83, slot.Result2)
        _code(85, slot.Result3)
        _code(86, slot.Result4)
    _fired(slot)


def prove_to_binary(slot):
    with _go(slot):
        copy(321, ds[3905])
        _seed(91, "ZZ")
        copy(ds[3905], txt[91], convert=to_binary)
        _code(91, slot.Result1)
        _code(92, slot.Result2)
        copy(200, ds[3906])
        _seed(95, "Z")
        copy(ds[3906], txt[95], convert=to_binary)
    _fault_flag(slot, slot.Result4)
    with _go(slot):
        _code(95, slot.Result3)
    _fired(slot)


def prove_pack_text_word_hex(slot):
    with _go(slot):
        _seed(101, "00FF")
        pack_text(txt.select(101, 104), dh[301])
        copy(dh[301], slot.Result1)
        _seed(106, "0010")
        pack_text(txt.select(106, 109), dh[302])
        copy(dh[302], slot.Result2)
        _seed(111, "ABCD")
        pack_text(txt.select(111, 114), dh[303])
        copy(7, dh[304])
        _seed(116, "12G4")
        pack_text(txt.select(116, 119), dh[304])
        copy(dh[304], slot.Result4)
    with Rung(slot.Cmd != 0, slot.Fired == 0, dh[303] == 0xABCD):
        copy(1, slot.Result3)
    _fired(slot)


def prove_pack_text_int(slot):
    with _go(slot):
        _seed(121, "-123")
        pack_text(txt.select(121, 124), ds[3911])
        copy(ds[3911], slot.Result1)
        _seed(126, "0042")
        pack_text(txt.select(126, 129), ds[3912])
        copy(ds[3912], slot.Result2)
        copy(5, ds[3913])
        _seed(131, "123456")
        pack_text(txt.select(131, 136), ds[3913])
    _fault_flag(slot, slot.ErrorCode)
    with _go(slot):
        copy(ds[3913], slot.Result3)
        copy(9, ds[3914])
        _seed(141, "12.5")
        pack_text(txt.select(141, 144), ds[3914])
        copy(ds[3914], slot.Result4)
    _fired(slot)


def prove_pack_text_dint(slot):
    with _go(slot):
        _seed(151, "-70000")
        pack_text(txt.select(151, 156), dd[301])
        _seed(161, "2147483647")
        pack_text(txt.select(161, 170), dd[302])
        copy(5, dd[303])
        _seed(171, "2147483648")
        pack_text(txt.select(171, 180), dd[303])
    _fault_flag(slot, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Fired == 0, dd[301] == -70000):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, dd[302] == 2147483647):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0, dd[303] == 2147483647):
        copy(1, slot.Result3)
    _fired(slot)


def prove_pack_text_whitespace(slot):
    with _go(slot):
        copy(99, ds[3921])
        _seed(181, " 12")
        pack_text(txt.select(181, 183), ds[3921])
    _fault_flag(slot, slot.Result2)
    with _go(slot):
        copy(ds[3921], slot.Result1)
        _seed(184, " 12")
        pack_text(txt.select(184, 186), ds[3922], allow_whitespace=True)
        copy(ds[3922], slot.Result3)
        _seed(187, "12 ")
        pack_text(txt.select(187, 189), ds[3923], allow_whitespace=True)
        copy(ds[3923], slot.Result4)
    _fired(slot)


def prove_pack_text_int_fraction(slot):
    with _go(slot):
        copy(99, ds[3931])
        _seed(301, "12.9")
        pack_text(txt.select(301, 304), ds[3931])
    _fault_flag(slot, slot.Result3)
    with _go(slot):
        copy(99, ds[3932])
        _seed(311, "12x")
        pack_text(txt.select(311, 313), ds[3932])
    _fault_flag(slot, slot.Result4)
    with _go(slot):
        copy(ds[3931], slot.Result1)
        copy(ds[3932], slot.Result2)
    _fired(slot)


def prove_fill_blank_ascii(slot):
    with _go(slot):
        _seed(191, "ZZ")
        fill("", txt.select(191, 192))
        copy(77, slot.Result1)
        _code(191, slot.Result1)
        copy(66, slot.Result2)
        _code(192, slot.Result2)
    _fired(slot)


def prove_reset_blank_ascii(slot):
    with _go(slot):
        _seed(196, "Z")
        reset(txt[196])
        copy(77, slot.Result1)
        _code(196, slot.Result1)
    _fired(slot)


def init_value_vs_ascii(slot):
    _clear(1, 2)


def init_null_ascii(slot):
    _clear(12, 12)


def init_to_value_rejects(slot):
    _clear(21, 21)


def init_to_text_suppress(slot):
    _clear(31, 35)
    copy(0, ds[3901])


def init_to_text_fixed(slot):
    _clear(41, 46)
    copy(0, ds[3902])


def init_to_text_fixed_negative(slot):
    _clear(51, 57)
    copy(0, ds[3903])


def init_to_text_termination(slot):
    _clear(61, 65)
    _clear(71, 75)
    copy(0, ds[3904])


def init_literal_fanout(slot):
    _clear(81, 86)


def init_to_binary(slot):
    _clear(91, 92)
    _clear(95, 95)
    fill(0, ds.select(3905, 3906))


def init_pack_text_word_hex(slot):
    _clear(101, 119)
    fill(0, dh.select(301, 304))


def init_pack_text_int(slot):
    _clear(121, 144)
    fill(0, ds.select(3911, 3914))


def init_pack_text_dint(slot):
    _clear(151, 180)
    fill(0, dd.select(301, 303))


def init_pack_text_whitespace(slot):
    _clear(181, 189)
    fill(0, ds.select(3921, 3923))


def init_pack_text_int_fraction(slot):
    _clear(301, 304)
    _clear(311, 313)
    fill(0, ds.select(3931, 3932))


def init_fill_blank_ascii(slot):
    _clear(191, 192)


def init_reset_blank_ascii(slot):
    _clear(196, 196)


group = Group(
    "text",
    [
        case(
            "Copy Character Value reads '5' as 5 and Copy ASCII Code Value reads the same '5' as 53, "
            "and ASCII Code Value of 'A' is 65",
            ladder=prove_value_vs_ascii,
            init=init_value_vs_ascii,
            expect={"Result1": 5, "Result2": 53, "Result3": 65, "Result4": 65},
        ),
        case(
            "ASCII Code Value of a TXT nothing wrote (NUL) writes 0 over the destination, "
            "and of a space writes 32",
            ladder=prove_null_ascii,
            init=init_null_ascii,
            expect={"Result1": 0, "Result2": 0, "Result3": 32},
        ),
        case(
            "Copy to text with suppress zero writes 123 as the characters 1, 2, 3 and leaves "
            "the next TXT alone",
            ladder=prove_to_text_suppress,
            init=init_to_text_suppress,
            expect={"Result1": 49, "Result2": 51, "Result3": 90, "Result4": 90},
        ),
        case(
            "Copy to text without suppress zero writes an Int 42 as five digits 00042 and "
            "leaves the next TXT alone",
            ladder=prove_to_text_fixed,
            init=init_to_text_fixed,
            expect={"Result1": 48, "Result2": 52, "Result3": 50, "Result4": 90},
        ),
        case(
            "Copy to text without suppress zero writes an Int -42 as a minus sign and five "
            "digits: -00042, six characters",
            ladder=prove_to_text_fixed_negative,
            init=init_to_text_fixed_negative,
            expect={"Result1": 45, "Result2": 48, "Result3": 52, "Result4": 90},
        ),
        case(
            "A termination code adds one character after the digits, and code 0 is written "
            "as NUL, not skipped",
            ladder=prove_to_text_termination,
            init=init_to_text_termination,
            expect={"Result1": 51, "Result2": 13, "Result3": 90, "Result4": 0},
        ),
        case(
            "Copying the literal HELLO to a TXT fills five consecutive TXT, one character "
            "each, and not the sixth",
            ladder=prove_literal_fanout,
            init=init_literal_fanout,
            expect={"Result1": 72, "Result2": 76, "Result3": 79, "Result4": 90},
        ),
        case(
            "Copy Binary writes only the low byte of an Int as one character (Result1 = "
            "65, the next TXT untouched: Result2 = 90), and a TXT is 8-bit: a byte of 200 "
            "is stored as 200 (Result3) with no fault (Result4 = 0)",
            ladder=prove_to_binary,
            init=init_to_binary,
            expect={"Result1": 65, "Result2": 90, "Result3": 200, "Result4": 0},
            probe="does Copy Binary of 200 set SC43",
        ),
        case(
            "Fill with empty text blanks a TXT to NUL, so ASCII Code Value of it writes 0 over "
            "the destination, as for a TXT nothing wrote",
            ladder=prove_fill_blank_ascii,
            init=init_fill_blank_ascii,
            expect={"Result1": 0, "Result2": 0},
        ),
        case(
            "Reset of a TXT blanks it to NUL, so ASCII Code Value of it writes 0 over the "
            "destination",
            ladder=prove_reset_blank_ascii,
            init=init_reset_blank_ascii,
            expect={"Result1": 0},
            in_process_only="CLICK's Reset coil takes bit memory only (X, Y, C, T, CT, SC), not TXT",
        ),
        case(
            "Pack text into an Int stops at a decimal point but refuses any other "
            "character that is not a digit: 12.9 is 12 (Result1), not rounded, with no "
            "fault (Result3 = 0); 12x is refused, the destination keeps 99 (Result2), "
            "with the fault (Result4 = 1)",
            ladder=prove_pack_text_int_fraction,
            init=init_pack_text_int_fraction,
            expect={"Result1": 12, "Result2": 99, "Result3": 0, "Result4": 1},
            probe='pack_text "12.9" -> 12 or 13; "12x" -> 12 or refused',
        ),
        # The cases that raise SC43 come last: every case fires on the same scan, so a
        # fault raised earlier in the subroutine would still be on at a later case's
        # flag probe (a CLICK's flag follows the last instruction, so there it is not).
        case(
            "Character Value of a letter is refused: the out-of-range fault comes up "
            "(Result2 = 1) and the destination keeps its 77 (Result1); of NUL it writes 0 "
            "(Result3), with no fault (Result4 = 0)",
            ladder=prove_to_value_rejects,
            init=init_to_value_rejects,
            expect={"Result1": 77, "Result2": 1, "Result3": 0, "Result4": 0},
            pending="SYS",
            probe="does Character Value of NUL set SC43",
        ),
        case(
            "Pack text into a WORD parses hex: 00FF is 255, 0010 is 16, ABCD is 0xABCD, and "
            "12G4 is refused and leaves the WORD alone",
            ladder=prove_pack_text_word_hex,
            init=init_pack_text_word_hex,
            expect={"Result1": 255, "Result2": 16, "Result3": 1, "Result4": 7},
        ),
        case(
            "Pack text with a leading space is refused unless whitespace is allowed: the "
            "destination keeps its 99 (Result1) and the fault comes up (Result2 = 1); "
            "allowed, leading and trailing spaces are trimmed (Result3 = Result4 = 12)",
            ladder=prove_pack_text_whitespace,
            init=init_pack_text_whitespace,
            expect={"Result1": 99, "Result2": 1, "Result3": 12, "Result4": 12},
            probe="does a refused pack_text set SC43",
        ),
        case(
            "Pack text into an Int parses signed decimal: -123 is -123 (Result1), 0042 is 42 "
            "(Result2), 123456 clamps to 32767 (Result3) with the fault (ErrorCode = 1), and "
            "12.5 stops at the point, 12 (Result4)",
            ladder=prove_pack_text_int,
            init=init_pack_text_int,
            expect={
                "Result1": -123,
                "Result2": 42,
                "Result3": 32767,
                "Result4": 12,
                "ErrorCode": 1,
            },
            probe="does a clamping pack_text set SC43",
        ),
        case(
            "Pack text into a Dint parses signed decimal over 32 bits (Result1 = 1, Result2 = "
            "1), and 2147483648 clamps to 2147483647 (Result3 = 1) with the fault (Result4 = 1)",
            ladder=prove_pack_text_dint,
            init=init_pack_text_dint,
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": 1},
            probe="does a clamping pack_text set SC43",
        ),
    ],
)


def test_text():
    assert_all_passed(run(group))
