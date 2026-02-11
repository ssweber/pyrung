"""Tests for PLCRunner - the generator-driven execution engine.

TDD: Write tests first, then implement to pass.
"""

from pyrung.core import SystemState


class TestPLCRunnerCreation:
    """Test PLCRunner construction."""

    def test_create_with_empty_logic(self):
        """Can create runner with no logic (empty program)."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[], initial_state=SystemState())

        assert runner.current_state.scan_id == 0

    def test_create_with_default_initial_state(self):
        """Can create runner without explicit initial state."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        assert runner.current_state.scan_id == 0
        assert runner.current_state.timestamp == 0.0


class TestPLCRunnerStep:
    """Test step() - the core execution primitive."""

    def test_step_advances_scan_id(self):
        """step() increments scan_id by 1."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.step()

        assert runner.current_state.scan_id == 1

    def test_step_returns_new_state(self):
        """step() returns the new SystemState."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        new_state = runner.step()

        assert new_state.scan_id == 1
        assert new_state is runner.current_state

    def test_multiple_steps(self):
        """Multiple step() calls accumulate."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.step()
        runner.step()
        runner.step()

        assert runner.current_state.scan_id == 3

    def test_step_preserves_tags(self):
        """step() preserves existing tag values."""
        from pyrung.core import PLCRunner

        initial = SystemState().with_tags({"Motor": True, "Speed": 100})
        runner = PLCRunner(logic=[], initial_state=initial)

        runner.step()

        assert runner.current_state.tags["Motor"] is True
        assert runner.current_state.tags["Speed"] == 100


class TestPLCRunnerRun:
    """Test run() - batch execution."""

    def test_run_cycles(self):
        """run(cycles=N) executes N scans."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.run(cycles=10)

        assert runner.current_state.scan_id == 10

    def test_run_returns_final_state(self):
        """run() returns the final state after all cycles."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        final = runner.run(cycles=5)

        assert final.scan_id == 5
        assert final is runner.current_state


class TestPLCRunnerPatch:
    """Test patch() - one-shot input injection."""

    def test_patch_applies_to_next_scan(self):
        """patch() applies tag values at start of next scan."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.patch(tags={"Button": True})
        runner.step()

        assert runner.current_state.tags["Button"] is True

    def test_patch_is_one_shot(self):
        """patch() values are released after one scan (not sticky)."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.patch(tags={"Button": True})
        runner.step()  # Button=True applied

        # Without logic to maintain it, we need to verify patch was consumed
        # The patch queue should be empty after step()
        assert runner._pending_patches == {}

    def test_multiple_patches_merge(self):
        """Multiple patch() calls before step() merge together."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.patch(tags={"A": 1})
        runner.patch(tags={"B": 2})
        runner.step()

        assert runner.current_state.tags["A"] == 1
        assert runner.current_state.tags["B"] == 2

    def test_patch_overwrites_previous(self):
        """Later patch() overwrites earlier patch() for same tag."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.patch(tags={"X": 1})
        runner.patch(tags={"X": 99})
        runner.step()

        assert runner.current_state.tags["X"] == 99

    def test_patch_accepts_string_values(self):
        """patch() supports CHAR-like string values."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        runner.patch(tags={"TXT1": "B"})
        runner.step()

        assert runner.current_state.tags["TXT1"] == "B"


class TestPLCRunnerSimulationTime:
    """Test simulation_time property."""

    def test_simulation_time_starts_at_zero(self):
        """simulation_time is 0.0 initially."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])

        assert runner.simulation_time == 0.0

    def test_simulation_time_equals_timestamp(self):
        """simulation_time returns current_state.timestamp."""
        from pyrung.core import PLCRunner

        runner = PLCRunner(logic=[])
        runner.step()

        assert runner.simulation_time == runner.current_state.timestamp
