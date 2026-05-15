"""Tests for PLC - the generator-driven execution engine.

TDD: Write tests first, then implement to pass.
"""

from pyrung.core import Program, SystemState


def _empty_program() -> Program:
    with Program(strict=False) as logic:
        pass
    return logic


class TestPLCCreation:
    """Test PLC construction."""

    def test_create_with_empty_logic(self):
        """Can create runner with no logic (empty program)."""
        from pyrung.core import PLC

        runner = PLC(logic=[], initial_state=SystemState())

        assert runner.current_state.scan_id == 0

    def test_create_with_default_initial_state(self, runner_factory):
        """Can create runner without explicit initial state."""
        runner = runner_factory(_empty_program())

        assert runner.current_state.scan_id == 0
        assert runner.current_state.timestamp == 0.0


class TestPLCStep:
    """Test step() - the core execution primitive."""

    def test_step_advances_scan_id(self, runner_factory):
        """step() increments scan_id by 1."""
        runner = runner_factory(_empty_program())

        runner.step()

        assert runner.current_state.scan_id == 1

    def test_step_returns_new_state(self, runner_factory):
        """step() returns the new SystemState."""
        runner = runner_factory(_empty_program())

        new_state = runner.step()

        assert new_state.scan_id == 1
        assert new_state is runner.current_state

    def test_multiple_steps(self, runner_factory):
        """Multiple step() calls accumulate."""
        runner = runner_factory(_empty_program())

        runner.step()
        runner.step()
        runner.step()

        assert runner.current_state.scan_id == 3

    def test_step_preserves_tags(self, runner_factory):
        """step() preserves existing tag values."""
        initial = SystemState().with_tags({"Motor": True, "Speed": 100})
        runner = runner_factory(_empty_program(), initial_state=initial)

        runner.step()

        assert runner.current_state.tags["Motor"] is True
        assert runner.current_state.tags["Speed"] == 100


class TestPLCRun:
    """Test run() - batch execution."""

    def test_run_cycles(self, runner_factory):
        """run(cycles=N) executes N scans."""
        runner = runner_factory(_empty_program())

        runner.run(cycles=10)

        assert runner.current_state.scan_id == 10

    def test_run_returns_final_state(self, runner_factory):
        """run() returns the final state after all cycles."""
        runner = runner_factory(_empty_program())

        final = runner.run(cycles=5)

        assert final.scan_id == 5
        assert final is runner.current_state


class TestPLCPatch:
    """Test patch() - one-shot input injection."""

    def test_patch_applies_to_next_scan(self, runner_factory):
        """patch() applies tag values at start of next scan."""
        runner = runner_factory(_empty_program())

        runner.patch(tags={"Button": True})
        runner.step()

        assert runner.current_state.tags["Button"] is True

    def test_patch_is_one_shot(self):
        """patch() values are released after one scan (not sticky)."""
        from pyrung.core import PLC

        runner = PLC(logic=[])

        runner.patch(tags={"Button": True})
        runner.step()  # Button=True applied

        # Without logic to maintain it, we need to verify patch was consumed
        # The patch queue should be empty after step()
        assert runner._pending_patches == {}

    def test_multiple_patches_merge(self, runner_factory):
        """Multiple patch() calls before step() merge together."""
        runner = runner_factory(_empty_program())

        runner.patch(tags={"A": 1})
        runner.patch(tags={"B": 2})
        runner.step()

        assert runner.current_state.tags["A"] == 1
        assert runner.current_state.tags["B"] == 2

    def test_patch_overwrites_previous(self, runner_factory):
        """Later patch() overwrites earlier patch() for same tag."""
        runner = runner_factory(_empty_program())

        runner.patch(tags={"X": 1})
        runner.patch(tags={"X": 99})
        runner.step()

        assert runner.current_state.tags["X"] == 99

    def test_patch_accepts_string_values(self, runner_factory):
        """patch() supports CHAR-like string values."""
        runner = runner_factory(_empty_program())

        runner.patch(tags={"TXT1": "B"})
        runner.step()

        assert runner.current_state.tags["TXT1"] == "B"


class TestPLCSimulationTime:
    """Test simulation_time property."""

    def test_simulation_time_starts_at_zero(self, runner_factory):
        """simulation_time is 0.0 initially."""
        runner = runner_factory(_empty_program())

        assert runner.simulation_time == 0.0

    def test_simulation_time_equals_timestamp(self, runner_factory):
        """simulation_time returns current_state.timestamp."""
        runner = runner_factory(_empty_program())
        runner.step()

        assert runner.simulation_time == runner.current_state.timestamp


class TestPLCEdgeHistory:
    """Regression coverage for _prev:* memory capture behavior."""

    def test_prev_memory_captures_existing_tags(self, runner_factory):
        """Existing tags should be mirrored into _prev:* each scan."""
        runner = runner_factory(
            _empty_program(),
            initial_state=SystemState().with_tags({"Existing": 42}),
        )

        runner.step()

        assert runner.current_state.memory.get("_prev:Existing") == 42

    def test_prev_memory_captures_newly_pending_tags(self, runner_factory):
        """New tags introduced via patch() should get _prev:* entries."""
        runner = runner_factory(_empty_program())

        runner.patch({"LateBound": 7})
        runner.step()

        assert runner.current_state.tags.get("LateBound") == 7
        assert runner.current_state.memory.get("_prev:LateBound") == 7

    def test_prev_memory_preserves_missing_and_default_value_behavior(self, runner_factory):
        """Missing tags stay absent; explicit default-valued tags are captured."""
        runner = runner_factory(_empty_program())

        runner.step()
        assert "_prev:NeverSeen" not in runner.current_state.memory

        runner.patch({"DefaultBool": False})
        runner.step()
        assert runner.current_state.memory.get("_prev:DefaultBool") is False


class TestPlcTags:
    """plc.tags read-only mapping."""

    def test_returns_known_tags(self):
        from pyrung.core import PLC, Bool, Program, Rung, out

        Light = Bool("Light")
        Button = Bool("Button")
        with Program(strict=False) as logic:
            with Rung(Button):
                out(Light)
        plc = PLC(logic, dt=0.010)

        tags = plc.tags
        assert "Light" in tags
        assert "Button" in tags
        assert tags["Light"] is Light

    def test_is_read_only(self):
        from pyrung.core import PLC, Bool, Program, Rung, out

        Light = Bool("Light")
        with Program(strict=False) as logic:
            with Rung():
                out(Light)
        plc = PLC(logic, dt=0.010)

        with __import__("pytest").raises(TypeError):
            plc.tags["Light"] = None  # ty: ignore[invalid-assignment]
