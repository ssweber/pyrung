"""Bounded, production-inert one-scan closure contracts."""

from __future__ import annotations

from dataclasses import fields, replace
from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import (
    PLC,
    And,
    Bool,
    Int,
    Or,
    Program,
    call,
    copy,
    latch,
    reset,
    rise,
    rung,
    subroutine,
)
from pyrung.core.analysis.pilot.effects import (
    ConsumerBoundary,
    EffectExpectation,
    EffectObligation,
    EffectObservation,
    EffectPolarity,
    consumer_boundary_reached,
    consumer_stop_reached,
    displacement_consumer_read,
    occurrence_selector,
    occurrence_snapshot,
    resolve_occurrence_selector,
)
from pyrung.core.analysis.pilot.execution import CheckpointRef
from pyrung.core.analysis.pilot.intrascan import (
    IntrascanAttempt,
    IntrascanClosureQuestion,
    IntrascanClosureResult,
    IntrascanClosureStatus,
    IntrascanRequirementDisposition,
    IntrascanWitness,
    build_intrascan_requirement_evidence,
    close_intrascan,
    draft_overlay_from_selected_actions,
    producer_guard_candidate_overlays,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    BearingObjective,
    ChannelHeading,
    TargetSpec,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
)
from pyrung.core.analysis.pilot.working_theory import (
    TheoryInvariantError,
    assert_detached_theory_value,
    theory_boundary_claim,
    theory_boundary_from_checkpoint,
    theory_claim_from_intrascan_witness,
)
from pyrung.core.analysis.pilot.world_key import _semantic_key
from pyrung.core.crossing import Cmp


def _checkpoint(
    source: PLC,
    label: str,
    pilot_rungs: tuple[PilotRung, ...] = (),
) -> Any:
    return SimpleNamespace(
        owner=object(),
        key=(label, source.state.scan_id),
        world=SimpleNamespace(work=source, pilot_rungs=pilot_rungs),
    )


def _projection(plc: PLC):
    projection = plc._replay_rung_write_projection_at(plc.state.scan_id)
    assert projection is not None
    return projection


def _produce(program: Program, rung_index: int, tag: Int, value: int) -> EffectObligation:
    return EffectObligation(
        tag.name,
        value,
        (None, rung_index, ()),
        None,
        (),
        producer_rung=program.rungs[rung_index],
    )


def test_consumer_boundary_matches_the_exact_producer_sourced_read() -> None:
    request = Bool("ConsumerBoundaryRequest", external=True)
    step = Int("ConsumerBoundaryStep")
    seen = Bool("ConsumerBoundarySeen")
    with Program() as program:
        with rung(request):
            copy(41, step)
        with rung(step == 41):
            latch(seen)

    plc = PLC(program)
    plc.patch({request.name: True})
    plc.step()
    projection = _projection(plc)
    produced = next(write for write in projection.writes if write.transition.tag_name == step.name)
    consumed = next(
        read
        for read in projection.reads
        if read.occurrence.name == step.name
        and projection.transition_observed_by_read(read) is not None
    )
    producer_selector = occurrence_selector(projection, produced)
    consumer_selector = occurrence_selector(projection, consumed)
    assert producer_selector is not None
    assert consumer_selector is not None
    boundary = ConsumerBoundary(
        produced_occurrence=occurrence_snapshot(produced),
        consumer_occurrence=occurrence_snapshot(consumed),
        producer_selector=producer_selector,
        consumer_selector=consumer_selector,
        producer_scan_offset=1,
        consumer_scan_offset=1,
    )

    assert resolve_occurrence_selector(projection, producer_selector) is produced
    assert resolve_occurrence_selector(projection, consumer_selector) is consumed
    assert consumer_boundary_reached(
        boundary,
        source_scan=0,
        projection_at=lambda scan_id: projection if scan_id == 1 else None,
    )


def test_consumer_boundary_proves_one_retained_cross_scan_handoff() -> None:
    produce = Bool("CrossScanProduce", external=True)
    consume = Bool("CrossScanConsume", external=True)
    step = Int("CrossScanStep")
    seen = Bool("CrossScanSeen")
    with Program() as program:
        with rung(produce):
            copy(41, step)
        with rung(consume, step == 41):
            latch(seen)

    plc = PLC(program)
    plc.patch({produce.name: True, consume.name: False})
    plc.step()
    produced_projection = _projection(plc)
    produced = next(
        write for write in produced_projection.writes if write.transition.tag_name == step.name
    )
    plc.patch({produce.name: False, consume.name: True})
    plc.step()
    consumer_projection = _projection(plc)
    consumed = next(read for read in consumer_projection.reads if read.occurrence.name == step.name)
    producer_selector = occurrence_selector(produced_projection, produced)
    consumer_selector = occurrence_selector(consumer_projection, consumed)
    assert producer_selector is not None
    assert consumer_selector is not None
    boundary = ConsumerBoundary(
        produced_occurrence=occurrence_snapshot(produced),
        consumer_occurrence=occurrence_snapshot(consumed),
        producer_selector=producer_selector,
        consumer_selector=consumer_selector,
        producer_scan_offset=1,
        consumer_scan_offset=2,
    )
    projections = {
        produced_projection.scan_id: produced_projection,
        consumer_projection.scan_id: consumer_projection,
    }

    assert consumer_boundary_reached(
        boundary,
        source_scan=0,
        projection_at=projections.get,
    )

    replay = PLC(program)
    replay.step()
    replay.patch({step.name: 50, consume.name: True})
    replay.step()
    changed_consumer_projection = _projection(replay)
    changed_projections = {changed_consumer_projection.scan_id: changed_consumer_projection}

    assert consumer_stop_reached(
        boundary,
        source_scan=0,
        projection_at=changed_projections.get,
    )
    assert not consumer_boundary_reached(
        boundary,
        source_scan=0,
        projection_at=changed_projections.get,
    )


def test_displacement_guard_names_one_cross_scan_consumer() -> None:
    produce = Bool("DisplacementProduce", external=True)
    displace = Bool("DisplacementEnable", external=True)
    step = Int("DisplacementStep")
    with Program() as program:
        with rung(produce):
            copy(41, step)
        with rung(displace, step == 41):
            copy(94, step)

    plc = PLC(program)
    plc.patch({produce.name: True, displace.name: False})
    plc.step()
    produced_projection = _projection(plc)
    produced = next(
        write for write in produced_projection.writes if write.transition.tag_name == step.name
    )
    plc.patch({produce.name: False, displace.name: True})
    plc.step()
    displacement_projection = _projection(plc)
    displaced = next(
        write for write in displacement_projection.writes if write.transition.tag_name == step.name
    )
    enabling_reads = displacement_projection.enabling_read_closure_observed_by_write(displaced)
    observation = EffectObservation(
        obligation=_produce(program, 0, step, 41),
        disposition="OVERWRITTEN",
        appeared=produced,
        displacement=displaced,
        displacement_enabling_reads=enabling_reads,
    )

    consumer = displacement_consumer_read(observation)
    assert consumer is not None
    assert consumer.occurrence.name == step.name
    assert consumer.occurrence.value == 41


def _prevent(
    program: Program,
    projection: Any,
    rung_index: int,
    tag: Int,
    value: int,
) -> EffectObligation:
    writes = tuple(
        write
        for write in projection.writes
        if write.run.rung is program.rungs[rung_index]
        and write.transition.tag_name == tag.name
        and write.transition.to_value == value
    )
    assert len(writes) == 1
    selector = occurrence_selector(projection, writes[0])
    assert selector is not None
    return EffectObligation(
        tag.name,
        value,
        (None, rung_index, ()),
        None,
        (),
        producer_rung=program.rungs[rung_index],
        polarity=EffectPolarity.PREVENT,
        occurrence_selector=selector,
    )


def _guard_evidence(
    source: PLC,
    evidence_plc: PLC,
    condition: GuardRequirementAtom | GuardRequirementExpr,
    label: str,
    *,
    steerable: frozenset[str],
    demanding_occurrence: Any | None = None,
    pilot_rungs: tuple[PilotRung, ...] = (),
):
    atoms: list[GuardRequirementAtom] = []

    def collect(term: GuardRequirementAtom | GuardRequirementExpr) -> None:
        if isinstance(term, GuardRequirementAtom):
            atoms.append(term)
        else:
            for child in term.terms:
                collect(child)

    collect(condition)
    checkpoint = _checkpoint(source, label, pilot_rungs)
    epoch = object()
    requirement = ActiveRequirement(
        condition=condition,
        demanding_occurrence=demanding_occurrence or atoms[0].deadline,
        deadline=atoms[0].deadline,
        selected_writer=(None, 0, ()),
        operand_authority=OperandAuthority.UNKNOWN,
        execution_epoch=epoch,
        execution_owner=SimpleNamespace(epoch=epoch),
        source_world_key=checkpoint.key,
        checkpoint_owner=checkpoint.owner,
        source_checkpoint=checkpoint,
    )
    evidence = build_intrascan_requirement_evidence(
        requirement,
        _projection(evidence_plc),
        steerable=steerable,
    )
    assert evidence.complete
    return checkpoint, evidence


def _read_atom(program: Program, projection: Any, rung_index: int, tag: Bool, path: int):
    reads = tuple(
        read
        for read in projection.reads
        if read.run.rung is program.rungs[rung_index] and read.occurrence.name == tag.name
    )
    assert len(reads) == 1
    snapshot = occurrence_snapshot(reads[0])
    return GuardRequirementAtom(
        Cmp(tag.name, "==", True),
        (snapshot,),
        snapshot,
        (path,),
        demanding_rung=program.rungs[rung_index],
    )


TwoProducerPermit = Bool("IntrascanTwoProducerPermit", external=True)
TwoOverwritePermit = Bool("IntrascanTwoOverwritePermit", external=True, default=True)
TwoFixedInput = Bool("IntrascanTwoFixedInput", external=True)
TwoStepper = Int("IntrascanTwoStepper")

with Program() as two_program:
    with rung(TwoProducerPermit):
        copy(1, TwoStepper)
    with rung(TwoOverwritePermit):
        copy(0, TwoStepper)
    with rung(TwoFixedInput, TwoStepper == 99):
        copy(2, TwoStepper)


def _two_expectation() -> EffectExpectation:
    baseline = PLC(two_program)
    baseline.patch({TwoProducerPermit.name: True})
    baseline.step()
    return EffectExpectation(
        (
            _produce(two_program, 0, TwoStepper, 1),
            _prevent(two_program, _projection(baseline), 1, TwoStepper, 0),
        )
    )


def test_two_component_produce_prevent_witness_requires_the_joint_overlay() -> None:
    source = PLC(two_program)
    fixed = (PilotRung(TwoFixedInput.name, True, TwoStepper == 99),)
    checkpoint = _checkpoint(source, "two-component", fixed)
    before_state = source.state
    question = IntrascanClosureQuestion(
        source_checkpoint=checkpoint,
        expectation=_two_expectation(),
        steady_guard=TwoStepper != 1,
        fixed_pilot_rungs=fixed,
        candidate_overlays=(
            ((TwoProducerPermit.name, True),),
            ((TwoOverwritePermit.name, False),),
            (
                (TwoProducerPermit.name, True),
                (TwoOverwritePermit.name, False),
            ),
        ),
        budget=3,
    )

    result = close_intrascan(question)

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    assert [attempt.witnessed for attempt in result.attempts] == [False, False, True]
    assert result.witness.overlay.assignments == (
        (TwoProducerPermit.name, True),
        (TwoOverwritePermit.name, False),
    )
    assert [item.disposition for item in result.witness.observations] == [
        "SURVIVED",
        "PREVENTED",
    ]
    assert result.witness.overlay.pilot_rungs == fixed
    assert source.state is before_state
    assert source.state.scan_id == 0
    assert question.fixed_pilot_rungs == fixed


def test_failed_disposable_attempt_reports_exact_overwriter_requirement() -> None:
    source = PLC(two_program)
    checkpoint = _checkpoint(source, "failed-attempt-report")
    source_state = source.state

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation((_produce(two_program, 0, TwoStepper, 1),)),
            steady_guard=TwoStepper != 1,
            draft_assignments=((TwoProducerPermit.name, True),),
            program=two_program,
            steerable=frozenset(
                (TwoProducerPermit.name, TwoOverwritePermit.name, TwoFixedInput.name)
            ),
            program_written=frozenset((TwoStepper.name,)),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.INCOMPLETE
    assert result.witness is None
    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.witnessed is False
    assert [item.disposition for item in attempt.observations] == ["OVERWRITTEN"]
    assert len(attempt.findings) == 1
    finding = attempt.findings[0]
    requirement = finding.derivation.requirement
    assert requirement is not None
    assert requirement.source_checkpoint is checkpoint
    assert requirement.checkpoint_owner is checkpoint.owner
    assert requirement.execution_epoch is finding.observation.execution_epoch
    assert requirement.execution_owner is finding.observation.execution_owner
    assert requirement.demanding_occurrence == requirement.deadline
    assert requirement.demanding_occurrence == occurrence_snapshot(
        finding.observation.observed_reads[0]
    )
    assert requirement.scope == (
        ("overwriter_guard", occurrence_snapshot(finding.observation.displacement)),
    )
    assert TwoOverwritePermit.name in {
        atom.condition.tag
        for atom in getattr(requirement.condition, "terms", (requirement.condition,))
    }
    assert source.state is source_state
    assert source.state.scan_id == 0


def test_witness_claim_records_prevention_without_inventing_an_occurrence() -> None:
    source = PLC(two_program)
    checkpoint = _checkpoint(source, "prevented-claim")
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=_two_expectation(),
            steady_guard=TwoStepper != 1,
            candidate_overlays=(
                (
                    (TwoProducerPermit.name, True),
                    (TwoOverwritePermit.name, False),
                ),
            ),
            budget=1,
        )
    )

    assert result.witness is not None
    claim = theory_claim_from_intrascan_witness(
        result.witness,
        BearingObjective(TargetSpec(TwoStepper.name, 1)),
        theory_boundary_from_checkpoint(checkpoint),
    )
    selected = claim.selected_boundary.occurrence_identity[1]
    produced, prevented = selected

    assert produced[5] is not None
    assert prevented[3] is not None  # Static relocatable selector.
    assert prevented[4] == "PREVENTED"
    assert prevented[5] is None  # Absence proof cannot invent a dynamic write.
    assert claim.selected_boundary.execution_ref == result.witness.execution_ref
    assert_detached_theory_value(claim, path="claim")


def test_cross_scan_boundary_claim_requires_and_retains_exact_live_owner() -> None:
    source = PLC(two_program)
    source.step()
    boundary = theory_boundary_from_checkpoint(_checkpoint(source, "cross-scan-owner"))

    assert boundary.execution_ref is not None
    claim = theory_boundary_claim(
        BearingObjective(TargetSpec(TwoStepper.name, 1)),
        boundary,
        ChannelHeading(TwoStepper.name, 1),
    )

    assert claim.source.execution_ref == boundary.execution_ref
    assert claim.selected_boundary.execution_ref == boundary.execution_ref
    assert_detached_theory_value(claim, path="cross_scan_claim")
    with pytest.raises(TheoryInvariantError, match="owner is unavailable"):
        theory_boundary_claim(
            BearingObjective(TargetSpec(TwoStepper.name, 1)),
            replace(boundary, scan_id=0, owner_ref=CheckpointRef(1_000_003)),
            ChannelHeading(TwoStepper.name, 1),
        )


ThreeProducerPermit = Bool("IntrascanThreeProducerPermit", external=True)
ThreeFirstOverwrite = Bool("IntrascanThreeFirstOverwrite", external=True, default=True)
ThreeSecondOverwrite = Bool("IntrascanThreeSecondOverwrite", external=True, default=True)
ThreeStepper = Int("IntrascanThreeStepper")

with Program() as three_program:
    with rung(ThreeProducerPermit):
        copy(3, ThreeStepper)
    with rung(ThreeFirstOverwrite):
        copy(1, ThreeStepper)
    with rung(ThreeSecondOverwrite):
        copy(2, ThreeStepper)


def _three_expectation() -> EffectExpectation:
    baseline = PLC(three_program)
    baseline.patch({ThreeProducerPermit.name: True})
    baseline.step()
    projection = _projection(baseline)
    return EffectExpectation(
        (
            _produce(three_program, 0, ThreeStepper, 3),
            _prevent(three_program, projection, 1, ThreeStepper, 1),
            _prevent(three_program, projection, 2, ThreeStepper, 2),
        )
    )


def test_three_component_atomic_witness_rejects_every_supplied_pair() -> None:
    source = PLC(three_program)
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "three-component"),
            expectation=_three_expectation(),
            steady_guard=ThreeStepper != 3,
            candidate_overlays=(
                ((ThreeProducerPermit.name, True),),
                (
                    (ThreeProducerPermit.name, True),
                    (ThreeFirstOverwrite.name, False),
                ),
                (
                    (ThreeProducerPermit.name, True),
                    (ThreeSecondOverwrite.name, False),
                ),
                (
                    (ThreeProducerPermit.name, True),
                    (ThreeFirstOverwrite.name, False),
                    (ThreeSecondOverwrite.name, False),
                ),
            ),
            budget=4,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    assert [attempt.witnessed for attempt in result.attempts] == [False, False, False, True]
    assert result.witness.overlay.assignments == (
        (ThreeProducerPermit.name, True),
        (ThreeFirstOverwrite.name, False),
        (ThreeSecondOverwrite.name, False),
    )
    assert [item.disposition for item in result.witness.observations] == [
        "SURVIVED",
        "PREVENTED",
        "PREVENTED",
    ]
    assert source.state.scan_id == 0


WrongSiblingPermit = Bool("IntrascanWrongSiblingPermit", external=True, default=True)
WrongSelectedPermit = Bool("IntrascanWrongSelectedPermit", external=True)
WrongProducerStepper = Int("IntrascanWrongProducerStepper")

with Program() as wrong_producer_program:
    with rung(WrongSiblingPermit):
        copy(1, WrongProducerStepper)
    with rung(WrongSelectedPermit):
        copy(1, WrongProducerStepper)


def test_endpoint_success_from_a_wrong_producer_is_not_an_exact_witness() -> None:
    source = PLC(wrong_producer_program)
    source_before = source.state
    expectation = EffectExpectation((_produce(wrong_producer_program, 1, WrongProducerStepper, 1),))
    endpoint_only = source.fork()
    endpoint_only.step()
    assert endpoint_only.state.tags[WrongProducerStepper.name] == 1

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "wrong-producer"),
            expectation=expectation,
            steady_guard=WrongProducerStepper != 1,
            candidate_overlays=(
                (),
                (
                    (WrongSiblingPermit.name, False),
                    (WrongSelectedPermit.name, True),
                ),
            ),
            budget=2,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert len(result.attempts) == 2
    assert result.attempts[0].observations[0].disposition == "ABSENT"
    assert result.attempts[0].witnessed is False
    assert result.attempts[1].observations[0].disposition == "SURVIVED"
    assert result.attempts[1].witnessed is True
    assert source.state is source_before


ConflictFirstPath = Bool("IntrascanConflictFirstPath", external=True)
ConflictSecondPath = Bool("IntrascanConflictSecondPath", external=True)
ConflictStepper = Int("IntrascanConflictStepper")

with Program() as conflict_program:
    with rung(Or(ConflictFirstPath, ConflictSecondPath)):
        copy(1, ConflictStepper)


def test_one_conflicting_composite_is_rejected_without_rejecting_its_sibling() -> None:
    source = PLC(conflict_program)
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "composite-conflict"),
            expectation=EffectExpectation((_produce(conflict_program, 0, ConflictStepper, 1),)),
            steady_guard=ConflictStepper != 1,
            draft_assignments=((ConflictFirstPath.name, False),),
            candidate_overlays=(
                ((ConflictFirstPath.name, True),),
                ((ConflictSecondPath.name, True),),
            ),
            budget=2,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert len(result.attempts) == 2
    assert result.attempts[0].witnessed is False
    assert result.attempts[0].detail == "composite assignments conflict"
    assert result.attempts[0].observations == ()
    assert result.attempts[1].witnessed is True
    assert result.witness is not None
    assert result.witness.overlay.assignments == (
        (ConflictFirstPath.name, False),
        (ConflictSecondPath.name, True),
    )


def test_duplicate_candidate_is_suppressed_and_budget_exhaustion_is_not_impossibility() -> None:
    source = PLC(wrong_producer_program)
    expectation = EffectExpectation((_produce(wrong_producer_program, 1, WrongProducerStepper, 1),))
    duplicate = ((WrongSiblingPermit.name, True),)
    witness = (
        (WrongSiblingPermit.name, False),
        (WrongSelectedPermit.name, True),
    )
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "deduplicated-budget"),
            expectation=expectation,
            steady_guard=WrongProducerStepper != 1,
            candidate_overlays=(duplicate, duplicate, witness),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.BUDGET_EXHAUSTED
    assert result.status is not IntrascanClosureStatus.IMPOSSIBLE
    assert result.witness is None
    assert len(result.attempts) == 1
    assert len(result.attempted_identities) == 1


NestedFirst = Bool("IntrascanNestedFirst", external=True)
NestedSecond = Bool("IntrascanNestedSecond", external=True)
NestedCommon = Bool("IntrascanNestedCommon", external=True)
NestedMarker = Int("IntrascanNestedMarker")
NestedStepper = Int("IntrascanNestedStepper")

with Program() as nested_program:
    with rung(NestedFirst):
        copy(1, NestedMarker)
    with rung(NestedSecond):
        copy(2, NestedMarker)
    with rung(NestedCommon):
        copy(3, NestedMarker)
    with rung(NestedSecond, NestedCommon):
        copy(1, NestedStepper)


def test_nested_all_any_closure_tries_only_joint_siblings_with_exact_atom_reads() -> None:
    evidence_plc = PLC(nested_program)
    evidence_plc.step()
    projection = _projection(evidence_plc)
    first = _read_atom(nested_program, projection, 0, NestedFirst, 0)
    second = _read_atom(nested_program, projection, 1, NestedSecond, 1)
    common = _read_atom(nested_program, projection, 2, NestedCommon, 2)
    condition = GuardRequirementExpr(
        GuardLogic.ALL,
        (GuardRequirementExpr(GuardLogic.ANY, (first, second)), common),
    )
    source = PLC(nested_program)
    checkpoint, evidence = _guard_evidence(
        source,
        evidence_plc,
        condition,
        "nested-all-any",
        steerable=frozenset({NestedFirst.name, NestedSecond.name, NestedCommon.name}),
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation((_produce(nested_program, 3, NestedStepper, 1),)),
            steady_guard=NestedStepper != 1,
            requirements=(evidence,),
            budget=2,
        )
    )

    expected = [
        frozenset(((NestedFirst.name, True), (NestedCommon.name, True))),
        frozenset(((NestedSecond.name, True), (NestedCommon.name, True))),
    ]
    assert [frozenset(attempt.overlay.assignments) for attempt in result.attempts] == expected
    assert all(len(attempt.overlay.assignments) == 2 for attempt in result.attempts)
    assert [attempt.witnessed for attempt in result.attempts] == [False, True]
    assert result.status is IntrascanClosureStatus.WITNESS
    for attempt in result.attempts:
        observation = attempt.requirement_observations[0]
        assert observation.disposition is IntrascanRequirementDisposition.SATISFIED
        assert [read.tag for read in observation.observed_reads] == [
            NestedFirst.name,
            NestedSecond.name,
            NestedCommon.name,
        ]
        assert len({read.dynamic_address for read in observation.observed_reads}) == 3


NestedJointFirst = Bool("IntrascanNestedJointFirst", external=True)
NestedJointSecond = Bool("IntrascanNestedJointSecond", external=True)
NestedSibling = Bool("IntrascanNestedSibling", external=True)
NestedJointMarker = Int("IntrascanNestedJointMarker")
NestedJointStepper = Int("IntrascanNestedJointStepper")

with Program() as nested_joint_program:
    with rung(NestedJointFirst):
        copy(1, NestedJointMarker)
    with rung(NestedJointSecond):
        copy(2, NestedJointMarker)
    with rung(NestedSibling):
        copy(3, NestedJointMarker)
    with rung(NestedSibling):
        copy(1, NestedJointStepper)


def test_nested_any_all_closure_keeps_joint_branch_and_atomic_sibling_separate() -> None:
    evidence_plc = PLC(nested_joint_program)
    evidence_plc.step()
    projection = _projection(evidence_plc)
    first = _read_atom(nested_joint_program, projection, 0, NestedJointFirst, 0)
    second = _read_atom(nested_joint_program, projection, 1, NestedJointSecond, 1)
    sibling = _read_atom(nested_joint_program, projection, 2, NestedSibling, 2)
    condition = GuardRequirementExpr(
        GuardLogic.ANY,
        (GuardRequirementExpr(GuardLogic.ALL, (first, second)), sibling),
    )
    source = PLC(nested_joint_program)
    checkpoint, evidence = _guard_evidence(
        source,
        evidence_plc,
        condition,
        "nested-any-all",
        steerable=frozenset({NestedJointFirst.name, NestedJointSecond.name, NestedSibling.name}),
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation(
                (_produce(nested_joint_program, 3, NestedJointStepper, 1),)
            ),
            steady_guard=NestedJointStepper != 1,
            requirements=(evidence,),
            budget=2,
        )
    )

    assert [frozenset(attempt.overlay.assignments) for attempt in result.attempts] == [
        frozenset(((NestedJointFirst.name, True), (NestedJointSecond.name, True))),
        frozenset(((NestedSibling.name, True),)),
    ]
    assert all(len(attempt.overlay.assignments) < 3 for attempt in result.attempts)
    assert [attempt.witnessed for attempt in result.attempts] == [False, True]
    assert result.status is IntrascanClosureStatus.WITNESS
    assert all(
        item.disposition is IntrascanRequirementDisposition.SATISFIED
        for attempt in result.attempts
        for item in attempt.requirement_observations
    )


EarlyTruePermit = Bool("IntrascanEarlyTruePermit", external=True)
EarlyTrueStepper = Int("IntrascanEarlyTrueStepper")

with Program() as early_true_program:
    with rung(EarlyTruePermit):
        copy(1, EarlyTrueStepper)
    with rung():
        reset(EarlyTruePermit)


def test_requirement_is_satisfied_at_its_exact_read_even_when_exit_is_false() -> None:
    evidence_plc = PLC(early_true_program)
    evidence_plc.patch({EarlyTruePermit.name: True})
    evidence_plc.step()
    projection = _projection(evidence_plc)
    atom = _read_atom(early_true_program, projection, 0, EarlyTruePermit, 0)
    source = PLC(early_true_program)
    checkpoint, evidence = _guard_evidence(
        source,
        evidence_plc,
        atom,
        "deadline-before-false-exit",
        steerable=frozenset({EarlyTruePermit.name}),
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation((_produce(early_true_program, 0, EarlyTrueStepper, 1),)),
            steady_guard=EarlyTrueStepper != 1,
            requirements=(evidence,),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    requirement = result.witness.requirement_observations[0]
    assert requirement.disposition is IntrascanRequirementDisposition.SATISFIED
    assert requirement.observed_reads[0].values == (True,)
    claim = theory_claim_from_intrascan_witness(
        result.witness,
        BearingObjective(TargetSpec(EarlyTrueStepper.name, 1)),
        theory_boundary_from_checkpoint(checkpoint),
    )
    retained_requirement = claim.selected_boundary.occurrence_identity[2][0]
    assert requirement.requirement_identity == evidence.identity
    assert retained_requirement[1] == _semantic_key(evidence.identity)
    assert retained_requirement[3][0][5] == requirement.observed_reads[0].dynamic_address
    landing = source.fork()
    landing.patch(dict(result.witness.overlay.assignments))
    landing.step()
    assert landing.state.tags[EarlyTruePermit.name] is False


LateTruePermit = Bool("IntrascanLateTruePermit", external=True)
LateTrueMarker = Int("IntrascanLateTrueMarker")
LateTrueStepper = Int("IntrascanLateTrueStepper")

with Program() as late_true_program:
    with rung():
        reset(LateTruePermit)
    with rung(LateTruePermit):
        copy(1, LateTrueMarker)
    with rung():
        copy(1, LateTrueStepper)
    with rung():
        latch(LateTruePermit)


def test_requirement_is_violated_at_its_exact_read_even_when_exit_is_true() -> None:
    evidence_plc = PLC(late_true_program)
    evidence_plc.step()
    projection = _projection(evidence_plc)
    atom = _read_atom(late_true_program, projection, 1, LateTruePermit, 0)
    source = PLC(late_true_program)
    checkpoint, evidence = _guard_evidence(
        source,
        evidence_plc,
        atom,
        "deadline-before-true-exit",
        steerable=frozenset({LateTruePermit.name}),
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation((_produce(late_true_program, 2, LateTrueStepper, 1),)),
            steady_guard=LateTrueStepper != 1,
            requirements=(evidence,),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.INCOMPLETE
    assert result.status is not IntrascanClosureStatus.IMPOSSIBLE
    assert result.witness is None
    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.observations[0].disposition == "SURVIVED"
    assert attempt.requirement_observations[0].disposition is (
        IntrascanRequirementDisposition.VIOLATED
    )
    assert attempt.requirement_observations[0].observed_reads[0].values == (False,)
    landing = source.fork()
    landing.patch(dict(attempt.overlay.assignments))
    landing.step()
    assert landing.state.tags[LateTruePermit.name] is True


DemandPermit = Bool("IntrascanDemandPermit", external=True)
DemandCallPermit = Bool("IntrascanDemandCallPermit", external=True)
DemandMarker = Int("IntrascanDemandMarker")
DemandStepper = Int("IntrascanDemandStepper")


@subroutine("IntrascanDemandConsumer", strict=False)
def demand_consumer() -> None:
    with rung(DemandPermit):
        copy(2, DemandMarker)


with Program() as demanding_program:
    with rung(DemandPermit):
        copy(1, DemandMarker)
    with rung(DemandCallPermit):
        call(demand_consumer)
    with rung():
        copy(1, DemandStepper)


def test_true_deadline_read_without_the_exact_demanding_occurrence_cannot_witness() -> None:
    evidence_plc = PLC(demanding_program)
    evidence_plc.patch({DemandPermit.name: True, DemandCallPermit.name: True})
    evidence_plc.step()
    projection = _projection(evidence_plc)
    atom = _read_atom(demanding_program, projection, 0, DemandPermit, 0)
    demanding_reads = tuple(
        read
        for read in projection.reads
        if read.run.rung is demanding_program.subroutines["IntrascanDemandConsumer"][0]
        and read.occurrence.name == DemandPermit.name
    )
    assert len(demanding_reads) == 1
    demanding_occurrence = occurrence_snapshot(demanding_reads[0])
    source = PLC(demanding_program)
    checkpoint, evidence = _guard_evidence(
        source,
        evidence_plc,
        atom,
        "missing-demanding-occurrence",
        steerable=frozenset({DemandPermit.name, DemandCallPermit.name}),
        demanding_occurrence=demanding_occurrence,
    )
    candidate = source.fork()
    candidate.patch({DemandPermit.name: True, DemandCallPermit.name: False})
    candidate.step()
    candidate_projection = candidate._replay_pilot_rung_write_projection_at(candidate.state.scan_id)
    assert candidate_projection is not None
    assert [
        read.occurrence.value
        for read in candidate_projection.reads
        if occurrence_selector(candidate_projection, read) == evidence.atoms[0].selector
    ] == [True]
    assert not any(
        occurrence_selector(candidate_projection, read) == evidence.demanding_selector
        for read in candidate_projection.reads
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation((_produce(demanding_program, 2, DemandStepper, 1),)),
            steady_guard=DemandStepper != 1,
            requirements=(evidence,),
            draft_assignments=((DemandCallPermit.name, False),),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.INCOMPLETE
    assert result.witness is None
    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.observations[0].disposition == "SURVIVED"
    observation = attempt.requirement_observations[0]
    assert observation.disposition is IntrascanRequirementDisposition.UNKNOWN
    assert "demanding" in observation.detail
    assert [read.values for read in observation.observed_reads] == [(True,)]


ShortCircuitState = Int("IntrascanShortCircuitState", external=True)
ShortCircuitPoison = Int("IntrascanShortCircuitPoison", external=True)
ShortCircuitMarker = Int("IntrascanShortCircuitMarker")
ShortCircuitStepper = Int("IntrascanShortCircuitStepper")

with Program() as short_circuit_program:
    with rung(ShortCircuitState == 2, ShortCircuitPoison == 1):
        copy(1, ShortCircuitMarker)
    with rung():
        copy(1, ShortCircuitStepper)


def test_decisive_same_guard_short_circuit_can_replace_later_demanding_read() -> None:
    evidence_plc = PLC(short_circuit_program)
    evidence_plc.patch({ShortCircuitState.name: 2, ShortCircuitPoison.name: 1})
    evidence_plc.step()
    projection = _projection(evidence_plc)
    state_read, poison_read = tuple(
        read for read in projection.reads if read.run.rung is short_circuit_program.rungs[0]
    )
    state_snapshot = occurrence_snapshot(state_read)
    poison_snapshot = occurrence_snapshot(poison_read)
    condition = GuardRequirementExpr(
        GuardLogic.ANY,
        (
            GuardRequirementAtom(
                Cmp(ShortCircuitState.name, "!=", 2),
                (state_snapshot,),
                state_snapshot,
                (0,),
                demanding_rung=short_circuit_program.rungs[0],
            ),
            GuardRequirementAtom(
                Cmp(ShortCircuitPoison.name, "!=", 1),
                (poison_snapshot,),
                poison_snapshot,
                (1,),
                demanding_rung=short_circuit_program.rungs[0],
            ),
        ),
    )
    source = PLC(short_circuit_program)
    checkpoint, evidence = _guard_evidence(
        source,
        evidence_plc,
        condition,
        "same-guard-short-circuit",
        steerable=frozenset(),
        demanding_occurrence=poison_snapshot,
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation(
                (_produce(short_circuit_program, 1, ShortCircuitStepper, 1),)
            ),
            steady_guard=ShortCircuitStepper != 1,
            requirements=(evidence,),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    observation = result.witness.requirement_observations[0]
    assert observation.disposition is IntrascanRequirementDisposition.SATISFIED
    assert [read.tag for read in observation.observed_reads] == [ShortCircuitState.name]
    assert observation.observed_reads[0].values == (0,)


SourceHeldPermit = Bool("IntrascanSourceHeldPermit", external=True)
SourceHeldStepper = Int("IntrascanSourceHeldStepper")

with Program() as source_held_program:
    with rung(SourceHeldPermit):
        copy(1, SourceHeldStepper)


def test_source_checkpoint_pilot_rungs_are_required_and_preserved_automatically() -> None:
    source = PLC(source_held_program)
    source_state = source.state
    required = PilotRung(
        SourceHeldPermit.name,
        True,
        SourceHeldStepper != 1,
    )
    checkpoint = _checkpoint(source, "source-held-overlay", (required,))
    expectation = EffectExpectation((_produce(source_held_program, 0, SourceHeldStepper, 1),))
    without_source_overlay = source.fork()
    without_source_overlay.step()
    assert without_source_overlay.state.tags[SourceHeldStepper.name] == 0

    inherited = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=expectation,
            steady_guard=SourceHeldStepper != 1,
            budget=1,
        )
    )

    assert inherited.status is IntrascanClosureStatus.WITNESS
    assert inherited.witness is not None
    assert inherited.witness.overlay.pilot_rungs == (required,)
    assert inherited.witness.added_pilot_rungs == ()
    assert inherited.witness.observations[0].disposition == "SURVIVED"
    assert source.state is source_state

    mismatched = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=expectation,
            steady_guard=SourceHeldStepper != 1,
            fixed_pilot_rungs=(
                PilotRung(
                    SourceHeldPermit.name,
                    False,
                    SourceHeldStepper != 1,
                ),
            ),
            budget=1,
        )
    )

    assert mismatched.status is IntrascanClosureStatus.INCOMPLETE
    assert mismatched.witness is None
    assert mismatched.attempts == ()
    assert "source" in mismatched.detail and "PilotRung" in mismatched.detail


MergeSourcePermit = Bool("IntrascanMergeSourcePermit", external=True)
MergeProposedPermit = Bool("IntrascanMergeProposedPermit", external=True)
MergeScheduledPermit = Bool("IntrascanMergeScheduledPermit", external=True)
MergeStepper = Int("IntrascanMergeStepper")

with Program() as merge_program:
    with rung(MergeSourcePermit, MergeProposedPermit, MergeScheduledPermit):
        copy(1, MergeStepper)


def test_proposed_and_scheduled_rungs_extend_exact_source_overlay_and_are_exposed() -> None:
    evidence_plc = PLC(merge_program)
    evidence_plc.patch(
        {
            MergeSourcePermit.name: True,
            MergeProposedPermit.name: True,
            MergeScheduledPermit.name: True,
        }
    )
    evidence_plc.step()
    projection = _projection(evidence_plc)
    scheduled_atom = _read_atom(
        merge_program,
        projection,
        0,
        MergeScheduledPermit,
        0,
    )
    source = PLC(merge_program)
    source_state = source.state
    source_rung = PilotRung(MergeSourcePermit.name, True, MergeStepper != 1)
    proposed_rung = PilotRung(MergeProposedPermit.name, True, MergeStepper != 1)
    checkpoint, guard_evidence = _guard_evidence(
        source,
        evidence_plc,
        scheduled_atom,
        "merge-source-proposed-scheduled",
        steerable=frozenset({MergeScheduledPermit.name}),
        pilot_rungs=(source_rung,),
    )
    scheduled_condition = Cmp(MergeScheduledPermit.name, "==", True)
    scheduled_requirement = replace(
        guard_evidence.requirement,
        condition=scheduled_condition,
        operand_authority=OperandAuthority.ADJUSTABLE,
    )
    scheduled_evidence = replace(
        guard_evidence,
        requirement=scheduled_requirement,
        condition=scheduled_condition,
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation((_produce(merge_program, 0, MergeStepper, 1),)),
            steady_guard=MergeStepper != 1,
            requirements=(scheduled_evidence,),
            fixed_pilot_rungs=(source_rung,),
            proposed_pilot_rungs=(proposed_rung,),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    installed = result.witness.overlay.pilot_rungs
    assert installed[:2] == (source_rung, proposed_rung)
    assert tuple(rung.dest for rung in installed) == (
        MergeSourcePermit.name,
        MergeProposedPermit.name,
        MergeScheduledPermit.name,
    )
    assert installed[2].value is True
    assert result.witness.added_pilot_rungs == (proposed_rung, installed[2])
    assert result.witness.observations[0].disposition == "SURVIVED"
    assert source.state is source_state


def test_proposed_rung_cannot_mask_an_exact_source_rung_mismatch() -> None:
    source = PLC(source_held_program)
    source_rung = PilotRung(SourceHeldPermit.name, True, SourceHeldStepper != 1)
    checkpoint = _checkpoint(source, "proposed-does-not-mask-source", (source_rung,))

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=EffectExpectation(
                (_produce(source_held_program, 0, SourceHeldStepper, 1),)
            ),
            steady_guard=SourceHeldStepper != 1,
            fixed_pilot_rungs=(),
            proposed_pilot_rungs=(source_rung,),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.INCOMPLETE
    assert result.witness is None
    assert result.attempts == ()
    assert "exact source" in result.detail


def test_finite_draft_overlay_adds_only_sound_steerable_shape_facts() -> None:
    obligation = replace(
        _produce(two_program, 0, TwoStepper, 1),
        required_shape=(
            (TwoProducerPermit.name, True),
            (TwoOverwritePermit.name, False),
            (TwoFixedInput.name, True),
        ),
    )
    expectation = EffectExpectation((obligation,))

    draft = draft_overlay_from_selected_actions(
        ((TwoProducerPermit.name, True),),
        expectation,
        steerable=frozenset({TwoOverwritePermit.name}),
        source_snapshot={TwoFixedInput.name: True},
    )

    assert draft.assignments == (
        (TwoProducerPermit.name, True),
        (TwoOverwritePermit.name, False),
    )
    assert draft.detail == ""


def test_finite_draft_overlay_defers_internal_shape_but_rejects_conflict() -> None:
    expectation = EffectExpectation(
        (
            replace(
                _produce(two_program, 0, TwoStepper, 1),
                required_shape=((TwoOverwritePermit.name, False),),
            ),
        )
    )

    configured_inputs = frozenset((TwoOverwritePermit.name,))
    unavailable = draft_overlay_from_selected_actions(
        (),
        expectation,
        steerable=frozenset((TwoOverwritePermit.name,)) - configured_inputs,
        source_snapshot={TwoOverwritePermit.name: True},
    )
    conflicting = draft_overlay_from_selected_actions(
        ((TwoOverwritePermit.name, True),),
        expectation,
        steerable=frozenset({TwoOverwritePermit.name}),
        source_snapshot={},
    )

    assert unavailable.assignments == ()
    assert unavailable.detail == ""
    assert conflicting.assignments is None
    assert "conflicts" in conflicting.detail


InternalGuardAction = Bool("IntrascanInternalGuardAction", external=True)
InternalGuardReady = Bool("IntrascanInternalGuardReady")
InternalGuardStepper = Int("IntrascanInternalGuardStepper")

with Program() as internal_guard_program:
    with rung(InternalGuardAction):
        latch(InternalGuardReady)
    with rung(InternalGuardReady):
        copy(1, InternalGuardStepper)


def test_internal_shape_and_guard_are_witnessed_when_produced_earlier_in_scan() -> None:
    source = PLC(internal_guard_program)
    expectation = EffectExpectation(
        (
            replace(
                _produce(internal_guard_program, 1, InternalGuardStepper, 1),
                required_shape=((InternalGuardReady.name, True),),
            ),
        )
    )
    draft = draft_overlay_from_selected_actions(
        ((InternalGuardAction.name, True),),
        expectation,
        steerable=frozenset((InternalGuardAction.name,)),
        source_snapshot=dict(source.state.tags),
    )
    assert draft.assignments == ((InternalGuardAction.name, True),)

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "internal-guard-produced"),
            expectation=expectation,
            steady_guard=None,
            draft_assignments=draft.assignments,
            producer_guard_rungs=(internal_guard_program.rungs[1],),
            producer_guard_steerable=frozenset((InternalGuardAction.name,)),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    assert result.witness.overlay.assignments == ((InternalGuardAction.name, True),)
    assert result.witness.observations[0].disposition == "SURVIVED"


def test_missing_internal_shape_and_guard_still_fail_closed_in_projection() -> None:
    source = PLC(internal_guard_program)
    expectation = EffectExpectation(
        (
            replace(
                _produce(internal_guard_program, 1, InternalGuardStepper, 1),
                required_shape=((InternalGuardReady.name, True),),
            ),
        )
    )
    draft = draft_overlay_from_selected_actions(
        (),
        expectation,
        steerable=frozenset((InternalGuardAction.name,)),
        source_snapshot=dict(source.state.tags),
    )
    assert draft.assignments == ()

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "internal-guard-missing"),
            expectation=expectation,
            steady_guard=None,
            draft_assignments=draft.assignments,
            producer_guard_rungs=(internal_guard_program.rungs[1],),
            producer_guard_steerable=frozenset((InternalGuardAction.name,)),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.INCOMPLETE
    assert result.witness is None
    assert len(result.attempts) == 1
    assert result.attempts[0].observations[0].disposition == "ABSENT"


ChartGuardFirst = Bool("ChartGuardFirst", external=True)
ChartGuardSecond = Bool("ChartGuardSecond", external=True)
ChartGuardThird = Bool("ChartGuardThird", external=True)
ChartGuardAlternative = Bool("ChartGuardAlternative", external=True)
ChartGuardStepper = Int("ChartGuardStepper")
ChartGuardAlternativeStepper = Int("ChartGuardAlternativeStepper")

with Program() as chart_guard_program:
    with rung(ChartGuardFirst, ChartGuardSecond, ChartGuardThird):
        copy(1, ChartGuardStepper)
    with rung(Or(And(ChartGuardFirst, ChartGuardSecond), ChartGuardAlternative)):
        copy(2, ChartGuardAlternativeStepper)


def test_chart_guard_closure_materializes_three_way_and_with_exact_projection() -> None:
    source = PLC(chart_guard_program)
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "chart-three-way-and"),
            expectation=EffectExpectation(
                (_produce(chart_guard_program, 0, ChartGuardStepper, 1),)
            ),
            steady_guard=None,
            producer_guard_rungs=(chart_guard_program.rungs[0],),
            producer_guard_steerable=frozenset(
                (ChartGuardFirst.name, ChartGuardSecond.name, ChartGuardThird.name)
            ),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    assert result.witness.overlay.assignments == (
        (ChartGuardFirst.name, True),
        (ChartGuardSecond.name, True),
        (ChartGuardThird.name, True),
    )
    assert result.witness.observations[0].disposition == "SURVIVED"


def test_chart_guard_closure_tries_or_alternatives_after_conflicting_and() -> None:
    source = PLC(chart_guard_program)
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "chart-or-alternatives"),
            expectation=EffectExpectation(
                (_produce(chart_guard_program, 1, ChartGuardAlternativeStepper, 2),)
            ),
            steady_guard=None,
            draft_assignments=((ChartGuardFirst.name, False),),
            producer_guard_rungs=(chart_guard_program.rungs[1],),
            producer_guard_steerable=frozenset(
                (ChartGuardFirst.name, ChartGuardSecond.name, ChartGuardAlternative.name)
            ),
            budget=2,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    assert [attempt.witnessed for attempt in result.attempts] == [False, True]
    assert "conflict" in result.attempts[0].detail
    assert result.witness.overlay.assignments == (
        (ChartGuardFirst.name, False),
        (ChartGuardAlternative.name, True),
    )


def test_chart_guard_conflict_rejects_only_the_composite_attempt() -> None:
    source = PLC(chart_guard_program)
    alternatives = producer_guard_candidate_overlays(
        (chart_guard_program.rungs[0],),
        source,
        selected_assignments=((ChartGuardFirst.name, False),),
        steerable=frozenset((ChartGuardFirst.name, ChartGuardSecond.name, ChartGuardThird.name)),
        limit=1,
    )
    assert alternatives.overlays is not None

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "chart-conflict"),
            expectation=EffectExpectation(
                (_produce(chart_guard_program, 0, ChartGuardStepper, 1),)
            ),
            steady_guard=None,
            draft_assignments=((ChartGuardFirst.name, False),),
            producer_guard_rungs=(chart_guard_program.rungs[0],),
            producer_guard_steerable=frozenset(
                (ChartGuardFirst.name, ChartGuardSecond.name, ChartGuardThird.name)
            ),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.INCOMPLETE
    assert result.witness is None
    assert len(result.attempts) == 1
    assert result.attempts[0].witnessed is False
    assert "conflict" in result.attempts[0].detail


UnsupportedGuardLeft = Int("IntrascanUnsupportedGuardLeft", external=True)
UnsupportedGuardRight = Int("IntrascanUnsupportedGuardRight", external=True)
UnsupportedGuardStepper = Int("IntrascanUnsupportedGuardStepper")

with Program() as unsupported_guard_program:
    with rung(UnsupportedGuardLeft == UnsupportedGuardRight):
        copy(1, UnsupportedGuardStepper)


def test_chart_guard_with_tag_operand_remains_fail_closed() -> None:
    source = PLC(unsupported_guard_program)

    result = producer_guard_candidate_overlays(
        (unsupported_guard_program.rungs[0],),
        source,
        steerable=frozenset((UnsupportedGuardLeft.name, UnsupportedGuardRight.name)),
        limit=1,
    )

    assert result.overlays is None
    assert "unsupported" in result.detail


RepeatedPositiveStepper = Int("IntrascanRepeatedPositiveStepper")


@subroutine("IntrascanRepeatedPositiveProducer", strict=False)
def repeated_positive_producer() -> None:
    with rung():
        copy(1, RepeatedPositiveStepper)


with Program() as repeated_positive_program:
    with rung():
        call(repeated_positive_producer)
        copy(0, RepeatedPositiveStepper)
        call(repeated_positive_producer)


def test_repeated_positive_obligation_is_fulfilled_by_one_surviving_occurrence() -> None:
    source = PLC(repeated_positive_program)
    expectation = EffectExpectation(
        (
            EffectObligation(
                RepeatedPositiveStepper.name,
                1,
                ("IntrascanRepeatedPositiveProducer", 0, ()),
                None,
                (),
                producer_rung=repeated_positive_program.subroutines[
                    "IntrascanRepeatedPositiveProducer"
                ][0],
            ),
        )
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "repeated-positive"),
            expectation=expectation,
            steady_guard=RepeatedPositiveStepper != 1,
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert result.witness is not None
    assert [item.disposition for item in result.witness.observations] == [
        "OVERWRITTEN",
        "SURVIVED",
    ]
    assert [item.appeared.call_invocation for item in result.witness.observations] == [0, 1]


def test_witness_claim_selects_exact_surviving_repeated_call() -> None:
    source = PLC(repeated_positive_program)
    checkpoint = _checkpoint(source, "repeated-positive-claim")
    expectation = EffectExpectation(
        (
            EffectObligation(
                RepeatedPositiveStepper.name,
                1,
                ("IntrascanRepeatedPositiveProducer", 0, ()),
                None,
                (),
                producer_rung=repeated_positive_program.subroutines[
                    "IntrascanRepeatedPositiveProducer"
                ][0],
            ),
        )
    )
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=checkpoint,
            expectation=expectation,
            steady_guard=RepeatedPositiveStepper != 1,
            budget=1,
        )
    )

    assert result.witness is not None
    source_boundary = theory_boundary_from_checkpoint(checkpoint)
    claim = theory_claim_from_intrascan_witness(
        result.witness,
        BearingObjective(TargetSpec(RepeatedPositiveStepper.name, 1)),
        source_boundary,
    )
    occurrence = claim.selected_boundary.occurrence_identity[1][0]
    producer = occurrence[5]

    assert producer is not None
    assert producer[5][5] == 1
    assert claim.source == source_boundary
    assert claim.selected_boundary.scan_id == result.witness.assertion_scan
    assert claim.selected_boundary.execution_ref == result.witness.execution_ref
    assert claim.selected_boundary.execution_ref != source_boundary.execution_ref
    assert_detached_theory_value(claim, path="claim")

    survived = next(item for item in result.witness.observations if item.disposition == "SURVIVED")
    ambiguous = replace(
        result.witness,
        observations=(*result.witness.observations, survived),
    )
    with pytest.raises(TheoryInvariantError, match="unambiguous"):
        theory_claim_from_intrascan_witness(
            ambiguous,
            BearingObjective(TargetSpec(RepeatedPositiveStepper.name, 1)),
            source_boundary,
        )


RepeatedConsumerStepper = Int("IntrascanRepeatedConsumerStepper")
RepeatedConsumerReceipt = Int("IntrascanRepeatedConsumerReceipt")


@subroutine("IntrascanRepeatedConsumer", strict=False)
def repeated_consumer() -> None:
    with rung():
        copy(1, RepeatedConsumerStepper)
    with rung(RepeatedConsumerStepper == 1):
        copy(1, RepeatedConsumerReceipt)


with Program() as repeated_consumer_program:
    with rung():
        call(repeated_consumer)
        copy(0, RepeatedConsumerStepper)
        call(repeated_consumer)


def test_repeated_surviving_consumers_are_incomplete_before_claim_admission() -> None:
    source = PLC(repeated_consumer_program)
    producer = repeated_consumer_program.subroutines["IntrascanRepeatedConsumer"][0]
    consumer = repeated_consumer_program.subroutines["IntrascanRepeatedConsumer"][1]
    expectation = EffectExpectation(
        (
            EffectObligation(
                RepeatedConsumerStepper.name,
                1,
                ("IntrascanRepeatedConsumer", 0, ()),
                ("IntrascanRepeatedConsumer", 1, ()),
                ((RepeatedConsumerStepper.name, 1),),
                producer_rung=producer,
                consumer_rung=consumer,
            ),
        )
    )

    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "repeated-consumer"),
            expectation=expectation,
            steady_guard=RepeatedConsumerReceipt != 1,
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.INCOMPLETE
    assert result.witness is None
    assert len(result.attempts) == 1
    assert result.attempts[0].witnessed is False
    survived = tuple(
        item for item in result.attempts[0].observations if item.disposition == "SURVIVED"
    )
    assert [item.appeared.call_invocation for item in survived] == [0, 1]
    assert [item.consumer_read.call_invocation for item in survived] == [0, 1]


MemoryProducerPermit = Bool("IntrascanMemoryProducerPermit", external=True)
MemoryProducerStepper = Int("IntrascanMemoryProducerStepper")

with Program() as memory_guard_program:
    with rung(rise(MemoryProducerPermit)):
        copy(1, MemoryProducerStepper)


def test_closure_uses_pilot_projection_with_exact_memory_backed_guard_reads(
    monkeypatch: Any,
) -> None:
    source = PLC(memory_guard_program)
    pilot_projection = PLC._replay_pilot_rung_write_projection_at
    observed_domains: list[tuple[str, ...]] = []

    def capture_pilot_projection(plc: PLC, scan_id: int):
        projection = pilot_projection(plc, scan_id)
        assert projection is not None
        observed_domains.append(tuple(read.occurrence.domain for read in projection.reads))
        return projection

    def reject_tag_only_projection(_plc: PLC, _scan_id: int):
        raise AssertionError("tag-only projection cannot certify intrascan closure")

    monkeypatch.setattr(
        PLC,
        "_replay_pilot_rung_write_projection_at",
        capture_pilot_projection,
    )
    monkeypatch.setattr(
        PLC,
        "_replay_rung_write_projection_at",
        reject_tag_only_projection,
    )
    result = close_intrascan(
        IntrascanClosureQuestion(
            source_checkpoint=_checkpoint(source, "memory-guard-projection"),
            expectation=EffectExpectation(
                (_produce(memory_guard_program, 0, MemoryProducerStepper, 1),)
            ),
            steady_guard=MemoryProducerStepper != 1,
            draft_assignments=((MemoryProducerPermit.name, True),),
            budget=1,
        )
    )

    assert result.status is IntrascanClosureStatus.WITNESS
    assert observed_domains
    assert any("memory" in domains for domains in observed_domains)


def test_closure_results_retain_no_executable_world_or_navigation_future() -> None:
    result_types = (IntrascanClosureResult, IntrascanWitness, IntrascanAttempt)
    forbidden = ("fork", "world", "bearing", "route", "cursor", "future")

    for record in result_types:
        names = tuple(item.name.lower() for item in fields(record))
        assert not any(token in name for name in names for token in forbidden)
