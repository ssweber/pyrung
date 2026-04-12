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
        from pyrung.core import PLC, TimeMode

        runner = PLC(logic=[])

        assert runner.time_mode == TimeMode.FIXED_STEP

    def test_set_fixed_step_with_dt(self):
        """Can set FIXED_STEP mode with specific dt."""
        from pyrung.core import PLC

        runner = PLC(logic=[], dt=0.05)

        runner.step()

        assert runner.simulation_time == pytest.approx(0.05)

    def test_fixed_step_accumulates(self):
        """Fixed step time accumulates predictably."""
        from pyrung.core import PLC

        runner = PLC(logic=[], dt=0.1)

        runner.run(cycles=10)

        assert runner.simulation_time == pytest.approx(1.0)

    def test_fixed_step_ignores_wall_clock(self):
        """Fixed step doesn't care about actual elapsed time."""
        from pyrung.core import PLC

        runner = PLC(logic=[], dt=0.001)

        # Even if wall clock is slower, simulation time is deterministic
        runner.run(cycles=1000)

        assert runner.simulation_time == pytest.approx(1.0)


class TestRealtimeMode:
    """Test REALTIME time mode - wall clock tracking."""

    def test_set_realtime_mode(self):
        """Can set REALTIME mode."""
        from pyrung.core import PLC, TimeMode

        runner = PLC(logic=[], realtime=True)

        assert runner.time_mode == TimeMode.REALTIME

    def test_realtime_tracks_wall_clock(self):
        """REALTIME mode uses actual elapsed time."""
        from pyrung.core import PLC

        runner = PLC(logic=[], realtime=True)

        start = time.perf_counter()
        time.sleep(0.05)  # Sleep 50ms
        runner.step()
        elapsed = time.perf_counter() - start

        # Simulation time should be approximately the wall clock time
        # Allow some tolerance for test execution overhead
        assert runner.simulation_time >= 0.04
        assert runner.simulation_time <= elapsed + 0.01


class TestTimeModeValidation:
    """Test dt=/realtime= mutual exclusion."""

    def test_dt_and_realtime_raises(self):
        """Cannot specify both dt= and realtime=True."""
        from pyrung.core import PLC

        with pytest.raises(ValueError, match="Cannot specify dt="):
            PLC(logic=[], dt=0.05, realtime=True)

    def test_default_dt_is_10ms(self):
        """Default dt is 0.010 (10 ms)."""
        from pyrung.core import PLC

        runner = PLC(logic=[])
        runner.step()
        assert runner.simulation_time == pytest.approx(0.010)


class TestRunFor:
    """Test run_for() - run until simulation time advances."""

    def test_run_for_seconds_fixed_step(self):
        """run_for() runs until simulation clock advances at least N seconds."""
        from pyrung.core import PLC

        runner = PLC(logic=[], dt=0.1)

        runner.run_for(seconds=1.0)

        # Runs until timestamp >= target, so with dt=0.1 starting at 0,
        # we need 11 scans to reach 1.1s (first scan where timestamp >= 1.0)
        assert runner.simulation_time >= 1.0
        assert runner.current_state.scan_id == 11

    def test_run_for_partial_cycle(self):
        """run_for() stops at cycle boundary, may overshoot slightly."""
        from pyrung.core import PLC

        runner = PLC(logic=[], dt=0.3)

        runner.run_for(seconds=1.0)

        # With dt=0.3, we need 4 cycles to exceed 1.0s (0.3*4=1.2)
        assert runner.simulation_time >= 1.0
        assert runner.current_state.scan_id == 4


class TestNormalizeUnit:
    """Test normalize_unit() accepts various human-friendly unit strings."""

    def test_canonical_forms(self):
        from pyrung.core import normalize_unit

        assert normalize_unit("Tms") == "Tms"
        assert normalize_unit("Ts") == "Ts"
        assert normalize_unit("Tm") == "Tm"
        assert normalize_unit("Th") == "Th"
        assert normalize_unit("Td") == "Td"

    def test_short_forms(self):
        from pyrung.core import normalize_unit

        assert normalize_unit("ms") == "Tms"
        assert normalize_unit("s") == "Ts"
        assert normalize_unit("min") == "Tm"
        assert normalize_unit("h") == "Th"
        assert normalize_unit("d") == "Td"

    def test_long_forms(self):
        from pyrung.core import normalize_unit

        assert normalize_unit("milliseconds") == "Tms"
        assert normalize_unit("seconds") == "Ts"
        assert normalize_unit("minutes") == "Tm"
        assert normalize_unit("hours") == "Th"
        assert normalize_unit("days") == "Td"

    def test_singular_forms(self):
        from pyrung.core import normalize_unit

        assert normalize_unit("millisecond") == "Tms"
        assert normalize_unit("second") == "Ts"
        assert normalize_unit("minute") == "Tm"
        assert normalize_unit("hour") == "Th"
        assert normalize_unit("day") == "Td"

    def test_case_insensitive(self):
        from pyrung.core import normalize_unit

        assert normalize_unit("MS") == "Tms"
        assert normalize_unit("Seconds") == "Ts"
        assert normalize_unit("MIN") == "Tm"

    def test_extra_aliases(self):
        from pyrung.core import normalize_unit

        assert normalize_unit("sec") == "Ts"
        assert normalize_unit("hr") == "Th"
        assert normalize_unit("msec") == "Tms"
        assert normalize_unit("m") == "Tm"

    def test_ambiguous_t_raises(self):
        from pyrung.core import normalize_unit

        with pytest.raises(ValueError, match="ambiguous"):
            normalize_unit("T")

    def test_unknown_unit_raises(self):
        from pyrung.core import normalize_unit

        with pytest.raises(ValueError, match="unknown time unit"):
            normalize_unit("fortnights")


class TestRunUntil:
    """Test run_until_fn() callable predicates and run_until() behavior."""

    def test_run_until_predicate_true(self):
        """run_until_fn() stops when predicate returns True."""
        from pyrung.core import PLC

        runner = PLC(logic=[], dt=0.1)

        # Run until scan_id reaches 5
        runner.run_until(lambda s: s.scan_id >= 5)

        assert runner.current_state.scan_id == 5

    def test_run_until_with_max_cycles(self):
        """run_until_fn() respects max_cycles limit."""
        from pyrung.core import PLC

        runner = PLC(logic=[])

        # Predicate never true, but max_cycles prevents infinite loop
        runner.run_until(lambda s: False, max_cycles=10)

        assert runner.current_state.scan_id == 10

    def test_run_until_returns_matched_state(self):
        """run_until_fn() returns state that matched predicate."""
        from pyrung.core import PLC

        runner = PLC(logic=[])

        result = runner.run_until(lambda s: s.scan_id == 3)

        assert result.scan_id == 3
