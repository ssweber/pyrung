"""Proposed rewrite of coils/edge_sees_previous_scan_end and edge_before_its_writer (scratch)."""
from pyrung.conformance import Group, case, run, format_report
from pyrung.core import Rung, calc, fall, latch, out, reset, rise
from tests.conformance.memory import c
from tests.conformance.scans import WINDOW, count_scans, done, scans_for

SCANS = scans_for()


def firings(edge, slot, dest, window=WINDOW):
    """dest = the scans *edge* passed power, as decimal digits (scans 3, 4 -> 34)."""
    with Rung(edge, slot.Cmd != 0, slot.Scratch <= window):
        calc(dest * 10 + slot.Scratch, dest)


def prove_edge_sees_previous_scan_end(slot):
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        latch(c[1541])
        reset(c[1542])
    firings(rise(c[1541]), slot, slot.Result1)
    firings(rise(c[1541]), slot, slot.Result2)   # a second contact, same bit, same place
    firings(fall(c[1542]), slot, slot.Result3)
    firings(fall(c[1542]), slot, slot.Result4)
    with Rung(slot.Cmd != 0):
        reset(c[1541])
        latch(c[1542])
    done(slot)


def prove_edge_before_its_writer(slot):
    count_scans(slot)
    firings(rise(c[1551]), slot, slot.Result1)
    firings(fall(c[1551]), slot, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3, slot.Scratch <= 5):
        out(c[1551])
    firings(rise(c[1551]), slot, slot.Result3)
    firings(fall(c[1551]), slot, slot.Result4)
    done(slot)


def init_edge_sees_previous_scan_end(slot):
    reset(c[1541])
    reset(c[1542])


group = Group("coils", [
    case("prev-scan-end", ladder=prove_edge_sees_previous_scan_end,
         init=init_edge_sees_previous_scan_end,
         expect={"Result1": 345, "Result2": 345, "Result3": 345, "Result4": 345}, scans=SCANS),
    case("before-writer", ladder=prove_edge_before_its_writer,
         expect={"Result1": 0, "Result2": 0, "Result3": 3, "Result4": 6}, scans=SCANS),
])

if __name__ == "__main__":
    print(format_report(run(group)))
