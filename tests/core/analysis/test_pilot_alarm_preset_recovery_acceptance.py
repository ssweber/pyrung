"""Public Phase-5 acceptance for same-scan watchdog recovery."""

from __future__ import annotations

from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures.pilot_alarm_presets import (
    aborted_on_first_scan,
    alarmed_at_start,
)

_RETIRED_REPLAY_EVENTS = {
    "retained_replay_try",
    "retained_replay_rejected",
    "retained_replay_accepted",
}


def _events(fixture: object, target: int):
    return tuple(
        pilot_events(
            PLC(fixture.logic, dt=0.010),
            fixture.ProcessStep == target,
            max_scans=100,
        )
    )


def _preset_requirement(events, preset_name: str):
    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None) == preset_name
    )
    assert len(requirements) == 1
    return requirements[0]


def _assert_no_historical_replay(events) -> None:
    assert not (_RETIRED_REPLAY_EVENTS & {event.kind for event in events})


def _recorded_correction_values(plan, tag: str) -> frozenset[object]:
    from_holds = tuple(
        rung.value for entry in plan.hold_log for rung in entry.pilot_rungs if rung.dest == tag
    )
    from_steps = tuple(
        rung.value for step in plan.journal for rung in step.rungs if rung.dest == tag
    )
    return frozenset((*from_holds, *from_steps))


def test_startup_alarm_becomes_a_compass_setup_bearing_in_one_scan() -> None:
    fixture = aborted_on_first_scan
    events = _events(fixture, fixture.AT_TARGET)

    requirement = _preset_requirement(events, fixture.WatchdogPresetMs.name)
    assert (
        requirement.condition.tag,
        requirement.condition.op,
        requirement.condition.bound,
    ) == (fixture.WatchdogPresetMs.name, ">", 10)
    assert requirement.operand_authority.value == "adjustable"
    assert requirement.source_scan == 0
    assert requirement.deadline.scan_id == 1

    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    temporal_tries = tuple(
        event
        for event in events
        if event.kind == "candidate_try"
        and event.data["applied"] == ((fixture.WatchdogPresetMs.name, 11),)
    )
    assert len(temporal_tries) == 1
    assert temporal_tries[0].scan == 0
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    _assert_no_historical_replay(events)

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.AT_TARGET,
        max_scans=100,
    )
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.total_scans == 1
    assert plan.state.scan_id == 1
    assert plan.state.tags[fixture.ProcessStep.name] == fixture.AT_TARGET
    assert plan.state.tags[fixture.WatchdogPresetMs.name] == 11
    assert plan.ordered_steps == [(1, {fixture.WatchdogPresetMs.name: 11})]
    assert tuple(
        pair
        for entry in plan.hold_log
        for pair in entry.tags
        if pair[0] == fixture.WatchdogPresetMs.name
    ) == ((fixture.WatchdogPresetMs.name, 11),)

    replay = plan.replay()
    assert replay.state.scan_id == 1
    assert replay.state.tags[fixture.ProcessStep.name] == fixture.AT_TARGET
    assert replay.state.tags[fixture.WatchdogPresetMs.name] == 11


def test_alarm_retry_uses_one_fresh_compass_transaction_per_discovery() -> None:
    fixture = alarmed_at_start
    events = _events(fixture, fixture.COMPLETE)

    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None)
        == fixture.WatchdogPresetMs.name
    )
    assert tuple(
        (
            requirement.condition.op,
            requirement.condition.bound,
            requirement.operand_authority.value,
            requirement.source_scan,
            requirement.deadline.scan_id,
        )
        for requirement in requirements
    ) == (
        (">", 10, "adjustable", 1, 2),
        (">", 20, "adjustable", 1, 3),
        (">", 30, "adjustable", 3, 4),
    )

    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    corrections = tuple(
        event.data["pilot_rung"].value
        for event in events
        if event.kind == "theory_correction_composed"
        and event.data["pilot_rung"].dest == fixture.WatchdogPresetMs.name
    )
    assert corrections == (11, 21, 31)
    assert not any(
        any(tag == fixture.WatchdogPresetMs.name for tag, _value in event.data["applied"])
        for event in events
        if event.kind == "candidate_try"
    )
    rearm_tries = tuple(
        event
        for event in events
        if event.kind == "candidate_try" and event.data["applied"] == ((fixture.Reset.name, False),)
    )
    assert rearm_tries == ()
    assert any(event.kind == "candidate_rejected" for event in events)
    _assert_no_historical_replay(events)

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.COMPLETE,
        max_scans=100,
    )
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.total_scans == 4
    assert plan.state.scan_id == 4
    assert plan.state.tags[fixture.ProcessStep.name] == fixture.COMPLETE
    assert plan.state.tags[fixture.WatchdogPresetMs.name] == 31
    assert plan.state.tags[fixture.Reset.name] is True
    assert plan.state.tags[fixture.AtTarget.name] is True
    assert plan.changes == {
        fixture.Reset.name: True,
        fixture.AtTarget.name: True,
    }
    assert plan.ordered_steps == [
        (2, {fixture.Reset.name: True}),
        (4, {fixture.AtTarget.name: True}),
    ]

    command_steps = tuple(step for step in plan.journal if step.kind == "pulse")
    assert len(command_steps) == 2
    assert command_steps[0].scan == 1
    assert command_steps[0].scans == 2
    assert command_steps[0].inputs == ((fixture.Reset.name, True),)
    assert command_steps[1].scan == 3
    assert command_steps[1].scans == 1
    assert command_steps[1].inputs == ((fixture.AtTarget.name, True),)
    assert _recorded_correction_values(plan, fixture.WatchdogPresetMs.name) == frozenset(
        {11, 21, 31}
    )

    replay = plan.replay()
    assert replay.state.scan_id == 4
    assert replay.state.tags[fixture.ProcessStep.name] == fixture.COMPLETE
    assert replay.state.tags[fixture.WatchdogPresetMs.name] == 31
    assert replay.state.tags[fixture.Reset.name] is True
    assert replay.state.tags[fixture.AtTarget.name] is True


def test_forced_zero_bootstrap_preset_remains_authoritative_across_checkpoint_fork() -> None:
    fixture = aborted_on_first_scan
    plc = PLC(fixture.logic, dt=0.010)
    plc.force(fixture.WatchdogPresetMs, 0)
    events = tuple(
        islice(
            pilot_events(
                plc,
                fixture.ProcessStep == fixture.AT_TARGET,
                max_scans=20,
            ),
            40,
        )
    )

    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None)
        == fixture.WatchdogPresetMs.name
    )
    assert requirements
    assert all(item.operand_authority.value == "configured" for item in requirements)
    assert not any(
        tag == fixture.WatchdogPresetMs.name
        for event in events
        if event.kind == "requirement_locally_repaired"
        for tag, _value in event.data["assignments"]
    )


def test_pending_zero_preset_remains_authoritative_at_expectation_checkpoint() -> None:
    fixture = alarmed_at_start
    plc = PLC(fixture.logic, dt=0.010)
    plc.patch({fixture.WatchdogPresetMs.name: 0})
    events = tuple(
        islice(
            pilot_events(
                plc,
                fixture.ProcessStep == fixture.COMPLETE,
                max_scans=20,
            ),
            50,
        )
    )

    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None)
        == fixture.WatchdogPresetMs.name
    )
    assert requirements
    assert all(item.operand_authority.value == "configured" for item in requirements)
    assert not any(
        tag == fixture.WatchdogPresetMs.name
        for event in events
        if event.kind == "requirement_locally_repaired"
        for tag, _value in event.data["assignments"]
    )
