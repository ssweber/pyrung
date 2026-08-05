"""Public authority boundaries for failed-effect recovery."""

from __future__ import annotations

from pyrung import PLC
from tests.fixtures.pilot_alarm_presets import conditional_negative


def test_configured_preset_is_not_mutated_to_prevent_a_committed_consequence() -> None:
    """A current-state recovery must honor the configured timer preset."""

    fixture = conditional_negative
    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.Consequence == bool(0),
        max_scans=20,
    )

    assert plan.reachable
    assert plan.state.tags[fixture.PresetMs.name] == fixture.DEFAULT_PRESET_MS
    assert fixture.PresetMs.name not in plan.changes
    assert all(tag != fixture.PresetMs.name for step in plan.journal for tag, _value in step.inputs)
    assert all(
        getattr(rung, "dest", None) != fixture.PresetMs.name
        for step in plan.journal
        for rung in step.rungs
    )

    replay = plan.replay()
    assert replay.state.tags[fixture.Consequence.name] is False
    assert replay.state.tags[fixture.PresetMs.name] == fixture.DEFAULT_PRESET_MS
