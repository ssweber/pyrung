"""A debounce's off TD at its fall, and flashers with a phase of 0: ``deb_fall`` and ``flash_zero``.

Part ``follow2_coils``.  A CLICK answered every case on 2026-11-10, twice.

``deb_fall/off_td_at_fall`` (B1): a debounce of 0 / 100 ms on a rung on scans 2 and 3, off from
scan 4.  Result1 is the off TD copied on scan 4, the fall, on the rung right after the debounce.
Result2 is the off TD on scan 5.  Result3 is 1 where the off TD is 0 on scan 3, with the rung
true.  A CLICK read 1..2, 3, 1.  The off count starts on the scan of the fall (0 would be a count
that starts a scan late), so the off side's scan of lag is in the compare
(``DebounceInstruction``).  The rise cleared the off TD, as ``coil_timing/off_td_held`` read.
pyrung reads the same: 10, 20, 1 at the in-process 10 ms scan.  Result1 and Result2 are
milliseconds and follow the target's scan, so they are ranges for any scan up to 50 ms.

``flash_zero``: each flasher's presets of 0 are DS (CLICK's editor takes no constant of 0), and
its rung is on from ``Cmd``.  Over a window of 14 scans Result1 is the scans its coil is on,
Result2 the first and Result3 the last (``scans.trace``).  The readings fit one rule.  A flasher
with a preset of 0 or below is dark and its TDs stand where they are (``FlasherInstruction``).

- ``off_zero_first_phase`` (L1): 1000 ms on, a DS off preset of 0.  Result4 the on TD and
  ErrorCode the off TD on scan 14.  A CLICK read 0, 0, 0, 0, 0: dark from the start, no first on
  phase, both TDs 0.
- ``on_zero_tds`` (L2): a DS on preset of 0, 1000 ms off, read as L1.  A CLICK read 0, 0, 0, 0, 0:
  dark, both TDs held at 0.  An off TD running would be the empty on phase passing to the off
  phase.
- ``lowered_to_zero`` (L3): two flashers on DS presets.  A, 1000 / 1000 ms, has its on preset
  lowered to 0 on scan 6, in its on phase.  B, 5 / 1000 ms, has its off preset lowered on scan 10,
  in its off phase.  Each reading is ``on TD * 180 + off TD``.  Result1 is A's on scan 5, and
  Result2 A's on scan 14 less Result1.  Result3 is B's on scan 9, and Result4 B's on scan 14 less
  Result3.  ErrorCode is A's lit scans from 6 on * 100 + B's from 10 on.  A CLICK read 1260 and
  1260 for A, 907..908 and 907..908 for B, and ErrorCode 0, so Result2 and Result4 are 0: both
  flashers go dark, and their TDs freeze where the 0 found them.  pyrung zeroed both TDs before
  (Result2 -9000 and Result4 -1880 here).  Result1 and Result3 follow the target's scan, so they
  are ranges.  Result1 is an on TD of at least 1 ms with the off TD 0.  Result3 is an on TD at
  its 5 ms preset or past it with the off TD running.  Each TD stays under 180 for any scan under
  about 35 ms, which keeps each reading in an Int.
- ``short_first_phase`` (L4): 1 ms on, 1000 ms off.  Result4 the on TD on scan 1.  A CLICK read 1,
  1, 1, 1..2: the coil is on on the rung's first true scan, though the on TD reached its preset
  on that scan.  The coil shows the phase that ran on a scan, and the next phase shows from the
  scan after.  pyrung read 0, 0, 0, 10 before: its coil showed the next phase at once, so an on
  phase of a scan or less stayed dark.  Result4 follows the target's scan, so it is a range.

The cases own what ``memory.OWNED_APART`` lists under these groups.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, copy, debounce, flasher, math, out
from tests.conformance.memory import ClickTimer, c, ds, td
from tests.conformance.scans import count_scans, done, scans_for, trace

# --- deb_fall ------------------------------------------------------------------------

#: off_td_at_fall: C661 the rung, C662 the debounce; T271 on, T272 off.
DEB_RUNG, DEB = c[661], c[662]
DEB_ON, DEB_OFF = ClickTimer(271), ClickTimer(272)


def prove_off_td_at_fall(slot: Any) -> None:
    count_scans(slot)
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 2, slot.Scratch == 3)):
        out(DEB_RUNG)
    with Rung(DEB_RUNG):
        debounce(DEB, on=(DEB_ON, 0), off=(DEB_OFF, 100))
    for scan, field in ((4, slot.Result1), (5, slot.Result2)):
        with Rung(slot.Cmd != 0, slot.Scratch == scan):
            copy(DEB_OFF.Acc, field)
    # The off TD before the rise depends on how long the reset ran: Result3 says only whether it is 0.
    with Rung(slot.Cmd != 0, slot.Scratch == 3, DEB_OFF.Acc == 0):
        copy(1, slot.Result3)
    done(slot)


deb_fall = Group(
    "deb_fall",
    [
        case(
            "A debounce's off TD counts on the scan its rung falls (Result1, a scan's ms) and a "
            "scan more on the next (Result2); the rise cleared it (Result3 = 1)",
            ladder=prove_off_td_at_fall,
            ready=lambda slot: ~DEB,
            # The off TD counts from power-up while the rung is off, and the on TD is kept after the
            # rung falls; the rise clears the off TD and starts the on TD from 0.
            keep=(DEB_ON.Acc, DEB_OFF.Acc),
            expect={
                "Result1": range(1, 51),
                "Result2": range(2, 101),
                "Result3": 1,
            },
            scans=scans_for(),
        ),
    ],
)


# --- flash_zero ----------------------------------------------------------------------

FLASH_WINDOW = 14


def _flash(
    slot: Any,
    coil: Any,
    on: tuple[int, Any],
    off: tuple[int, Any],
) -> tuple[Any, Any]:
    """A flasher on from ``Cmd``, traced over the window; its TDs on the last scan."""
    count_scans(slot, FLASH_WINDOW)
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(on[0]), on[1]), off=(ClickTimer(off[0]), off[1]))
    trace(slot, coil, FLASH_WINDOW)
    return td[on[0]], td[off[0]]


def _tds_on(slot: Any, scan: int, on_td: Any, off_td: Any) -> None:
    with Rung(slot.Cmd != 0, slot.Scratch == scan):
        copy(on_td, slot.Result4)
        copy(off_td, slot.ErrorCode)


#: off_zero_first_phase: C671, T273/T274, the off preset DS4031.
def prove_off_zero_first_phase(slot: Any) -> None:
    on_td, off_td = _flash(slot, c[671], (273, 1000), (274, ds[4031]))
    _tds_on(slot, FLASH_WINDOW, on_td, off_td)
    done(slot, FLASH_WINDOW)


def init_off_zero_first_phase(slot: Any) -> None:
    copy(0, ds[4031])


#: on_zero_tds: C672, T275/T276, the on preset DS4032.
def prove_on_zero_tds(slot: Any) -> None:
    on_td, off_td = _flash(slot, c[672], (275, ds[4032]), (276, 1000))
    _tds_on(slot, FLASH_WINDOW, on_td, off_td)
    done(slot, FLASH_WINDOW)


def init_on_zero_tds(slot: Any) -> None:
    copy(0, ds[4032])


#: lowered_to_zero: C673/C674 the coils; T277..280; DS4033..4036 the presets (A on, A off, B on, B off).
LOWER_PRESETS = {4033: 1000, 4034: 1000, 4035: 5, 4036: 1000}
A_LOWER, B_LOWER = 6, 10


def prove_lowered_to_zero(slot: Any) -> None:
    a, b = c[673], c[674]
    a_on, a_off, b_on, b_off = (ds[n] for n in LOWER_PRESETS)
    count_scans(slot, FLASH_WINDOW)
    with Rung(slot.Cmd != 0, slot.Scratch == A_LOWER):
        copy(0, a_on)
    with Rung(slot.Cmd != 0, slot.Scratch == B_LOWER):
        copy(0, b_off)
    with Rung(slot.Cmd != 0):
        flasher(a, on=(ClickTimer(277), a_on), off=(ClickTimer(278), a_off))
    with Rung(slot.Cmd != 0):
        flasher(b, on=(ClickTimer(279), b_on), off=(ClickTimer(280), b_off))
    for (on_td, off_td), before, after, scan in (
        ((td[277], td[278]), slot.Result1, slot.Result2, A_LOWER - 1),
        ((td[279], td[280]), slot.Result3, slot.Result4, B_LOWER - 1),
    ):
        with Rung(slot.Cmd != 0, slot.Scratch == scan):
            math(on_td * 180 + off_td, before)
        # 0 where the TDs stand where they were before the preset went to 0.
        with Rung(slot.Cmd != 0, slot.Scratch == FLASH_WINDOW):
            math(on_td * 180 + off_td - before, after)
    for coil, after, add in ((a, A_LOWER, 100), (b, B_LOWER, 1)):
        with Rung(slot.Cmd != 0, slot.Scratch >= after, slot.Scratch <= FLASH_WINDOW, coil):
            math(slot.ErrorCode + add, slot.ErrorCode)
    done(slot, FLASH_WINDOW)


def init_lowered_to_zero(slot: Any) -> None:
    for n, value in LOWER_PRESETS.items():
        copy(value, ds[n])


#: short_first_phase: C677, T281/T282.
def prove_short_first_phase(slot: Any) -> None:
    on_td, _off_td = _flash(slot, c[677], (281, 1), (282, 1000))
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(on_td, slot.Result4)
    done(slot, FLASH_WINDOW)


def _flash_case(sentence: str, ladder: Any, coils: tuple[int, ...], **kwargs: Any) -> Any:
    timers = kwargs.pop("timers")
    return case(
        sentence,
        ladder=ladder,
        ready=lambda slot: tuple(~c[n] for n in coils),
        # A flasher's TDs freeze with its rung false, and the restart clears both.
        keep=tuple(td[n] for n in timers),
        scans=scans_for(FLASH_WINDOW),
        **kwargs,
    )


#: Dark over the whole window.
_DARK = {"Result1": 0, "Result2": 0, "Result3": 0}

flash_zero = Group(
    "flash_zero",
    [
        _flash_case(
            "A flasher of 1000 ms on and a DS off preset of 0 is dark from scan 1 (Result1 = 0 "
            "scans lit, Result2 = 0 the first, Result3 = 0 the last), its on TD (Result4 = 0) and "
            "off TD (ErrorCode = 0) on scan 14",
            prove_off_zero_first_phase,
            (671,),
            timers=(273, 274),
            init=init_off_zero_first_phase,
            expect={**_DARK, "Result4": 0, "ErrorCode": 0},
        ),
        _flash_case(
            "A flasher with a DS on preset of 0 and 1000 ms off holds its on TD (Result4 = 0) "
            "and off TD (ErrorCode = 0) at 0 on scan 14, dark (Result1 = 0 scans lit, Result2 = "
            "0 the first, Result3 = 0 the last)",
            prove_on_zero_tds,
            (672,),
            timers=(275, 276),
            init=init_on_zero_tds,
            expect={**_DARK, "Result4": 0, "ErrorCode": 0},
        ),
        _flash_case(
            "A flasher's DS preset lowered to 0 while its TDs run freezes them, as on TD * 180 + "
            "off TD: before (Result1) and the change after (Result2 = 0) for the on preset in "
            "the on phase, before (Result3) and the change after (Result4 = 0) for the off "
            "preset in the off phase; dark after (ErrorCode = 0 lit scans)",
            prove_lowered_to_zero,
            (673, 674),
            timers=(277, 278, 279, 280),
            init=init_lowered_to_zero,
            expect={
                "Result1": range(180, 32768),
                "Result2": 0,
                "Result3": range(901, 32768),
                "Result4": 0,
                "ErrorCode": 0,
            },
        ),
        _flash_case(
            "A flasher of 1 ms on and 1000 ms off is on on its rung's first true scan only "
            "(Result1 = 1 scan lit, Result2 = 1 the first, Result3 = 1 the last), its on TD past "
            "its preset on that scan (Result4, a scan's ms)",
            prove_short_first_phase,
            (677,),
            timers=(281, 282),
            expect={"Result1": 1, "Result2": 1, "Result3": 1, "Result4": range(1, 51)},
        ),
    ],
)


def test_deb_fall():
    assert_all_passed(run(deb_fall))


def test_flash_zero():
    assert_all_passed(run(flash_zero))
