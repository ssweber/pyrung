"""A write past the bank: the ``past_write`` group (subroutine ``Conf_PastWrite``).

The last group of part ``copy``, the group the other groups never depend on, so it
can be left out.  **It writes into a real CLICK's memory past the DD bank.**  Do
not load its subroutine, or run its group, on a CLICK whose memory matters; delete
the ``Conf_PastWrite`` call rung in main to leave it out.  The simulator and
the P1AM code have no memory there.

``past_bank_write`` (probe): ``copy(5, dd[ptr])`` with ptr 1531, then the DD read
back through the pointer, and the flags.  Result1 is the read before the write:
the write happens only if it is 0 (memory that reads 0 looks unused), so a CLICK
whose DD1531 holds anything is left alone (the case then reads Result1 as that
value, and the write, flags and read after are not what it asks).  Result2 is the read after.  Result3 has bit 1 for SC43
on after the write and bit 2 for SC44.  pyrung drops the write: 0, 0, 0.  A CLICK
that stores it reads Result2 = 5; one that raises a flag reads Result3.

Why DD1531, and one address: ``faults/pointer_past_bank`` and
``faults/pointer_past_bank_lands`` read past the bank on a CLICK as 1 at DD1001 and
0 at DD1531 and DD1532.  DD1001 holds something (the memory that follows the
bank); DD1531 and DD1532 read 0 and no wrap (``lands`` found DD531 untouched), so
they look unused.  DD1531 is the one pointer ``lands`` already reads, and the case
writes there and nowhere else.

The flags are read with coils (a Math or Copy to read the first would settle the
second), the first read after the write, and folded into Result3 a scan later.
The group owns C1381, C1382 and DS4226 (the pointer).
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, calc, copy, latch, reset, system
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
    with Rung(slot.Scratch == 2):
        copy(ADDRESS, pointer)
    with Rung(slot.Scratch == 3):
        copy(dd[pointer], slot.Result1)
    with Rung(slot.Scratch == 4, slot.Result1 == 0):
        copy(5, dd[pointer])
    with Rung(slot.Scratch == 4, slot.Result1 == 0, SC43):
        latch(c[1381])
    with Rung(slot.Scratch == 4, slot.Result1 == 0, SC44):
        latch(c[1382])
    with Rung(slot.Scratch == 5):
        copy(dd[pointer], slot.Result2)
    with Rung(slot.Scratch == 6, c[1381]):
        calc(slot.Result3 + 1, slot.Result3)
    with Rung(slot.Scratch == 6, c[1382]):
        calc(slot.Result3 + 2, slot.Result3)
    done(slot, WINDOW)


def init_past_bank_write(slot):
    reset(c.select(1381, 1382))
    copy(0, POINTER)


group = Group(
    "past_write",
    [
        case(
            "WRITES PAST THE DD BANK on a CLICK.  A Copy of 5 into DD1531 through a pointer "
            "drops the write: DD1531 reads 0 before (Result1) and after (Result2), and "
            "neither SC43 nor SC44 is on after the write (Result3 = 0)",
            ladder=prove_past_bank_write,
            init=init_past_bank_write,
            expect={"Result1": 0, "Result2": 0, "Result3": 0},
            scans=SCANS,
            probe="a Copy into a pointer past the DD bank: does it land, which flag",
        ),
    ],
)


def test_past_bank_write():
    assert_all_passed(run(group))
