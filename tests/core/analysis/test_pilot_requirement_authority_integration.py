"""Public authority boundaries for failed-effect recovery."""

from __future__ import annotations

from pyrung import PLC, Int, Program, Timer, copy, on_delay, rung, system
from pyrung.core.analysis.pilot import pilot_events
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


def test_unconfigured_external_zero_preset_remains_adjustable() -> None:
    initial = 0
    target = 1
    diverted = 9
    state = Int("AuthorityExternalZeroState", default=initial)
    preset = Int("AuthorityExternalZeroPresetMs", external=True)
    watchdog = Timer.clone("AuthorityExternalZeroWatchdog")

    with Program() as logic:
        with rung(system.sys.first_scan):
            copy(target, state)
        with rung(state == target):
            on_delay(watchdog, preset)
        with rung(watchdog.Done):
            copy(diverted, state, oneshot=True)

    events = tuple(pilot_events(PLC(logic, dt=0.010), state == target, max_scans=20))
    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None) == preset.name
    )

    assert len(requirements) == 1
    assert requirements[0].operand_authority.value == "adjustable"
    assert any(
        event.kind == "theory_correction_composed"
        and event.data["configuration"] == ((preset.name, 11),)
        for event in events
    )
    assert not any(
        event.kind == "candidate_try" and (preset.name, 11) in event.data["applied"]
        for event in events
    )
    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
