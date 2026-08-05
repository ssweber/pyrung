"""A corrected local retry may expose another exact delayed requirement."""

from __future__ import annotations

import importlib

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_successive_delayed_hazards as fixture


def test_second_delayed_hazard_is_repaired_before_its_landing_is_adopted(
    monkeypatch,
) -> None:
    pilot_mod = importlib.import_module("pyrung.core.analysis.pilot.pilot")
    original_retain = pilot_mod._retain_expectation_receipt
    original_repair = pilot_mod._repair_one_active_requirement
    retry_receipts = []
    owned_sources = []

    def retain_from_causal_source(trial, act, state, checkpoint):
        before = len(state.expectation_receipts)
        original_retain(trial, act, state, checkpoint)
        if state.active_requirements:
            retry_receipts.extend(
                (
                    checkpoint,
                    trial.attempt.bearing,
                    receipt,
                    tuple(
                        requirement.source_checkpoint for requirement in state.active_requirements
                    ),
                )
                for receipt in state.expectation_receipts[before:]
            )

    def capture_composed_source(state, ctx):
        scalar = tuple(
            requirement
            for requirement in state.active_requirements
            if getattr(requirement.condition, "tag", None)
            in {fixture.FirstPresetMs.name, fixture.SecondPresetMs.name}
        )
        if len(scalar) == 2:
            receipts = tuple(
                pilot_mod._exact_failed_source(requirement, state) for requirement in scalar
            )
            if all(receipt is not None for receipt in receipts):
                owned_sources[:] = [(scalar, receipts)]
        return original_repair(state, ctx)

    monkeypatch.setattr(
        pilot_mod,
        "_retain_expectation_receipt",
        retain_from_causal_source,
    )
    monkeypatch.setattr(
        pilot_mod,
        "_repair_one_active_requirement",
        capture_composed_source,
    )
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
    # hazard. Both requirements remain owned by the original selected
    # transaction, so recovery adopts one composed correction from that exact
    # source rather than treating the disposable retry as a new checkpoint.
    assert [item.source_scan for item in requirements] == [1, 1]
    assert requirements[0].source_world_key == requirements[1].source_world_key
    assert len(retry_receipts) == 1
    checkpoint, retry_bearing, receipt, active_sources = retry_receipts[0]
    assert checkpoint is active_sources[0]
    assert retry_bearing.world_key != checkpoint.key
    assert receipt.source_checkpoint is checkpoint
    assert receipt.checkpoint_owner is checkpoint.owner
    assert receipt.source_world_key == checkpoint.key
    assert receipt.local_bearing is retry_bearing

    assert len(owned_sources) == 1
    owned_requirements, owned_receipts = owned_sources[0]
    assert owned_requirements[0].checkpoint_owner is owned_requirements[1].checkpoint_owner
    assert owned_requirements[0].source_checkpoint is owned_requirements[1].source_checkpoint
    original_act_identity = ("pulse", ((fixture.CompleteCommand.name, True),))
    assert [item.act_identity for item in owned_receipts] == [
        original_act_identity,
        original_act_identity,
    ]
    assert [event.data["assignments"] for event in repairs] == [
        (
            (fixture.FirstPresetMs.name, 11),
            (fixture.SecondPresetMs.name, 11),
        ),
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
