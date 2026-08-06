"""Bounded, production-inert one-scan closure contracts."""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace
from typing import Any

from pyrung import (
    PLC,
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
    EffectExpectation,
    EffectObligation,
    EffectPolarity,
    occurrence_selector,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.intrascan import (
    IntrascanAttempt,
    IntrascanClosureQuestion,
    IntrascanClosureResult,
    IntrascanClosureStatus,
    IntrascanRequirementDisposition,
    IntrascanWitness,
    build_intrascan_requirement_evidence,
    close_intrascan,
)
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
)
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
):
    atoms: list[GuardRequirementAtom] = []

    def collect(term: GuardRequirementAtom | GuardRequirementExpr) -> None:
        if isinstance(term, GuardRequirementAtom):
            atoms.append(term)
        else:
            for child in term.terms:
                collect(child)

    collect(condition)
    checkpoint = _checkpoint(source, label)
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
