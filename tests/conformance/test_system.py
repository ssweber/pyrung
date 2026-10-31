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
the three cases are ``pending="SYS"``.  hardware round 2, 2026-10-30: SC43 and SC44 follow
the last instruction (0, 0, 0); SC40 follows the last Math (346, 0, 0), so a
Copy leaves it as it is.

What else clears a flag (probes, ``pending="SYS"``).  Round 2 tried a Copy and a
Math only.  Three more cases raise their fault again before each instruction
under test, so each probe reads the flag right after one instruction and
nothing else: a fault, the instruction, the probe coil, the next fault, ...
Result1 is the control, a probe right after the fault with nothing between
(346 on every model: a 0 means the fault did not raise the flag).

- ``out_of_range_flag_vs_other_instructions`` (SC43, a Dint Math overflow)
  and ``address_flag_vs_other_instructions`` (SC44, a copy through pointer
  ``Fired``, 0 while the case runs): Result2 after an Out coil on a powered
  rung, Result3 after a rung whose contact is open over a Set coil (contacts
  only: no instruction executes), Result4 after a TON, ErrorCode after a CTU.
  The models were: every instruction that executes clears the flag and an
  open rung does not: 346, 0, 346, 0, 0; only box instructions (Copy, Math,
  timer, counter) clear it: 346, 346, 346, 0, 0; coils too, even unexecuted:
  346, 0, 0, 0, 0; the flag on to the end of the scan (pyrung): 346 in every
  field.  Hardware round 2 (2026-10-31): SC44 reads 346 in every field, so a
  coil, an open rung, a TON and a CTU all leave it as it is (only a Copy or a
  Math clears it, ``address_flag_clears_each_scan``), and pyrung matches.
  Since then the case records scans 3, 4 and 6 only, which leaves that
  reading as it was and keeps the other SC44 cases' faults out of it.
  SC43 reads 0 in every field, the control too: a Dint Math that wraps does
  not raise SC43 on a CLICK (an Int Math overflow does,
  ``out_of_range_flag_clears_each_scan``), so the case asks nothing about
  what clears it; pyrung raises SC43 on the Dint wrap and reads 346 in every
  field (step SYS).  ``out_of_range_int_vs_other_instructions`` asks the
  question again with an Int overflow, and the ``faults`` group asks which
  stores raise SC43.
- ``out_of_range_int_vs_other_instructions`` (SC43, probe): the question
  ``out_of_range_flag_vs_other_instructions`` could not ask, with a fault
  that does raise SC43 on a CLICK, an Int Math overflow (Scratch + 32767 into
  Result4, as ``out_of_range_flag_clears_each_scan`` faults), and the same
  steps on their own bits.  The fault writes a slot field, so the scans are
  recorded in DD961..DD965, on scans 3, 4 and 6 only, and copied into the
  fields once the window ends.
  Adopted, as SC44 read: only a Copy or a Math clears it, 346 in every field
  (pyrung reads the same).  Every instruction that executes clears it: 346, 0,
  346, 0, 0; and so on, as above.
- ``division_flag_vs_hex_math`` (SC40, a division by ``Fired``): Result2 after
  a hex-mode Math (WORDs only, no division), Result3 after a Copy, Result4
  after a decimal Math.  Round 2 read Result3 = 346 and Result4 = 0.  Adopted,
  any Math clears it, hex or decimal: 346, 0, 346, 0; a decimal Math only:
  346, 346, 346, 0.  pyrung: 346 in every field.

Scan clock and counter.  ``scan_counter_steps_by_one`` shows SD9 counts every
scan (7 steps of one over scans 1..8), and ``scan_clock_follows_counter`` that
SC3 toggles every scan (8 edges over 8 scans) and is on exactly when SD9 is
even, as round 1 read on a CLICK (pyrung's SC3 was on when SD9 is odd, and
read Result2 = 0, until hardware round 2).  ``first_scan_counter_and_clock`` pins
which of the two is offset from pyrung's: rungs outside the ``Cmd`` gate copy
SD9 and SC3 on the first scan (SC2) into DD981..DD983, which the reset leaves
alone.  SD9 starts at 0 with SC3 on (Result1 = 0, Result3 = 1; hardware round 2, 2026-10-30,
where SD9 starting at 1 with SC3 off was adopted); pyrung read 0, 0 before
round 2: its SD9 was right and its SC3 a scan out of phase.  Result2 = 1 shows the capture ran.  ``scan_counter_wraps_to_zero`` keeps
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
half period of SC5, and a target run on a loaded machine does stall that
long (pyrung's own sim advances a scan at most 40 ms, ``MAX_SCAN_DT_S``, but
the P1AM-200 code times itself), so such a scan starts the window again; the
case fires after the first 2 s without one.  SC4 (10 ms) is left out: at pyrung's nominal 10 ms scan
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
from pyrung.core import (
    Or,
    Rung,
    calc,
    copy,
    count_up,
    fall,
    fill,
    latch,
    on_delay,
    out,
    rise,
)
from pyrung.core.system_points import system
from tests.conformance.memory import ClickCounter, ClickTimer, c, dd, dh
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


#: The fields a refault case records into: the control, then one per instruction.
_REFAULT_FIELDS = ("Result1", "Result2", "Result3", "Result4", "ErrorCode")


def _refault_case(slot, flag, base, fault, steps, records=None, fault_scans_only=False):
    """The fault again before each step, the step, then a probe coil C<base+i>.

    The first step is the control: nothing.  Records as in :func:`_fault_case`,
    or, given *records* (one register per step), into them over the window and
    then into the slot fields on the scan that raises ``Fired``, so the fault
    may write a slot field while the window runs.  *fault_scans_only* records
    the fault's own scans (3, 4 and 6) only, so a field that reads 346 cannot
    pick up another case's fault: on the wire the cases start on different
    scans, and pyrung holds a flag to the end of the scan.
    """
    probes = [c[base + i] for i in range(len(steps))]
    count_scans(slot)
    for step, probe in zip(steps, probes, strict=True):
        with Rung(*_on_fault_scans(slot)):
            fault()
        if step is not None:
            step()
        with Rung(*_in_window(slot), flag):
            out(probe)
    fields = [getattr(slot, name) for name in _REFAULT_FIELDS]
    for into, probe in zip(records or fields, probes, strict=False):
        if fault_scans_only:
            _record(slot, into, probe, _on_fault_scans(slot)[1])
        else:
            _record(slot, into, probe)
    if records is not None:
        with Rung(slot.Cmd != 0, slot.Scratch > WINDOW):
            for field, into in zip(fields, records, strict=False):
                copy(into, field)
    done(slot)


def _other_instructions(slot, base, timer, counter):
    """An Out coil, an open rung over a Set coil, a TON and a CTU, each on its rung.

    C<base+5> is the Out coil, C<base+6> a bit nothing sets, C<base+7> the Set coil.
    """

    def coil():
        with Rung(*_in_window(slot)):
            out(c[base + 5])

    def open_rung():
        with Rung(*_in_window(slot), c[base + 6]):
            latch(c[base + 7])

    def ton():
        with Rung(*_in_window(slot)):
            on_delay(ClickTimer(timer), 30000)

    def ctu():
        with Rung(*_in_window(slot)):
            count_up(ClickCounter(counter), 1000).reset(slot.Cmd == 0)

    return [None, coil, open_rung, ton, ctu]


def prove_out_of_range_flag_vs_other_instructions(slot):
    _refault_case(
        slot,
        system.fault.out_of_range,
        931,
        fault=lambda: calc(slot.Scratch + 2147483647, dd[931]),
        steps=_other_instructions(slot, 931, timer=131, counter=171),
    )


def prove_address_flag_vs_other_instructions(slot):
    # Fired is 0 until the window is recorded: there is no DD0.
    _refault_case(
        slot,
        system.fault.address_error,
        941,
        fault=lambda: copy(dd[slot.Fired], dd[941]),
        steps=_other_instructions(slot, 941, timer=141, counter=172),
        fault_scans_only=True,
    )


def prove_out_of_range_int_vs_other_instructions(slot):
    # The fault writes Result4 while the window runs, so the scans are
    # recorded in DD961..DD965 and copied into the fields at the end.
    _refault_case(
        slot,
        system.fault.out_of_range,
        961,
        fault=lambda: calc(slot.Scratch + 32767, slot.Result4),
        steps=_other_instructions(slot, 961, timer=112, counter=173),
        records=[dd[961 + i] for i in range(len(_REFAULT_FIELDS))],
        fault_scans_only=True,
    )


def prove_division_flag_vs_hex_math(slot):
    # Fired is 0 until the window is recorded: the divisor.
    def step(instruction):
        def run():
            with Rung(*_in_window(slot)):
                instruction()

        return run

    _refault_case(
        slot,
        system.fault.division_error,
        951,
        fault=lambda: calc(slot.Scratch / slot.Fired, dd[951]),
        steps=[
            None,
            step(lambda: calc(dh[401] + 1, dh[402])),
            step(lambda: copy(slot.Scratch, dd[952])),
            step(lambda: calc(slot.Scratch + 1, dd[953])),
        ],
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


#: SC43 and SC44 follow the last instruction (hardware round 2), so every probe reads them off.
_FAULT_EXPECT = {"Result1": 0, "Result2": 0, "Result3": 0}


def init_out_of_range_int_vs_other_instructions(slot):
    fill(0, dd.select(961, 965))


def init_division_flag_vs_hex_math(slot):
    fill(0, dd.select(951, 959))
    fill(0, dh.select(401, 402))


group = Group(
    "system",
    [
        case(
            "The division-by-zero flag SC40 shows the last Math: dividing by zero on "
            "scans 3, 4 and 6, it is still on right after a plain copy (Result1 = 346), "
            "and off after a Math that does not divide (Result2 = 0) and a division that "
            "succeeds (Result3 = 0), each run after the fault on the same scan",
            ladder=prove_division_flag_clears_each_scan,
            init=_fault_init(901),
            expect={"Result1": 346, "Result2": 0, "Result3": 0},
            scans=SCANS,
            pending="SYS",
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
            "On the first scan after STOP->RUN the scan counter SD9 reads 0 (Result1) and "
            "the scan clock SC3 is on (Result3 = 1); Result2 = 1 shows the first-scan "
            "rung ran",
            ladder=prove_first_scan_counter_and_clock,
            expect={"Result1": 0, "Result2": 1, "Result3": 1},
        ),
        case(
            "The scan counter SD9 wraps from 32767 to 0, never negative: the least value "
            "it reads since the first scan is not below 0 (Result1 = 0); run once the "
            "PLC has been in RUN for 32768 scans",
            ladder=prove_scan_counter_wraps_to_zero,
            expect={"Result1": 0},
        ),
        case(
            "A Dint Math that wraps does not raise the out-of-range flag SC43: "
            "2147483647 + the scan number into a DD on scans 3, 4 and 6 leaves SC43 off "
            "right after it (Result1 = 0) and after an Out coil (Result2 = 0), a rung "
            "whose open contact keeps its Set coil from executing (Result3 = 0), a TON "
            "(Result4 = 0) and a CTU (ErrorCode = 0)",
            ladder=prove_out_of_range_flag_vs_other_instructions,
            init=_fault_init(931),
            expect=dict.fromkeys(_REFAULT_FIELDS, 0),
            scans=SCANS,
            pending="SYS",
        ),
        case(
            "The address-error flag SC44 is left on by an instruction that is not a Copy "
            "or a Math: raised again before each, it is on on scans 3, 4 and 6 right "
            "after the fault (Result1 = 346), after an Out coil (Result2), a rung whose "
            "open contact keeps its Set coil from executing (Result3), a TON (Result4) "
            "and a CTU (ErrorCode)",
            ladder=prove_address_flag_vs_other_instructions,
            expect=dict.fromkeys(_REFAULT_FIELDS, 346),
            scans=SCANS,
        ),
        case(
            "The out-of-range flag SC43 is left on by an instruction that is not a Copy "
            "or a Math: an Int Math overflow raised again before each, it is on on scans "
            "3, 4 and 6 right after the fault (Result1 = 346), after an Out coil "
            "(Result2), a rung whose open contact keeps its Set coil from executing "
            "(Result3), a TON (Result4) and a CTU (ErrorCode)",
            ladder=prove_out_of_range_int_vs_other_instructions,
            init=init_out_of_range_int_vs_other_instructions,
            expect=dict.fromkeys(_REFAULT_FIELDS, 346),
            scans=SCANS,
            probe="which instructions clear SC43: coil, open rung, timer, counter",
        ),
        case(
            "The division-by-zero flag SC40 is cleared by a hex-mode Math too: raised "
            "again before each, it is on right after the fault (Result1 = 346, scans 3, "
            "4 and 6), off after a WORD-only Math (Result2 = 0), still on after a Copy "
            "(Result3 = 346) and off after a decimal Math (Result4 = 0)",
            ladder=prove_division_flag_vs_hex_math,
            init=init_division_flag_vs_hex_math,
            expect={"Result1": 346, "Result2": 0, "Result3": 346, "Result4": 0},
            scans=SCANS,
            pending="SYS",
            probe="does a hex-mode Math clear SC40",
        ),
    ],
)


def test_system():
    assert_all_passed(run(group))
