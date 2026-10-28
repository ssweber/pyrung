"""Coils and edge contacts: the ``coils`` group (subroutine ``Conf_Coils``).

``out``, ``out(oneshot=True)``, ``latch``/``reset`` and the ``rise``/``fall``
contacts.  Each coil is a C bit; the cases own C1501..C1600 (``memory.OWNED``),
ten bits per case.

Each case counts its own scans and records what its bits did over scans
1..8 (``scans.py``): the rung under test keys on the scan number, and
``Fired`` goes up once the record is final.

An edge contact is the first contact of its rung, so it is scanned with power
on its left on every scan, whatever the rest of the rung says.

The reset needs little: every ``out`` rung is gated on ``Cmd != 0``, so its
coil drops and its one-shot rearms while ``Cmd == 0``, and edge history follows
the bits.  Latched bits are reset in ``init``.

The expectations are pyrung's answers, unconfirmed on a CLICK.  pyrung's edge
contacts compare a bit with its value at the end of the previous scan, one
history per bit.  If CLICK keeps a history per contact instead (the value that
contact saw when it last ran), ``edge_before_its_writer`` and
``edge_sees_previous_scan_end`` are where it shows.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, fall, latch, out, reset, rise
from tests.conformance.memory import c
from tests.conformance.scans import count_scans, done, edges, scans_for, trace

SCANS = scans_for()


def prove_out_follows_rung(slot):
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(c[1501])
    trace(slot, c[1501])
    done(slot)


def prove_out_oneshot_fires_one_scan(slot):
    # The rung goes true on scan 3 and stays true.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        out(c[1511], oneshot=True)
    trace(slot, c[1511])
    done(slot)


def prove_out_oneshot_refires(slot):
    # The rung is true on scans 2..4, false on scan 5 only, true from scan 6.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch != 5):
        out(c[1521], oneshot=True)
    trace(slot, c[1521])
    done(slot)


def prove_rise_fall_fire_one_scan(slot):
    # C1531 is on for scans 3..5, written above the edge contacts.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(c[1531])
    edges(rise(c[1531]), slot, slot.Result1, slot.Result2)
    edges(fall(c[1531]), slot, slot.Result3, slot.Result4)
    done(slot)


def prove_edge_sees_previous_scan_end(slot):
    # On scans 3..5, C1541 is on only between the rung that sets it and the
    # rung that clears it, and C1542 is off only between the rung that clears
    # it and the rung that sets it.  Each ends every scan as it began it.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        latch(c[1541])
        reset(c[1542])
    edges(rise(c[1541]), slot, slot.Result1, slot.Result2)
    edges(fall(c[1542]), slot, slot.Result3, slot.Result4)
    with Rung(slot.Cmd != 0):
        reset(c[1541])
        latch(c[1542])
    done(slot)


def prove_edge_before_its_writer(slot):
    # C1551 is on for scans 3..5, written between two pairs of edge contacts.
    count_scans(slot)
    edges(rise(c[1551]), slot, slot.Result1)
    edges(fall(c[1551]), slot, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(c[1551])
    edges(rise(c[1551]), slot, slot.Result3)
    edges(fall(c[1551]), slot, slot.Result4)
    done(slot)


def prove_latch_reset_sticky(slot):
    # The latch rung is true on scan 3 only, the reset rung on scan 6 only.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        latch(c[1561])
    with Rung(slot.Cmd != 0, slot.Scratch == 6):
        reset(c[1561])
    trace(slot, c[1561])
    done(slot)


def init_edge_sees_previous_scan_end(slot):
    reset(c[1541])
    reset(c[1542])


def init_latch_reset_sticky(slot):
    reset(c[1561])


group = Group(
    "coils",
    [
        case(
            "An out coil follows its rung on the same scan: true on scans 3..5, the "
            "coil is on for those 3 scans (Result1 = 3), from scan 3 (Result2) to "
            "scan 5 (Result3)",
            ladder=prove_out_follows_rung,
            expect={"Result1": 3, "Result2": 3, "Result3": 5},
            scans=SCANS,
        ),
        case(
            "A one-shot out coil is on for one scan when its rung goes true, and off "
            "while the rung stays true: true from scan 3, the coil is on for 1 scan "
            "(Result1 = 1), scan 3 (Result2 = 3, Result3 = 3)",
            ladder=prove_out_oneshot_fires_one_scan,
            expect={"Result1": 1, "Result2": 3, "Result3": 3},
            scans=SCANS,
        ),
        case(
            "A one-shot out coil fires again after its rung is false for one scan: "
            "true on scans 2..4 and from scan 6, the coil is on for 2 scans "
            "(Result1 = 2), scans 2 (Result2) and 6 (Result3)",
            ladder=prove_out_oneshot_refires,
            expect={"Result1": 2, "Result2": 2, "Result3": 6},
            scans=SCANS,
        ),
        case(
            "rise and fall contacts pass power for one scan each: a bit on for scans "
            "3..5, written above them, rises once (Result1 = 1) on scan 3 (Result2) "
            "and falls once (Result3 = 1) on scan 6 (Result4)",
            ladder=prove_rise_fall_fire_one_scan,
            expect={"Result1": 1, "Result2": 3, "Result3": 1, "Result4": 6},
            scans=SCANS,
        ),
        case(
            "An edge contact compares the bit with its value at the end of the "
            "previous scan: a bit set above the rise contact and cleared below it "
            "on scans 3..5 rises on all 3 (Result1 = 3, last on scan 5 = Result2), "
            "and a bit cleared above the fall contact and set below it falls on all "
            "3 (Result3 = 3, Result4 = 5)",
            ladder=prove_edge_sees_previous_scan_end,
            init=init_edge_sees_previous_scan_end,
            expect={"Result1": 3, "Result2": 5, "Result3": 3, "Result4": 5},
            scans=SCANS,
        ),
        case(
            "An edge contact above the rung that writes its bit never fires: it "
            "reads the bit as the previous scan ended, which is what it compares "
            "with. A bit on for scans 3..5 neither rises (Result1 = 0) nor falls "
            "(Result2 = 0) above its out rung, and rises (Result3 = 1) and falls "
            "(Result4 = 1) once each below it",
            ladder=prove_edge_before_its_writer,
            expect={"Result1": 0, "Result2": 0, "Result3": 1, "Result4": 1},
            scans=SCANS,
        ),
        case(
            "A latched bit stays on after its rung goes false, and a reset bit stays "
            "off: latched on scan 3 and reset on scan 6, it is on for 3 scans "
            "(Result1 = 3), scans 3 (Result2) to 5 (Result3)",
            ladder=prove_latch_reset_sticky,
            init=init_latch_reset_sticky,
            expect={"Result1": 3, "Result2": 3, "Result3": 5},
            scans=SCANS,
        ),
    ],
)


def test_coils():
    assert_all_passed(run(group))
