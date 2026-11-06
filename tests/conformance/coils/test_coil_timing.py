"""A debounce's off side and a flasher's empty on phase: the ``coil_timing`` group (part
``follow_timing``).

Three questions the fix step ``timed-coils`` (5 in the ``conform-fix`` arc) built on with no
reading, beside ``deb_zero``'s, answered on a CLICK on 2026-11-06.

- ``off_td_held``: does a debounce's off TD keep its value while the rung is true?  A debounce of
  0 / 500 ms, its rung on to 50 ms on the case's clock, off to 150 ms, on again.  The reset leaves
  the off TD timed out (500 ms or more), and the case starts with it so.  Result1 = 1 when the coil
  stayed on through the 100 ms off (read from 100 to 150 ms), Result3 the off TD on the last scan
  before 150 ms, and at 200 ms Result2 is 1 where the TD still holds that value, 2 where it went
  back to 0.  The readings:

  - 1, 2, about 100: the rung's rise clears the off TD, and the fall times from 0.
  - 1, 1, about 100: the off TD is held while the rung is true (as for the on side), and a fall
    starts it from 0 (pyrung from timed-coils to 2026-11-06).
  - 0, 1, 500 or more: it is held while the rung is true and a fall resumes it, so it is past its
    preset at once and the coil drops at 50 ms.

  A CLICK read 1, 2, 101: the first.  Result3 also says where the off side's scan of lag is (see
  ``deb_off_against_tof``).  An off TD that ticks from the scan of the fall reads the clock's last
  value before 150 ms less its last before 50: up to a scan over 100.  One that starts ticking a
  scan after the fall reads less than 100.  101 is the first, so pyrung ticks the off TD on the
  scan of the fall and lags the compare instead (``DebounceInstruction``).  101 against 100 is one
  millisecond: the reading that would settle it is the off TD on the scan of the fall, a scan's
  time for a lagged compare, 0 for a lagged count.  Result3 is a range for any scan up to 50 ms.
- ``flasher_on_zero``: a flasher of 0 on / 1 ms off, its rung on throughout: scans its coil is on
  of 14 (Result1), 0 where an on phase of 0 stays dark after each off phase.  The control
  (Result2) is a flasher of 1 ms / 1 ms, on for some of them.  A CLICK read 0, 7: dark (pyrung read
  7, 8 before timed-coils).  pyrung holds a flasher with a preset of 0 or below reset
  (``flasher_off``).
- ``deb_off_against_tof``: a debounce of 0 / 5 ms and a TOF of 5 ms on one rung, on scans 2 and 3.
  The scan each goes off first (Result1 the debounce, Result2 the TOF, up to 40, 0 for never) and
  ErrorCode = Result1 - Result2: 1 where the debounce's off side takes a scan more than a TOF of
  its preset, as ``zero_and_below`` read at 0, 0 where they agree.  A CLICK read 6, 5, 1: a scan
  more.  pyrung reads 5, 4, 1 at the in-process 10 ms scan (on a CLICK the scans are shorter, so
  the off comes later), so the scans are ranges and ErrorCode is exact.

pyrung's in-process readings are pinned in ``test_pyrung_reads_the_debounce_lag``.  The cases own
what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, debounce, flasher, off_delay, out
from tests.conformance.memory import ClickTimer, c, t, td
from tests.conformance.probe_kit import end_at, in_window, ms_clock, once
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
            "A debounce's off TD goes to 0 while its rung is true (Result2 = 2, 1 if it keeps "
            "its value) and times from 0 at a fall, about 100 ms by 150 ms (Result3), so the "
            "coil stays on through 100 ms of a 500 ms off (Result1 = 1, 0 if it resumes)",
            ladder=prove_off_td_held,
            ready=lambda slot: ~c[242],
            # The off TD is timed out in the reset: what the case starts from, on every repeat.
            # The on TD is kept after the rung falls, as for any debounce; the next rise starts it from 0.
            keep=(td[115], td[116]),
            expect={"Result1": 1, "Result2": 2, "Result3": range(50, 151)},
            scans=25,
            fires_within=1.0,
        ),
        case(
            "A flasher with an on preset of 0 stays dark (Result1 = 0); a 1 ms / 1 ms flasher "
            "lights (Result2 = 1..14)",
            ladder=prove_flasher_on_zero,
            init=init_flasher_on_zero,
            ready=lambda slot: (~c[243], ~c[244]),
            expect={"Result1": 0, "Result2": range(1, FLASH_WINDOW + 1)},
            scans=scans_for(FLASH_WINDOW),
        ),
        case(
            "A debounce's off side goes off a scan after a TOF of its preset: the debounce "
            "(Result1), the TOF (Result2), the debounce's lag (ErrorCode = 1, 0 on one scan)",
            ladder=prove_deb_off_against_tof,
            init=init_deb_off_against_tof,
            ready=lambda slot: (~c[246], ~t[124]),
            expect={
                "Result1": range(5, OFF_WINDOW + 1),
                "Result2": range(4, OFF_WINDOW + 1),
                "ErrorCode": 1,
            },
            scans=scans_for(OFF_WINDOW),
        ),
    ],
)


def test_coil_timing():
    assert_all_passed(run(group))


def test_pyrung_reads_the_debounce_lag():
    """What pyrung reads at the in-process 10 ms scan."""
    read = {r.case.id: r.actual for r in run(group)}
    assert read == {
        "off_td_held": {"Result1": 1, "Result2": 2, "Result3": 100},
        "flasher_on_zero": {"Result1": 0, "Result2": 7},
        "deb_off_against_tof": {"Result1": 5, "Result2": 4, "ErrorCode": 1},
    }
