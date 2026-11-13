"""On-delay, retentive on-delay and off-delay timers: the ``timers`` group
(subroutine ``Conf_Timers``).

Each timer is a raw CLICK Timer, ``T<n>`` with ``TD<n>`` (``ClickTimer``).  The
cases own T1..T100 and C1401..C1500 (``memory.OWNED``), one run of ten timers
per case (T1.., T11.., ...); the last two cases split T91..T100.

Over the wire a timer sees wall-clock time, so no expectation is a raw
accumulator.  A case either counts its own scans (``scans.py``) and asks what
the timer did on a given scan, or runs in steps that ``Scratch`` holds.  A step
ends on a threshold of a timer's own Acc or Done, so no expectation depends on
how long a scan lasts.  A step's rungs come below the timers in reverse order,
so each new step starts on the next scan.  A step that waits a set time runs a
200 ms TON of its own and ends on its Done.  Each stepped case has a deadline
TON of its own (``scans.deadline``) that times the case out at a step.
``test_stepped_cases_at_any_scan`` runs each one at scans of 0.2 to 50 ms.

The reset is the instructions' own: every TON and TOF rung is gated on ``Cmd``,
and each RTON's reset input is ``Cmd == 0``.  A TOF has no reset: once its rung
drops it times out and its TD keeps counting, so the TOF cases ``keep`` it.

Every case but the six stepped ones passed on a CLICK as written (hardware round 1, 2026-10-29).
The six run on a CLICK at the next hardware round.  Which case tells pyrung's model from which other model:

- ``ton_auto_reset``: step 0 runs the TON until its Acc reads 500, and step 1
  is the first scan its rung is false.  A TON that held its accumulator when
  the rung went false (an RTON with no reset) reads Result1 > 0 and Result2 = 1.
  One whose Acc stopped at the preset never ends step 0 (it times out there).
- ``ton_done_on_enabling_scan``: pyrung adds the enabling scan's time and
  judges Done on that scan, so a 0 ms preset is done on the scan its rung goes
  true.  A timer that only starts timing on the enabling scan ("no tick until
  the next scan") reads Result1 = 5, Result2 = 4.
- ``rton_holds_across_rung_false``: step 0 runs the RTON until its Acc reads
  250, step 1 waits 200 ms with its rung false, and step 2 reads it.  An RTON
  that dropped like a TON reads 0, 0.  One that kept timing reads Result2 = 0.
- ``rton_reset_gap``: pyrung's reset clears Acc and Done and returns, so the
  reset scan reads Done off even with a 0 ms preset and the rung true.  A reset
  that then re-judges Done (Acc 0 >= preset 0) reads Result1 = 0; one that also
  counts the enabled rung's time on the reset scan reads Result4 = 0.
- ``tof_never_enabled``: two TOFs with 1000 ms presets, one whose rung is true
  on scans 1..2 and one whose rung is never true.  A TOF that times whenever
  its rung is false, enabled or not, reads Result3 > 0; one that starts done,
  as if it had been enabled, reads Result1 = 8; one done only while its rung
  is true (no off-delay) reads Result2 = 2.
- ``tof_times_only_when_false``: step 0 holds the rung true for 200 ms, step 1
  holds it false until the TOF's Acc reads 400, and step 2 reads it.  The
  case keeps the TOF's TD, so its reset writes 0 to it.  The TOF's rise scan
  sets Done and clears Acc (``tof_enable_atomic``), so step 0 counts it as
  Done on with Acc 0.  A TOF that counts while the rung is true reads
  Result1 > 0.  A TOF whose Done is judged before its Acc moves (Done a scan
  late) reads Result2 = 1.  A TOF that drops Done at once reads Result3 = 0.
  A TOF whose Acc stops at the preset never ends step 1 (it times out there).
- ``tof_enable_atomic``: a TOF that sets Done a scan after the rung returns
  reads Result1 = 3, Result3 = 5; one that clears Acc a scan late reads
  Result4 = 1.
- ``preset_raised_resumes`` and ``preset_lowered_fires_at_once``: ``Result4``
  holds the preset.  Step 0 ends on the TON's Done (raised) or on its Acc
  reading 300 (lowered), and writes the new preset.  Step 1 is the first scan
  with it, and the next step ends the case.  A timer that reads its preset only
  when it starts timing reads Result1 = 0 in both.  A timer that restarts
  timing on a preset change reads Result2 = 0.  A timer that times a raised
  preset from the scan it changed (rather than from its enable) reads
  Result3 = 0.  The raised case ends step 2 on the TON's Done and reads
  Result3 = 1 if Acc is under 600 then.  "Lowered below Acc" also catches a
  Done that is set only on the scan Acc crosses the preset (Result1 = 0).
- ``fractional_accumulation``: a timer that drops the part of a unit each scan
  leaves a 1 s timer at 0 when every scan is 10 ms (Result1 = 0); one that
  counts seconds on a free-running 1 s tick, not from its enable, is almost
  always done early (Result2 = 0).
- ``acc_clamps_at_32767``: once the TON is timing, its TD is written to 32000,
  so it reaches the clamp in under a second rather than 33 s.  An accumulator
  that wraps reads negative; one that stops at the preset never fires; a TON
  that keeps its own count and ignores the write never fires and reads
  Result3 = 0.  Step 0 writes the TD and step 1 ends once Acc reads 32767.
  Result2 is 1 if Done is on during that scan.  Step 2 runs a 200 ms TON (T98),
  then reads Result1 = Acc.  The case times out after 3000 ms on T97 if it has
  not fired (``scans.deadline``).

The 0 ms presets (``ton_done_on_enabling_scan``, ``rton_reset_gap``,
``tof_enable_atomic``) keep those cases about scans, not milliseconds: they
hold however short the PLC's scan is.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.conformance import _runner as runner_module
from pyrung.core import Or, Rung, copy, math, off_delay, on_delay, out
from tests.conformance.memory import ClickTimer, c
from tests.conformance.scans import WINDOW, count_scans, deadline, done, scans_for, trace

SCANS = scans_for()
#: Each deadline is about 3 times what the case takes at 40 ms scans.  The timer is the case's own.
#: ``ton_auto_reset`` takes about 550 ms (T3).
TON_AUTO_RESET_DEADLINE_MS = 1500
#: ``rton_holds_across_rung_false`` takes about 500 ms (T23).
RTON_HOLDS_DEADLINE_MS = 1500
#: ``tof_times_only_when_false`` takes about 650 ms (T53).
TOF_TIMES_DEADLINE_MS = 2000
#: ``preset_raised_resumes`` takes about 550 ms (T72).
PRESET_RAISED_DEADLINE_MS = 1800
#: ``preset_lowered_fires_at_once`` takes about 350 ms (T82).
PRESET_LOWERED_DEADLINE_MS = 1000
#: ``acc_clamps_at_32767`` takes about 1000 ms (T97).
ACC_CLAMP_DEADLINE_MS = 3000


def step(slot, n):
    """The conditions of step *n*.  ``Scratch`` holds the step the case is on."""
    return (slot.Cmd != 0, slot.Scratch == n)


def prove_ton_auto_reset(slot):
    # T1's rung is true in step 0 only.  The step ends once Acc reads 500, past the 200 ms
    # preset.  Step 1 is the first scan the rung is false.
    ton = ClickTimer(1)
    with Rung(*step(slot, 0)):
        on_delay(ton, 200)
    with Rung(*step(slot, 2)):
        copy(1, slot.Fired)
    with Rung(*step(slot, 1), ton.Acc != 0):
        copy(ton.Acc, slot.Result1)
    with Rung(*step(slot, 1), ton.Done):
        copy(1, slot.Result2)
    with Rung(*step(slot, 1)):
        copy(2, slot.Scratch)
    with Rung(*step(slot, 0), ton.Acc >= 500, ton.Done):
        copy(1, slot.Result3)
    with Rung(*step(slot, 0), ton.Acc >= 500):
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(3), TON_AUTO_RESET_DEADLINE_MS)


def prove_ton_done_on_enabling_scan(slot):
    # The rung goes true on scan 3; the preset is 0 ms.
    ton = ClickTimer(11)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        on_delay(ton, 0)
    trace(slot, ton.Done)
    done(slot)


def prove_rton_holds_across_rung_false(slot):
    # T21's rung is true in step 0 only, until its Acc reads 250.  Step 1 runs T22, a 200 ms
    # TON, so the RTON would have counted on if it did not hold.  Step 2 reads it.
    rton, wait = ClickTimer(21), ClickTimer(22)
    with Rung(*step(slot, 0)):
        on_delay(rton, 200).reset(slot.Cmd == 0)
    with Rung(*step(slot, 1)):
        on_delay(wait, 200)
    with Rung(*step(slot, 2)):
        copy(1, slot.Fired)
    with Rung(*step(slot, 2), rton.Done):
        copy(1, slot.Result1)
    with Rung(*step(slot, 2), rton.Acc >= 250, rton.Acc < 300):
        copy(1, slot.Result2)
    with Rung(*step(slot, 1), wait.Done):
        copy(2, slot.Scratch)
    with Rung(*step(slot, 0), rton.Acc >= 250):
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(23), RTON_HOLDS_DEADLINE_MS)


def prove_rton_reset_gap(slot):
    # The rung is true throughout, the preset is 0 ms, and the reset is on for
    # scan 4 only.
    rton = ClickTimer(31)
    count_scans(slot)
    with Rung(slot.Cmd != 0):
        on_delay(rton, 0).reset(Or(slot.Cmd == 0, slot.Scratch == 4))
    trace(slot, ~rton.Done)
    with Rung(slot.Cmd != 0, slot.Scratch == 4, rton.Acc == 0):
        copy(1, slot.Result4)
    done(slot)


#: The ``tof_never_enabled`` presets: far longer than the window takes, so the
#: TOF that was enabled is still timing (and done) when the window ends.
TOF_LONG_PRESET = 1000


def prove_tof_never_enabled(slot):
    # T41's rung is true on scans 1..2, then false.  Scratch is 1 or more on
    # every scan Cmd != 0, so T42's rung is never true.
    dropped, never = ClickTimer(41), ClickTimer(42)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch <= 2):
        off_delay(dropped, TOF_LONG_PRESET)
    with Rung(slot.Cmd != 0, slot.Scratch == 0):
        off_delay(never, TOF_LONG_PRESET)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, never.Done):
        math(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, dropped.Done):
        math(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, never.Acc != 0):
        math(slot.Result3 + 1, slot.Result3)
    done(slot)


def init_tof_times_only_when_false(slot):
    # The TOF's TD is kept, so no step may start from one the last run left.
    copy(0, ClickTimer(51).Acc)


def prove_tof_times_only_when_false(slot):
    # T51's rung is true in step 0, which T52, a 200 ms TON, ends.  The rung is false from step 1,
    # which ends once the TOF's Acc reads 400.  Step 2 reads it.
    tof, wait = ClickTimer(51), ClickTimer(52)
    with Rung(*step(slot, 0)):
        off_delay(tof, 200)
    with Rung(*step(slot, 0)):
        on_delay(wait, 200)
    with Rung(*step(slot, 2)):
        copy(1, slot.Fired)
    with Rung(*step(slot, 2), ~tof.Done, tof.Acc >= 400):
        copy(1, slot.Result4)
    # Rung false: count the scans Done disagrees with Acc < 200.
    with Rung(*step(slot, 1), tof.Done, tof.Acc >= 200):
        math(slot.Result2 + 1, slot.Result2)
    with Rung(*step(slot, 1), ~tof.Done, tof.Acc < 200):
        math(slot.Result2 + 1, slot.Result2)
    # It timed with Done still on.
    with Rung(*step(slot, 1), tof.Done, tof.Acc > 0, tof.Acc < 200):
        copy(1, slot.Result3)
    with Rung(*step(slot, 1), tof.Acc >= 400):
        copy(2, slot.Scratch)
    # Rung true: count the scans Done is off or Acc is not 0.
    with Rung(*step(slot, 0), Or(~tof.Done, tof.Acc != 0)):
        math(slot.Result1 + 1, slot.Result1)
    with Rung(*step(slot, 0), wait.Done):
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(53), TOF_TIMES_DEADLINE_MS)


def prove_tof_enable_atomic(slot):
    # The rung is false on scans 3 and 4 only; the preset is 0 ms, so Done
    # falls on scan 3.
    tof = ClickTimer(61)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch != 3, slot.Scratch != 4):
        off_delay(tof, 0)
    trace(slot, ~tof.Done)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, tof.Done, tof.Acc != 0):
        math(slot.Result4 + 1, slot.Result4)
    done(slot)


def prove_preset_raised_resumes(slot):
    # The preset is Result4: 200 ms, raised to 500 ms on the scan T71 is done (step 0).
    # Step 1 is the first scan T71 runs with the raised preset.  Step 2 ends on the TON's Done.
    ton = ClickTimer(71)
    with Rung(slot.Cmd != 0, slot.Result4 == 0):
        copy(200, slot.Result4)
    with Rung(slot.Cmd != 0):
        on_delay(ton, slot.Result4)
    with Rung(*step(slot, 3)):
        copy(1, slot.Fired)
    with Rung(*step(slot, 2), ton.Done, ton.Acc < 600):
        copy(1, slot.Result3)
    with Rung(*step(slot, 2), ton.Done):
        copy(3, slot.Scratch)
    with Rung(*step(slot, 1), ~ton.Done):
        copy(1, slot.Result1)
    with Rung(*step(slot, 1), ton.Acc >= 200):
        copy(1, slot.Result2)
    with Rung(*step(slot, 1)):
        copy(2, slot.Scratch)
    with Rung(*step(slot, 0), ton.Done):
        copy(500, slot.Result4)
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(72), PRESET_RAISED_DEADLINE_MS)


def prove_preset_lowered_fires_at_once(slot):
    # The preset is Result4: 1000 ms, lowered to 100 ms once Acc reads 300 (step 0).
    # Step 1 is the first scan T81 runs with the lowered preset.
    ton = ClickTimer(81)
    with Rung(slot.Cmd != 0, slot.Result4 == 0):
        copy(1000, slot.Result4)
    with Rung(slot.Cmd != 0):
        on_delay(ton, slot.Result4)
    with Rung(*step(slot, 1), ton.Done):
        copy(1, slot.Result1)
    with Rung(*step(slot, 2)):
        copy(1, slot.Fired)
    with Rung(*step(slot, 1), ton.Acc >= 300):
        copy(1, slot.Result2)
    with Rung(*step(slot, 1)):
        copy(2, slot.Scratch)
    with Rung(*step(slot, 0), ton.Acc >= 300):
        copy(100, slot.Result4)
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(82), PRESET_LOWERED_DEADLINE_MS)


def prove_fractional_accumulation(slot):
    # T91 counts seconds, T92 milliseconds, both from the same scan.  C1491 is
    # on for the scan T91 is first done.
    sec, clock = ClickTimer(91), ClickTimer(92)
    with Rung(slot.Cmd != 0):
        on_delay(clock, 3000)
    with Rung(slot.Cmd != 0):
        on_delay(sec, 1, unit="sec")
    with Rung(slot.Cmd != 0, sec.Done):
        out(c[1491], oneshot=True)
    with Rung(slot.Cmd != 0, c[1491], clock.Acc >= 990):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, c[1491], clock.Acc < 1100):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, clock.Acc >= 1500):
        copy(sec.Acc, slot.Result1, oneshot=True)
        copy(1, slot.Fired, oneshot=True)


def prove_acc_clamps_at_32767(slot):
    # A 100 ms TON.  A rung below it writes its TD to 32000 on the first scan its Acc reads above 0
    # (step 0).  The write comes after the TON runs, so a TON that clears TD as it starts has
    # already done so.  Step 1 ends once Acc reads 32767.  Step 2 runs T98, a 200 ms TON, so a TD
    # that wraps or falls back shows.
    ton, wait = ClickTimer(96), ClickTimer(98)
    with Rung(slot.Cmd != 0):
        on_delay(ton, 100)
    with Rung(*step(slot, 2)):
        on_delay(wait, 200)
    with Rung(*step(slot, 3)):
        copy(1, slot.Fired)
    with Rung(*step(slot, 2), wait.Done):
        copy(ton.Acc, slot.Result1)
        copy(3, slot.Scratch)
    with Rung(*step(slot, 1), ton.Acc >= 32000):
        copy(1, slot.Result3)
    with Rung(*step(slot, 1), ton.Acc == 32767, ton.Done):
        copy(1, slot.Result2)
    with Rung(*step(slot, 1), ton.Acc == 32767):
        copy(2, slot.Scratch)
    with Rung(*step(slot, 0), ton.Acc > 0):
        copy(32000, ton.Acc)
        copy(1, slot.Scratch)
    deadline(slot, ClickTimer(97), ACC_CLAMP_DEADLINE_MS)


group = Group(
    "timers",
    [
        case(
            "A TON drops Acc and Done together on the scan its rung goes false: run past "
            "its 200 ms preset (Acc 500 or more, Result3 = 1), its rung dropped, it reads "
            "Acc 0 on that scan (Result1 = 0) and Done off (Result2 = 0)",
            ladder=prove_ton_auto_reset,
            expect={"Result1": 0, "Result2": 0, "Result3": 1},
            scans=80,
        ),
        case(
            "A TON counts the scan that enables it and judges Done on it: with a 0 ms "
            "preset and its rung true from scan 3, Done is on for 6 scans (Result1 = 6), "
            "from scan 3 (Result2) to scan 8 (Result3)",
            ladder=prove_ton_done_on_enabling_scan,
            expect={"Result1": 6, "Result2": 3, "Result3": 8},
            scans=SCANS,
        ),
        case(
            "An RTON holds Done and Acc while its rung is false: true until its Acc reads 250 "
            "on a 200 ms preset, 200 ms later it is still done (Result1 = 1) and its Acc "
            "still reads 250..299 (Result2 = 1)",
            ladder=prove_rton_holds_across_rung_false,
            expect={"Result1": 1, "Result2": 1},
            scans=60,
        ),
        case(
            "An RTON reset clears Acc and Done and does not judge Done again that scan: "
            "with a 0 ms preset, its rung always true and its reset on for scan 4 only, "
            "Done is off for 1 scan (Result1 = 1), scan 4 (Result2 = 4, Result3 = 4), "
            "with Acc 0 (Result4 = 1)",
            ladder=prove_rton_reset_gap,
            expect={"Result1": 1, "Result2": 4, "Result3": 4, "Result4": 1},
            scans=SCANS,
        ),
        case(
            "A TOF whose rung has never been true is not done and does not time, unlike "
            "one whose rung was true and fell: with 1000 ms presets, the never-enabled "
            "TOF is done for 0 of 8 scans (Result1 = 0) with Acc 0 on all of them "
            "(Result3 = 0), while the one true on scans 1..2 is done on all 8 "
            "(Result2 = 8)",
            ladder=prove_tof_never_enabled,
            keep=[ClickTimer(41).Done, ClickTimer(41).Acc],
            expect={"Result1": 0, "Result2": 8, "Result3": 0},
            scans=SCANS,
        ),
        case(
            "A TOF is done with Acc 0 while its rung is true (Result1 = 0 scans not), "
            "times only once it is false, done exactly while Acc is under its 200 ms "
            "preset (Result2 = 0 scans not, Result3 = 1), and its Acc goes on past the "
            "preset after Done falls, to 400 or more (Result4 = 1)",
            ladder=prove_tof_times_only_when_false,
            init=init_tof_times_only_when_false,
            keep=[ClickTimer(51).Done, ClickTimer(51).Acc],
            expect={"Result1": 0, "Result2": 0, "Result3": 1, "Result4": 1},
            scans=100,
        ),
        case(
            "A TOF sets Done and clears Acc on the scan its rung comes back: with a 0 ms "
            "preset and its rung false on scans 3..4, Done is off for 2 scans "
            "(Result1 = 2), scans 3 (Result2) to 4 (Result3), and never on with Acc "
            "not 0 (Result4 = 0)",
            ladder=prove_tof_enable_atomic,
            keep=[ClickTimer(61).Done, ClickTimer(61).Acc],
            expect={"Result1": 2, "Result2": 3, "Result3": 4, "Result4": 0},
            scans=SCANS,
        ),
        case(
            "A TON whose preset is raised past its Acc resumes: raised from 200 to 500 ms "
            "on the scan it is done, the next scan it is not done (Result1 = 1) with its "
            "Acc kept (Result2 = 1), and it is done again before 600 ms (Result3 = 1)",
            ladder=prove_preset_raised_resumes,
            expect={"Result1": 1, "Result2": 1, "Result3": 1},
            scans=70,
        ),
        case(
            "A TON whose preset is lowered below its Acc is done on the next scan: "
            "lowered from 1000 to 100 ms at Acc 300, the next scan it is done "
            "(Result1 = 1) with its Acc kept (Result2 = 1)",
            ladder=prove_preset_lowered_fires_at_once,
            expect={"Result1": 1, "Result2": 1},
            scans=50,
        ),
        case(
            "A timer carries the part of a unit each scan leaves over: a 1 s timer whose "
            "scans each add 0.01 s reads 1 at 1.5 s (Result1 = 1), and is done once "
            "990..1099 ms have passed since its enable (Result2 = 1, Result3 = 1)",
            ladder=prove_fractional_accumulation,
            expect={"Result1": 1, "Result2": 1, "Result3": 1},
            scans=170,
        ),
        case(
            "A timer's Acc runs on past its preset and stops at 32767: a 100 ms TON whose "
            "TD is written to 32000 once it is timing keeps counting from there "
            "(Result3 = 1), is done on the scan it first reads 32767 (Result2 = 1) and "
            "still reads 32767 200 ms later (Result1)",
            ladder=prove_acc_clamps_at_32767,
            expect={"Result1": 32767, "Result2": 1, "Result3": 1},
            scans=120,
        ),
    ],
)


def test_timers():
    assert_all_passed(run(group))


#: The seconds each stepped case runs for, with room to spare.
STEPPED_SECONDS = {
    "ton_auto_reset": 1.2,
    "rton_holds_across_rung_false": 1.2,
    "tof_times_only_when_false": 1.5,
    "preset_raised_resumes": 1.3,
    "preset_lowered_fires_at_once": 1.0,
    "acc_clamps_at_32767": 2.0,
}


@pytest.mark.parametrize("dt", [0.0002, 0.0005, 0.001, 0.010, 0.025, 0.040, 0.050])
@pytest.mark.parametrize("case_id", sorted(STEPPED_SECONDS))
def test_stepped_cases_at_any_scan(monkeypatch, case_id, dt):
    """The CLICK scans in about 1 ms, the soft PLC in 10..40 ms."""
    monkeypatch.setattr(runner_module, "DT", dt)
    (stepped,) = (c for c in group.cases if c.id == case_id)
    stepped = replace(stepped, scans=int(STEPPED_SECONDS[case_id] / dt))
    assert_all_passed(run(Group("timers", [stepped])))
