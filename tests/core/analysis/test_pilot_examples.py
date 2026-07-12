"""PILOT integration against the shipped ``examples/`` programs.

These drive ``pilot_how`` end-to-end on realistic programs.  Two kinds of case:

* **Clean targets** — ``how()`` reaches them today; the test asserts ``reachable``
  *and* replays the returned path on a fresh PLC (two-oracle check, matching the
  convention in ``test_packml_diagnosis``).
* **Frontier targets** — cases PILOT *should* solve but cannot yet, marked
  ``xfail`` with a ``pilot:`` reason.  They assert the behaviour PILOT should
  have, so the day it gains the capability the test **xpasses** and flags the
  gap as closed (xfail is non-strict — an xpass is a signal, not a failure).

The example modules run an import-time simulation unless ``PYRUNG_DAP_ACTIVE``
is set; PILOT only needs the program object, so we skip the demo run.
"""

from __future__ import annotations

import os

os.environ.setdefault("PYRUNG_DAP_ACTIVE", "1")


from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replays_to(plc_factory, path, tag: str, expected) -> bool:
    """Replay the plan's recording and report whether ``tag`` lands on ``expected``.

    The reached fork's ``scan_log`` + synthesis holds are the recording, so
    ``Plan.replay`` reconstructs the drive (holds and all) with no re-derivation —
    the concrete oracle behind every ``how()`` result.  ``plc_factory`` is unused
    now that the recording carries its own program and initial state."""
    return path.replay().state.tags.get(tag) == expected


# ===========================================================================
# click_conveyor — start/stop latch, sort state machine, diverter, counters
# ===========================================================================


def _conveyor():
    """Lazy import (after ``_clean_block_state`` has reset ClickBlocks) so the
    program is built against fresh block state."""
    from examples import click_conveyor as cv

    return cv


def _cv_plc(cv):
    return PLC(cv.logic, dt=0.010)


def test_conveyor_motor_reachable():
    """ConveyorMotor should be reachable: hold StopBtn/EstopOK healthy and Auto,
    pulse StartBtn to latch Running, which gates the motor.  Preserve-tracing
    surfaces the NC-reset interlocks (``StopBtn``/``EstopOK`` held healthy) as
    prerequisites of the latch persisting, so the establish + preserve actions
    widen into one pulse instead of wandering the sort state machine."""
    cv = _conveyor()
    path = pilot_how(_cv_plc(cv), cv.ConveyorMotor, max_scans=300)
    assert path.reachable
    assert _replays_to(lambda: _cv_plc(cv), path, "ConveyorMotor", True)


def test_running_route_ambiguous_resolves():
    """Running's latch is gated ``StartBtn ∧ Or(Auto, Manual)``.  The OR is over
    directly-steerable inputs, so PILOT collapses it (no ``choice=`` needed)
    rather than reporting ambiguous, and preserve-tracing holds the NC resets
    (``StopBtn``/``EstopOK``) healthy so the latch sticks."""
    cv = _conveyor()
    path = pilot_how(_cv_plc(cv), cv.Running)
    assert path.reachable


def test_conveyor_status_light_reachable():
    """StatusLight shares the motor's enable (``EstopOK`` branch on ``Running``).
    Same latch + preserve path as the motor — a second terminal off the same
    establish chain, exercised independently."""
    cv = _conveyor()
    path = pilot_how(_cv_plc(cv), cv.StatusLight, max_scans=300)
    assert path.reachable
    assert _replays_to(lambda: _cv_plc(cv), path, "StatusLight", True)


def test_conveyor_is_large_reachable():
    """IsLarge latches when ``State == DETECTING ∧ SizeReading > SizeThreshold``.
    PILOT must enter DETECTING (pulse EntrySensor from IDLE) *and* steer the
    analog size comparison — a two-step establish across a state guard and a
    relational threshold."""
    cv = _conveyor()
    path = pilot_how(_cv_plc(cv), cv.IsLarge, max_scans=500)
    assert path.reachable
    assert _replays_to(lambda: _cv_plc(cv), path, "IsLarge", True)


def test_conveyor_state_sorting_reachable():
    """SORTING is the committed state after the DETECTING dwell.  PILOT pulses
    EntrySensor (IDLE→DETECTING), then let-run coasts DetTimer to Done, which
    advances State→SORTING — a command pulse handed off to a timer-dwell zoom."""
    cv = _conveyor()
    target_value = 2  # SortState.SORTING
    path = pilot_how(_cv_plc(cv), cv.State == target_value, max_scans=500)
    assert path.reachable
    assert _replays_to(lambda: _cv_plc(cv), path, "State", target_value)


def test_conveyor_diverter_reachable():
    """DiverterCmd fires on ``EstopOK ∧ (auto-sort ∨ manual-jog)``.  The manual
    branch (``Manual ∧ DiverterBtn ∧ EstopOK``) is fully steerable, so PILOT
    collapses the OR onto it (no ``choice=`` needed) rather than reporting the
    two Bool output routes as ambiguous — the internal auto-sort branch stays
    available via ``choice=``/``via=`` but is not the default."""
    cv = _conveyor()
    path = pilot_how(_cv_plc(cv), cv.DiverterCmd, max_scans=500)
    assert path.reachable
    assert _replays_to(lambda: _cv_plc(cv), path, "DiverterCmd", True)


# NOTE: a transient entry-state like ``State == DETECTING`` is deliberately *not*
# a how() target.  It auto-advances out from under any hold (rise(EntrySensor)
# enters it, DetTimer.Done leaves it ~50 scans later), so "pilot me there" is
# ill-posed — there is no input that makes the machine rest there.  PILOT reaches
# the settled SORTING state straight through it; that is the right granularity.


# ===========================================================================
# fill_station — start latch, valve, watchdog alarm over a linked flow sensor
# ===========================================================================


def _fill():
    from examples import fill_station as fs

    return fs


def _fs_plc(fs):
    return PLC(fs.logic, dt=0.010)


def test_fill_enable_reachable():
    """FillEnable latches on ``StartBtn ∧ ~LevelSensor ∧ ~FlowAlarm``.  PILOT pulses
    StartBtn and preserve-tracing holds the two NC-style interlocks healthy
    (LevelSensor/FlowAlarm low) so the latch persists."""
    fs = _fill()
    path = pilot_how(_fs_plc(fs), fs.FillEnable, max_scans=500)
    assert path.reachable
    assert _replays_to(lambda: _fs_plc(fs), path, "FillEnable", True)


def test_fill_valve_reachable():
    """FillValve is the unconditional ``out(FillValve)`` on ``FillEnable`` — one
    transparent hop past the start latch."""
    fs = _fill()
    path = pilot_how(_fs_plc(fs), fs.FillValve, max_scans=500)
    assert path.reachable
    assert _replays_to(lambda: _fs_plc(fs), path, "FillValve", True)


def test_fill_flow_alarm_reachable():
    """FlowAlarm latches off ``FaultTimer.Done`` (valve open, no flow, 3 s).  FlowSensor
    is ``link``-synthesised from FillValve, so reaching the alarm means deliberately
    faulting that feedback — the captain opts in with ``unlink=["FlowSensor"]`` (fault
    injection: model a dead sensor).  PILOT then frees FlowSensor, holds it at its
    resting False while the valve is open, and let-run coasts FaultTimer to Done."""
    fs = _fill()
    path = pilot_how(_fs_plc(fs), fs.FlowAlarm, max_scans=2000, unlink=["FlowSensor"])
    assert path.reachable
    assert _replays_to(lambda: _fs_plc(fs), path, "FlowAlarm", True)


def test_fill_flow_alarm_blocked_without_unlink():
    """Without ``unlink=``, PILOT honestly cannot reach FlowAlarm — the intact harness
    holds FlowSensor lockstep with FillValve, so "valve open, no flow" never sustains.
    Rather than wander the budget and report a generic miss, PILOT names the offending
    link and points at the ``unlink=`` override (the honest diagnostic, not a planning
    gap)."""
    fs = _fill()
    path = pilot_how(_fs_plc(fs), fs.FlowAlarm, max_scans=2000)
    assert not path.reachable
    reason = path.reason or ""
    assert "physical link" in reason
    assert "FlowSensor<-FillValve" in reason
    assert "unlink=['FlowSensor']" in reason


# ===========================================================================
# traffic_light — pure timer-driven Char state machine (let-run dwell)
# ===========================================================================


def _green():
    """A committed green snapshot — the example's natural start state.  Char
    ``State`` only commits after a scan, so patch + step to seed it before the
    fork."""
    from examples import traffic_light as tl

    plc = PLC(tl.logic, dt=0.010)
    plc.patch({tl.State: "g"})
    plc.step()
    return tl, plc


def test_traffic_light_yellow_reachable():
    """green→yellow is a pure timer dwell: GreenTimer completes on its own under
    the held state and ``copy("y", State)`` fires.  No steerable input — let-run
    zoom coasts the accumulator to Done.  Exercises a Char-valued ``==`` target."""
    tl, plc = _green()
    path = pilot_how(plc, tl.State == "y", max_scans=2000)
    assert path.reachable
    assert _replays_to(lambda: _green()[1], path, "State", "y")


def test_traffic_light_red_reachable():
    """green→yellow→red is two chained dwells (GreenTimer then YellowTimer).
    PILOT must coast across two self-advancing accumulators in sequence to land
    on red."""
    tl, plc = _green()
    path = pilot_how(plc, tl.State == "r", max_scans=2000)
    assert path.reachable
    assert _replays_to(lambda: _green()[1], path, "State", "r")


# ===========================================================================
# learn/counters — count-to-preset accumulator (tutorial Lesson 6)
# ===========================================================================


def test_counter_done_reachable():
    """Done latches at ``Acc == preset``.  PILOT recognizes the counter Done bit
    as a self-advancing accumulator frontier (a coast leaf on ``Acc`` plus its
    advance driver) and, because the driver is ``rise(BinASensor)``, oscillates
    that input — a toggling ``PilotRung`` — so the let-run coast walks the
    accumulator to preset.  The recorded step carries the oscillator as a
    ``reactive_holds`` entry, which the replay re-installs."""
    from examples.learn import counters as ct

    path = pilot_how(PLC(ct.logic, dt=0.010), ct.BinACounter.Done, max_scans=500)
    assert path.reachable
    assert _replays_to(lambda: PLC(ct.logic, dt=0.010), path, "BinACounter_Done", True)
