"""Recovery of a transient target outside the selected act expectation."""

from __future__ import annotations

from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_transient_target_restore as fixture


def test_fixture_target_exists_only_inside_later_autonomous_scan() -> None:
    plc = PLC(fixture.logic, dt=0.010)
    plc.patch(
        {
            fixture.Advance.name: True,
            fixture.EarlyPresetMs.name: 11,
        }
    )
    plc.step()
    assert plc.state.tags[fixture.State.name] == fixture.QUALIFIED

    plc.patch({fixture.Advance.name: False})
    plc.step()
    assert plc.state.tags[fixture.State.name] == fixture.TARGET_SOURCE

    source = plc.state.tags[fixture.State.name]
    plc.step()
    projection = plc._replay_rung_write_projection_at(plc.state.scan_id)
    assert projection is not None
    state_writes = tuple(
        write.transition.to_value
        for write in projection.writes
        if write.transition.tag_name == fixture.State.name
    )
    assert state_writes == (
        fixture.TARGET,
        fixture.INTERMEDIATE,
        fixture.TARGET_SOURCE,
    )
    assert plc.state.tags[fixture.State.name] == source == fixture.TARGET_SOURCE


def test_without_exact_target_appearance_later_requirement_is_absent() -> None:
    events = tuple(
        islice(
            pilot_events(
                PLC(fixture.without_target_logic, dt=0.010),
                fixture.State == fixture.TARGET,
                max_scans=16,
            ),
            40,
        )
    )

    assert not any(
        event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None) == fixture.LaterPresetMs.name
        for event in events
    )


def test_scan_tip_recovery_does_not_use_folded_program_continuation(
    monkeypatch,
) -> None:
    def reject_legacy_continuation(*_args, **_kwargs):
        raise AssertionError("scan-tip recovery must not fold a logical gap")

    monkeypatch.setattr(
        pilot_module,
        "_repaired_program_continuation",
        reject_legacy_continuation,
    )
    events = tuple(
        islice(
            pilot_events(
                PLC(fixture.logic, dt=0.010),
                fixture.State == fixture.TARGET,
                max_scans=16,
            ),
            80,
        )
    )

    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True


def test_recovery_observes_target_before_same_scan_rollback() -> None:
    events = tuple(
        islice(
            pilot_events(
                PLC(fixture.logic, dt=0.010),
                fixture.State == fixture.TARGET,
                max_scans=16,
            ),
            80,
        )
    )

    later_requirement_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None) == fixture.LaterPresetMs.name
    )
    discovery_receipts = tuple(
        event.data["receipt"]
        for event in events[:later_requirement_index]
        if event.kind == "expectation_committed"
    )
    assert discovery_receipts
    assert all(
        not any(
            obligation.tag == fixture.State.name and obligation.value == fixture.TARGET
            for obligation in receipt.obligations
        )
        for receipt in discovery_receipts
    )

    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None)
        in {fixture.EarlyPresetMs.name, fixture.LaterPresetMs.name}
    )
    assert [
        (item.condition.tag, item.condition.op, item.condition.bound) for item in requirements
    ] == [
        (fixture.EarlyPresetMs.name, ">", 10),
        (fixture.LaterPresetMs.name, ">", 10),
    ]
    assert [item.source_scan for item in requirements] == [1, 3]
    assert requirements[0].source_world_key != requirements[1].source_world_key

    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    compositions = tuple(
        assignment
        for event in events
        if event.kind == "theory_correction_composed"
        for assignment in event.data["configuration"]
    )
    assert compositions == (
        (fixture.EarlyPresetMs.name, 11),
        (fixture.LaterPresetMs.name, 11),
    )
    advance_tries = tuple(
        tuple(event.data["applied"]) for event in events if event.kind == "candidate_try"
    )
    assert advance_tries == (
        ((fixture.Advance.name, True),),
        ((fixture.Advance.name, True),),
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.State == fixture.TARGET,
        max_scans=16,
    )
    assert plan.reachable, plan.reason
    assert plan.state.tags[fixture.EarlyPresetMs.name] == 11
    assert plan.state.tags[fixture.LaterPresetMs.name] == 11
    assert plan.state.tags[fixture.State.name] == fixture.TARGET
