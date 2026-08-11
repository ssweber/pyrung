"""Acceptance for a non-advance guard that destroys scan-0 work."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, branch, copy, out, rung, system
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.requirements import GuardRequirementAtom, OperandAuthority
from tests.fixtures import pilot_bootstrap_guard_overwrite as fixture
from tests.fixtures import pilot_bootstrap_intermediate_guard as intermediate_fixture


def _condition_tags(condition) -> frozenset[str]:
    tag = getattr(condition, "tag", None)
    if tag is not None:
        return frozenset((tag,))
    nested = getattr(condition, "condition", None)
    if nested is not None:
        return _condition_tags(nested)
    return frozenset(
        tag for term in getattr(condition, "terms", ()) for tag in _condition_tags(term)
    )


def _events():
    return tuple(
        pilot_events(
            PLC(fixture.logic, dt=0.010),
            fixture.SequenceState == fixture.TARGET,
            max_scans=20,
        )
    )


def test_scan_zero_guard_overwrite_is_retried_at_boundary_zero() -> None:
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

    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    assert any(
        event.kind == "candidate_try"
        and event.data["applied"] == ((fixture.OverwriteInterlock.name, True),)
        and event.scan == 0
        for event in events
    )
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
    assert plan.ordered_steps == [(1, {fixture.OverwriteInterlock.name: True})]


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
        event.kind == "candidate_try"
        and event.data["applied"] == ((interlock.name, False),)
        and event.scan == 0
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


def test_late_program_guard_rebases_to_its_pre_bootstrap_writer() -> None:
    """A later guard can implicate state poisoned by the unselected first scan."""

    source = 0
    target = 2
    diverted = 9
    state = Int("BootstrapRebaseState", default=source)
    start = Bool("BootstrapRebaseStart", external=True)
    prevent_poison = Bool("BootstrapRebasePreventPoison", external=True)
    poisoned = Int("BootstrapRebasePoisoned")

    with Program(strict=False) as logic:
        # This write is not target-relevant during the pre-scan trace, so its
        # consequence becomes known only after the later target overwrite.
        with rung(~prevent_poison):
            with branch(state == source):
                copy(1, poisoned, oneshot=True)
        with rung(start, state == source):
            copy(target, state, oneshot=True)
        with rung(state == target, poisoned == 1):
            copy(diverted, state)

    events = tuple(pilot_events(PLC(logic), state == target, max_scans=30))

    program_guards = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and poisoned.name in _condition_tags(event.data["requirement"].condition)
    )
    assert program_guards, tuple(
        (event.kind, event.scan, event.data.get("reason")) for event in events
    )
    program_guard = program_guards[0]
    assert program_guard.source_scan > 0
    assert program_guard.operand_authority is OperandAuthority.PROGRAM_WRITTEN

    rebased = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and event.data["requirement"].provenance == "program-guard-rebase"
    )
    assert len(rebased) == 1
    assert rebased[0].source_scan == 0
    assert prevent_poison.name in _condition_tags(rebased[0].condition)

    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    assert any(
        event.kind == "candidate_try" and event.data["applied"] == ((prevent_poison.name, True),)
        for event in events
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = PLC(logic).how(state == target, max_scans=30)
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.state.tags[state.name] == target
    assert plan.state.tags[poisoned.name] == 0
    assert plan.replay().state.tags[state.name] == target

    # ClickNick's generated run.py executes one scan before DAP discovers the
    # runner. The same transition must remain recoverable from retained runner
    # history even though Pilot did not own it as a bootstrap execution.
    warmed = PLC(logic)
    warmed.step()
    assert warmed.state.tags[poisoned.name] == 1
    warm_events = tuple(pilot_events(warmed, state == target, max_scans=30))
    warm_rebased = tuple(
        event.data["requirement"]
        for event in warm_events
        if event.kind == "requirement_activated"
        and event.data["requirement"].provenance == "program-guard-rebase"
    )
    assert len(warm_rebased) == 1
    assert (warm_rebased[0].source_scan, warm_rebased[0].deadline.scan_id) == (0, 1)
    assert warm_events[-1].kind == "finished"
    assert warm_events[-1].data["reached"] is True


def test_current_read_can_prevent_poison_before_a_rebase_is_needed() -> None:
    """Ordinary look-ahead may establish the guard before failure teaches it."""

    source = 0
    armed = 1
    target = 2
    diverted = 9
    state = Int("HistoryRebaseState", default=source)
    advance = Bool("HistoryRebaseAdvance", external=True)
    finish = Bool("HistoryRebaseFinish", external=True)
    prevent_poison = Bool("HistoryRebasePreventPoison", external=True)
    poisoned = Int("HistoryRebasePoisoned")

    with Program(strict=False) as logic:
        with rung(advance, state == source):
            copy(armed, state, oneshot=True)
        with rung(~prevent_poison, state == armed):
            copy(1, poisoned, oneshot=True)
        with rung(finish, state == armed):
            copy(target, state, oneshot=True)
        with rung(state == target, poisoned == 1):
            copy(diverted, state)

    events = tuple(pilot_events(PLC(logic), state == target, max_scans=30))
    rebased = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and event.data["requirement"].provenance == "program-guard-rebase"
    )
    assert rebased == ()
    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    assert events[-1].data["work"].state.tags[prevent_poison.name] is True
    assert events[-1].data["work"].state.tags[poisoned.name] == 0


def test_scan_zero_retries_an_intermediate_then_fresh_orients_to_the_target() -> None:
    events = tuple(
        pilot_events(
            PLC(intermediate_fixture.logic, dt=0.010),
            intermediate_fixture.SequenceState == intermediate_fixture.COMPLETE,
            max_scans=20,
        )
    )

    retry_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try"
        and event.data["applied"] == ((intermediate_fixture.PreserveIntermediate.name, True),)
    )
    finish_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "candidate_try"
        and (intermediate_fixture.FinishCommand.name, True) in tuple(event.data["applied"])
    )

    assert retry_index < finish_index
    assert not any(event.kind == "requirement_locally_repaired" for event in events)
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
        (1, {intermediate_fixture.PreserveIntermediate.name: True}),
        (2, {intermediate_fixture.FinishCommand.name: True}),
    ]
