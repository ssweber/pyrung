"""Acceptance contract for repeated scan-zero route refinement."""

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_scan_zero_sequence_route as fixture


def test_terminal_route_is_composed_from_exact_adjacent_scan_receipts() -> None:
    events = tuple(
        pilot_events(
            PLC(fixture.logic, dt=0.010),
            fixture.SequenceStep == 81,
            max_scans=120,
        )
    )

    journal = events[-1].data["plan_journal"]
    applied_tags = {tag for step in journal for tag, _value in step.inputs}

    # These are independent facts learned at different points in the same
    # charted route.  The lifecycle must retain and compose all of them.
    assert fixture.SafetyPermit.name in applied_tags
    assert fixture.BaseSensor.name in applied_tags
    assert fixture.ReadyCommand.name in applied_tags
    assert fixture.SimulationMode.name in applied_tags
    assert fixture.CheckpointSensor.name in applied_tags
    assert fixture.FirstWatchdogPresetMs.name in applied_tags
    assert fixture.SecondWatchdogPresetMs.name in applied_tags
    # The active-low interruption branch fires on the observed entry scan and
    # thereby spends its one-shot.  The successful route must preserve that
    # exact execution state; pulsing the input would re-arm the late reset.
    assert fixture.InterruptionInput.name not in applied_tags

    work = events[-1].data["work"]
    entry_projection = work._replay_rung_write_projection_at(1)
    terminal_projection = work._replay_rung_write_projection_at(work.state.scan_id)
    assert entry_projection is not None
    assert terminal_projection is not None
    assert any(
        write.run.rung_id.rung_index == 13
        and write.transition.tag_name == fixture.SequenceStep.name
        and write.transition.to_value == 10
        for write in entry_projection.writes
    )
    assert not any(
        write.run.rung_id.rung_index == 13
        and write.transition.tag_name == fixture.SequenceStep.name
        for write in terminal_projection.writes
    )

    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    first_coast = next(event for event in events if event.kind == "bearing_coast")
    assert first_coast.scan == 0
    assert first_coast.data["reason"] == "observe exactly one entry scan"
    second_setup = next(
        index
        for index, step in enumerate(journal)
        if any(tag == fixture.SecondWatchdogPresetMs.name for tag, _value in step.inputs)
    )
    retained_coast = next(step for step in journal[second_setup + 1 :] if step.kind == "coast")
    # Established synthetic corrections are phase-discharged from later
    # actions, but their executable rungs remain in the overlay until the
    # structural target.  CheckpointSensor is different: it was an ordinary
    # level patch, so the runner carries its input-image value forward without
    # inventing a duplicate PilotRung.
    assert {
        fixture.SafetyPermit.name,
        fixture.BaseSensor.name,
        fixture.ReadyCommand.name,
        fixture.SimulationMode.name,
        fixture.FirstWatchdogPresetMs.name,
        fixture.SecondWatchdogPresetMs.name,
    }.issubset(set(retained_coast.steady_holds))
    assert work.state.tags[fixture.CheckpointSensor.name] is True

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.SequenceStep == 81,
        max_scans=120,
    )
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.state.tags[fixture.SequenceStep.name] == 81
    replay = plan.replay()
    assert replay.state.tags[fixture.SequenceStep.name] == 81
    assert replay.state.tags[fixture.CheckpointSensor.name] is True
