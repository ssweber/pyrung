"""A corrected local retry may expose another exact delayed requirement."""

from __future__ import annotations

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_successive_delayed_hazards as fixture


def test_second_delayed_hazard_is_repaired_before_its_landing_is_adopted() -> None:
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
    repairs = tuple(event for event in events if event.kind == "requirement_locally_repaired")

    assert [
        (item.condition.tag, item.condition.op, item.condition.bound) for item in requirements
    ] == [
        (fixture.FirstPresetMs.name, ">", 10),
        (fixture.SecondPresetMs.name, ">", 10),
    ]
    # The first corrected retry is disposable because it exposes the second
    # hazard. Its installed first-preset correction is part of the second
    # requirement's exact checkpoint, so only the second repair is adopted.
    assert [event.data["assignments"] for event in repairs] == [
        ((fixture.SecondPresetMs.name, 11),),
    ]
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
