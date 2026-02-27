"""Tests for TimeMode - fixed step vs realtime execution.

TDD: Write tests first, then implement to pass.
"""

import time

import pytest


class TestTimeModeEnum:
    """Test TimeMode enum definition."""

    def test_fixed_step_mode_exists(self):
        """FIXED_STEP mode is defined."""
        from pyrung.core import TimeMode

        assert TimeMode.FIXED_STEP.value == "fixed_step"

    def test_realtime_mode_exists(self):
        """REALTIME mode is defined."""
        from pyrung.core import TimeMode

        assert TimeMode.REALTIME.value == "realtime"


class TestFixedStepMode:
    """Test FIXED_STEP time mode - deterministic timing."""

    def test_default_is_fixed_step(self):
        """Runner defaults to FIXED_STEP mode."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])

        assert runner.time_mode == TimeMode.FIXED_STEP

    def test_set_fixed_step_with_dt(self):
        """Can set FIXED_STEP mode with specific dt."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.05)  # 50ms

        runner.step()

        assert runner.simulation_time == pytest.approx(0.05)

    def test_fixed_step_accumulates(self):
        """Fixed step time accumulates predictably."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

        runner.run(cycles=10)

        assert runner.simulation_time == pytest.approx(1.0)

    def test_fixed_step_ignores_wall_clock(self):
        """Fixed step doesn't care about actual elapsed time."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.001)  # 1ms

        # Even if wall clock is slower, simulation time is deterministic
        runner.run(cycles=1000)

        assert runner.simulation_time == pytest.approx(1.0)


class TestRealtimeMode:
    """Test REALTIME time mode - wall clock tracking."""

    def test_set_realtime_mode(self):
        """Can set REALTIME mode."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.REALTIME)

        assert runner.time_mode == TimeMode.REALTIME

    def test_realtime_tracks_wall_clock(self):
        """REALTIME mode uses actual elapsed time."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.REALTIME)

        start = time.perf_counter()
        time.sleep(0.05)  # Sleep 50ms
        runner.step()
        elapsed = time.perf_counter() - start

        # Simulation time should be approximately the wall clock time
        # Allow some tolerance for test execution overhead
        assert runner.simulation_time >= 0.04
        assert runner.simulation_time <= elapsed + 0.01


class TestRunFor:
    """Test run_for() - run until simulation time advances."""

    def test_run_for_seconds_fixed_step(self):
        """run_for() runs until simulation clock advances at least N seconds."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

        runner.run_for(seconds=1.0)

        # Runs until timestamp >= target, so with dt=0.1 starting at 0,
        # we need 11 scans to reach 1.1s (first scan where timestamp >= 1.0)
        assert runner.simulation_time >= 1.0
        assert runner.current_state.scan_id == 11

    def test_run_for_partial_cycle(self):
        """run_for() stops at cycle boundary, may overshoot slightly."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.3)

        runner.run_for(seconds=1.0)

        # With dt=0.3, we need 4 cycles to exceed 1.0s (0.3*4=1.2)
        assert runner.simulation_time >= 1.0
        assert runner.current_state.scan_id == 4


class TestRunUntil:
    """Test run_until_fn() callable predicates and run_until() behavior."""

    def test_run_until_predicate_true(self):
        """run_until_fn() stops when predicate returns True."""
        from pyrung.core import PLCRunner, TimeMode

        runner = PLCRunner(logic=[])
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

        # Run until scan_id reaches 5
        runner.run_until_fn(lambda s: s.scan_id >= 5)

        assert runner.current_state.scan_id == 5

    def test_run_until_with_max_cycles(self):
        """run_until_fn() respects max_cycles limit."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        # Predicate never true, but max_cycles prevents infinite loop
        runner.run_until_fn(lambda s: False, max_cycles=10)

        assert runner.current_state.scan_id == 10

    def test_run_until_returns_matched_state(self):
        """run_until_fn() returns state that matched predicate."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        result = runner.run_until_fn(lambda s: s.scan_id == 3)

        assert result.scan_id == 3
