"""Acceptance for a non-advance guard that destroys scan-0 work."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, copy, out, rung, system
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.requirements import GuardRequirementAtom, OperandAuthority
from tests.fixtures import pilot_bootstrap_guard_overwrite as fixture
from tests.fixtures import pilot_bootstrap_intermediate_guard as intermediate_fixture


def _events():
    return tuple(
        pilot_events(
            PLC(fixture.logic, dt=0.010),
            fixture.SequenceState == fixture.TARGET,
            max_scans=20,
        )
    )


def test_scan_zero_guard_overwrite_is_repaired_at_boundary_zero() -> None:
    events = _events()
    requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition.condition, "tag", None)
        == fixture.OverwriteInterlock.name
    )

    assert len(requirements) == 1
    requirement = requirements[0]
    assert isinstance(requirement.condition, GuardRequirementAtom)
    assert requirement.condition.operand_authority is OperandAuthority.ADJUSTABLE
    assert requirement.operand_authority is OperandAuthority.ADJUSTABLE
    assert (
        requirement.condition.condition.op,
        requirement.condition.condition.bound,
        requirement.source_scan,
        requirement.deadline.scan_id,
        requirement.provenance,
    ) == ("!=", False, 0, 1, "bootstrap-overwriter")

    repairs = tuple(event for event in events if event.kind == "requirement_locally_repaired")
    assert len(repairs) == 1
    assert repairs[0].data["assignments"] == ((fixture.OverwriteInterlock.name, True),)
    assert repairs[0].data["detail"] == "bootstrap local transaction repaired"
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.SequenceState == fixture.TARGET,
        max_scans=20,
    )
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.total_scans == 1
    assert plan.state.scan_id == 1
    assert plan.state.tags[fixture.SequenceState.name] == fixture.TARGET
    assert plan.state.tags[fixture.OverwriteInterlock.name] is True
    assert plan.ordered_steps == []


def test_nonzero_external_guard_is_adjustable_without_parameter_heuristics() -> None:
    initial = 0
    target = 1
    diverted = 9
    state = Int("BootstrapNonzeroGuardState", default=initial)
    interlock = Bool("BootstrapNonzeroGuardInterlock", default=True, external=True)

    with Program() as logic:
        with rung(system.sys.first_scan):
            copy(target, state)
        with rung(interlock):
            copy(diverted, state, oneshot=True)

    events = tuple(pilot_events(PLC(logic), state == target, max_scans=20))
    requirement = next(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )

    assert isinstance(requirement.condition, GuardRequirementAtom)
    assert requirement.condition.operand_authority is OperandAuthority.ADJUSTABLE
    assert requirement.operand_authority is OperandAuthority.ADJUSTABLE
    assert any(
        event.kind == "requirement_locally_repaired"
        and event.data["assignments"] == ((interlock.name, False),)
        for event in events
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True


def test_forced_false_guard_is_authoritative_and_not_assigned() -> None:
    plc = PLC(fixture.logic, dt=0.010)
    plc.force(fixture.OverwriteInterlock, False)
    events = tuple(
        pilot_events(
            plc,
            fixture.SequenceState == fixture.TARGET,
            max_scans=20,
        )
    )
    requirement = next(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )

    assert isinstance(requirement.condition, GuardRequirementAtom)
    assert requirement.condition.operand_authority is OperandAuthority.CONFIGURED
    assert requirement.operand_authority is OperandAuthority.CONFIGURED
    assert not any(
        tag == fixture.OverwriteInterlock.name
        for event in events
        for key in ("assignments", "applied")
        for tag, _value in tuple(event.data.get(key, ()))
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is False


def test_pending_false_guard_is_authoritative_and_not_assigned() -> None:
    plc = PLC(fixture.logic, dt=0.010)
    plc.patch({fixture.OverwriteInterlock.name: False})
    events = tuple(
        pilot_events(
            plc,
            fixture.SequenceState == fixture.TARGET,
            max_scans=20,
        )
    )
    requirement = next(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )

    assert isinstance(requirement.condition, GuardRequirementAtom)
    assert requirement.condition.operand_authority is OperandAuthority.CONFIGURED
    assert requirement.operand_authority is OperandAuthority.CONFIGURED
    assert not any(
        tag == fixture.OverwriteInterlock.name
        for event in events
        for key in ("assignments", "applied")
        for tag, _value in tuple(event.data.get(key, ()))
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is False


def test_program_written_false_guard_is_authoritative_and_not_assigned() -> None:
    initial = 0
    target = 1
    diverted = 9
    state = Int("BootstrapOwnedGuardState", default=initial)
    program_guard = Bool("BootstrapOwnedProgramGuard")

    with Program() as logic:
        with rung():
            out(program_guard)
        with rung(system.sys.first_scan):
            copy(target, state)
        with rung(program_guard):
            copy(diverted, state, oneshot=True)

    events = tuple(pilot_events(PLC(logic), state == target, max_scans=20))
    requirement = next(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )

    assert isinstance(requirement.condition, GuardRequirementAtom)
    assert requirement.condition.operand_authority is OperandAuthority.PROGRAM_WRITTEN
    assert requirement.operand_authority is OperandAuthority.PROGRAM_WRITTEN
    assert not any(
        tag == program_guard.name
        for event in events
        for key in ("assignments", "applied")
        for tag, _value in tuple(event.data.get(key, ()))
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is False


def test_scan_zero_repairs_an_intermediate_then_fresh_orients_to_the_target() -> None:
    events = tuple(
        pilot_events(
            PLC(intermediate_fixture.logic, dt=0.010),
            intermediate_fixture.SequenceState == intermediate_fixture.COMPLETE,
            max_scans=20,
        )
    )

    repair_index = next(
        index for index, event in enumerate(events) if event.kind == "requirement_locally_repaired"
    )
    finish_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try"
        and (intermediate_fixture.FinishCommand.name, True) in tuple(event.data["applied"])
    )

    assert repair_index < finish_index
    assert events[repair_index].data["assignments"] == (
        (intermediate_fixture.PreserveIntermediate.name, True),
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = PLC(intermediate_fixture.logic, dt=0.010).how(
        intermediate_fixture.SequenceState == intermediate_fixture.COMPLETE,
        max_scans=20,
    )
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.total_scans == 2
    assert plan.state.tags[intermediate_fixture.SequenceState.name] == intermediate_fixture.COMPLETE
    assert plan.state.tags[intermediate_fixture.PreserveIntermediate.name] is True
    assert plan.ordered_steps == [
        (2, {intermediate_fixture.FinishCommand.name: True}),
    ]
