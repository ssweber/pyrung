"""Public Phase-5 acceptance for same-scan watchdog recovery."""

from __future__ import annotations

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


def test_startup_alarm_repairs_checkpoint_zero_in_one_scan() -> None:
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

    repairs = tuple(event for event in events if event.kind == "requirement_locally_repaired")
    assert len(repairs) == 1
    assert repairs[0].scan == 1
    assert repairs[0].data["assignments"] == ((fixture.WatchdogPresetMs.name, 11),)
    assert repairs[0].data["detail"] == "bootstrap local transaction repaired"
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
    assert plan.ordered_steps == []
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


def test_disposable_alarm_retry_is_one_corrected_local_transaction() -> None:
    fixture = alarmed_at_start
    events = _events(fixture, fixture.COMPLETE)

    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    requirement = _preset_requirement(events, fixture.WatchdogPresetMs.name)
    assert (
        requirement.condition.tag,
        requirement.condition.op,
        requirement.condition.bound,
    ) == (fixture.WatchdogPresetMs.name, ">", 10)
    assert requirement.operand_authority.value == "adjustable"
    assert requirement.source_scan == 1
    assert requirement.deadline.scan_id == 2

    repairs = tuple(
        event
        for event in events
        if event.kind == "requirement_locally_repaired"
        and (fixture.WatchdogPresetMs.name, 11) in event.data["assignments"]
    )
    assert len(repairs) == 1
    assert repairs[0].scan == 2
    assert repairs[0].data["detail"] == "local transaction repaired"
    assert any(event.kind == "candidate_rejected" for event in events)
    assert all(
        (fixture.Reset.name, False) not in tuple(event.data.get("applied", ())) for event in events
    )
    _assert_no_historical_replay(events)

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.COMPLETE,
        max_scans=100,
    )
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.total_scans == 2
    assert plan.state.scan_id == 2
    assert plan.state.tags[fixture.ProcessStep.name] == fixture.COMPLETE
    assert plan.state.tags[fixture.WatchdogPresetMs.name] == 11
    assert plan.state.tags[fixture.Reset.name] is True
    assert plan.state.tags[fixture.AtTarget.name] is True
    assert plan.changes == {
        fixture.Reset.name: True,
        fixture.AtTarget.name: True,
    }
    assert plan.ordered_steps == [
        (
            2,
            {
                fixture.Reset.name: True,
                fixture.AtTarget.name: True,
            },
        )
    ]

    command_steps = tuple(step for step in plan.journal if step.kind == "pulse")
    assert len(command_steps) == 1
    step = command_steps[0]
    assert step.scan == 1
    assert step.scans == 1
    assert step.inputs == (
        (fixture.Reset.name, True),
        (fixture.AtTarget.name, True),
    )
    assert all(pair != (fixture.Reset.name, False) for pair in step.inputs)
    assert _recorded_correction_values(plan, fixture.WatchdogPresetMs.name) == frozenset({11})

    replay = plan.replay()
    assert replay.state.scan_id == 2
    assert replay.state.tags[fixture.ProcessStep.name] == fixture.COMPLETE
    assert replay.state.tags[fixture.WatchdogPresetMs.name] == 11
    assert replay.state.tags[fixture.Reset.name] is True
    assert replay.state.tags[fixture.AtTarget.name] is True
