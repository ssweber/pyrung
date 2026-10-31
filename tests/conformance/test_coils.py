"""Coils and edge contacts: the ``coils`` group (subroutine ``Conf_Coils``).

``out``, ``out(oneshot=True)``, ``latch``/``reset`` and the ``rise``/``fall``
contacts.  Each coil is a C bit; the cases own C1501..C1600 (``memory.OWNED``),
ten bits per case.

Each case counts its own scans and records what its bits did over scans
1..8 (``scans.py``): the rung under test keys on the scan number, and
``Fired`` goes up once the record is final.

An edge contact is the first contact of its rung, so it is scanned with power
on its left on every scan, whatever the rest of the rung says -- except in the
two probes that ask what an edge contact without power on its left does.

The reset needs little: every ``out`` rung is gated on ``Cmd != 0``, so its
coil drops and its one-shot rearms while ``Cmd == 0``, and edge history follows
the bits.  Latched bits are reset in ``init``.

The expectations are what a CLICK does.  A CLICK edge contact keeps a memory of
its own: the bit as that contact saw it when it last ran (hardware round 1,
2026-10-29).  pyrung's edge contacts compare the bit with its value at the end
of the previous scan, one history per bit, so the two cases that tell them
apart are ``pending="S3"`` until per-contact memory lands.  Each lists the
scans its edge contacts passed power as digits (``firings``):

- ``edge_sees_previous_scan_end``: a bit set above two rise contacts and
  cleared below them on scans 3..5.  Per-contact memory: each contact rises on
  scan 3 only (3, 3; the falls 3, 3).  pyrung's end-of-scan history: on all
  three scans (345 x 4).  One memory per bit updated by every read: the first
  contact 3, the second never (3, 0; the falls 3, 0).
- ``edge_before_its_writer``: a bit on for scans 3..5, with a rise and a fall
  contact above its out rung and another pair below.  Per-contact memory: the
  pair above fires a scan late, rise on 4 and fall on 7; the pair below on 3
  and 6.  pyrung's end-of-scan history: the pair above never fires (0, 0).

The open probes (round 2), with the answer the expectation adopts:

- ``edge_behind_open_contact``: an edge contact behind an open series contact.
  Its memory is updated every scan the contact is solved ("scanned", adopted):
  rise and fall behind the gate read 0 and 0.  Updated only when power reaches
  it ("powered"): the rise fires on scan 5 and the fall on scan 8 (5, 8).  The
  rail contacts beside them read 3 and 7 under both, the bit's own edges.
- ``edge_in_false_rung_branch``: the same question for edge contacts in the
  branches of a rung whose rail contact is open.  Scanned: 0 and 0.
  Powered: 5 and 8.
- ``edge_on_first_scan``: a bit an unconditional rung turns on from the first
  scan, read by a rise contact below it.  Edge memory that starts off at
  STOP->RUN fires on the first scan and latches a flag (Result1 = 1, adopted);
  memory seeded from the bit, or carried over STOP, never fires (Result1 = 0).
  Only the first scan after the PLC last went to RUN counts, so this one reads
  the loaded program's first scan, whatever run reads it.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, branch, calc, copy, fall, latch, out, reset, rise, system
from tests.conformance.memory import c
from tests.conformance.scans import WINDOW, count_scans, done, edges, firings, scans_for, trace

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
    # it and the rung that sets it.  Each ends every scan as it began it.  Two
    # contacts of each kind, side by side, read the same bit at the same place.
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        latch(c[1541])
        reset(c[1542])
    firings(rise(c[1541]), slot, slot.Result1)
    firings(rise(c[1541]), slot, slot.Result2)
    firings(fall(c[1542]), slot, slot.Result3)
    firings(fall(c[1542]), slot, slot.Result4)
    with Rung(slot.Cmd != 0):
        reset(c[1541])
        latch(c[1542])
    done(slot)


def prove_edge_before_its_writer(slot):
    # C1551 is on for scans 3..5, written between two pairs of edge contacts.
    count_scans(slot)
    firings(rise(c[1551]), slot, slot.Result1)
    firings(fall(c[1551]), slot, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(c[1551])
    firings(rise(c[1551]), slot, slot.Result3)
    firings(fall(c[1551]), slot, slot.Result4)
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


def _gated_bit(slot, bit, gate):
    """*bit* on for scans 3..6; *gate* on for scans 5 and 8 only."""
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 6):
        out(bit)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        out(gate)
    with Rung(slot.Cmd != 0, slot.Scratch == 8):
        out(gate)


def prove_edge_behind_open_contact(slot):
    # The gate C1572 is open while C1571 rises (scan 3) and falls (scan 7),
    # and closed on scans 5 and 8, after each.
    bit, gate = c[1571], c[1572]
    count_scans(slot)
    _gated_bit(slot, bit, gate)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, gate, rise(bit)):
        calc(slot.Result1 * 10 + slot.Scratch, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, gate, fall(bit)):
        calc(slot.Result2 * 10 + slot.Scratch, slot.Result2)
    firings(rise(bit), slot, slot.Result3)
    firings(fall(bit), slot, slot.Result4)
    done(slot)


def prove_edge_in_false_rung_branch(slot):
    # As edge_behind_open_contact, with the edge contacts in two branches of a
    # rung whose rail contact is the gate C1582.
    bit, gate = c[1581], c[1582]
    count_scans(slot)
    _gated_bit(slot, bit, gate)
    with Rung(gate, slot.Cmd != 0, slot.Scratch <= WINDOW):
        with branch(rise(bit)):
            calc(slot.Result1 * 10 + slot.Scratch, slot.Result1)
        with branch(fall(bit)):
            calc(slot.Result2 * 10 + slot.Scratch, slot.Result2)
    done(slot)


def prove_edge_on_first_scan(slot):
    # Outside the Cmd gate: C1591 is on from the first scan, written above the
    # rise contact; C1592 latches if that contact ever passes power.  The
    # reset leaves both alone, and C1591 never falls, so only the first scan
    # can latch C1592.
    with Rung():
        out(c[1591])
    with Rung(rise(c[1591])):
        latch(c[1592])
    with Rung(slot.Cmd != 0, slot.Fired == 0, c[1592]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0, system.sys.first_scan):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


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
            "Each edge contact compares the bit with what it saw itself when it last "
            "ran, not with the bit at the end of the previous scan: a bit set above "
            "two rise contacts and cleared below them on scans 3..5 rises on scan 3 "
            "only, at each (Result1 = Result2 = 3), and a bit cleared above two fall "
            "contacts and set below them falls on scan 3 only, at each (Result3 = "
            "Result4 = 3)",
            ladder=prove_edge_sees_previous_scan_end,
            init=init_edge_sees_previous_scan_end,
            expect={"Result1": 3, "Result2": 3, "Result3": 3, "Result4": 3},
            scans=SCANS,
            pending="S3",
        ),
        case(
            "An edge contact above the rung that writes its bit fires a scan late: it "
            "last saw the bit before the writer ran. A bit on for scans 3..5 rises on "
            "scan 4 (Result1) and falls on scan 7 (Result2) above its out rung, and "
            "rises on scan 3 (Result3) and falls on scan 6 (Result4) below it",
            ladder=prove_edge_before_its_writer,
            expect={"Result1": 4, "Result2": 7, "Result3": 3, "Result4": 6},
            scans=SCANS,
            pending="S3",
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
        case(
            "An edge contact behind an open contact still tracks its bit: a bit on "
            "for scans 3..6 rises and falls while the gate in front of a rise and a "
            "fall contact is open, so when the gate closes on scans 5 and 8 neither "
            "fires (Result1 = 0, Result2 = 0); on the rail beside them the bit rises "
            "on scan 3 (Result3) and falls on scan 7 (Result4)",
            ladder=prove_edge_behind_open_contact,
            expect={"Result1": 0, "Result2": 0, "Result3": 3, "Result4": 7},
            scans=SCANS,
        ),
        case(
            "An edge contact in a branch of a false rung still tracks its bit: a bit "
            "on for scans 3..6 rises and falls while the rung's rail contact is open, "
            "so when it closes on scans 5 and 8 neither the rise nor the fall branch "
            "fires (Result1 = 0, Result2 = 0)",
            ladder=prove_edge_in_false_rung_branch,
            expect={"Result1": 0, "Result2": 0},
            scans=SCANS,
        ),
        case(
            "An edge contact's memory starts off when the PLC goes to RUN: a bit on "
            "from the first scan, written above a rise contact, fires it on that scan "
            "and latches a flag the case reads (Result1 = 1); Result2 = 0 shows the "
            "case itself did not run on a first scan",
            ladder=prove_edge_on_first_scan,
            expect={"Result1": 1, "Result2": 0},
        ),
    ],
)


def test_coils():
    assert_all_passed(run(group))
