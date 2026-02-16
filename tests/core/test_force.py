"""Tests for PLCRunner force() debug overrides."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, out, rise, system


class _SetTagRung:
    def __init__(self, name: str, value: bool | int | float | str) -> None:
        self._name = name
        self._value = value

    def evaluate(self, ctx) -> None:  # noqa: ANN001
        ctx.set_tag(self._name, self._value)


class _MidCycleWriteProbeRung:
    """Checks that logic sees writes it made in the same scan."""

    def evaluate(self, ctx) -> None:  # noqa: ANN001
        ctx.set_tag("Signal", False)
        ctx.set_tag("SawOwnWrite", ctx.get_tag("Signal"))


class TestPLCRunnerForce:
    def test_add_force_persists_across_scans(self):
        runner = PLCRunner(logic=[])
        runner.add_force("Button", True)

        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags["Button"] is True

        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags["Button"] is True

    def test_add_force_accepts_tag_object(self):
        button = Bool("Button")
        runner = PLCRunner(logic=[])

        runner.add_force(button, True)
        runner.step()

        assert runner.current_state.tags["Button"] is True

    def test_add_force_rejects_read_only_system_point(self):
        runner = PLCRunner(logic=[])

        with pytest.raises(ValueError, match="read-only system point"):
            runner.add_force(system.sys.always_on, False)

    def test_remove_force(self):
        runner = PLCRunner(logic=[])
        runner.add_force("Light", True)

        runner.remove_force("Light")
        runner.patch({"Light": False})
        runner.step()

        assert runner.current_state.tags["Light"] is False

    def test_remove_force_accepts_tag_object(self):
        light = Bool("Light")
        runner = PLCRunner(logic=[])
        runner.add_force(light, True)

        runner.remove_force(light)
        runner.patch({"Light": False})
        runner.step()

        assert runner.current_state.tags["Light"] is False

    def test_remove_force_nonexistent_raises(self):
        runner = PLCRunner(logic=[])

        with pytest.raises(KeyError):
            runner.remove_force("Missing")

    def test_clear_forces(self):
        runner = PLCRunner(logic=[])
        runner.add_force("A", True)
        runner.add_force("B", False)

        runner.clear_forces()
        runner.patch({"A": False, "B": True})
        runner.step()

        assert dict(runner.forces) == {}
        assert runner.current_state.tags["A"] is False
        assert runner.current_state.tags["B"] is True

    def test_force_overwrites_patch(self):
        runner = PLCRunner(logic=[])
        runner.add_force("A", True)

        runner.patch({"A": False})
        runner.step()

        assert runner.current_state.tags["A"] is True

    def test_force_reasserts_after_logic(self):
        runner = PLCRunner(logic=[_SetTagRung("A", False)])
        runner.add_force("A", True)

        runner.step()

        assert runner.current_state.tags["A"] is True

    def test_force_does_not_lock_midcycle(self):
        runner = PLCRunner(logic=[_MidCycleWriteProbeRung()])
        runner.add_force("Signal", True)

        runner.step()

        assert runner.current_state.tags["Signal"] is True
        assert runner.current_state.tags["SawOwnWrite"] is False

    def test_force_context_manager_temporary(self):
        runner = PLCRunner(logic=[])

        with runner.force({"A": True}):
            runner.step()
            assert runner.current_state.tags["A"] is True
            assert dict(runner.forces) == {"A": True}

        runner.patch({"A": False})
        runner.step()

        assert dict(runner.forces) == {}
        assert runner.current_state.tags["A"] is False

    def test_force_context_manager_accepts_tag_keys(self):
        a = Bool("A")
        runner = PLCRunner(logic=[])

        with runner.force({a: True}):
            runner.step()
            assert runner.current_state.tags["A"] is True
            assert dict(runner.forces) == {"A": True}

        runner.patch({"A": False})
        runner.step()

        assert dict(runner.forces) == {}
        assert runner.current_state.tags["A"] is False

    def test_force_context_manager_nested(self):
        runner = PLCRunner(logic=[])
        runner.add_force("Outer", True)

        with runner.force({"Outer": False, "Mid": True}):
            assert dict(runner.forces) == {"Outer": False, "Mid": True}
            with runner.force({"Mid": False, "Inner": True}):
                assert dict(runner.forces) == {"Outer": False, "Mid": False, "Inner": True}
            assert dict(runner.forces) == {"Outer": False, "Mid": True}

        assert dict(runner.forces) == {"Outer": True}

    def test_force_context_manager_exception_safe(self):
        runner = PLCRunner(logic=[])
        runner.add_force("Baseline", True)

        with pytest.raises(RuntimeError, match="boom"):
            with runner.force({"Temp": True}):
                raise RuntimeError("boom")

        assert dict(runner.forces) == {"Baseline": True}

    def test_forces_property_readonly(self):
        runner = PLCRunner(logic=[])
        runner.add_force("A", True)

        active_forces = runner.forces
        assert dict(active_forces) == {"A": True}

        with pytest.raises(TypeError):
            active_forces["A"] = False  # type: ignore[index]

    def test_force_and_edge_detection(self):
        button = Bool("Button")
        pulse = Bool("Pulse")
        with Program() as logic:
            with Rung(rise(button)):
                out(pulse)

        runner = PLCRunner(logic=logic)
        runner.add_force(button, True)

        runner.step()
        assert runner.current_state.tags["Pulse"] is True

        runner.step()
        assert runner.current_state.tags["Pulse"] is False

    def test_force_multiple_tags(self):
        runner = PLCRunner(logic=[])
        runner.add_force("A", True)
        runner.add_force("Count", 42)

        runner.step()

        assert runner.current_state.tags["A"] is True
        assert runner.current_state.tags["Count"] == 42

    def test_peek_live_reflects_force(self):
        count = Int("Count")
        runner = PLCRunner(logic=[])
        runner.add_force(count, 7)

        with runner.active():
            assert count.value == 7
