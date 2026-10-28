"""On-delay, retentive on-delay and off-delay timers: the ``timers`` group
(subroutine ``Conf_Timers``).

Each timer is a raw CLICK Timer, ``T<n>`` with ``TD<n>`` (``ClickTimer``).  The
cases own T1..T100 and C1401..C1500 (``memory.OWNED``), one run of ten timers
per case (T1.., T11.., ...); the last two cases split T91..T100.

Over the wire a timer sees wall-clock time, so no expectation is a raw
accumulator.  A case either counts its own scans (``scans.py``) and asks what
the timer did on a given scan, or runs a millisecond clock timer of its own,
enabled on the same scan as the timer under test, and records flags: "on the
scan X happened, Y held".  The two timers get the same time on every scan, so
"the timer under test read at least 500" and "the clock read under 600" pin a
window that does not depend on how fast the PLC scans.

The reset is the instructions' own: every TON and TOF rung is gated on ``Cmd``,
and each RTON's reset input is ``Cmd == 0``.  A TOF has no reset: once its rung
drops it times out and its TD keeps counting, so the TOF cases ``keep`` it.

The expectations are pyrung's answers, unconfirmed on a CLICK.  Which case
tells pyrung's model from which other model:

- ``ton_auto_reset``: a TON that held its accumulator when the rung went false
  (an RTON with no reset) reads Result1 > 0 and Result2 = 1; one whose Acc
  stopped at the preset reads Result3 = 0.
- ``ton_done_on_enabling_scan``: pyrung adds the enabling scan's time and
  judges Done on that scan, so a 0 ms preset is done on the scan its rung goes
  true.  A timer that only starts timing on the enabling scan ("no tick until
  the next scan") reads Result1 = 5, Result2 = 4.
- ``rton_holds_across_rung_false``: an RTON that dropped like a TON reads 0, 0.
- ``rton_reset_gap``: pyrung's reset clears Acc and Done and returns, so the
  reset scan reads Done off even with a 0 ms preset and the rung true.  A reset
  that then re-judges Done (Acc 0 >= preset 0) reads Result1 = 0; one that also
  counts the enabled rung's time on the reset scan reads Result4 = 0.
- ``tof_never_enabled``: two TOFs with 1000 ms presets, one whose rung is true
  on scans 1..2 and one whose rung is never true.  A TOF that times whenever
  its rung is false, enabled or not, reads Result3 > 0; one that starts done,
  as if it had been enabled, reads Result1 = 8; one done only while its rung
  is true (no off-delay) reads Result2 = 2.
- ``tof_times_only_when_false``: a TOF that counts while the rung is true
  reads Result1 > 0; one whose Done is judged before its Acc moves (Done a scan
  late) reads Result2 = 1; one that drops Done at once reads Result3 = 0; one
  whose Acc stops at the preset reads Result4 = 0.
- ``tof_enable_atomic``: a TOF that sets Done a scan after the rung returns
  reads Result1 = 3, Result3 = 5; one that clears Acc a scan late reads
  Result4 = 1.
- ``preset_raised_resumes`` and ``preset_lowered_fires_at_once``: a timer that
  reads its preset only when it starts timing reads Result1 = 0 in both; one
  that restarts timing on a preset change reads Result2 = 0; one that applies a
  raised preset from the scan it changed (rather than from its enable) reads
  Result3 = 0.  "Lowered below Acc" also catches a Done that is set only on the
  scan Acc crosses the preset (Result1 = 0).
- ``fractional_accumulation``: a timer that drops the part of a unit each scan
  leaves a 1 s timer at 0 when every scan is 10 ms (Result1 = 0); one that
  counts seconds on a free-running 1 s tick, not from its enable, is almost
  always done early (Result2 = 0).
- ``acc_clamps_at_32767``: once the TON is timing, its TD is written to 32000,
  so it reaches the clamp in under a second rather than 33 s.  An accumulator
  that wraps reads negative; one that stops at the preset never fires; a TON
  that keeps its own count and ignores the write never fires and reads
  Result3 = 0.

The 0 ms presets (``ton_done_on_enabling_scan``, ``rton_reset_gap``,
``tof_enable_atomic``) keep those cases about scans, not milliseconds: they
hold however short the PLC's scan is.
"""

from __future__ import annotations

from pyrung.conformance import Group, assert_all_passed, case, run
from pyrung.core import Or, Rung, calc, copy, off_delay, on_delay, out
from tests.conformance.memory import ClickTimer, c
from tests.conformance.scans import WINDOW, count_scans, done, scans_for, trace

SCANS = scans_for()


def prove_ton_auto_reset(slot):
    # T2 is a clock; T1's rung is true while the clock reads under 600 ms, so
    # T1 runs past its 200 ms preset and then drops.
    clock, ton = ClickTimer(2), ClickTimer(1)
    with Rung(slot.Cmd != 0):
        on_delay(clock, 3000)
    with Rung(slot.Cmd != 0, clock.Acc < 600):
        on_delay(ton, 200)
    with Rung(slot.Cmd != 0, clock.Acc < 600, ton.Done, ton.Acc >= 500):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, clock.Acc >= 600):
        copy(ton.Acc, slot.Result1, oneshot=True)
    with Rung(slot.Cmd != 0, clock.Acc >= 600, ton.Done):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, clock.Acc >= 700):
        copy(1, slot.Fired, oneshot=True)


def prove_ton_done_on_enabling_scan(slot):
    # The rung goes true on scan 3; the preset is 0 ms.
    ton = ClickTimer(11)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch >= 3):
        on_delay(ton, 0)
    trace(slot, ton.Done)
    done(slot)


def prove_rton_holds_across_rung_false(slot):
    # T22 is a clock; T21's rung is true while the clock reads under 300 ms.
    clock, rton = ClickTimer(22), ClickTimer(21)
    with Rung(slot.Cmd != 0):
        on_delay(clock, 3000)
    with Rung(slot.Cmd != 0, clock.Acc < 300):
        on_delay(rton, 200).reset(slot.Cmd == 0)
    with Rung(slot.Cmd != 0, clock.Acc >= 500, rton.Done):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, clock.Acc >= 500, rton.Acc >= 200, rton.Acc < 300):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, clock.Acc >= 500):
        copy(1, slot.Fired, oneshot=True)


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
        calc(slot.Result1 + 1, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, dropped.Done):
        calc(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, never.Acc != 0):
        calc(slot.Result3 + 1, slot.Result3)
    done(slot)


def prove_tof_times_only_when_false(slot):
    # T52 is a clock; T51's rung is true while the clock reads under 300 ms.
    clock, tof = ClickTimer(52), ClickTimer(51)
    with Rung(slot.Cmd != 0):
        on_delay(clock, 3000)
    with Rung(slot.Cmd != 0, clock.Acc < 300):
        off_delay(tof, 200)
    # Rung true (100..299 ms): count the scans Done is off or Acc is not 0.
    with Rung(slot.Cmd != 0, clock.Acc >= 100, clock.Acc < 300, Or(~tof.Done, tof.Acc != 0)):
        calc(slot.Result1 + 1, slot.Result1)
    # Rung false (300..799 ms): count the scans Done disagrees with Acc < 200.
    with Rung(slot.Cmd != 0, clock.Acc >= 300, clock.Acc < 800, tof.Done, tof.Acc >= 200):
        calc(slot.Result2 + 1, slot.Result2)
    with Rung(slot.Cmd != 0, clock.Acc >= 300, clock.Acc < 800, ~tof.Done, tof.Acc < 200):
        calc(slot.Result2 + 1, slot.Result2)
    # It timed with Done still on ...
    with Rung(slot.Cmd != 0, clock.Acc >= 300, tof.Done, tof.Acc > 0, tof.Acc < 200):
        copy(1, slot.Result3)
    # ... and its Acc went on past the preset after Done fell.
    with Rung(slot.Cmd != 0, clock.Acc >= 800, ~tof.Done, tof.Acc >= 400):
        copy(1, slot.Result4)
    with Rung(slot.Cmd != 0, clock.Acc >= 800):
        copy(1, slot.Fired, oneshot=True)


def prove_tof_enable_atomic(slot):
    # The rung is false on scans 3 and 4 only; the preset is 0 ms, so Done
    # falls on scan 3.
    tof = ClickTimer(61)
    count_scans(slot)
    with Rung(slot.Cmd != 0, slot.Scratch != 3, slot.Scratch != 4):
        off_delay(tof, 0)
    trace(slot, ~tof.Done)
    with Rung(slot.Cmd != 0, slot.Scratch <= WINDOW, tof.Done, tof.Acc != 0):
        calc(slot.Result4 + 1, slot.Result4)
    done(slot)


def prove_preset_raised_resumes(slot):
    # The preset (Scratch) is 200 ms, raised to 500 ms on the scan T71 is done.
    # C1471 is on for the first scan T71 runs with the raised preset.
    ton = ClickTimer(71)
    with Rung(slot.Cmd != 0):
        copy(200, slot.Scratch, oneshot=True)
    with Rung(slot.Cmd != 0):
        on_delay(ton, slot.Scratch)
    with Rung(slot.Cmd != 0, slot.Scratch == 500):
        out(c[1471], oneshot=True)
    with Rung(slot.Cmd != 0, c[1471], ~ton.Done):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, c[1471], ton.Acc >= 200):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch == 500, ton.Done, ton.Acc < 600):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, slot.Scratch == 500, ton.Done):
        copy(1, slot.Fired, oneshot=True)
    with Rung(slot.Cmd != 0, slot.Scratch == 200, ton.Done):
        copy(500, slot.Scratch)


def prove_preset_lowered_fires_at_once(slot):
    # The preset (Scratch) is 1000 ms, lowered to 100 ms once Acc reads 300.
    # C1481 is on for the first scan T81 runs with the lowered preset.
    ton = ClickTimer(81)
    with Rung(slot.Cmd != 0):
        copy(1000, slot.Scratch, oneshot=True)
    with Rung(slot.Cmd != 0):
        on_delay(ton, slot.Scratch)
    with Rung(slot.Cmd != 0, slot.Scratch == 100):
        out(c[1481], oneshot=True)
    with Rung(slot.Cmd != 0, c[1481], ton.Done):
        copy(1, slot.Result1)
    with Rung(slot.Cmd != 0, c[1481], ton.Acc >= 300):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, c[1481]):
        copy(1, slot.Fired, oneshot=True)
    with Rung(slot.Cmd != 0, slot.Scratch == 1000, ton.Acc >= 300):
        copy(100, slot.Scratch)


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
    # A 100 ms TON whose TD is written to 32000 once it is timing (not on the
    # enabling scan, in case a TON clears TD as it starts), so it reaches the
    # clamp in 767 ms.  Scratch counts the scans its Acc reads 32767.
    ton = ClickTimer(96)
    with Rung(slot.Cmd != 0):
        on_delay(ton, 100)
    with Rung(slot.Cmd != 0, ton.Acc > 0):
        copy(32000, ton.Acc, oneshot=True)
    with Rung(slot.Cmd != 0, ton.Acc >= 32000):
        copy(1, slot.Result3)
    with Rung(slot.Cmd != 0, ton.Acc == 32767, slot.Scratch < 10):
        calc(slot.Scratch + 1, slot.Scratch)
    with Rung(slot.Cmd != 0, slot.Scratch >= 10):
        copy(ton.Acc, slot.Result1)
    with Rung(slot.Cmd != 0, slot.Scratch >= 10, ton.Done):
        copy(1, slot.Result2)
    with Rung(slot.Cmd != 0, slot.Scratch >= 10):
        copy(1, slot.Fired, oneshot=True)


group = Group(
    "timers",
    [
        case(
            "A TON drops Acc and Done together on the scan its rung goes false: run past "
            "its 200 ms preset (Acc 500 or more, Result3 = 1), its rung dropped at 600 ms, "
            "it reads Acc 0 on that scan (Result1 = 0) and Done never on after (Result2 = 0)",
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
            "An RTON holds Done and Acc while its rung is false: true until 300 ms on a "
            "200 ms preset, 200 ms later it is still done (Result1 = 1) and its Acc still "
            "reads 200..299 (Result2 = 1)",
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
            "preset after Done falls (Result4 = 1)",
            ladder=prove_tof_times_only_when_false,
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
            "(Result3 = 1), reads 32767 (Result1) and is still done (Result2 = 1)",
            ladder=prove_acc_clamps_at_32767,
            expect={"Result1": 32767, "Result2": 1, "Result3": 1},
            scans=100,
        ),
    ],
)


def test_timers():
    assert_all_passed(run(group))
