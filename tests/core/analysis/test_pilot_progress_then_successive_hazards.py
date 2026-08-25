"""Successive recovery across autonomous progress and fresh orientation."""

from __future__ import annotations

from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_progress_then_successive_hazards as fixture


def test_later_displacement_is_retried_from_each_productive_scan_tip() -> None:
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

    assert not any(event.kind == "requirement_locally_repaired" for event in events)

    decisions = [
        (
            event.kind,
            (
                event.data["configuration"][0]
                if event.kind == "theory_correction_composed"
                else tuple(event.data["applied"])
            ),
        )
        for event in events
        if event.kind in {"candidate_try", "theory_correction_composed"}
    ]
    assert decisions == [
        ("candidate_try", ((fixture.StartCommand.name, True),)),
        (
            "theory_correction_composed",
            (fixture.FirstPresetMs.name, 11),
        ),
        ("candidate_try", ((fixture.StartCommand.name, True),)),
        ("candidate_try", ((fixture.ConfirmCommand.name, True),)),
        (
            "theory_correction_composed",
            (fixture.SecondPresetMs.name, 11),
        ),
        ("candidate_try", ((fixture.ConfirmCommand.name, True),)),
    ]

    first_retry_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try"
        and event.data["applied"] == ((fixture.StartCommand.name, True),)
        and any(
            prior.kind == "theory_correction_composed"
            and prior.data["configuration"][0][0] == fixture.FirstPresetMs.name
            for prior in events[:index]
        )
    )
    confirm_try_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try"
        and (fixture.ConfirmCommand.name, True) in tuple(event.data["applied"])
    )
    second_retry_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try"
        and event.data["applied"] == ((fixture.ConfirmCommand.name, True),)
        and any(
            prior.kind == "theory_correction_composed"
            and prior.data["configuration"][0][0] == fixture.SecondPresetMs.name
            for prior in events[:index]
        )
    )
    assert first_retry_index < confirm_try_index < second_retry_index
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
