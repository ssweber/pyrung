"""A corrected local retry may expose another exact delayed requirement."""

from __future__ import annotations

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_successive_delayed_hazards as fixture


def test_second_delayed_hazard_refines_the_same_source_before_adoption() -> None:
    events = tuple(
        pilot_events(
            PLC(fixture.logic, dt=0.010),
            fixture.SequenceState == fixture.COMPLETE,
            max_scans=40,
        )
    )
    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None)
        in {fixture.FirstPresetMs.name, fixture.SecondPresetMs.name}
    )
    assert [
        (item.condition.tag, item.condition.op, item.condition.bound) for item in requirements
    ] == [
        (fixture.FirstPresetMs.name, ">", 10),
        (fixture.SecondPresetMs.name, ">", 10),
    ]
    # The first corrected retry is disposable because it exposes the second
    # hazard. Its scan number remains the same source edge, while its world key
    # records that the first requirement was already known. The next fresh read
    # composes both requirements with the original command and adopts only that
    # exact transaction.
    assert [item.source_scan for item in requirements] == [1, 1]
    assert requirements[0].source_world_key != requirements[1].source_world_key
    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    retries = tuple(event.data["applied"] for event in events if event.kind == "candidate_try")
    assert retries == (
        ((fixture.CompleteCommand.name, True),),
        (
            (fixture.FirstPresetMs.name, 11),
            (fixture.CompleteCommand.name, True),
        ),
        (
            (fixture.FirstPresetMs.name, 11),
            (fixture.SecondPresetMs.name, 11),
            (fixture.CompleteCommand.name, True),
        ),
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.SequenceState == fixture.COMPLETE,
        max_scans=40,
    )
    assert plan.reachable, plan.reason
    assert plan.state.tags[fixture.FirstPresetMs.name] == 11
    assert plan.state.tags[fixture.SecondPresetMs.name] == 11
    assert plan.state.tags[fixture.SequenceState.name] == fixture.COMPLETE
    assert all(
        changes.get(fixture.SequenceState.name) not in {fixture.FIRST_HAZARD, fixture.SECOND_HAZARD}
        for _scan, changes in plan.ordered_steps
    )
