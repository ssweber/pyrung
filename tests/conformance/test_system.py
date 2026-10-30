"""System points a ladder reads in RUN: the ``system`` group (subroutine ``Conf_System``).

Fault flags, the scan clock and scan counter, the free-running clocks, the
mode bits and the real-time clock.  The ladder reads them as pyrung's system
points (``system.fault.division_error`` and so on), which the TagMap exports
as their SC/SD addresses.  A raw ``sc[40]`` or ``sd[9]`` is a plain tag in
pyrung that nothing drives, so it would read 0 in-process and on the local
sim, and the real flag only on a CLICK.

The cases use C901.., timers T101.. and DD901.. of the group's memory
(``memory.OWNED``), ten per case.

Fault flags.  Each fault case counts its own scans (``scans.py``) and raises
its fault on scans 3, 4 and 6.  After the fault, three instructions run on
every scan of the window, each followed by a probe coil that copies the flag
the instant after it; only once every probe has run do later rungs record, as
digits over scans 2..8, the scans each probe was on (346 means scans 3, 4 and
6, 0 means never): Result1 after a plain copy, Result2 after a Math of
another kind (one that cannot raise the flag), Result3 after a successful
instruction of the faulting kind.  Hardware round 1 read 346 right after the
fault and 0 after a later successful one, so a CLICK flag does not hold to the
end of the scan.  The adopted model: a flag follows the last instruction, any
instruction (Result1..3 = 0, 0, 0).  The others: any Math clears it (346, 0,
0); only an instruction of its own kind (346, 346, 0); pyrung's flag on from
the fault to the end of the scan (346, 346, 346).  pyrung's is the last, so
the three cases are ``pending="SYS"``.

Scan clock and counter.  ``scan_counter_steps_by_one`` shows SD9 counts every
scan (7 steps of one over scans 1..8), and ``scan_clock_follows_counter`` that
SC3 toggles every scan (8 edges over 8 scans) and is on exactly when SD9 is
even, as round 1 read on a CLICK (``pending="SYS"``: pyrung's SC3 is on when
SD9 is odd, and reads Result2 = 0).  ``first_scan_counter_and_clock`` pins
which of the two is offset from pyrung's: rungs outside the ``Cmd`` gate copy
SD9 and SC3 on the first scan (SC2) into DD981..DD983, which the reset leaves
alone.  Adopted: SD9 starts at 1 with SC3 off (Result1 = 1, Result3 = 0);
SD9 starting at 0 with SC3 on reads Result1 = 0, Result3 = 1; pyrung reads 0,
0.  Result2 = 1 shows the capture ran.  ``scan_counter_wraps_to_zero`` keeps
the least SD9 seen since the first scan in DD991: SD9 wrapping from 32767 to
0 (adopted) reads Result1 = 0, one counting on into negative values (an
unsigned 16-bit counter read as an Int) reads 1.  It only tells once the PLC
has run 32768 scans since it went to RUN (5.5 minutes at 10 ms a scan); before
that both read 0, so against a CLICK the wire test first waits for SD9 to pass
32767 (``wait_for_scan_counter_wrap``).  The first-scan cases read the loaded program's first scan,
whatever run reads them.

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

Not here: everything else about STOP->RUN (SD9 and SC2 back at their start,
retentive memory kept), which needs a harness that switches modes.

The fault cases share their flags with every other group on the wire.  This
group's subroutine is called first (``suite.ALL_GROUPS``), so its probes read
each flag before any other group runs that scan.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, fall, fill, on_delay, out, rise
from pyrung.core.system_points import system
from tests.conformance.memory import ClickTimer, c, dd
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


def _fault_case(slot, flag, base, fault, other_kind, same_kind):
    """A fault on scans 3, 4 and 6, then three instructions each probed by a coil.

    The probes are C<base>..C<base+2>; DD<base+9> takes the plain copy.
    Nothing records until every probe has run.
    """
    probes = [c[base + i] for i in range(3)]
    steps = (lambda: copy(slot.Scratch, dd[base + 9]), other_kind, same_kind)
    count_scans(slot)
    with Rung(*_on_fault_scans(slot)):
        fault()
    for step, probe in zip(steps, probes, strict=True):
        with Rung(*_in_window(slot)):
            step()
        with Rung(*_in_window(slot), flag):
            out(probe)
    for field, probe in zip((slot.Result1, slot.Result2, slot.Result3), probes, strict=True):
        _record(slot, field, probe)
    done(slot)


def prove_division_flag_clears_each_scan(slot):
    # Result4 is the divisor: the reset zeroes it and nothing writes it.
    _fault_case(
        slot,
        system.fault.division_error,
        901,
        fault=lambda: calc(slot.Scratch / slot.Result4, dd[901]),
        other_kind=lambda: calc(slot.Scratch + 1, dd[902]),
        same_kind=lambda: calc(100 / slot.Scratch, dd[903]),
    )


def prove_out_of_range_flag_clears_each_scan(slot):
    # A division cannot overflow an Int here: it is the Math of another kind.
    _fault_case(
        slot,
        system.fault.out_of_range,
        911,
        fault=lambda: calc(slot.Scratch + 32767, slot.Result4),
        other_kind=lambda: calc(100 / slot.Scratch, dd[912]),
        same_kind=lambda: calc(slot.Scratch + 1, slot.Result4),
    )


def prove_address_flag_clears_each_scan(slot):
    # Result4 is the pointer: 0 (no DD0) on the faulting copy, DD922 on the good one.
    def fault():
        copy(0, slot.Result4)
        copy(dd[slot.Result4], dd[921])

    def same_kind():
        copy(922, slot.Result4)
        copy(dd[slot.Result4], dd[921])

    _fault_case(
        slot,
        system.fault.address_error,
        921,
        fault=fault,
        other_kind=lambda: calc(slot.Scratch + 1, dd[923]),
        same_kind=same_kind,
    )


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
    with Rung(*_in_window(slot), clock, slot.Result3 == 0):
        calc(slot.Result2 + 1, slot.Result2)
    with Rung(*_in_window(slot), ~clock, slot.Result3 == 1):
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


def prove_first_scan_counter_and_clock(slot):
    # Outside the Cmd gate, on the first scan only: DD981 = SD9, DD982 = 1,
    # DD983 = SC3.  The reset leaves them alone.
    first = system.sys.first_scan
    with Rung(first):
        copy(system.sys.scan_counter, dd[981])
        copy(1, dd[982])
        copy(0, dd[983])
    with Rung(first, system.sys.scan_clock_toggle):
        copy(1, dd[983])
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(dd[981], slot.Result1)
        copy(dd[982], slot.Result2)
        copy(dd[983], slot.Result3)
        copy(1, slot.Fired)


def prove_scan_counter_wraps_to_zero(slot):
    # Outside the Cmd gate: DD991 is the least SD9 since the first scan.
    counter = system.sys.scan_counter
    with Rung(system.sys.first_scan):
        copy(counter, dd[991])
    with Rung(counter < dd[991]):
        copy(counter, dd[991])
    with Rung(slot.Cmd != 0, slot.Fired == 0, dd[991] < 0):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Fired == 0):
        copy(1, slot.Fired)


def _fault_init(base):
    def init(slot):
        fill(0, dd.select(base, base + 9))

    return init


#: The adopted model: a flag follows the last instruction, so every probe reads it off.
_FAULT_EXPECT = {"Result1": 0, "Result2": 0, "Result3": 0}
_FAULT_PROBE = "fault flags: which instruction clears them"

group = Group(
    "system",
    [
        case(
            "The division-by-zero flag SC40 shows the last instruction: dividing by zero "
            "on scans 3, 4 and 6, it reads off right after a plain copy (Result1 = 0), a "
            "Math that does not divide (Result2 = 0) and a division that succeeds "
            "(Result3 = 0), each run after the fault on the same scan",
            ladder=prove_division_flag_clears_each_scan,
            init=_fault_init(901),
            expect=_FAULT_EXPECT,
            scans=SCANS,
            pending="SYS",
            probe=_FAULT_PROBE,
        ),
        case(
            "The out-of-range flag SC43 shows the last instruction: overflowing an Int on "
            "scans 3, 4 and 6, it reads off right after a plain copy (Result1 = 0), a "
            "Math that cannot overflow (Result2 = 0) and a calc that fits (Result3 = 0), "
            "each run after the fault on the same scan",
            ladder=prove_out_of_range_flag_clears_each_scan,
            init=_fault_init(911),
            expect=_FAULT_EXPECT,
            scans=SCANS,
            pending="SYS",
            probe=_FAULT_PROBE,
        ),
        case(
            "The address-error flag SC44 shows the last instruction: copying through "
            "pointer 0 on scans 3, 4 and 6, it reads off right after a plain copy "
            "(Result1 = 0), a Math (Result2 = 0) and a pointer copy in range (Result3 = "
            "0), each run after the fault on the same scan",
            ladder=prove_address_flag_clears_each_scan,
            init=_fault_init(921),
            expect=_FAULT_EXPECT,
            scans=SCANS,
            pending="SYS",
            probe=_FAULT_PROBE,
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
            "counter SD9 is even: over 8 scans it rises or falls 8 times (Result1 = 8) "
            "and matches SD9's parity on all 8 (Result2 = 8)",
            ladder=prove_scan_clock_follows_counter,
            expect={"Result1": 8, "Result2": 8},
            scans=SCANS,
            pending="SYS",
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
        case(
            "On the first scan after STOP->RUN the scan counter SD9 reads 1 (Result1) and "
            "the scan clock SC3 is off (Result3 = 0); Result2 = 1 shows the first-scan "
            "rung ran",
            ladder=prove_first_scan_counter_and_clock,
            expect={"Result1": 1, "Result2": 1, "Result3": 0},
            pending="SYS",
            probe="first scan: SD9 starts at 1, or SC3 starts on",
        ),
        case(
            "The scan counter SD9 wraps from 32767 to 0, never negative: the least value "
            "it reads since the first scan is not below 0 (Result1 = 0); run once the "
            "PLC has been in RUN for 32768 scans",
            ladder=prove_scan_counter_wraps_to_zero,
            expect={"Result1": 0},
            probe="SD9 wrap value",
        ),
    ],
)


def test_system():
    assert_all_passed(run(group))
