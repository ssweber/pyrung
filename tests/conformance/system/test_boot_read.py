"""What a boot leaves behind, flags across a scan and a For past 16 bits: part ``boot``.

The groups ``boot_read``, ``rtc_set``, ``for_wide`` and ``flag_scan``.  ``for_wide`` may make a
scan of about 1.1 s (``loop/negative_count_reads_unsigned`` measured that for 65535 runs of one
Math), or trip the watchdog, so it holds its long loops for a second, by when the others have
fired.  It stays above ``flag_scan`` in the program, because that case needs no Math below its op
on the scan, and a scan count is a Math.

``boot_read`` needs a person; ``tests/conformance/AGENTS.md`` has the procedure.  A run writes
sentinels and reads what the previous boot's first scan found.  Its fields are ranges: what it
reads depends on the steps taken before the run, so the harness prints the reading and asserts
none.

- ``what_survives``: on its first scan, the group records which sentinels the last run left (a mask,
  in a register the first scan writes).  Result1, written directly: C 1, TD 2, CTD 4, DS 8, DD 16,
  DF 32, DH 64, TXT 128, T of a done TON 256, CT of a done counter 512.  Result2, a TD written by
  a pointer: 1.  Result3 is SC203, ``_Battery_Installed``: 1 where the project's Battery Backup
  Setup box is ticked.  It is a setting that travels with the project, not a detection (a CLICK
  cannot detect its battery).  Run fresh: 0, 0.  Over STOP->RUN a CLICK keeps the retentive blocks
  (``DEFAULT_RETENTIVE``: CT, CTD, DS, DD, DH, DF, TXT = 4 + 8 + 16 + 32 + 64 + 128 + 512 = 764,
  and 0) and clears the others, C, TD, T and the pointer TD.  A power cycle with the battery in
  reads the same, 764, 0, and so does a short one with it out, since a capacitor holds memory for
  a few days (SC27 says when it ran out).  pyrung's ``reboot()`` reads 764, 0 with
  ``battery_present`` and 0, 0 without it, the worst case: the capacitor gone too.  Answered for
  this project's default retention settings.
- ``first_scan_edges``: rungs true from the first scan, outside the ``Cmd`` gate, each captured by
  the first scan (``keep``).  Result1 by bit, 1 where it acted on the first scan: toggle 1, one-shot
  Copy 2, one-shot Out 4, a timed coil 8, one-shot For 16, an event drum's rising event 32, a shift's
  clock 64.  Result2 is SD9 on the first scan.  Result3 counts scans ``first_scan`` was true since the last run, read
  once it has had 10 scans, and the read starts it over.  Result4 the flasher on its first scan (1).
  pyrung: 95 (all but the drum, which starts on step 1 that scan), 0, 1, 1.
  0 in a bit of Result1 reads as a rung taken to have been high before.  Result3 = 0 means no first
  scan since the last run: a run with no boot before it, or a boot ``first_scan`` missed, and then
  the other fields are the boot before's.

``for_wide``:

- ``count_forms``: a For's count from a DD past 16 bits is read whole (hardware 2026-11-05): 65537
  runs 65537 (Result1 = 2; 1 for the low word's 1), 65536 runs 65536 (Result2 = 2; 1 for none).
  A DD counts, since an Int would wrap back onto 1.  A TD at 3 (Result3) and a CTD at 3 (Result4)
  count 3.  Whole, those two For loops take about 2.2 s, and the case starts them 1 s after its
  ``Cmd``.

``flag_scan``, ahead of ``store_probe`` so that no flag it raises reaches a reader's scan:

- ``math_flags_over_a_scan``: ``_clear_transient_status`` clears SC40 and SC46 at scan start.  Each op
  is the last Math of its scan (the case starts last and waits 40 scans for its neighbours), read on the next scan
  above the case's own count, the first Math there.  Result1 = 1 for SC40 still on (0 cleared),
  Result2 = 1 for SC46 still on (0 cleared).  ``ErrorCode`` = 11 is the control: SC40 (tens) and
  SC46 (units) were each on right after the op.  SC43 and SC44 are not asked: main's hash Copy on every scan settles them.

``rtc_set``:

- ``date_and_time_apply``: a date set by SC53 and a time by SC55 (each from a coil on one scan), read
  2 scans later, then set back.  Year 2030 applied: Result1 = 1.  Hour moved: Result2 = 1.  A bad date
  (month 13) raises SC54 and a bad time (hour 25) SC56 (Result3 = 11), and neither moves the clock
  (Result4 = 1).  Last, whatever those read, the date and time go back to what scan 1 saved, in case a
  bad one was taken; ErrorCode = 1 reads them back.  Backends that keep no settable clock read 0s, so
  each field is a range.  A minute that turns over in those scans comes through.

The cases own what ``memory.OWNED`` lists under these groups.
"""

from __future__ import annotations

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance._ladder import emit_case
from pyrung.conformance._runner import DT, _fire, _reset
from pyrung.conformance._slot import make_slot
from pyrung.core import (
    PLC,
    Or,
    Program,
    Rung,
    call,
    copy,
    count_up,
    event_drum,
    fill,
    flasher,
    forloop,
    latch,
    math,
    on_delay,
    out,
    shift,
    subroutine,
    system,
    timed,
    toggle,
)
from pyrung.core.compiled_plc import CompiledPLC
from pyrung.core.expression import sqrt
from tests.conformance.memory import (
    ClickCounter,
    ClickTimer,
    c,
    ct,
    ctd,
    dd,
    df,
    dh,
    ds,
    t,
    td,
    txt,
)
from tests.conformance.probe_kit import between, once
from tests.conformance.scans import count_scans, done, scans_for

FIRST = system.sys.first_scan
SC40 = system.fault.division_error
SC46 = system.fault.math_operation_error


def _noted(mask, *pairs):
    """On the first scan: *mask* = the sum of the bits whose condition holds."""
    with Rung(FIRST):
        copy(0, mask)
    for condition, bit in pairs:
        with Rung(FIRST, *condition):
            math(mask + bit, mask)


#: A sentinel, in each bank a CLICK has one.
_SEEN = 7

# A pointer write counts as a write to the whole bank unless its choices bound it.
_TD_POINTER = ds[4020]
ds.slot(4020, choices={0: "unset", 118: "the reached TD"})


def prove_what_survives(slot):
    direct_mask, pointer_mask = dd[65], dd[66]
    _noted(
        direct_mask,
        ((c[1603],), 1),
        ((td[116] == _SEEN,), 2),
        ((ctd[213] == _SEEN,), 4),
        ((ds[4017] == _SEEN,), 8),
        ((dd[67] == _SEEN,), 16),
        ((df[203] == _SEEN,), 32),
        ((dh[71] == _SEEN,), 64),
        ((txt[781] == "Z",), 128),
        ((t[117],), 256),
        ((ct[214],), 512),
    )
    _noted(pointer_mask, ((td[118] == _SEEN,), 1))
    # Written once per run and kept; the T and CT are the Done of a preset of 1.
    with Rung(*once(slot)):
        latch(c[1603])
        copy(_SEEN, td[116])
        copy(_SEEN, ctd[213])
        copy(_SEEN, ds[4017])
        copy(_SEEN, dd[67])
        copy(float(_SEEN), df[203])
        copy(_SEEN, dh[71])
        copy("Z", txt[781])
        copy(118, _TD_POINTER)
        copy(_SEEN, td[_TD_POINTER])
    with Rung(c[1603]):
        on_delay(ClickTimer(117), 1)
    with Rung(slot.Cmd != 0):
        count_up(ClickCounter(214), 1).reset(c[1620])
    with Rung(*once(slot)):
        copy(direct_mask, slot.Result1)
        copy(pointer_mask, slot.Result2)
    with Rung(*once(slot), system.sys.battery_present):
        copy(1, slot.Result3)
    with Rung(*once(slot)):
        copy(1, slot.Fired)


def prove_first_scan_edges(slot):
    mask = dd[68]
    tog, shot_out, pulse, flash = c[1604], c[1605], c[1606], c[1609]
    high = c[1610]
    a, b, flag = c[1614], c[1615], c[1616]
    shot_copy, for_copy = dd[69], dd[70]
    with Rung():
        out(high)
    with Rung(high):
        toggle(tog)
    with Rung(high):
        copy(1, shot_copy, oneshot=True)
    with Rung(high):
        out(shot_out, oneshot=True)
    with Rung(high):
        timed(pulse, ClickTimer(119), 1000)
    with Rung(high):
        with forloop(1, oneshot=True):
            copy(1, for_copy)
    with Rung(high):
        event_drum(
            outputs=[a, b],
            events=[high, high],
            pattern=[[1, 0], [0, 1]],
            current_step=ds[4019],
            completion_flag=flag,
        ).reset(c[1617])
    with Rung(high):
        shift(c.select(1618, 1619)).clock(high).reset(c[1617])
    with Rung(high):
        flasher(flash, on=(ClickTimer(120), 1000), off=(ClickTimer(121), 1000))
    _noted(
        mask,
        ((tog,), 1),
        ((shot_copy == 1,), 2),
        ((shot_out,), 4),
        ((pulse,), 8),
        ((for_copy == 1,), 16),
        ((ds[4019] == 2,), 32),
        ((c[1618],), 64),
    )
    with Rung(FIRST):
        copy(0, dd[71])
    with Rung(FIRST, flash):
        copy(1, dd[71])
    with Rung(FIRST):
        copy(system.sys.scan_counter, dd[72])
    with Rung(FIRST):
        math(dd[73] + 1, dd[73])
    count_scans(slot, 10)
    # Scratch holds at 11; Fired comes up on this scan, so this reads once.  The count
    # starts over, so the next run counts the boot between them and no other.
    with Rung(slot.Cmd != 0, slot.Scratch == 11, slot.Fired == 0):
        copy(mask, slot.Result1)
        copy(dd[72], slot.Result2)
        copy(dd[73], slot.Result3)
        copy(dd[71], slot.Result4)
        copy(0, dd[73])
    done(slot, 10)


boot_group = Group(
    "boot_read",
    [
        case(
            "What a boot keeps, by mask: written directly (Result1), by a pointer (Result2), with "
            "the battery bit (Result3); a run from fresh reads 0, 0",
            ladder=prove_what_survives,
            keep=[
                c[1603],
                td[116],
                ctd[213],
                ds[4017],
                dd[67],
                df[203],
                dh[71],
                txt[781],
                td[118],
                _TD_POINTER,
                dd[65],
                dd[66],
                t[117],
                td[117],
                ct[214],
                ctd[214],
            ],
            expect={
                "Result1": between(0, 1023),
                "Result2": between(0, 1),
                "Result3": between(0, 1),
            },
            probe="what a boot keeps: run after each boot step (AGENTS.md)",
        ),
        case(
            "A rung true on the first scan: what acted (Result1, by bit), SD9 on the first scan "
            "(Result2), scans first_scan is true (Result3), a flasher on (Result4)",
            ladder=prove_first_scan_edges,
            scans=scans_for(10),
            keep=[
                c[1604],
                c[1605],
                c[1606],
                c[1609],
                c[1610],
                c[1614],
                c[1615],
                c[1616],
                c[1618],
                c[1619],
                dd[68],
                dd[69],
                dd[70],
                dd[71],
                dd[72],
                dd[73],
                ds[4019],
                t[119],
                td[119],
                t[120],
                td[120],
                t[121],
                td[121],
            ],
            expect={
                "Result1": between(0, 127),
                "Result2": between(0, 1),
                "Result3": between(0, 100),
                "Result4": between(0, 1),
            },
            probe="each edge's memory on the first scan; first_scan and SD9 at boot",
        ),
    ],
)


def prove_count_forms(slot):
    with Rung(slot.Cmd != 0):
        on_delay(ClickTimer(123), 1000)
    with Rung(slot.Cmd != 0, slot.Scratch <= 3, t[123]):
        math(slot.Scratch + 1, slot.Scratch)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(65537, dd[61])
        copy(65536, dd[62])
        copy(3, td[125])
        copy(3, ctd[212])
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        with forloop(dd[61]):
            math(dd[63] + 1, dd[63])
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        with forloop(dd[62]):
            math(dd[64] + 1, dd[64])
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        with forloop(td[125]):
            math(slot.Result3 + 1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == 2):
        with forloop(ctd[212]):
            math(slot.Result4 + 1, slot.Result4)
    for value, readings, into in (
        (dd[63], (1, 65537), slot.Result1),
        (dd[64], (0, 65536), slot.Result2),
    ):
        for number, reading in enumerate(readings, start=1):
            with Rung(slot.Cmd != 0, slot.Scratch == 3, value == reading):
                copy(number, into)
    done(slot, 3)


def init_count_forms(slot):
    fill(0, dd.select(61, 64))
    copy(0, td[125])
    copy(0, ctd[212])


wide_group = Group(
    "for_wide",
    [
        case(
            "A For reads a DD count whole: DD 65537 runs 65537 (Result1 = 2), DD 65536 runs 65536 "
            "(Result2 = 2); a TD (Result3 = 3) and a CTD (Result4 = 3) count",
            ladder=prove_count_forms,
            init=init_count_forms,
            scans=120,
            expect={"Result1": 2, "Result2": 2, "Result3": 3, "Result4": 3},
        ),
    ],
)


#: Scans a case waits for its neighbours' Math to have stopped.
_WAIT = 40


def prove_math_flags_over_a_scan(slot):
    # The reads come first, above the case's own count: nothing but the Copy of main
    # runs between the op and them.
    with Rung(slot.Cmd != 0, slot.Scratch == _WAIT, SC40):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == _WAIT + 2, SC46):
        copy(1, slot.Result2)
    count_scans(slot, _WAIT + 4)
    with Rung(slot.Cmd != 0, slot.Scratch == _WAIT):
        math(1 / slot.Result3, slot.Result4)
    with Rung(slot.Cmd != 0, slot.Scratch == _WAIT, SC40):
        copy(10, slot.ErrorCode)
    with Rung(slot.Cmd != 0, slot.Scratch == _WAIT + 2):
        copy(-1.0, df[204])
        math(sqrt(df[204]), df[204])
    with Rung(slot.Cmd != 0, slot.Scratch == _WAIT + 2, SC46):
        math(slot.ErrorCode + 1, slot.ErrorCode)
    done(slot, _WAIT + 4)


def init_math_flags_over_a_scan(slot):
    copy(0.0, df[204])


flag_group = Group(
    "flag_scan",
    [
        case(
            "SC40 and SC46 clear at the scan's start: SC40 (Result1 = 0) and SC46 (Result2 = 0) on "
            "the scan after the op, each on right after it (ErrorCode = 11)",
            ladder=prove_math_flags_over_a_scan,
            init=init_math_flags_over_a_scan,
            scans=scans_for(_WAIT + 4),
            expect={"Result1": 0, "Result2": 0, "ErrorCode": 11},
            probe="SC40 and SC46 carried to the next scan, or cleared at its start",
        ),
    ],
)


def prove_date_and_time_apply(slot):
    rtc = system.rtc
    count_scans(slot, 15)
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(rtc.year4, dd[74])
        copy(rtc.hour, dd[75])
        copy(rtc.month, dd[84])
        copy(rtc.day, dd[85])
        copy(2030, rtc.new_year4)
        copy(rtc.month, rtc.new_month)
        copy(rtc.day, rtc.new_day)
    with Rung(slot.Cmd != 0, slot.Scratch == 3, rtc.year4 == 2030):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch == 3):
        copy(dd[74], rtc.new_year4)
    with Rung(slot.Cmd != 0, slot.Scratch == 5):
        math((dd[75] + 1) % 24, dd[76])
        copy(dd[76], rtc.new_hour)
        copy(rtc.minute, rtc.new_minute)
        copy(rtc.second, rtc.new_second)
    with Rung(slot.Cmd != 0, slot.Scratch == 7, rtc.hour == dd[76]):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 7):
        copy(dd[75], rtc.new_hour)
        copy(rtc.minute, rtc.new_minute)
        copy(rtc.second, rtc.new_second)
    with Rung(slot.Cmd != 0, slot.Scratch == 9):
        copy(2030, rtc.new_year4)
        copy(13, rtc.new_month)
        copy(25, rtc.new_hour)
        copy(rtc.minute, rtc.new_minute)
        copy(rtc.second, rtc.new_second)
    # Back to what scan 1 saved, on every path, even if the bad date or time was taken.
    with Rung(slot.Cmd != 0, slot.Scratch == 12):
        copy(dd[74], rtc.new_year4)
        copy(dd[84], rtc.new_month)
        copy(dd[85], rtc.new_day)
        copy(dd[75], rtc.new_hour)
        copy(rtc.minute, rtc.new_minute)
        copy(rtc.second, rtc.new_second)
    # One Out per apply bit: of several, the last rung would win.
    with Rung(
        slot.Cmd != 0,
        Or(slot.Scratch == 1, slot.Scratch == 3, slot.Scratch == 9, slot.Scratch == 12),
    ):
        out(rtc.apply_date)
    with Rung(
        slot.Cmd != 0,
        Or(slot.Scratch == 5, slot.Scratch == 7, slot.Scratch == 9, slot.Scratch == 12),
    ):
        out(rtc.apply_time)
    with Rung(slot.Cmd != 0, slot.Scratch == 10, rtc.apply_date_error):
        math(slot.Result3 + 10, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == 10, rtc.apply_time_error):
        math(slot.Result3 + 1, slot.Result3)
    # Two scans after the bad apply, as a good one is read, and a scan before the set-back,
    # so a CLICK that applies at once cannot show the restored clock here.
    with Rung(
        slot.Cmd != 0,
        slot.Scratch == 11,
        rtc.year4 == dd[74],
        rtc.month == dd[84],
        rtc.hour == dd[75],
    ):
        copy(1, slot.Result4)
    with Rung(
        slot.Cmd != 0,
        slot.Scratch == 14,
        rtc.year4 == dd[74],
        rtc.month == dd[84],
        rtc.day == dd[85],
        rtc.hour == dd[75],
    ):
        copy(1, slot.ErrorCode)
    done(slot, 15)


def init_date_and_time_apply(slot):
    fill(0, dd.select(74, 76))
    fill(0, dd.select(84, 85))


rtc_group = Group(
    "rtc_set",
    [
        case(
            "A date set by SC53 applies (Result1 = 1), a time by SC55 (Result2 = 1); a bad one raises "
            "SC54 and SC56 (Result3 = 11) and leaves the clock (Result4 = 1); the clock is set back "
            "last, every time (ErrorCode = 1)",
            ladder=prove_date_and_time_apply,
            init=init_date_and_time_apply,
            scans=scans_for(15),
            expect={
                "Result1": between(0, 1),
                "Result2": between(0, 1),
                "Result3": between(0, 11),
                "Result4": between(0, 1),
                "ErrorCode": between(0, 1),
            },
            probe="RTC set from the ladder: moved and set back, a bad value refused",
        ),
    ],
)


def test_boot_read():
    assert_all_passed(run(boot_group))


def test_for_wide():
    assert_all_passed(run(wide_group))


def test_flag_scan():
    assert_all_passed(run(flag_group))


def test_rtc_set():
    assert_all_passed(run(rtc_group))


@pytest.mark.parametrize("engine", [PLC, CompiledPLC], ids=["interpreter", "kernel"])
@pytest.mark.parametrize(
    ("boot", "battery", "after"),
    [
        ("reboot", True, (764, 0, 1)),
        ("reboot", False, (0, 0, 0)),
        ("stop_run", True, (764, 0, 1)),
    ],
)
def test_what_survives_reads_through_a_boot(engine, boot, battery, after):
    # The hardware ladder, run, booted, and run again, as AGENTS.md's procedure does.
    (survives,) = [x for x in boot_group.cases if x.id == "what_survives"]
    slot = make_slot(1, name=boot_group.title)
    with Program(strict=False) as logic:
        for _ in range(boot_group.calls):
            with Rung():
                call(boot_group.subroutine)
        with subroutine(boot_group.subroutine):
            emit_case("boot_read", survives, slot, 1)
    plc = engine(logic, dt=DT)
    plc.battery_present = battery

    def read():
        _reset(plc, slot, [1])
        _fire(plc, slot, [survives], [1])
        tags = plc.current_state.tags
        return tuple(tags.get(getattr(slot[1], f).name) for f in ("Result1", "Result2", "Result3"))

    assert read() == (0, 0, int(battery))
    if boot == "reboot":
        plc.reboot()
    else:
        plc.stop()
        plc.start()
    assert read() == after
