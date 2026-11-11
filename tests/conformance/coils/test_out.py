"""Out coil variants: the ``out`` group (subroutine ``Conf_Out``).

Each coil is a C bit and each phase a raw Timer pair, ``T<n>`` with ``TD<n>``,
which is what CLICK's Out dialog names (its TD) and claims (the whole timer
number).  The cases own what ``memory.OWNED`` lists under this group.

Every coil's rung is gated on ``Cmd``, so the reset drops each coil's input.
A debounce coil then turns off only when its off side times out, so those
cases are ``ready`` once the coil is off, and ``keep`` the off side's timer.
Each such case ends with its coil on.  A CLICK's off TD counts while the input
is off after the coil was on, so after the reset it reads its preset or more.
A fresh PLC's reads 0.  A CLICK keeps a coil's other TDs after the rung falls
(``out_more``).  So each case's ``init`` writes 0 to every TD that ends one of
its steps.  No step then depends on the coil clearing that TD on its rise scan.

The cases are the probes O1-O6 of ``scratchpad/click-v4/RESEARCH.md``.  Hardware
round 1 (2026-10-29) confirmed O1, O2, O3 and O5 and answered O6.  An Out coil
never sets its Timer's done bit T<n>, so a contact on that T<n> is always off
(pyrung matches).  Round 2 (2026-10-30) read O4.

Each case but O1 runs in steps that ``Scratch`` holds.  A step ends on a timer's
own TD, so no reading depends on how long a scan takes.  A step's rungs come
below the coil in reverse order, so each new step starts on the next scan.  A
timer of the case's own times a wait that the coil's TDs cannot time.

- ``debounce_td_keeps_counting`` (O2, on side 100 ms): the case ends on the
  scan the on TD reaches 300.  Result2 is 1 if the coil is on then.  An on TD
  that stopped at its preset would never end the case, so T299 times the case
  out at step 0 after 1000 ms (``scans.deadline``).
- ``debounce_bounce_restarts_on_side`` (O3, on side 300 ms): the input is on
  until the on TD reads 200.  The input is then off while T302 times 100 ms,
  and one scan more, which resets T302.  Then the input is on again, and T302
  times from that rise.  Result2 is 1 if the coil is on at 200 ms on T302, and
  Result1 if it is on at 400 ms.  An on side that restarts at the rise comes
  on at 300 ms and reads Result2 = 0, Result1 = 1.  One that runs on from 200
  comes on near 100 ms and reads Result2 = 1, Result1 = 1.  On 2026-11-11 a
  CLICK ran an earlier ladder, which ended the off time once the off TD read
  100.  That case never fired, so the off TD never reached 100.  The coil
  was off all that time.  Every other reading of an off TD had the coil on.
  So no step ends on the off TD now.
- ``flasher_restarts_with_on_phase`` (O4, 300 ms phases): the rung drops once
  the off TD reads 150.  It stays false while T305 times 100 ms, then one scan
  more, which resets T305.  On the scan T305 reaches 100 the case reads the
  on TD (Result1: 1 = cleared, 2 = held at or past its preset) and the off TD
  (Result3: 1 = cleared, 2 = kept mid-phase).  ErrorCode is 1 if the off TD
  is under 200 then (frozen or cleared) and 2 if not (still counting).  T305
  then times from the restart.  Result2 is 1 if the coil is on at 50 ms, and
  Result4 if it is on at 200 ms.  A CLICK keeps both TDs, frozen, yet
  restarts with an on phase, and pyrung now does too.  Hardware round 2
  (2026-10-30) read 2, 1, 2, 1, 1 on an earlier ladder that read the coil at
  fixed times.  A flasher that froze and resumed (pyrung before round 2)
  reads 2, 0, 2, 1, 1.  It runs the off phase's last 150 ms and then a new on phase.  A
  fresh off phase reads Result2 = 0, Result4 = 0.
- ``timed_needs_fresh_edge`` (O5, a 200 ms pulse): Result1 is 1 if the coil is
  on before its TD reaches 200.  T308 then times 200 ms with the rung still
  held, and Result2 is 1 if the coil is on in that time.
- ``out_coil_sets_timer_done`` (O6, on side 200 ms): Result1 is 1 if T310 is
  on on any scan.  The case ends on the scan the on TD reaches 300, and
  Result2 is 1 if the coil is on then.  T300 times it out at step 0 after
  1000 ms, as T299 does O2.

A CLICK read each expectation on an earlier ladder that read the same things
at fixed times.  None is new.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Or, Rung, copy, debounce, flasher, on_delay, reset, timed, toggle
from tests.conformance.memory import ClickTimer, c, t, td
from tests.conformance.scans import deadline

#: O2 and O6 wait for an on TD to reach 300 ms.  A TON of their own times them out at 1000 ms.
DEADLINE_MS = 1000


def prove_toggle_flips_once_per_edge(slot):
    # O1: the rung goes true and stays true; the bit flips once.
    with Rung(slot.Cmd != 0):
        toggle(c[1801])
    with Rung(slot.Cmd != 0, c[1801]):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, ~c[1801]):
        copy(2, slot.Result1)
    with Rung(slot.Cmd != 0):
        copy(1, slot.Fired, oneshot=True)


def _step(slot: Any, n: int) -> tuple[Any, Any]:
    return (slot.Cmd != 0, slot.Scratch == n)


def prove_debounce_td_keeps_counting(slot):
    # O2: the on side fires at 100 ms.  The case ends on the scan its TD reaches 300.
    with Rung(slot.Cmd != 0):
        debounce(c[1811], on=(ClickTimer(301), 100), off=(ClickTimer(311), 1000))
    with Rung(slot.Cmd != 0, c[1811]):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 1)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 0), td[301] >= 300, c[1811]):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 0), td[301] >= 300):
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(299), DEADLINE_MS)


def prove_debounce_bounce_restarts_on_side(slot):
    # O3: the input is on until the on TD reads 200, off for 100 ms on T302, then on.
    # T302 times again from the second rise.
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 0, slot.Scratch >= 3)):
        debounce(c[1821], on=(ClickTimer(303), 300), off=(ClickTimer(304), 1000))
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 1, slot.Scratch >= 3)):
        on_delay(ClickTimer(302), 2000)
    with Rung(*_step(slot, 5)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 4), td[302] >= 400, c[1821]):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 4), td[302] >= 400):
        copy(5, slot.Scratch)
    with Rung(*_step(slot, 3), td[302] >= 200, c[1821]):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 3), td[302] >= 200):
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2)):
        copy(3, slot.Scratch)
    with Rung(*_step(slot, 1), td[302] >= 100):
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), td[303] >= 200):
        copy(1, slot.Scratch)


def prove_flasher_restarts_with_on_phase(slot):
    # O4: 300 ms phases.  The rung drops once the off TD reads 150, for 100 ms on T305 and one
    # scan more, which resets T305.  T305 then times from the restart.
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 0, slot.Scratch >= 3)):
        flasher(c[1831], on=(ClickTimer(306), 300), off=(ClickTimer(307), 300))
    with Rung(slot.Cmd != 0, Or(slot.Scratch == 1, slot.Scratch >= 3)):
        on_delay(ClickTimer(305), 2000)
    with Rung(*_step(slot, 5)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 4), td[305] >= 200, c[1831]):
        copy(1, slot.Result4)
    with Rung(*_step(slot, 4), td[305] >= 200):
        copy(5, slot.Scratch)
    with Rung(*_step(slot, 3), td[305] >= 50, c[1831]):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 3), td[305] >= 50):
        copy(4, slot.Scratch)
    with Rung(*_step(slot, 2)):
        copy(3, slot.Scratch)
    falling = (*_step(slot, 1), td[305] >= 100)
    with Rung(*falling, td[306] == 0):
        copy(1, slot.Result1)
    with Rung(*falling, td[306] >= 300):
        copy(2, slot.Result1)
    with Rung(*falling, td[307] == 0):
        copy(1, slot.Result3)
    with Rung(*falling, td[307] > 0, td[307] < 300):
        copy(2, slot.Result3)
    with Rung(*falling, td[307] < 200):
        copy(1, slot.ErrorCode)
    with Rung(*falling, td[307] >= 200):
        copy(2, slot.ErrorCode)
    with Rung(*falling):
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), td[307] >= 150):
        copy(1, slot.Scratch)


def prove_timed_needs_fresh_edge(slot):
    # O5: a 200 ms pulse with the rung held.  T308 times 200 ms more from the pulse's end.
    with Rung(slot.Cmd != 0):
        timed(c[1841], ClickTimer(309), 200)
    with Rung(*_step(slot, 1)):
        on_delay(ClickTimer(308), 2000)
    with Rung(*_step(slot, 2)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 1), c[1841]):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 1), td[308] >= 200):
        copy(2, slot.Scratch)
    with Rung(*_step(slot, 0), c[1841]):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 0), td[309] >= 200):
        copy(1, slot.Scratch)


def prove_out_coil_sets_timer_done(slot):
    # O6: does the coil write its T<n>?  The case ends on the scan the on TD reaches 300.
    with Rung(slot.Cmd != 0):
        debounce(c[1851], on=(ClickTimer(310), 200), off=(ClickTimer(312), 1000))
    with Rung(slot.Cmd != 0, t[310]):
        copy(1, slot.Result1)
    with Rung(*_step(slot, 1)):
        copy(1, slot.Fired)
    with Rung(*_step(slot, 0), td[310] >= 300, c[1851]):
        copy(1, slot.Result2)
    with Rung(*_step(slot, 0), td[310] >= 300):
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(300), DEADLINE_MS)


def init_toggle_flips_once_per_edge(slot):
    reset(c[1801])


def init_debounce_td_keeps_counting(slot):
    copy(0, td[301])


def init_debounce_bounce_restarts_on_side(slot):
    copy(0, td[303])


def init_flasher_restarts_with_on_phase(slot):
    copy(0, td[306])
    copy(0, td[307])


def init_timed_needs_fresh_edge(slot):
    copy(0, td[309])


def init_out_coil_sets_timer_done(slot):
    copy(0, td[310])


group = Group(
    "out",
    [
        case(
            "A toggle coil flips once when its rung goes true and holds while it stays "
            "true (Result1 = 1 for on)",
            ladder=prove_toggle_flips_once_per_edge,
            init=init_toggle_flips_once_per_edge,
            expect={"Result1": 1},
            scans=20,
        ),
        case(
            "A debounce coil's on-side TD keeps counting past its preset while the input "
            "stays on, as a TON's does (Result2 = 1 once it passes 300 ms on a 100 ms "
            "preset)",
            ladder=prove_debounce_td_keeps_counting,
            init=init_debounce_td_keeps_counting,
            ready=lambda slot: ~c[1811],
            keep=[t[311], td[311], td[301]],
            expect={"Result1": 1, "Result2": 1},
            scans=50,
        ),
        case(
            "A bounce restarts a debounce coil's on-side timing: on for 200 ms, off for "
            "100 ms, then on with a 300 ms on side, the coil is still off 200 ms after the "
            "input comes back (Result2 = 0) and on 400 ms after (Result1 = 1)",
            ladder=prove_debounce_bounce_restarts_on_side,
            init=init_debounce_bounce_restarts_on_side,
            ready=lambda slot: ~c[1821],
            keep=[t[304], td[304]],
            expect={"Result1": 1, "Result2": 0},
            scans=100,
        ),
        case(
            "A flasher whose rung goes false freezes, then restarts with an on phase: 150 ms "
            "into its off phase, its on-side TD is kept (Result1 = 2) and its off-side TD "
            "kept (Result3 = 2) and standing still (ErrorCode = 1); back on, it is on 50 ms "
            "after (Result2 = 1) and 200 ms after (Result4 = 1)",
            ladder=prove_flasher_restarts_with_on_phase,
            init=init_flasher_restarts_with_on_phase,
            expect={"Result1": 2, "Result2": 1, "Result3": 2, "Result4": 1, "ErrorCode": 1},
            scans=120,
        ),
        case(
            "A timed coil (not maintained) pulses once with its rung held: on (Result1 = "
            "1), then off through the 200 ms after the pulse (Result2 = 0)",
            ladder=prove_timed_needs_fresh_edge,
            init=init_timed_needs_fresh_edge,
            expect={"Result1": 1, "Result2": 0},
            scans=50,
        ),
        case(
            "An Out coil never sets its Timer's done bit T<n>: a debounce coil whose on "
            "phase is done (Result2 = 1) leaves T<n> off (Result1 = 0)",
            ladder=prove_out_coil_sets_timer_done,
            init=init_out_coil_sets_timer_done,
            ready=lambda slot: ~c[1851],
            keep=[t[312], td[312], td[310]],
            expect={"Result1": 0, "Result2": 1},
            scans=40,
        ),
    ],
)


def test_out():
    assert_all_passed(run(group))


#: Each case redesigned to step on its timers, with the time it takes in-process.
STEPPED = {
    "debounce_td_keeps_counting": 0.5,
    "debounce_bounce_restarts_on_side": 1.0,
    "flasher_restarts_with_on_phase": 1.0,
    "timed_needs_fresh_edge": 0.6,
    "out_coil_sets_timer_done": 0.5,
}


@pytest.mark.parametrize("dt", [0.001, 0.010, 0.025, 0.040, 0.050])
@pytest.mark.parametrize("case_id", STEPPED)
def test_out_at_any_scan(monkeypatch, dt, case_id):
    """The CLICK scans in about 1 ms, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    stepped = next(c for c in group.cases if c.id == case_id)
    scans = int(STEPPED[case_id] / dt)
    assert_all_passed(run(Group("out", [replace(stepped, scans=scans)])))
