"""A Real copied as text where its digits run past TXT1000: the ``txt_end`` group (part
``follow_extra``).

``copy_more/to_text_real`` read on a CLICK that 1.5 copied as text (Copy Option 4c) writes
"1.5000000", nine characters.  Started at TXT995 only six of them fit before the bank ends.  The
fix step ``store-click`` (3 in the ``conform-fix`` arc) waits on what CLICK does then (store-click
review NB4).

``by_pointer`` stages TXT994..TXT1000 to 'Z' on scan 1, copies the DF 1.5 as text through a DS
pointer of 995 on scan 2 (the path pyrung's NB4 is about), and reads TXT995..TXT1000 as pairs of codes, ``128 * first + second``: Result1 TXT995-996,
Result2 TXT997-998, Result3 TXT999-1000.  "ZZ" is 11610, a pair the Copy left alone.  The digits
up to TXT1000 read 6318 ("1."), 6832 ("50"), 6192 ("00"); a NUL is 0.

- The Copy writes what fits and stops (pyrung, no flag): 6318, 6832, 6192.
- It writes nothing, all or nothing: 11610, 11610, 11610.
- Anything else is a third answer, which the pairs spell out.

ErrorCode is the code of TXT994, 90 where nothing before the start was written.  Result4 has SC43
(bit 1) and SC44 (bit 2) on the rung right after the Copy, and the controls on scan 3: SC43 from
a Copy of DD 40000 into a DS (bit 4, ``faults/copy_clamp_out_of_range``) and SC44 from a Copy
through a DD pointer of 0 (bit 8), so 12 is two raises that took and the flags read as they
should.  The expectation takes any of 0..15, since a CLICK may latch a flag across scans or not
raise SC44 for a pointer of 0: the bits are the reading (1 SC43 and 2 SC44 after the Copy, 4 and 8
the controls).  pyrung reads 6318, 6832, 6192, 12, 90.

No Copy into a TXT by pointer has been pasted (``to_text_real`` pasted the form by address): if
CLICK's editor refuses it, delete the group's call rung.  A case by address would need the same
TXT, which no two cases may share.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, fill, system, to_ascii, to_text
from tests.conformance.memory import dd, df, ds, txt
from tests.conformance.probe_kit import between
from tests.conformance.scans import count_scans, done, scans_for

SC43 = system.fault.out_of_range
SC44 = system.fault.address_error
WINDOW = 3
STAGE_SCAN, PROBE_SCAN, CONTROL_SCAN = 1, 2, 3
START = 995
BLANK = "\x00"
#: The DF, the pointer, a code, the Int a clamp goes into, the pointer of 0; DD 40000 and a sink.
REAL, POINTER, CODE, CLAMPED, ZERO = df[41], ds[3941], ds[3942], ds[3943], ds[3944]
BIG, SINK = dd[95], dd[96]
# A write or read through a pointer touches every tag of its block as far as the build's
# disjointness check can tell, unless the pointer's choices bound it.
ds.slot(3941, choices={0: "unset", START: "TXT995"})
ds.slot(3944, choices={0: "no address"})


def _flag(slot: Any, scan: int, flag: Any, bit: int) -> None:
    with Rung(slot.Scratch == scan, flag):
        calc(slot.Result4 + bit, slot.Result4)


def _pair(slot: Any, first: int, into: Any) -> None:
    with Rung(slot.Scratch > WINDOW):
        copy(txt[first], CODE, convert=to_ascii)
        calc(CODE * 128, into)
        copy(txt[first + 1], CODE, convert=to_ascii)
        calc(into + CODE, into)


def _txt_end(slot: Any, store: Any) -> None:
    count_scans(slot, WINDOW)
    with Rung(slot.Scratch == STAGE_SCAN):
        copy("ZZZZZZZ", txt[START - 1])
        copy(1.5, REAL)
        copy(START, POINTER)
        copy(40000, BIG)
        copy(0, ZERO)
    with Rung(slot.Scratch == PROBE_SCAN):
        copy(REAL, store, convert=to_text())
    _flag(slot, PROBE_SCAN, SC43, 1)
    _flag(slot, PROBE_SCAN, SC44, 2)
    with Rung(slot.Scratch == CONTROL_SCAN):
        copy(BIG, CLAMPED)
    _flag(slot, CONTROL_SCAN, SC43, 4)
    with Rung(slot.Scratch == CONTROL_SCAN):
        copy(dd[ZERO], SINK)
    _flag(slot, CONTROL_SCAN, SC44, 8)
    _pair(slot, START, slot.Result1)
    _pair(slot, START + 2, slot.Result2)
    _pair(slot, START + 4, slot.Result3)
    with Rung(slot.Scratch > WINDOW):
        copy(txt[START - 1], slot.ErrorCode, convert=to_ascii)
    done(slot, WINDOW)


def prove_by_pointer(slot: Any) -> None:
    _txt_end(slot, txt[POINTER])


def init_txt_end(slot: Any) -> None:
    fill(BLANK, txt.select(START - 1, 1000))
    copy(0.0, REAL)
    fill(0, ds.select(3941, 3944))
    fill(0, dd.select(95, 96))


group = Group(
    "txt_end",
    [
        case(
            "A Real copied as text by a DS pointer to TXT995, its digits running past TXT1000: "
            "TXT995..TXT1000 as pairs of codes (Result1-3: the digits that fit, 6318, 6832, 6192, "
            "or the pairs left alone, 11610 each, or another answer); SC43 and SC44 after it "
            "(Result4 bits 1, 2, over the controls' 12); TXT994 (ErrorCode, 90 untouched)",
            ladder=prove_by_pointer,
            init=init_txt_end,
            expect={
                "Result1": between(0, 16383),
                "Result2": between(0, 16383),
                "Result3": between(0, 16383),
                "Result4": between(0, 15),
                "ErrorCode": between(0, 127),
            },
            scans=scans_for(WINDOW),
            probe="a Real as text by pointer past TXT1000: part, none, which flag (3)",
        ),
    ],
)


def test_txt_end():
    assert_all_passed(run(group))
