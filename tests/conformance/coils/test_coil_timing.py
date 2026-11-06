"""A debounce's off side and a flasher's empty on phase: the ``coil_timing`` group (part
``follow_timing``).

Three questions the fix step ``timed-coils`` (5 in the ``conform-fix`` arc) builds on with no
reading, beside ``deb_zero``'s.

- ``off_td_held``: does a debounce's off TD keep its value while the rung is true?  A debounce of
  0 / 500 ms, its rung on to 50 ms on the case's clock, off to 150 ms, on again.  The reset leaves
  the off TD timed out (500 ms or more), and the case starts with it so.  Result1 = 1 when the coil
  stayed on through the 100 ms off (read from 100 to 150 ms), Result3 the off TD on the last scan
  before 150 ms, and at 200 ms Result2 is 1 where the TD still holds that value, 2 where it went
  back to 0.  The readings:

  - 1, 2, about 100: the rung's rise clears the off TD, and the fall times from 0 (pyrung on dev
    42dd614 reads 1, 2, 100).
  - 1, 1, about 100: the off TD is held while the rung is true (as CLICK showed for the on side),
    and a fall starts it from 0.
  - 0, 1, 500 or more: it is held while the rung is true and a fall resumes it, so it is past its
    preset at once and the coil drops at 50 ms.
- ``flasher_on_zero``: a flasher of 0 on / 1 ms off, its rung on throughout: scans its coil is on
  of 14 (Result1), 0 where an on phase of 0 stays dark after each off phase.  The control
  (Result2) is a flasher of 1 ms / 1 ms, on for some of them.  pyrung on dev reads 7, 8: a scan
  lit after each off phase.
- ``deb_off_against_tof``: a debounce of 0 / 5 ms and a TOF of 5 ms on one rung, on scans 2 and 3.
  The scan each goes off first (Result1 the debounce, Result2 the TOF, up to 40, 0 for never) and
  ErrorCode = Result1 - Result2: 1 where the debounce's off side takes a scan more than a TOF of
  its preset, as ``zero_and_below`` read at 0, 0 where they agree.  pyrung reads 4, 4, 0 (a 10 ms
  scan in-process; on a CLICK the scans are shorter, so the off comes later).

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, debounce, flasher, off_delay, out
from tests.conformance.memory import ClickTimer, c, t, td
from tests.conformance.probe_kit import between, end_at, in_window, ms_clock, once
from tests.conformance.scans import count_scans, done, scans_for

FLASH_WINDOW = 14
OFF_WINDOW = 40


def prove_off_td_held(slot: Any) -> None:
    rung, deb = c[241], c[242]
    off_td = td[116]
    clock = ms_clock(slot, 117)
    with Rung(slot.Cmd != 0, Or(clock < 50, clock >= 150)):
        out(rung)
    with Rung(rung):
        debounce(deb, on=(ClickTimer(115), 0), off=(ClickTimer(116), 500))
    with Rung(*in_window(slot, clock, 100, 150)):
        copy(off_td, slot.Result3)
    with Rung(*in_window(slot, clock, 100, 150), deb):
        copy(1, slot.Result1)
    with Rung(*once(slot), clock >= 200, off_td == slot.Result3):
        copy(1, slot.Result2)
    with Rung(*once(slot), clock >= 200, off_td == 0):
        copy(2, slot.Result2)
    end_at(slot, clock, 200)


def prove_flasher_on_zero(slot: Any) -> None:
    dark, control = c[243], c[244]
    count_scans(slot, FLASH_WINDOW)
    with Rung(slot.Cmd != 0):
        flasher(dark, on=(ClickTimer(118), 0), off=(ClickTimer(119), 1))
    with Rung(slot.Cmd != 0):
        flasher(control, on=(ClickTimer(120), 1), off=(ClickTimer(121), 1))
    for bit, field in ((dark, slot.Result1), (control, slot.Result2)):
        with Rung(slot.Cmd != 0, slot.Scratch <= FLASH_WINDOW, bit):
            calc(field + 1, field)
    done(slot, FLASH_WINDOW)


def init_flasher_on_zero(slot: Any) -> None:
    for n in range(118, 122):
        copy(0, td[n])


def prove_deb_off_against_tof(slot: Any) -> None:
    rung, deb = c[245], c[246]
    tof = ClickTimer(124)
    count_scans(slot, OFF_WINDOW)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3)):
        out(rung)
    with Rung(rung):
        debounce(deb, on=(ClickTimer(122), 0), off=(ClickTimer(123), 5))
    with Rung(rung):
        off_delay(tof, 5)
    for bit, field in ((deb, slot.Result1), (tof.Done, slot.Result2)):
        with Rung(slot.Cmd != 0, slot.Scratch >= 4, slot.Scratch <= OFF_WINDOW, ~bit, field == 0):
            copy(slot.Scratch, field)
    with Rung(slot.Cmd != 0, slot.Scratch == OFF_WINDOW + 1, slot.Fired == 0):
        calc(slot.Result1 - slot.Result2, slot.ErrorCode)
    done(slot, OFF_WINDOW)


def init_deb_off_against_tof(slot: Any) -> None:
    for n in (122, 123, 124):
        copy(0, td[n])


group = Group(
    "coil_timing",
    [
        case(
            "A debounce's off TD keeps its value while its rung is true (Result2 = 1, 2 if it "
            "goes to 0; Result3 the TD) and times from 0 at a fall, so the coil stays on through "
            "100 ms of a 500 ms off (Result1 = 1, 0 if it resumes)",
            ladder=prove_off_td_held,
            ready=lambda slot: ~c[242],
            # Timed out in the reset: what the case starts from, on every repeat.
            keep=(td[116],),
            expect={
                "Result1": between(0, 1),
                "Result2": between(0, 2),
                "Result3": between(0, 32767),
            },
            scans=25,
            fires_within=1.0,
            probe="a debounce's off TD while its rung is true: kept or cleared (5)",
        ),
        case(
            "A flasher with an on preset of 0 stays dark after each off phase (Result1 = 0); a "
            "1 ms / 1 ms flasher lights (Result2 = 1..14)",
            ladder=prove_flasher_on_zero,
            init=init_flasher_on_zero,
            ready=lambda slot: (~c[243], ~c[244]),
            expect={"Result1": between(0, FLASH_WINDOW), "Result2": range(1, FLASH_WINDOW + 1)},
            scans=scans_for(FLASH_WINDOW),
            probe="a flasher's on phase of 0 after an off phase: dark or a scan (5)",
        ),
        case(
            "A debounce's off side and a TOF of one preset go off on one scan: the debounce "
            "(Result1), the TOF (Result2), the debounce's lag (ErrorCode = 0, 1 a scan more)",
            ladder=prove_deb_off_against_tof,
            init=init_deb_off_against_tof,
            ready=lambda slot: (~c[246], ~t[124]),
            expect={
                "Result1": between(0, OFF_WINDOW),
                "Result2": between(0, OFF_WINDOW),
                "ErrorCode": between(-OFF_WINDOW, OFF_WINDOW),
            },
            scans=scans_for(OFF_WINDOW),
            probe="a debounce's off side against a TOF: a scan more or not (5)",
        ),
    ],
)


def test_coil_timing():
    assert_all_passed(run(group))
