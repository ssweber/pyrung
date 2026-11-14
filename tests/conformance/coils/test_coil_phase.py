"""A debounce's off TD at its fall, and flashers with a phase of 0: ``deb_fall`` and ``flash_zero``.

Part ``follow2_coils``.  A CLICK answered every case on 2026-11-10, twice.  It read ``on_zero_tds``
again on 2026-11-12, after other parts had left values in T275 and T276 (see below).

``deb_fall/off_td_at_fall`` (B1): a debounce of 0 / 100 ms on a rung on scans 2 and 3, off from
scan 4.  Result1 is the off TD copied on scan 4, the fall, on the rung right after the debounce.
Result2 is the off TD on scan 5.  Result3 is 1 where the off TD is 0 on scan 3, with the rung
true.  A CLICK read 1..2, 3, 1.  The off count starts on the scan of the fall (0 would be a count
that starts a scan late), so the debounce turns off a scan after its off TD reaches the preset
(``DebounceInstruction``).  The rise cleared the off TD, as ``coil_timing/off_td_held`` read.
pyrung reads the same, 10, 20, 1 at the in-process 10 ms scan.  Result1 and Result2 are
milliseconds and follow the target's scan, so they are ranges for any scan up to 50 ms.

``flash_zero``: each flasher's presets of 0 are DS (CLICK's editor takes no constant of 0), and
its rung is on from ``Cmd``.  Over a window of 14 scans Result1 is the scans its coil is on,
Result2 the first and Result3 the last (``scans.trace``).  The readings fit one rule.  A flasher
with a preset of 0 or below is dark and its TDs stand where they are (``FlasherInstruction``).
A flasher runs on whatever its TDs hold when it starts, so each case's init writes 0 to its TDs.

- ``off_zero_first_phase`` (L1): 1000 ms on, a DS off preset of 0.  Result4 the on TD and
  ErrorCode the off TD on scan 14.  A CLICK read 0, 0, 0, 0, 0.  The flasher was dark from the
  start, with no first on phase and both TDs at 0 from the init.
- ``on_zero_tds`` (L2): a DS on preset of 0, 1000 ms off, read as L1.  A CLICK read 0, 0, 0, 0, 0.
  The flasher was dark and left both TDs at 0 from the init.  An off TD running would be
  the empty on phase passing to the off phase.  On 2026-11-12 the TDs were not cleared, and a CLICK
  read 0, 0, 0, 164, 1000.  T275 and T276 held 164 and 1000 from an earlier program, and the dark
  flasher left them there.  The case now clears them.
- ``lowered_to_zero`` (L3): two flashers on DS presets, their rungs on from ``Cmd``.  The reset
  writes 0 to their four TDs.  A, 1000 / 1000 ms, has its on preset lowered to 0 on the scan its on
  TD first reads 100 or more, in its on phase.  B, 5 / 1000 ms, has its off preset lowered on the
  scan its off TD first reads 300 or more, in its off phase.  Result1 is A's on TD on that scan, and
  Result3 B's off TD.  Each is a range of one scan of up to 50 ms past its threshold.  Result2 is 1
  from A's lowering, and 2 once a later scan finds A's on TD off Result1 or its off TD off 0.
  Result4 is 1 from B's lowering, and 2 once B's off TD is off Result3 or its on TD is under its 5
  ms preset.  ErrorCode is 1 if A is lit after its lowering, and 2 if B is.  ``Fired`` goes up once
  B's preset is 0.  The three verdicts go on watching after that, through the harness's two reads.
  A CLICK read an earlier ladder that lowered A's preset on scan 6 and B's on scan 10, with
  ``on TD * 180 + off TD`` read before and on scan 14.  A read 1260 both times and B 907..908 both
  times.  Both flashers went dark, and their TDs froze where the 0 found them.  So Result2 and
  Result4 are 1 and ErrorCode is 0 here.  That CLICK held the TDs for 8 scans, and this ladder
  holds them for longer.  pyrung zeroed both TDs before (Result2 and Result4 2 here).
- ``short_first_phase`` (L4): 1 ms on, 1000 ms off.  Result4 the on TD on scan 1.  A CLICK read 1,
  1, 1, 1..2.  The coil is on on the rung's first true scan, though the on TD reached its preset
  on that scan.  The coil shows the phase that ran on a scan, and the next phase shows from the
  scan after.  pyrung read 0, 0, 0, 10 before.  Its coil showed the next phase at once, so an on
  phase of a scan or less stayed dark.  Result4 follows the target's scan, so it is a range.

The cases own what ``memory.OWNED_APART`` lists under these groups.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Or, Rung, copy, debounce, flasher, out
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
            # The off TD counts while the rung is off after the coil was on, so it reads 100 or more
            # after the reset.  The on TD is kept after the rung falls.
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
    # An earlier program may have left values in the TDs, and a dark flasher keeps them.
    copy(0, td[273])
    copy(0, td[274])


#: on_zero_tds: C672, T275/T276, the on preset DS4032.
def prove_on_zero_tds(slot: Any) -> None:
    on_td, off_td = _flash(slot, c[672], (275, ds[4032]), (276, 1000))
    _tds_on(slot, FLASH_WINDOW, on_td, off_td)
    done(slot, FLASH_WINDOW)


def init_on_zero_tds(slot: Any) -> None:
    copy(0, ds[4032])
    copy(0, td[275])
    copy(0, td[276])


#: lowered_to_zero: C673/C674 the coils; T277..280; DS4033..4036 the presets (A on, A off, B on, B off).
LOWER_PRESETS = {4033: 1000, 4034: 1000, 4035: 5, 4036: 1000}
#: The TDs that lower the presets: A's on TD in its on phase, B's off TD in its off phase.
A_LOWER_AT, B_LOWER_AT = 100, 300


def prove_lowered_to_zero(slot: Any) -> None:
    a, b = c[673], c[674]
    a_on, a_off, b_on, b_off = (ds[n] for n in LOWER_PRESETS)
    a_on_td, a_off_td, b_on_td, b_off_td = td[277], td[278], td[279], td[280]
    with Rung(slot.Cmd != 0):
        flasher(a, on=(ClickTimer(277), a_on), off=(ClickTimer(278), a_off))
    with Rung(slot.Cmd != 0):
        flasher(b, on=(ClickTimer(279), b_on), off=(ClickTimer(280), b_off))
    # These rungs sit above the rungs that lower the presets, so they start on the scan after.
    with Rung(slot.Cmd != 0, a_on == 0, a):
        copy(1, slot.ErrorCode)
    with Rung(slot.Cmd != 0, b_off == 0, b):
        copy(2, slot.ErrorCode)
    with Rung(slot.Cmd != 0, a_on == 0, Or(a_on_td != slot.Result1, a_off_td != 0)):
        copy(2, slot.Result2)
    with Rung(slot.Cmd != 0, b_off == 0, Or(b_off_td != slot.Result3, b_on_td < b_on)):
        copy(2, slot.Result4)
    with Rung(slot.Cmd != 0, a_on != 0, a_on_td >= A_LOWER_AT):
        copy(a_on_td, slot.Result1)
        copy(1, slot.Result2)
        copy(0, a_on)
    with Rung(slot.Cmd != 0, b_off != 0, b_off_td >= B_LOWER_AT):
        copy(b_off_td, slot.Result3)
        copy(1, slot.Result4)
        copy(0, b_off)
    with Rung(slot.Cmd != 0, b_off == 0):
        copy(1, slot.Fired)


def init_lowered_to_zero(slot: Any) -> None:
    for n, value in LOWER_PRESETS.items():
        copy(value, ds[n])
    # The last run froze the TDs past both thresholds, so each run starts them from 0.
    for n in range(277, 281):
        copy(0, td[n])


#: short_first_phase: C677, T281/T282.
def prove_short_first_phase(slot: Any) -> None:
    on_td, _off_td = _flash(slot, c[677], (281, 1), (282, 1000))
    # No init clears T281/T282: stale TDs (164/1000 checked) change nothing, as the rung's rise restarts them.
    with Rung(slot.Cmd != 0, slot.Scratch == 1):
        copy(on_td, slot.Result4)
    done(slot, FLASH_WINDOW)


def _flash_case(sentence: str, ladder: Any, coils: tuple[int, ...], **kwargs: Any) -> Any:
    timers = kwargs.pop("timers")
    kwargs.setdefault("scans", scans_for(FLASH_WINDOW))
    return case(
        sentence,
        ladder=ladder,
        ready=lambda slot: tuple(~c[n] for n in coils),
        # The flasher leaves its TDs behind, and keep stops the leak check flagging them.
        keep=tuple(td[n] for n in timers),
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
            "A flasher with a DS on preset of 0 and 1000 ms off leaves its on TD (Result4 = 0) "
            "and off TD (ErrorCode = 0) at 0 on scan 14, dark (Result1 = 0 scans lit, "
            "Result2 = 0 the first, Result3 = 0 the last)",
            prove_on_zero_tds,
            (672,),
            timers=(275, 276),
            init=init_on_zero_tds,
            expect={**_DARK, "Result4": 0, "ErrorCode": 0},
        ),
        _flash_case(
            "A flasher's DS preset lowered to 0 while its TDs run freezes them: the on preset "
            "at an on TD of 100 (Result1) in the on phase (Result2 = 1, 2 once a TD moves), the "
            "off preset at an off TD of 300 (Result3) in the off phase (Result4 = 1, 2 once a "
            "TD moves); dark after (ErrorCode = 0)",
            prove_lowered_to_zero,
            (673, 674),
            timers=(277, 278, 279, 280),
            init=init_lowered_to_zero,
            expect={
                # The first TD past each threshold, a scan of at most 50 ms later.
                "Result1": range(A_LOWER_AT, A_LOWER_AT + 50),
                "Result2": 1,
                "Result3": range(B_LOWER_AT, B_LOWER_AT + 50),
                "Result4": 1,
                "ErrorCode": 0,
            },
            scans=60,
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


@pytest.mark.parametrize("dt", [0.0004, 0.001, 0.010, 0.025, 0.040])
def test_lowered_to_zero_at_any_scan(monkeypatch, dt):
    """The CLICK scans in about 1 ms or less, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    lowered = next(c for c in flash_zero.cases if c.id == "lowered_to_zero")
    assert_all_passed(run(Group("flash_zero", [replace(lowered, scans=int(0.6 / dt))])))


@pytest.mark.parametrize("case_id", ["off_zero_first_phase", "on_zero_tds"])
def test_dark_flasher_cases_clear_stale_tds(monkeypatch, case_id):
    """A CLICK held 164 and 1000 in a dark flasher's TDs from another program, and it keeps them."""
    case_ = next(c for c in flash_zero.cases if c.id == case_id)
    stale = dict(zip([t.name for t in case_.keep], (164, 1000), strict=True))

    class StalePLC(runner_module.PLC):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.patch(stale)

    monkeypatch.setattr(runner_module, "PLC", StalePLC)
    assert_all_passed(run(Group("flash_zero", [case_])))
