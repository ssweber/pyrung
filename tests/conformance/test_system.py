"""System points a ladder reads in RUN: the ``system`` group (subroutine ``Conf_System``).

Fault flags, the scan clock and scan counter, the free-running clocks, the
mode bits and the real-time clock.  The ladder reads them as pyrung's system
points (``system.fault.division_error`` and so on), which the TagMap exports
as their SC/SD addresses.  A raw ``sc[40]`` or ``sd[9]`` is a plain tag in
pyrung that nothing drives, so it would read 0 in-process and on the local
sim, and the real flag only on a CLICK.

The cases use C901.., timers T101.. and DD901.. of the group's memory
(``memory.OWNED``), ten per case.

Fault flags.  Each fault case counts its own scans (``scans.py``), raises its
fault on scans 3, 4 and 6, and records over scans 2..8 when the flag reads
on at three points of the scan: at the case's first rung (Result1), right after
the faulting instruction (Result2), and after a later instruction of the same
kind that succeeds (Result3).  A record lists the scans as digits, in order:
346 means scans 3, 4 and 6, 0 means never.  pyrung clears SC40, SC43 and SC44
at the start of every scan and sets one when an instruction raises its fault,
so the flag is on from the faulting instruction to the end of that scan:
Result1 = 0, Result2 = Result3 = 346.  Scan 4 raises the fault again on the
scan after scan 3, so the flag reads off at the top of scan 4 and on again
after the fault: a retriggered flag is not "still on".  Other models read:

- latched until STOP or a reset: Result1 = 45678, Result2 = 345678 (both wrap
  an Int, and fail either way);
- set at the end of the scan, visible on the next: Result1 = Result2 = 457;
- the status of the last instruction of that kind: Result3 = 0.

Scan clock and counter.  ``scan_counter_steps_by_one`` shows SD9 counts every
scan (7 steps of one over scans 1..8), and ``scan_clock_follows_counter`` that
SC3 toggles every scan (8 edges over 8 scans) and is on exactly when SD9 is
odd.  pyrung's SD9 is 0 on the first scan, so that phase means SC3 starts off;
scan 0 itself is out of reach (see below).  A CLICK whose SD9 starts at 1, or
whose SC3 starts on, reads Result2 = 0.

Clocks.  ``clock_bits_toggle_at_their_rate`` counts rising edges of SC5 and
SC6 over a 2 s timer: 20 and 4 when the named time is the full period (on
half, off half), as pyrung has it.  Clocks named for their half period read 10
and 2; for a quarter period 40 and 8.  The edges are ``rise()`` contacts, first
on their rungs.  Over the wire the window is wall clock
and its ends fall between scans, so each count may be one off.  A scan longer
than 40 ms (SD10, pyrung's time since the previous scan) could hide a whole
half period of SC5, and the local sim does stall that long on a loaded
machine, so such a scan starts the window again; the case fires after the
first 2 s without one.  SC4 (10 ms) is left out: at pyrung's nominal 10 ms scan
it aliases (in-process it never reads on).  So is SC7 (1 s): over 2 s a
half-period SC7 also reads 1 or 2, so it would tell nothing SC6 doesn't.

``mode_bits_on_in_run`` and ``rtc_seconds_advance`` are sanity cases: the ladder
only runs in RUN, so it can't see either mode bit off, and an RTC whose seconds
stand still reads 0.

Not here: ``first_scan`` and everything about STOP->RUN.  The suite runs long
after scan 0 and nothing switches modes.  One form would reach SC2 without a
mode harness: a rung outside the ``Cmd`` gate that adds 1 to a register on
every scan SC2 is on, which the reset leaves alone (``keep``), and a case that
reports it.  It reads 1 if SC2 was on for one scan since the PLC last went to
RUN, but it says nothing about which scan, and it only means something on a
CLICK that went to RUN after its program was loaded.

The fault cases share their flags with every other group on the wire.  This
group's subroutine is called first (``suite.ALL_GROUPS``), so at its first rung
no instruction has run yet this scan and, under pyrung's semantics, Result1
reads 0 whatever the other groups do; Result2 and Result3 are read before any
other group runs.  Called later, a group that faulted on one of its recorded
scans would show up in Result1.  The other groups' flag reads expect a flag on,
so this group's faults can only make one of those pass, never fail.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, fall, fill, on_delay, rise
from pyrung.core.system_points import system
from tests.conformance.memory import ClickTimer, dd
from tests.conformance.scans import WINDOW, count_scans, done, edges, scans_for

SCANS = scans_for()
#: The scans a fault case raises its fault on.
FAULT_SCANS = (3, 4, 6)

#: Timer windows (ms) for the wall-clock cases.
CLOCK_WINDOW_MS = 2000
RTC_WINDOW_MS = 1500
#: SC5 and SC6.
CLOCKS = (system.sys.clock_100ms, system.sys.clock_500ms)
#: A scan (SD10, ms) longer than this restarts the clock case's window.
LONG_SCAN_MS = 40


def _record(slot, field, *conditions):
    """Append the scan number, as a digit, to *field* on scans 2..8 where *conditions* hold."""
    with Rung(slot.Cmd != 0, slot.Scratch >= 2, slot.Scratch <= WINDOW, *conditions):
        calc(field * 10 + slot.Scratch, field)


def _on_fault_scans(slot):
    return (slot.Cmd != 0, Or(*(slot.Scratch == n for n in FAULT_SCANS)))


def _in_window(slot):
    return (slot.Cmd != 0, slot.Scratch <= WINDOW)


def prove_division_flag_clears_each_scan(slot):
    # Result4 is the divisor: the reset zeroes it and nothing writes it.
    flag = system.fault.division_error
    count_scans(slot)
    _record(slot, slot.Result1, flag)
    with Rung(*_on_fault_scans(slot)):
        calc(slot.Scratch / slot.Result4, dd[901])
    _record(slot, slot.Result2, flag)
    with Rung(*_in_window(slot)):
        calc(100 / slot.Scratch, dd[902])
    _record(slot, slot.Result3, flag)
    done(slot)


def prove_out_of_range_flag_clears_each_scan(slot):
    flag = system.fault.out_of_range
    count_scans(slot)
    _record(slot, slot.Result1, flag)
    with Rung(*_on_fault_scans(slot)):
        calc(slot.Scratch + 32767, slot.Result4)
    _record(slot, slot.Result2, flag)
    with Rung(*_in_window(slot)):
        calc(slot.Scratch + 1, slot.Result4)
    _record(slot, slot.Result3, flag)
    done(slot)


def prove_address_flag_clears_each_scan(slot):
    # Result4 is the pointer: 0 (no DD0) on the faulting copy, DD922 on the good one.
    flag = system.fault.address_error
    count_scans(slot)
    _record(slot, slot.Result1, flag)
    with Rung(*_on_fault_scans(slot)):
        copy(0, slot.Result4)
        copy(dd[slot.Result4], dd[921])
    _record(slot, slot.Result2, flag)
    with Rung(*_in_window(slot)):
        copy(922, slot.Result4)
        copy(dd[slot.Result4], dd[921])
    _record(slot, slot.Result3, flag)
    done(slot)


def prove_scan_counter_steps_by_one(slot):
    # Result2 holds SD9 from the previous scan, Result3 the step since then.
    # pyrung's SD9 wraps from 32767 to 0, a step of -32767.
    counter = system.sys.scan_counter
    count_scans(slot)
    with Rung(*_in_window(slot)):
        calc(counter - slot.Result2, slot.Result3)
    with Rung(*_in_window(slot), slot.Scratch >= 2, Or(slot.Result3 == 1, slot.Result3 == -32767)):
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(*_in_window(slot)):
        copy(counter, slot.Result2)
    done(slot)


def prove_scan_clock_follows_counter(slot):
    # Result3 = SD9 mod 2 on the scan in progress.
    clock = system.sys.scan_clock_toggle
    count_scans(slot)
    edges(rise(clock), slot, slot.Result1)
    edges(fall(clock), slot, slot.Result1)
    with Rung(*_in_window(slot)):
        calc(system.sys.scan_counter % 2, slot.Result3)
    with Rung(*_in_window(slot), clock, slot.Result3 == 1):
        calc(slot.Result2 + 1, slot.Result2)
    with Rung(*_in_window(slot), ~clock, slot.Result3 == 0):
        calc(slot.Result2 + 1, slot.Result2)
    done(slot)


def prove_clock_bits_toggle_at_their_rate(slot):
    # A scan longer than LONG_SCAN_MS could hide a whole half period of SC5, so
    # one starts the window again: its timer rung goes false for that scan, and
    # the last rung zeroes the counts.  Once Fired is up, nothing restarts.
    window = ClickTimer(101)
    scan_ms = system.sys.scan_time_current_ms
    with Rung(slot.Cmd != 0, Or(slot.Fired != 0, scan_ms <= LONG_SCAN_MS)):
        on_delay(window, CLOCK_WINDOW_MS)
    for clock, field in zip(CLOCKS, (slot.Result1, slot.Result2), strict=True):
        with Rung(rise(clock), slot.Cmd != 0, ~window.Done):
            calc(field + 1, field)
    with Rung(slot.Cmd != 0, window.Done):
        copy(1, slot.Fired)
    with Rung(slot.Cmd != 0, slot.Fired == 0, scan_ms > LONG_SCAN_MS):
        copy(0, slot.Result1)
        copy(0, slot.Result2)


def prove_mode_bits_on_in_run(slot):
    for bit, field in (
        (system.sys.mode_run, slot.Result1),
        (system.sys.mode_switch_run, slot.Result2),
    ):
        with Rung(slot.Cmd != 0, slot.Fired == 0, bit):
            copy(1, field)
        with Rung(slot.Cmd != 0, slot.Fired == 0, ~bit):
            copy(2, field)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def prove_rtc_seconds_advance(slot):
    # Result2 = SD26 when the timer starts; Result1 = seconds passed, mod 60.
    window = ClickTimer(111)
    second = system.rtc.second
    with Rung(slot.Cmd != 0):
        on_delay(window, RTC_WINDOW_MS)
        copy(second, slot.Result2, oneshot=True)
    with Rung(slot.Cmd != 0, window.Done):
        calc((second - slot.Result2 + 60) % 60, slot.Result1, oneshot=True)
        copy(1, slot.Fired)


def init_division_flag_clears_each_scan(slot):
    fill(0, dd.select(901, 902))


_FAULT_EXPECT = {"Result1": 0, "Result2": 346, "Result3": 346}

group = Group(
    "system",
    [
        case(
            "The division-by-zero flag SC40 is off at the start of every scan and on "
            "from a division by zero to the end of that scan. Dividing by zero on scans "
            "3, 4 and 6, and listing the scans of 2..8 it reads on as digits: never at "
            "the case's first rung (Result1 = 0), on scans 3, 4 and 6 right after the "
            "division (Result2 = 346) and after a later division that succeeds "
            "(Result3 = 346)",
            ladder=prove_division_flag_clears_each_scan,
            init=init_division_flag_clears_each_scan,
            expect=_FAULT_EXPECT,
            scans=SCANS,
        ),
        case(
            "The out-of-range flag SC43 is off at the start of every scan and on from "
            "an overflowing calc to the end of that scan. Overflowing an Int on scans "
            "3, 4 and 6, and listing the scans of 2..8 it reads on as digits: never at "
            "the case's first rung (Result1 = 0), on scans 3, 4 and 6 right after the "
            "calc (Result2 = 346) and after a later calc that fits (Result3 = 346)",
            ladder=prove_out_of_range_flag_clears_each_scan,
            expect=_FAULT_EXPECT,
            scans=SCANS,
        ),
        case(
            "The address-error flag SC44 is off at the start of every scan and on from "
            "a copy through an out-of-range pointer to the end of that scan. Copying "
            "through pointer 0 on scans 3, 4 and 6, and listing the scans of 2..8 it "
            "reads on as digits: never at the case's first rung (Result1 = 0), on scans "
            "3, 4 and 6 right after the copy (Result2 = 346) and after a later pointer "
            "copy in range (Result3 = 346)",
            ladder=prove_address_flag_clears_each_scan,
            expect=_FAULT_EXPECT,
            scans=SCANS,
        ),
        case(
            "The scan counter SD9 goes up by one every scan: over scans 1..8 it steps "
            "by one 7 times (Result1 = 7)",
            ladder=prove_scan_counter_steps_by_one,
            expect={"Result1": 7},
            scans=SCANS,
        ),
        case(
            "The scan clock SC3 toggles every scan and is on exactly when the scan "
            "counter SD9 is odd, so it was off on the first scan: over 8 scans it "
            "rises or falls 8 times (Result1 = 8) and matches SD9's parity on all 8 "
            "(Result2 = 8)",
            ladder=prove_scan_clock_follows_counter,
            expect={"Result1": 8, "Result2": 8},
            scans=SCANS,
        ),
        case(
            "The 100 ms and 500 ms clocks SC5 and SC6 have the named time as their "
            "full period: over 2 s of scans no longer than 40 ms (SD10) they rise 20 "
            "times (Result1 in 19..21) and 4 times (Result2 in 3..5)",
            ladder=prove_clock_bits_toggle_at_their_rate,
            expect={"Result1": range(19, 22), "Result2": range(3, 6)},
            scans=CLOCK_WINDOW_MS // 10 + 5,
        ),
        case(
            "The ladder reads the PLC mode bit SC11 and the mode switch bit SC10 on "
            "in RUN (Result1 = 1, Result2 = 1; 2 would mean off)",
            ladder=prove_mode_bits_on_in_run,
            expect={"Result1": 1, "Result2": 1},
        ),
        case(
            "The real-time clock runs while the ladder does: its seconds SD26 move "
            "on by 1 or 2 over a 1.5 s timer (Result1 in 1..2, mod 60)",
            ladder=prove_rtc_seconds_advance,
            expect={"Result1": range(1, 3)},
            scans=RTC_WINDOW_MS // 10 + 5,
        ),
    ],
)


def test_system():
    assert_all_passed(run(group))
