"""Public recovery across a refined same-tag occurrence deadline."""

from __future__ import annotations

from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
)
from pyrung.core.crossing import Cmp
from tests.fixtures import pilot_zero_net_deadline_composition as fixture


def test_fixture_target_is_hidden_by_an_exact_zero_net_final_scan() -> None:
    plc = PLC(fixture.logic)
    plc.patch({fixture.Advance.name: True})
    plc.step()
    plc.patch({fixture.Advance.name: False})
    plc.step()
    plc.step()
    assert plc.state.tags[fixture.State.name] == fixture.TARGET_SOURCE

    plc.step()
    projection = plc._replay_rung_write_projection_at(plc.state.scan_id)
    assert projection is not None
    assert tuple(
        write.transition.to_value
        for write in projection.writes
        if write.transition.tag_name == fixture.State.name
    ) == (fixture.TARGET, fixture.LOW, fixture.TARGET_SOURCE)
    assert plc.state.tags[fixture.State.name] == fixture.TARGET_SOURCE


def test_public_pilot_composes_the_earlier_guard_and_reaches_target() -> None:
    events = tuple(
        islice(
            pilot_events(
                PLC(fixture.logic),
                fixture.State == fixture.TARGET,
                max_scans=16,
            ),
            100,
        )
    )

    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and isinstance(event.data["requirement"].condition, GuardRequirementExpr)
    )
    assert len(requirements) == 1
    condition = requirements[0].condition
    assert isinstance(condition, GuardRequirementExpr)
    assert condition.logic is GuardLogic.ANY
    assert condition.exhaustive is True
    assert all(isinstance(term, GuardRequirementAtom) for term in condition.terms)
    assert [term.condition for term in condition.terms] == [
        Cmp(fixture.LinkHealthy.name, "!=", False),
        Cmp(fixture.KeepTarget.name, "!=", False),
    ]
    assert (
        condition.terms[0].demanding_rung is fixture.logic.subroutines["ZeroNetDeadlineRollback"][0]
    )
    assert condition.terms[1].demanding_rung is fixture.logic.rungs[2]
    assert condition.terms[1].deadline.ordinal < condition.terms[0].deadline.ordinal

    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    assert any(
        event.kind == "candidate_try"
        and event.data["applied"] == ((fixture.KeepTarget.name, True),)
        for event in events
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    # The earlier speculative branch is discarded when WorkingTheory restores
    # its exact source.  The executable journal is the receipt for the fresh
    # composition. Ordinary level patches persist in the runner input image;
    # unlike conditional corrective PilotRungs, they are not duplicated in a
    # journal step's ``steady_holds`` field.
    journal = events[-1].data["plan_journal"]
    applied = tuple(pair for step in journal for pair in step.inputs)
    assert (fixture.Advance.name, True) in applied
    assert (fixture.KeepTarget.name, True) in applied
    assert not any(tag in {fixture.State.name, fixture.LinkHealthy.name} for tag, _value in applied)
    work = events[-1].data["work"]
    assert work.state.tags[fixture.Advance.name] is True
    assert work.state.tags[fixture.KeepTarget.name] is True

    plan = PLC(fixture.logic).how(
        fixture.State == fixture.TARGET,
        max_scans=16,
    )
    assert plan.reachable, plan.reason
    assert plan.state.tags[fixture.State.name] == fixture.TARGET
    assert plan.state.tags[fixture.Advance.name] is True
    assert plan.state.tags[fixture.KeepTarget.name] is True
    assert plan.state.tags[fixture.LinkHealthy.name] is False
    replay = plan.replay()
    assert replay.state.tags[fixture.State.name] == fixture.TARGET
    assert replay.state.tags[fixture.Advance.name] is True
    assert replay.state.tags[fixture.KeepTarget.name] is True
