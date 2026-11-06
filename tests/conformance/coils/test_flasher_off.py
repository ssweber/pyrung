"""A flasher whose off preset is 0 or below: the ``flasher_off`` group (part ``follow_extra``).

``zero_and_below`` and ``coil_timing/flasher_on_zero`` ask about an on phase of 0.  These two ask
about an off phase of 0 or below, from a DS.

Each case: a flasher of 5 ms on and its off preset from a DS, its rung on from ``Cmd``, read from
50 to 150 ms on the case's clock, well after its first on phase.  In that window Result1 is the
scans its coil is on, Result2 the scans it turns on (a rise from the scan before), Result3 the
scans.  The readings:

- Dark after the first on phase: Result1 = 0, Result2 = 0.
- Lit throughout: Result1 = Result3, Result2 = 0.
- On and off on alternate scans: Result1 and Result2 about Result3 / 2.
- An on phase of 5 ms, then a scan off, again: Result2 about 20, one a 5 ms phase, and Result1 a
  little under Result3.

ErrorCode is the flasher's TDs at 160 ms, ``on TD * 180 + off TD``.  Each TD is at most 160 plus
a scan there.  With scans under about 20 ms (a Result3 of 5 or more) both stay under 180, so the
pair fits an Int and ``divmod(ErrorCode, 180)`` decodes it exactly.  With longer scans the
decode can be off.  ErrorCode tells apart what the counts alone do not:

- Dark, the off phase timing on and never ending (a preset of 0 or below that never ends, or -5
  read as 65531 ms): off TD about 155, on TD 5 or 0 (about 1055 or 155).  Within 160 ms the two
  read alike; ``ds_zero`` tells them apart, since 0 is 0 unsigned too.
- Dark, the off TD standing at 0: ErrorCode a multiple of 180.
- Dark, the on TD running on past its preset with the coil off: on TD about 160 (about 28800).
- Lit, the on phase never ending: on TD about 160 and off TD 0 (about 28800), told from the line
  above by Result1.
- Lit, an off phase of 0 ending on the scan it starts: both TDs small, ErrorCode under about 1100.

Result4, the control, is the rises of a flasher of 5 ms / 5 ms off a DS, on a rung of its own
under the same condition: about 10, at least 1, so the DS off preset, the window and the count
work.

Before 2026-11-06 pyrung read, in-process (10 ms scans), 0, 0, 10, 5 and ErrorCode 28800 (on TD
160, off TD 0) in both cases: dark, the on TD running on.  A CLICK read 0, 0, 59, 10, 0 for 0 and
0, 0, 60, 10, 0 for -5 (2026-11-06): dark, both TDs at 0, so a preset of 0 or below holds the
flasher reset (``FlasherInstruction``), and pyrung now does so.  Whether its first on phase shows
before the reset takes hold has no reading: the window starts well after it.  Over the wire the
scan counts follow the target's scan, so Result3 and Result4 are ranges; pyrung's in-process
readings (0, 0, 10, 5, 0) are pinned in ``test_pyrung_holds_the_flasher_reset``.

- ``ds_zero``: the DS holds 0.
- ``ds_minus_five``: the DS holds -5.

The cases own what ``memory.OWNED_APART`` lists under this group.
"""

from __future__ import annotations

from typing import Any

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Rung, copy, fill, flasher, math, out, reset
from tests.conformance.memory import ClickTimer, c, ds, td
from tests.conformance.probe_kit import end_at, in_window, ms_clock, once

ON_MS = 5
LOW, HIGH, END = 50, 150, 160
#: Per case: C bits (coil, its last scan, the control and its last scan), T (clock, on, off,
#: control on, control off), DS (off preset, control off preset).
BITS = {"ds_zero": range(251, 255), "ds_minus_five": range(255, 259)}
TIMERS = {"ds_zero": 131, "ds_minus_five": 136}
PRESETS = {"ds_zero": 4405, "ds_minus_five": 4407}


def _flasher_off(slot: Any, name: str, off: int) -> None:
    coil, last, control, control_last = (c[n] for n in BITS[name])
    first = TIMERS[name]
    preset, control_preset = ds[PRESETS[name]], ds[PRESETS[name] + 1]
    clock = ms_clock(slot, first)
    with Rung(slot.Cmd != 0):
        copy(off, preset)
        copy(ON_MS, control_preset)
    with Rung(slot.Cmd != 0):
        flasher(coil, on=(ClickTimer(first + 1), ON_MS), off=(ClickTimer(first + 2), preset))
    with Rung(slot.Cmd != 0):
        flasher(
            control,
            on=(ClickTimer(first + 3), ON_MS),
            off=(ClickTimer(first + 4), control_preset),
        )
    window = in_window(slot, clock, LOW, HIGH)
    with Rung(*window, coil):
        math(slot.Result1 + 1, slot.Result1)
    with Rung(*window, coil, ~last):
        math(slot.Result2 + 1, slot.Result2)
    with Rung(*window):
        math(slot.Result3 + 1, slot.Result3)
    with Rung(*window, control, ~control_last):
        math(slot.Result4 + 1, slot.Result4)
    with Rung(coil):
        out(last)
    with Rung(control):
        out(control_last)
    with Rung(*once(slot), clock >= END):
        math(td[first + 1] * 180 + td[first + 2], slot.ErrorCode)
    end_at(slot, clock, END)


def prove_ds_zero(slot: Any) -> None:
    _flasher_off(slot, "ds_zero", 0)


def prove_ds_minus_five(slot: Any) -> None:
    _flasher_off(slot, "ds_minus_five", -5)


def _init(name: str) -> Any:
    def init(slot: Any) -> None:
        bits = BITS[name]
        reset(c.select(bits.start, bits.stop - 1))
        fill(0, td.select(TIMERS[name] + 1, TIMERS[name] + 4))
        fill(0, ds.select(PRESETS[name], PRESETS[name] + 1))

    return init


def _case(name: str, ladder: Any, holds: str) -> Any:
    bits = BITS[name]
    return case(
        f"A flasher of 5 ms on, its off preset a DS holding {holds}, is held reset: from 50 to "
        "150 ms it is never lit (Result1 = 0) and never turns on (Result2 = 0) in Result3 scans, "
        "while a 5 ms / 5 ms control turns on (Result4 >= 1); both TDs are 0 at 160 ms "
        "(ErrorCode = on * 180 + off = 0)",
        ladder=ladder,
        init=_init(name),
        ready=lambda slot: (~c[bits.start], ~c[bits.start + 2]),
        expect={
            "Result1": 0,
            "Result2": 0,
            "Result3": range(1, 32768),
            "Result4": range(1, 32768),
            "ErrorCode": 0,
        },
        scans=20,
        fires_within=1.0,
    )


group = Group(
    "flasher_off",
    [
        _case("ds_zero", prove_ds_zero, "0"),
        _case("ds_minus_five", prove_ds_minus_five, "-5"),
    ],
)


def test_flasher_off():
    assert_all_passed(run(group))


def test_pyrung_holds_the_flasher_reset():
    """What pyrung reads at the in-process 10 ms scan."""
    for result in run(group):
        assert result.actual == {
            "Result1": 0,
            "Result2": 0,
            "Result3": 10,
            "Result4": 5,
            "ErrorCode": 0,
        }, result.case.id
