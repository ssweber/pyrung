"""Exact same-scan requirement source-walk contracts."""

from __future__ import annotations

from dataclasses import fields, replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyrung import PLC, And, Bool, Int, Or, Program, call, copy, out, rung, subroutine
from pyrung.core import branch
from pyrung.core.analysis.causal._rung_writes import (
    RungRead,
    RungWrite,
    ScanRungWriteProjection,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObligation,
    observe_execution_window,
    occurrence_snapshot,
    promote_terminal_target_observation,
)
from pyrung.core.analysis.pilot.intrascan import IntrascanFinding
from pyrung.core.analysis.pilot.requirement_recovery import guard_alternatives
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementSourceWalk,
    RequirementSourceWalkStatus,
    derive_occurrence_source_requirement,
    derive_overwriter_guard_requirement_from_effect,
)
from pyrung.core.crossing import Cmp
from pyrung.core.executor import WriteOccurrence


def _terminal_overwrite(
    program: Program,
    state: Int,
    *,
    source_value: int,
    target_value: int,
) -> tuple[Any, ScanRungWriteProjection, Any]:
    plc = PLC(program)
    source = plc.fork()
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    obligation = EffectObligation(
        state.name,
        target_value,
        (None, 0, ()),
        None,
        (),
        terminal_target=True,
        producer_rung=program.rungs[0],
    )
    observations = observe_execution_window(
        EffectExpectation((obligation,)),
        plc,
        scan_before=0,
        kernel_scan_ids=(1,),
        action_scan=1,
    )
    promoted = promote_terminal_target_observation(
        observations,
        window_entry_value=source_value,
        final_landing_value=source_value,
    )
    assert promoted is not None and promoted.displacement is not None
    checkpoint = SimpleNamespace(
        owner=object(),
        key=("source-walk", 0),
        world=SimpleNamespace(work=source),
    )
    return source, projection, (promoted, checkpoint)


def _derive_terminal_overwrite(
    program: Program,
    state: Int,
    *,
    source_value: int,
    target_value: int,
):
    source, projection, (observation, checkpoint) = _terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )
    before = source.state
    result = derive_overwriter_guard_requirement_from_effect(
        observation,
        projection,
        execution_epoch=observation.execution_epoch,
        execution_owner=observation.execution_owner,
        selected_writer=observation.obligation.producer,
        source_world_key=checkpoint.key,
        source_checkpoint=checkpoint,
        preserved_values=((state.name, target_value),),
    )
    assert source.state is before
    return result, projection, IntrascanFinding(observation, result, checkpoint)


def _condition_atoms(
    condition: GuardRequirementCondition,
) -> tuple[GuardRequirementAtom, ...]:
    if isinstance(condition, GuardRequirementAtom):
        return (condition,)
    return tuple(atom for term in condition.terms for atom in _condition_atoms(term))


def _state_source_atom(
    projection: ScanRungWriteProjection,
    *,
    state: Int,
    demanding_rung: Any,
    condition: Cmp,
) -> GuardRequirementAtom:
    reads = tuple(
        read
        for read in projection.reads
        if read.run.rung is demanding_rung and read.occurrence.name == state.name
    )
    assert len(reads) == 1
    snapshot = occurrence_snapshot(reads[0])
    return GuardRequirementAtom(
        condition=condition,
        supporting_occurrences=(snapshot,),
        deadline=snapshot,
        source_path=(0,),
        demanding_rung=demanding_rung,
    )


def test_two_hop_walk_uses_exact_sources_with_strictly_earlier_deadlines() -> None:
    source_value = 70
    target_value = 71
    state = Int("TwoHopSourceState", default=source_value)
    first_permit = Bool("TwoHopFirstPermit", external=True, default=True)
    second_permit = Bool("TwoHopSecondPermit", external=True, default=True)
    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung(first_permit, state == target_value):
            copy(50, state, oneshot=True)
        with rung(second_permit, state <= 50):
            copy(30, state, oneshot=True)
        with rung(state <= 30):
            copy(source_value, state, oneshot=True)

    derivation, _projection, _finding = _derive_terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )

    assert derivation.source_walk is not None
    walk = derivation.source_walk
    assert walk.status is RequirementSourceWalkStatus.COMPLETE
    assert len(walk.links) == 2
    assert [link.source_address for link in walk.links] == [
        (None, 2, ()),
        (None, 1, ()),
    ]
    assert walk.links[1].required_read.ordinal < walk.links[0].source_write.ordinal
    assert tuple(
        tuple(atom.condition for atom in alternative)
        for alternative in guard_alternatives(walk.condition)
    ) == (
        (Cmp(second_permit.name, "!=", True),),
        (Cmp(first_permit.name, "!=", True),),
    )


def test_cross_tag_walk_is_transitive_evidence_without_changing_legacy_requirement() -> None:
    source_value = 40
    target_value = 41
    state = Int("CrossTagStepperState", default=source_value)
    producer_enabled = Bool("CrossTagProducerEnabled", external=True, default=True)
    produced_permit = Bool("CrossTagProducedPermit")
    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung(producer_enabled):
            out(produced_permit)
        with rung(produced_permit):
            copy(source_value, state, oneshot=True)

    derivation, _projection, finding = _derive_terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )

    assert derivation.requirement is not None
    legacy = derivation.requirement.condition
    assert isinstance(legacy, GuardRequirementAtom)
    assert legacy.condition == Cmp(produced_permit.name, "!=", True)
    assert legacy.source_links == ()

    walk = derivation.source_walk
    assert walk is not None
    assert walk.status is RequirementSourceWalkStatus.COMPLETE
    assert len(walk.links) == 1
    transitive = walk.condition
    assert isinstance(transitive, GuardRequirementAtom)
    assert transitive.condition == Cmp(producer_enabled.name, "!=", True)
    link = walk.links[0]
    assert link.source_address == (None, 1, ())
    assert link.required_read == legacy.deadline
    assert link.required_read.tag == produced_permit.name
    assert link.source_write.tag == produced_permit.name
    assert link.enabling_reads == (transitive.deadline,)
    assert transitive.deadline.ordinal < link.source_write.ordinal < link.required_read.ordinal
    assert transitive.source_links == (link,)
    diagnostic = finding.diagnostic_snapshot()
    assert diagnostic.requirement is not None
    assert diagnostic.requirement.condition == legacy
    assert diagnostic.source_walk == walk
    assert diagnostic.source_walk.condition == transitive


def test_cross_tag_walk_preserves_nested_false_options_report_only() -> None:
    source_value = 60
    target_value = 61
    state = Int("NestedCrossTagStepperState", default=source_value)
    path_a = Bool("NestedCrossTagPathA", external=True, default=True)
    path_b = Bool("NestedCrossTagPathB", external=True)
    unread_path = Bool("NestedCrossTagUnreadPath", external=True, default=True)
    producer_permit = Bool("NestedCrossTagProducerPermit", external=True, default=True)
    produced_permit = Bool("NestedCrossTagProducedPermit")
    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung(Or(And(path_a, path_b, unread_path), producer_permit)):
            out(produced_permit)
        with rung(produced_permit):
            copy(source_value, state, oneshot=True)

    derivation, _projection, _finding = _derive_terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )

    assert derivation.requirement is not None
    legacy = derivation.requirement.condition
    assert isinstance(legacy, GuardRequirementAtom)
    assert legacy.condition == Cmp(produced_permit.name, "!=", True)
    assert legacy.source_links == ()

    walk = derivation.source_walk
    assert walk is not None
    assert walk.status is RequirementSourceWalkStatus.COMPLETE
    assert len(walk.links) == 1
    condition = walk.condition
    assert isinstance(condition, GuardRequirementExpr)
    assert condition.logic.value == "all"
    nested = condition.terms[0]
    assert isinstance(nested, GuardRequirementExpr)
    assert nested.logic.value == "any"
    assert nested.exhaustive is False
    assert tuple(
        tuple(atom.condition for atom in alternative)
        for alternative in guard_alternatives(condition)
    ) == (
        (
            Cmp(path_a.name, "!=", True),
            Cmp(producer_permit.name, "!=", True),
        ),
        (
            Cmp(path_b.name, "!=", True),
            Cmp(producer_permit.name, "!=", True),
        ),
    )
    link = walk.links[0]
    assert link.required_address == (None, 2, ())
    assert link.source_address == (None, 1, ())
    assert link.required_instruction_path == ()
    assert link.instruction_path
    assert [read.tag for read in link.enabling_reads] == [
        path_a.name,
        path_b.name,
        producer_permit.name,
    ]
    assert unread_path.name not in {read.tag for read in link.enabling_reads}
    atoms = _condition_atoms(condition)
    assert [atom.deadline.ordinal for atom in atoms[:2]] == sorted(
        atom.deadline.ordinal for atom in atoms[:2]
    )
    assert all(atom.deadline.ordinal < link.source_write.ordinal for atom in atoms)


def test_three_hop_walk_preserves_nested_boolean_alternatives_and_deadlines() -> None:
    source_value = 90
    target_value = 100
    state = Int("SourceWalkState", default=source_value)
    first_permit = Bool("SourceWalkFirstPermit", external=True, default=True)
    second_permit = Bool("SourceWalkSecondPermit", external=True, default=True)
    third_permit = Bool("SourceWalkThirdPermit", external=True, default=True)
    path_a = Bool("SourceWalkPathA", external=True)
    path_b = Bool("SourceWalkPathB", external=True, default=True)

    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung(first_permit, state == target_value):
            copy(80, state, oneshot=True)
        with rung(second_permit, state <= 80):
            copy(60, state, oneshot=True)
        with rung(third_permit, state <= 60):
            copy(40, state, oneshot=True)
        with rung(And(Or(path_a, path_b), state <= 40)):
            copy(source_value, state, oneshot=True)

    derivation, _projection, _finding = _derive_terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )

    assert derivation.requirement is not None
    walk = derivation.source_walk
    assert walk is not None
    assert walk.status is RequirementSourceWalkStatus.COMPLETE
    assert len(walk.links) == 3
    assert all(link.source_write.ordinal < link.required_read.ordinal for link in walk.links)
    assert all(
        later.required_read.ordinal < earlier.source_write.ordinal
        for earlier, later in zip(walk.links, walk.links[1:], strict=False)
    )
    condition = walk.condition
    alternatives = guard_alternatives(condition)
    alternative_conditions = tuple(
        tuple(atom.condition for atom in alternative) for alternative in alternatives
    )
    assert alternative_conditions == (
        (
            Cmp(path_a.name, "!=", True),
            Cmp(path_b.name, "!=", True),
        ),
        (Cmp(third_permit.name, "!=", True),),
        (Cmp(second_permit.name, "!=", True),),
        (Cmp(first_permit.name, "!=", True),),
    )
    atoms = _condition_atoms(condition)
    assert len({atom.deadline.dynamic_address for atom in atoms}) == len(atoms)
    assert all(atom.deadline.scan_id == 1 for atom in atoms)
    assert all(atom.source_links == walk.links[: len(atom.source_links)] for atom in atoms)


def test_same_constraint_at_distinct_deadlines_is_not_deduplicated() -> None:
    source_value = 20
    target_value = 21
    state = Int("DeadlineIdentityState", default=source_value)
    permit = Bool("DeadlineIdentityPermit", external=True, default=True)
    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung(permit, permit):
            copy(source_value, state, oneshot=True)

    derivation, _projection, _finding = _derive_terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )

    assert derivation.requirement is not None
    condition = derivation.requirement.condition
    assert isinstance(condition, GuardRequirementExpr)
    atoms = _condition_atoms(condition)
    assert [atom.condition for atom in atoms] == [
        Cmp(permit.name, "!=", True),
        Cmp(permit.name, "!=", True),
    ]
    assert atoms[0].deadline.dynamic_address != atoms[1].deadline.dynamic_address


def test_walk_uses_the_exact_repeated_subroutine_invocation_source() -> None:
    source_value = 30
    target_value = 31
    state = Int("RepeatedSourceState", default=source_value)
    permit = Bool("RepeatedSourcePermit", external=True, default=True)

    @subroutine("RepeatedSourceWriter", strict=False)
    def write_intermediate() -> None:
        with rung(permit):
            copy(10, state)

    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung():
            call(write_intermediate)
            copy(target_value, state)
            call(write_intermediate)
        with rung(state <= 10):
            copy(source_value, state, oneshot=True)

    derivation, _projection, _finding = _derive_terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )

    assert derivation.source_walk is not None
    assert derivation.source_walk.status is RequirementSourceWalkStatus.COMPLETE
    assert len(derivation.source_walk.links) == 1
    link = derivation.source_walk.links[0]
    assert link.source_write.call_invocation == 1
    assert link.required_read.call_invocation is None
    assert link.source_write.dynamic_address != link.required_read.dynamic_address
    assert link.required_address == (None, 2, ())
    assert link.required_instruction_path == ()
    assert link.source_address == ("RepeatedSourceWriter", 0, ())
    assert link.instruction_path


def test_walk_retains_the_exact_sibling_branch_source_address() -> None:
    source_value = 50
    target_value = 51
    state = Int("BranchSourceState", default=source_value)
    first_path = Bool("BranchSourceFirstPath", external=True, default=True)
    second_path = Bool("BranchSourceSecondPath", external=True, default=True)
    with Program() as program:
        with rung():
            copy(target_value, state, oneshot=True)
        with rung():
            with branch(first_path):
                copy(20, state)
            with branch(second_path):
                copy(10, state)
        with rung(state <= 10):
            copy(source_value, state, oneshot=True)

    derivation, _projection, _finding = _derive_terminal_overwrite(
        program,
        state,
        source_value=source_value,
        target_value=target_value,
    )

    assert derivation.source_walk is not None
    assert derivation.source_walk.status is RequirementSourceWalkStatus.COMPLETE
    assert len(derivation.source_walk.links) == 1
    link = derivation.source_walk.links[0]
    assert link.required_address == (None, 2, ())
    assert link.required_instruction_path == ()
    assert link.source_address == (None, 1, (1,))
    assert link.instruction_path
    assert link.source_write.execution_kind == "branch"


def _one_hop_projection() -> tuple[
    ScanRungWriteProjection,
    GuardRequirementAtom,
    RungRead,
    RungWrite,
]:
    state = Int("IncompleteSourceState", default=7)
    permit = Bool("IncompleteSourcePermit", external=True, default=True)
    with Program() as program:
        with rung(permit):
            copy(3, state)
        with rung(state <= 3):
            copy(1, Int("IncompleteSourceConsumer"))
    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    atom = _state_source_atom(
        projection,
        state=state,
        demanding_rung=program.rungs[1],
        condition=Cmp(state.name, ">", 3),
    )
    deadline = next(read for read in projection.reads if occurrence_snapshot(read) == atom.deadline)
    source = next(
        write
        for write in projection.writes
        if isinstance(deadline.occurrence.source, WriteOccurrence)
        and write.occurrence is deadline.occurrence.source
    )
    return projection, atom, deadline, source


def _replace_read(
    projection: ScanRungWriteProjection,
    selected: RungRead,
    replacement: RungRead | None,
) -> ScanRungWriteProjection:
    reads = tuple(
        candidate
        for read in projection.reads
        for candidate in (
            () if read is selected and replacement is None else (replacement or read,)
        )
    )
    return replace(projection, reads=reads)


@pytest.mark.parametrize("damage", ("missing", "duplicate", "pending", "duplicate-source"))
def test_walk_fails_closed_on_ambiguous_or_indirect_projection_evidence(
    damage: str,
) -> None:
    projection, atom, deadline, source = _one_hop_projection()
    if damage == "missing":
        damaged = _replace_read(projection, deadline, None)
    elif damage == "duplicate":
        damaged = replace(projection, reads=(*projection.reads, deadline))
    elif damage == "pending":
        pending = replace(
            deadline,
            occurrence=replace(deadline.occurrence, source="pending"),
        )
        damaged = _replace_read(projection, deadline, pending)
    else:
        damaged = replace(projection, writes=(*projection.writes, source))

    walk = derive_occurrence_source_requirement(atom, damaged)

    assert walk.status is RequirementSourceWalkStatus.INCOMPLETE
    assert walk.condition == atom
    assert walk.detail


def test_walk_fails_closed_on_an_unconditional_source_and_non_decreasing_source() -> None:
    state = Int("UnconditionalSourceState", default=8)
    with Program() as program:
        with rung():
            copy(4, state)
        with rung(state <= 4):
            copy(1, Int("UnconditionalSourceConsumer"))
    plc = PLC(program)
    plc.step()
    projection = plc._replay_rung_write_projection_at(1)
    assert projection is not None
    atom = _state_source_atom(
        projection,
        state=state,
        demanding_rung=program.rungs[1],
        condition=Cmp(state.name, ">", 4),
    )

    unconditional = derive_occurrence_source_requirement(atom, projection)
    assert unconditional.status is RequirementSourceWalkStatus.INCOMPLETE
    assert unconditional.condition == atom

    deadline = next(read for read in projection.reads if occurrence_snapshot(read) == atom.deadline)
    source = cast(WriteOccurrence, deadline.occurrence.source)
    late_source = replace(source, ordinal=deadline.ordinal)
    late_read = replace(
        deadline,
        occurrence=replace(deadline.occurrence, source=late_source),
    )
    source_write = next(write for write in projection.writes if write.occurrence is source)
    late_write = replace(
        source_write,
        ordinal=deadline.ordinal,
        occurrence=late_source,
        transition=replace(
            source_write.transition,
            occurrence_ordinal=deadline.ordinal,
        ),
    )
    damaged = replace(
        _replace_read(projection, deadline, late_read),
        writes=tuple(late_write if write is source_write else write for write in projection.writes),
    )

    non_decreasing = derive_occurrence_source_requirement(atom, damaged)
    assert non_decreasing.status is RequirementSourceWalkStatus.INCOMPLETE
    assert "earlier" in non_decreasing.detail or "ordinal" in non_decreasing.detail


def test_walk_fails_closed_when_constraint_tag_does_not_match_deadline_read() -> None:
    projection, atom, _deadline, _source = _one_hop_projection()
    mismatched = replace(
        atom,
        condition=Cmp("MismatchedSourceState", ">", 3),
    )

    walk = derive_occurrence_source_requirement(mismatched, projection)

    assert walk.status is RequirementSourceWalkStatus.INCOMPLETE
    assert walk.condition == mismatched
    assert "exact tag read" in walk.detail


def test_source_walk_contract_is_report_only_and_retains_no_future() -> None:
    projection, atom, _deadline, _source = _one_hop_projection()
    before = (dict(projection.entry_tags), dict(projection.exit_tags))

    walk = derive_occurrence_source_requirement(atom, projection)

    assert isinstance(walk, RequirementSourceWalk)
    assert before == (dict(projection.entry_tags), dict(projection.exit_tags))
    forbidden = {
        "action",
        "bearing",
        "candidate",
        "cursor",
        "future",
        "overlay",
        "predicted_world",
        "route_suffix",
    }
    assert forbidden.isdisjoint(field.name for field in fields(walk))
    assert all(forbidden.isdisjoint(field.name for field in fields(link)) for link in walk.links)


def test_existing_one_hop_facade_matches_the_typed_walk() -> None:
    from pyrung.core.analysis.pilot.requirements import _refine_preserved_tag_deadlines

    projection, atom, _deadline, _source = _one_hop_projection()
    typed = derive_occurrence_source_requirement(atom, projection)

    assert typed.status is RequirementSourceWalkStatus.COMPLETE
    legacy = _refine_preserved_tag_deadlines(atom, projection, ())
    assert legacy is atom
    assert isinstance(legacy, GuardRequirementAtom)
    assert legacy.source_links == ()
    assert isinstance(atom.condition, Cmp)
    strengthened = _refine_preserved_tag_deadlines(
        atom,
        projection,
        ((atom.condition.tag, projection.entry_tags[atom.condition.tag]),),
    )
    assert isinstance(strengthened, GuardRequirementAtom)
    assert strengthened.source_links == ()
    assert isinstance(typed.condition, GuardRequirementAtom)
    stage1_condition = replace(typed.condition, source_links=())
    assert strengthened == stage1_condition

    epoch = object()
    active = ActiveRequirement(
        condition=strengthened,
        demanding_occurrence=atom.deadline,
        deadline=atom.deadline,
        selected_writer=(None, 0, ()),
        operand_authority=OperandAuthority.UNKNOWN,
        execution_epoch=epoch,
        execution_owner=SimpleNamespace(epoch=epoch),
        source_world_key=("stage1-navigation",),
        checkpoint_owner=object(),
        source_checkpoint=object(),
    )
    expected = replace(active, condition=stage1_condition)
    transitive = replace(active, condition=typed.condition)
    assert active.navigation_identity == expected.navigation_identity
    assert active.navigation_identity == transitive.navigation_identity
