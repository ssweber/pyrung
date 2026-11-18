"""Test a write past the bank, in the ``past_write`` group (subroutine ``Conf_PastWrite``).

This is the last group of part ``copy``. No other group depends on it, so you can leave it out.
**It writes into a real CLICK's memory past the DD bank.** Do not load its subroutine, or run
its group, on a CLICK whose memory matters. To leave it out, delete the ``Conf_PastWrite`` call
rung in main. The simulator and the P1AM code have no memory there.

``past_bank_write`` runs ``copy(5, dd[ptr])`` with ptr 1531. It then reads the DD back through
the pointer and reads the flags. Result1 is the read before the write. The write happens only if
Result1 is 0, because memory that reads 0 looks unused. So a CLICK whose DD1531 holds anything
is left alone. In that case Result1 reads as that value, and the write, the flags and the read
after are not what the case asks. Result2 is the read after. Result3 has bit 1 for SC43 on after
the write and bit 2 for SC44. A last Copy writes 0 back, under the same guard, so the 5 does not
outlive the case. It once outlived two project loads and broke
``faults/pointer_past_bank_lands``. A nonzero DD1531 is left alone, and ``past_clean`` clears
it.

On a CLICK the write lands and raises no flag, and the three results are 0, 5, 0. pyrung drops
the write and reads 0, 0, 0, so the case is ``pending="PASTBANK"``.

Why DD1531, and one address: ``faults/pointer_past_bank`` and ``faults/pointer_past_bank_lands``
read past the bank on a CLICK as 1 at DD1001 and 0 at DD1531 and DD1532. DD1001 holds something,
the memory that follows the bank. DD1531 and DD1532 read 0 and do not wrap (``lands`` found
DD531 untouched), so they look unused. DD1531 is the one pointer ``lands`` already reads. The
case writes there and nowhere else.

The flags are read with coils (a Math or Copy to read the first would settle the
second), the first read after the write, and folded into Result3 a scan later.
The group owns C1381, C1382 and DS4226 (the pointer).
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import copy, latch, math, reset, rung, system
from tests.conformance.memory import c, dd, ds
from tests.conformance.scans import count_scans, done, scans_for

WINDOW = 8
SCANS = scans_for(WINDOW)
ADDRESS = 1531
# The pointer.  A write through a pointer is a write to every tag of its block as
# far as the build's disjointness check can tell, unless the pointer's choices bound
# it: 1531 is no tag of the bank, so this write touches none.
POINTER = ds[4226]
ds.slot(4226, choices={0: "unset", ADDRESS: "past the DD bank"})
SC43 = system.fault.out_of_range
SC44 = system.fault.address_error


def prove_past_bank_write(slot):
    pointer = POINTER
    count_scans(slot, WINDOW)
    with rung(slot.Scratch == 2):
        copy(ADDRESS, pointer)
    with rung(slot.Scratch == 3):
        copy(dd[pointer], slot.Result1)
    with rung(slot.Scratch == 4, slot.Result1 == 0):
        copy(5, dd[pointer])
    with rung(slot.Scratch == 4, slot.Result1 == 0, SC43):
        latch(c[1381])
    with rung(slot.Scratch == 4, slot.Result1 == 0, SC44):
        latch(c[1382])
    with rung(slot.Scratch == 5):
        copy(dd[pointer], slot.Result2)
    with rung(slot.Scratch == 6, slot.Result1 == 0):
        copy(0, dd[pointer])
    with rung(slot.Scratch == 6, c[1381]):
        math(slot.Result3 + 1, slot.Result3)
    with rung(slot.Scratch == 6, c[1382]):
        math(slot.Result3 + 2, slot.Result3)
    done(slot, WINDOW)


def init_past_bank_write(slot):
    reset(c.select(1381, 1382))
    copy(0, POINTER)


group = Group(
    "past_write",
    [
        case(
            "WRITES PAST THE DD BANK on a CLICK.  A Copy of 5 into DD1531 through a pointer "
            "lands: DD1531 reads 0 before (Result1) and 5 after (Result2), neither SC43 nor SC44 "
            "is on after the write (Result3 = 0), and a last Copy writes 0 back",
            ladder=prove_past_bank_write,
            init=init_past_bank_write,
            expect={"Result1": 0, "Result2": 5, "Result3": 0},
            scans=SCANS,
            pending="PASTBANK",
        ),
    ],
)


def test_past_bank_write():
    assert_all_passed(run(group))
