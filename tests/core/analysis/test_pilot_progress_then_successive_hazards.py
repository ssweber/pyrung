"""Successive recovery across autonomous progress and fresh orientation."""

from __future__ import annotations

from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_progress_then_successive_hazards as fixture


def test_later_displacement_is_repaired_after_legitimate_program_progress() -> None:
    events = tuple(
        islice(
            pilot_events(
                PLC(fixture.logic, dt=0.010),
                fixture.SequenceState == fixture.COMPLETE,
                max_scans=20,
            ),
            120,
        )
    )

    requirements = tuple(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )
    assert [
        (item.condition.tag, item.condition.op, item.condition.bound) for item in requirements
    ] == [
        (fixture.FirstPresetMs.name, ">", 10),
        (fixture.SecondPresetMs.name, ">", 10),
    ]

    repairs = tuple(event for event in events if event.kind == "requirement_locally_repaired")
    assert [event.data["assignments"] for event in repairs] == [
        ((fixture.FirstPresetMs.name, 11),),
        ((fixture.SecondPresetMs.name, 11),),
    ]

    selected_actions = [
        tuple(event.data["applied"])
        for event in events
        if event.kind == "candidate_try"
        and {
            fixture.StartCommand.name,
            fixture.ConfirmCommand.name,
        }.intersection(tag for tag, _value in event.data["applied"])
    ]
    assert selected_actions == [
        ((fixture.StartCommand.name, True),),
        ((fixture.ConfirmCommand.name, True),),
    ]

    first_repair_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "requirement_locally_repaired"
        and event.data["assignments"] == ((fixture.FirstPresetMs.name, 11),)
    )
    confirm_try_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try"
        and (fixture.ConfirmCommand.name, True) in tuple(event.data["applied"])
    )
    assert first_repair_index < confirm_try_index
    confirmation_source = next(
        event for event in reversed(events[:confirm_try_index]) if event.kind == "iteration"
    )
    assert (
        confirmation_source.data["snapshot"][fixture.SequenceState.name]
        == fixture.AWAITING_CONFIRMATION
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.SequenceState == fixture.COMPLETE,
        max_scans=20,
    )
    assert plan.reachable, plan.reason
    assert plan.state.tags[fixture.FirstPresetMs.name] == 11
    assert plan.state.tags[fixture.SecondPresetMs.name] == 11
    assert plan.state.tags[fixture.SequenceState.name] == fixture.COMPLETE
    sequence_writes = tuple(
        write
        for scan_id in range(plan.anchor_scan + 1, plan.state.scan_id + 1)
        for projection in (plan.fork._replay_rung_write_projection_at(scan_id),)
        if projection is not None
        for write in projection.writes
        if write.transition.tag_name == fixture.SequenceState.name
    )
    assert all(
        write.transition.to_value not in {fixture.FIRST_HAZARD, fixture.SECOND_HAZARD}
        for write in sequence_writes
    )
    assert all(
        changes.get(fixture.SequenceState.name) not in {fixture.FIRST_HAZARD, fixture.SECOND_HAZARD}
        for _scan, changes in plan.ordered_steps
    )
