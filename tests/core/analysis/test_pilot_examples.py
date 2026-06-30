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

import pytest

from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replays_to(plc_factory, path, tag: str, expected) -> bool:
    """Replay ``path`` on a fresh PLC and report whether ``tag`` lands on
    ``expected`` — the concrete oracle behind every abstract ``how()`` trace."""
    plc = plc_factory()
    for step in path.steps:
        plc.patch(step.action)
        for _ in range(step.scans):
            plc.step()
    return plc.state.tags[tag] == expected


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


@pytest.mark.xfail(
    reason="pilot: FlowAlarm needs the valve open with no flow for 3 s, but FlowSensor "
    "is harness-linked to FillValve (it follows the valve), so PILOT removes it from "
    "the steerable set and honestly cannot reach the alarm.  The fault IS reachable "
    "by defeating the link (forcing a dead sensor) — an OPEN QUESTION of whether "
    "how() should be allowed to break a physical link for fault injection, not a "
    "straightforward planning gap like the count-to-preset frontier.",
    strict=False,
)
def test_fill_flow_alarm_reachable():
    """FlowAlarm latches off ``FaultTimer.Done`` (valve open, no flow, 3 s).  Because
    FlowSensor is ``link``-synthesised from FillValve, reaching the alarm means
    deliberately faulting that feedback.  Kept as a documented design question — if
    fault-injection how() is out of scope, this becomes an accepted limitation."""
    fs = _fill()
    path = pilot_how(_fs_plc(fs), fs.FlowAlarm, max_scans=2000)
    assert path.reachable
    assert _replays_to(lambda: _fs_plc(fs), path, "FlowAlarm", True)


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


@pytest.mark.xfail(
    reason="pilot: BinACounter.Done requires the accumulator to reach preset (10) via "
    "ten edge-triggered rise(BinASensor) pulses.  PILOT does not yet emit a "
    "repeated-pulse plan to drive a counter to its target — the count-to-preset "
    "frontier.",
    strict=False,
)
def test_counter_done_reachable():
    """Done latches at ``Acc == preset``.  Reaching it means PILOT proposes a
    train of rising edges on BinASensor — repeated-pulse accumulation it cannot
    yet plan."""
    from examples.learn import counters as ct

    path = pilot_how(PLC(ct.logic, dt=0.010), ct.BinACounter.Done, max_scans=500)
    assert path.reachable
    assert _replays_to(lambda: PLC(ct.logic, dt=0.010), path, "BinACounter_Done", True)
