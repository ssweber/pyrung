"""WorkingTheory persistence across a pulse and an actionless Coast."""

from __future__ import annotations

from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_progress_then_autonomous_hazard as fixture


def test_later_autonomous_displacement_retries_the_fresh_program_transaction() -> None:
    events = tuple(
        islice(
            pilot_events(
                PLC(fixture.logic, dt=0.010),
                fixture.SequenceState == fixture.COMPLETE,
                max_scans=20,
            ),
            140,
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
        ("theory_correction_composed", (fixture.FirstPresetMs.name, 11)),
        ("candidate_try", ((fixture.StartCommand.name, True),)),
        ("theory_correction_composed", (fixture.SecondPresetMs.name, 11)),
    ]

    second_composition = next(
        index
        for index, event in enumerate(events)
        if event.kind == "theory_correction_composed"
        and event.data["configuration"][0][0] == fixture.SecondPresetMs.name
    )
    assert any(
        event.kind == "bearing_coast"
        and event.data["reason"] == "working theory: continue the freshly read program transaction"
        for event in events[second_composition + 1 :]
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
