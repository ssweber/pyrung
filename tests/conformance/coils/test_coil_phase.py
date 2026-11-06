"""A debounce's off TD at its fall, and flashers with a phase of 0: ``deb_fall`` and ``flash_zero``.

Part ``follow2_coils``.

``deb_fall/off_td_at_fall`` (B1): ``coil_timing/off_td_held`` (1, 2, 101) and
``deb_off_against_tof`` (6, 5: a debounce's off side drops a scan after a TOF of its preset) leave
open where the scan goes: the off count may start a scan late, or count from the fall and lag in
the compare.  A debounce of 0 / 100 ms on a rung on scans 2 and 3, off from scan 4.  Result1 is
the off TD copied on scan 4, the fall, on the rung right after the debounce: 0 where the off count
starts a scan late, about a scan's ms where it counts on the fall (the lag in the compare, as
pyrung has it).  Result2 is the off TD on scan 5, for scale.  Result3 is 1 where the off TD is 0
on scan 3, with the rung true: the rise cleared it, as ``off_td_held`` read (Result2 = 2); 0 where
it holds what it counted through the reset.  pyrung reads 10, 20, 0 (it holds the off TD).

``flash_zero``, the flasher readings the audit left (``out_more/zero_and_below`` R3 0,
``coil_timing/flasher_on_zero`` 0, ``flasher_off`` dark).  One rule fits them all, "either preset
of 0 or below: dark, both TDs 0", and so do others.  Each flasher's presets of 0 are DS (CLICK's
editor takes no constant of 0); its rung is on from ``Cmd``.  Over a window of 14 scans Result1 is
the scans its coil is on, Result2 the first and Result3 the last (``scans.trace``).

- ``off_zero_first_phase`` (L1): 1000 ms on, a DS off preset of 0, read from scan 1.  Result1 = 14,
  Result2 = 1: the first on phase lights; 0, 0, 0: dark from the start, as "either preset <= 0"
  has it.  Result4 the on TD and ErrorCode the off TD on scan 14: about 14 scans' ms and 0 for an
  on phase timing; 0, 0 held in reset.  pyrung reads 14, 1, 14, 140, 0.
- ``on_zero_tds`` (L2): a DS on preset of 0, 1000 ms off.  Result4 the on TD and ErrorCode the off
  TD on scan 14: 0, 0 held in reset ("either preset <= 0"); the off TD about 14 scans' ms where the
  empty on phase passes to the off phase (pyrung: 0, 0, 0, then 0 and 140, dark); the on TD
  running where the on phase never ends.
- ``lowered_to_zero`` (L3): two flashers on DS presets.  A, 1000 / 1000 ms, has its on preset
  lowered to 0 on scan 6, in its on phase; B, 5 / 1000 ms, its off preset on scan 10, in its off
  phase.  Each reading is ``on TD * 180 + off TD`` (each TD under 180 here, so it reads back as
  ``divmod(x, 180)``): A on scan 5 (Result1) and 14 (Result2), B on scan 9 (Result3) and 14
  (Result4).  Result2 = 0: both TDs zero; Result2 about Result1: they freeze; an off TD in Result2:
  A's on phase ended and its off phase runs.  The same for B, an on TD in Result4 a new on phase.
  ErrorCode is A's lit scans from 6 on * 100 + B's from 10 on.  pyrung reads 9000, 9090, 1880,
  10800, 0: A's on phase ends at its 0 (on TD 50, off TD 90 on scan 14), B's off phase ends at
  its 0 and its on TD runs past its 5 ms (60), and both stay dark.
- ``short_first_phase`` (L4): 1 ms on, 1000 ms off: is the coil on on the rung's first true
  scan?  Result2 = 1: yes (Result1 the scans lit, 1 or a few); 0 with Result1 = 0: an on phase of
  a scan or less does not show.  Result4 the on TD on scan 1.  pyrung reads 0, 0, 0, 10: the
  on phase ends on the scan it starts, dark.

The cases own what ``memory.OWNED_APART`` lists under these groups.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, copy, debounce, flasher, math, out
from tests.conformance.memory import ClickTimer, c, ds, td
from tests.conformance.probe_kit import between
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
            "A debounce's off TD counts on the scan its rung falls (Result1 about a scan's ms, "
            "0 if it starts a scan late), a scan more on the next (Result2); the rise cleared it "
            "(Result3 = 1, 0 if held)",
            ladder=prove_off_td_at_fall,
            ready=lambda slot: ~DEB,
            # The off TD counts from power-up while the rung is off, and the on TD is kept after the
            # rung falls; the rise clears the off TD and starts the on TD from 0.
            keep=(DEB_ON.Acc, DEB_OFF.Acc),
            expect={
                "Result1": between(0, 32767),
                "Result2": between(0, 32767),
                "Result3": between(0, 1),
            },
            scans=scans_for(),
            probe="a debounce's off count: from the fall, or a scan late (5)",
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
    for scan, (on_td, off_td), field in (
        (A_LOWER - 1, (td[277], td[278]), slot.Result1),
        (FLASH_WINDOW, (td[277], td[278]), slot.Result2),
        (B_LOWER - 1, (td[279], td[280]), slot.Result3),
        (FLASH_WINDOW, (td[279], td[280]), slot.Result4),
    ):
        with Rung(slot.Cmd != 0, slot.Scratch == scan):
            math(on_td * 180 + off_td, field)
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


_TRACE = {
    "Result1": between(0, FLASH_WINDOW),
    "Result2": between(0, FLASH_WINDOW),
    "Result3": between(0, FLASH_WINDOW),
}

flash_zero = Group(
    "flash_zero",
    [
        _flash_case(
            "A flasher of 1000 ms on and a DS off preset of 0 lights its first on phase from "
            "scan 1 (Result1 = 14 scans, Result2 = 1 the first, Result3 the last; 0 dark), its "
            "on TD (Result4) and off TD (ErrorCode) on scan 14",
            prove_off_zero_first_phase,
            (671,),
            timers=(273, 274),
            init=init_off_zero_first_phase,
            expect={
                **_TRACE,
                "Result4": between(0, 32767),
                "ErrorCode": between(0, 32767),
            },
            probe="a flasher's off preset of 0: is its first on phase lit (5)",
        ),
        _flash_case(
            "A flasher with a DS on preset of 0 and 1000 ms off: its on TD (Result4) and off TD "
            "(ErrorCode) on scan 14, 0 and 0 held, an off TD running if the on phase passes; "
            "scans lit (Result1), the first (Result2), the last (Result3)",
            prove_on_zero_tds,
            (672,),
            timers=(275, 276),
            init=init_on_zero_tds,
            expect={
                **_TRACE,
                "Result4": between(0, 32767),
                "ErrorCode": between(0, 32767),
            },
            probe="a flasher's on preset of 0: its TDs held at 0 or running (5)",
        ),
        _flash_case(
            "A flasher's DS preset lowered to 0 while its TDs run: on TD * 180 + off TD before "
            "and after, for the on preset in the on phase (Result1, Result2) and the off preset "
            "in the off phase (Result3, Result4), 0 after where both TDs zero; lit scans after "
            "(ErrorCode = on phase * 100 + off phase)",
            prove_lowered_to_zero,
            (673, 674),
            timers=(277, 278, 279, 280),
            init=init_lowered_to_zero,
            expect={
                "Result1": between(-32768, 32767),
                "Result2": between(-32768, 32767),
                "Result3": between(-32768, 32767),
                "Result4": between(-32768, 32767),
                "ErrorCode": between(0, FLASH_WINDOW * 101),
            },
            probe="a flasher preset lowered to 0 mid-cycle: TDs zero, freeze or run (5)",
        ),
        _flash_case(
            "A flasher of 1 ms on and 1000 ms off is on on its rung's first true scan "
            "(Result2 = 1, Result1 the scans lit, Result3 the last; 0 if a phase of a scan or "
            "less does not show), its on TD on scan 1 (Result4)",
            prove_short_first_phase,
            (677,),
            timers=(281, 282),
            expect={**_TRACE, "Result4": between(0, 32767)},
            probe="a flasher's 1 ms first on phase: lit on the first scan (5)",
        ),
    ],
)


def test_deb_fall():
    assert_all_passed(run(deb_fall))


def test_flash_zero():
    assert_all_passed(run(flash_zero))
