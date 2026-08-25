"""Occurrence-exact observations used by production intrascan verification."""

from __future__ import annotations

from itertools import count
from types import SimpleNamespace
from typing import Any

from pyrung import PLC, Bool, Int, Program, call, copy, latch, reset, rung, subroutine
from pyrung.core.analysis.pilot.effects import occurrence_selector, occurrence_snapshot
from pyrung.core.analysis.pilot.execution import CheckpointRef
from pyrung.core.analysis.pilot.intrascan import (
    IntrascanRequirementDisposition,
    build_intrascan_requirement_evidence,
    observe_intrascan_requirement,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
)
from pyrung.core.crossing import Cmp
from pyrung.core.runner import EpochRef

_EPOCH_REFS = count(1_000_000)


def _checkpoint(source: PLC, label: str) -> Any:
    return SimpleNamespace(
        owner=SimpleNamespace(reference=CheckpointRef()),
        key=(label, source.state.scan_id),
        world=SimpleNamespace(work=source, pilot_rungs=()),
    )


def _projection(plc: PLC):
    projection = plc._replay_rung_write_projection_at(plc.state.scan_id)
    assert projection is not None
    return projection


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
    epoch = SimpleNamespace(reference=EpochRef(next(_EPOCH_REFS)))
    requirement = ActiveRequirement(
        condition=condition,
        demanding_occurrence=demanding_occurrence or atoms[0].deadline,
        deadline=atoms[0].deadline,
        selected_writer=(None, 0, ()),
        operand_authority=OperandAuthority.UNKNOWN,
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
    return evidence


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


def test_requirement_is_satisfied_at_its_exact_read_even_when_exit_is_false() -> None:
    permit = Bool("IntrascanEarlyTruePermit", external=True)
    stepper = Int("IntrascanEarlyTrueStepper")
    with Program() as program:
        with rung(permit):
            copy(1, stepper)
        with rung():
            reset(permit)

    execution = PLC(program)
    execution.patch({permit.name: True})
    execution.step()
    projection = _projection(execution)
    evidence = _guard_evidence(
        PLC(program),
        execution,
        _read_atom(program, projection, 0, permit, 0),
        "deadline-before-false-exit",
        steerable=frozenset({permit.name}),
    )

    observation = observe_intrascan_requirement(evidence, projection)

    assert observation.disposition is IntrascanRequirementDisposition.SATISFIED
    assert observation.requirement_identity == evidence.identity
    assert observation.observed_reads[0].values == (True,)
    assert execution.state.tags[permit.name] is False


def test_requirement_is_violated_at_its_exact_read_even_when_exit_is_true() -> None:
    permit = Bool("IntrascanLateTruePermit", external=True)
    marker = Int("IntrascanLateTrueMarker")
    stepper = Int("IntrascanLateTrueStepper")
    with Program() as program:
        with rung():
            reset(permit)
        with rung(permit):
            copy(1, marker)
        with rung():
            copy(1, stepper)
        with rung():
            latch(permit)

    execution = PLC(program)
    execution.step()
    projection = _projection(execution)
    evidence = _guard_evidence(
        PLC(program),
        execution,
        _read_atom(program, projection, 1, permit, 0),
        "deadline-before-true-exit",
        steerable=frozenset({permit.name}),
    )

    observation = observe_intrascan_requirement(evidence, projection)

    assert observation.disposition is IntrascanRequirementDisposition.VIOLATED
    assert observation.observed_reads[0].values == (False,)
    assert execution.state.tags[permit.name] is True


def test_true_deadline_without_demanding_occurrence_remains_unknown() -> None:
    permit = Bool("IntrascanDemandPermit", external=True)
    call_permit = Bool("IntrascanDemandCallPermit", external=True)
    marker = Int("IntrascanDemandMarker")
    stepper = Int("IntrascanDemandStepper")

    @subroutine("IntrascanDemandConsumer", strict=False)
    def consumer() -> None:
        with rung(permit):
            copy(2, marker)

    with Program() as program:
        with rung(permit):
            copy(1, marker)
        with rung(call_permit):
            call(consumer)
        with rung():
            copy(1, stepper)

    evidence_execution = PLC(program)
    evidence_execution.patch({permit.name: True, call_permit.name: True})
    evidence_execution.step()
    projection = _projection(evidence_execution)
    atom = _read_atom(program, projection, 0, permit, 0)
    demanding_reads = tuple(
        read
        for read in projection.reads
        if read.run.rung is program.subroutines["IntrascanDemandConsumer"][0]
        and read.occurrence.name == permit.name
    )
    assert len(demanding_reads) == 1
    evidence = _guard_evidence(
        PLC(program),
        evidence_execution,
        atom,
        "missing-demanding-occurrence",
        steerable=frozenset({permit.name, call_permit.name}),
        demanding_occurrence=occurrence_snapshot(demanding_reads[0]),
    )

    candidate = PLC(program)
    candidate.patch({permit.name: True, call_permit.name: False})
    candidate.step()
    candidate_projection = _projection(candidate)
    assert [
        read.occurrence.value
        for read in candidate_projection.reads
        if occurrence_selector(candidate_projection, read) == evidence.atoms[0].selector
    ] == [True]

    observation = observe_intrascan_requirement(evidence, candidate_projection)

    assert observation.disposition is IntrascanRequirementDisposition.UNKNOWN
    assert "demanding" in observation.detail
    assert [read.values for read in observation.observed_reads] == [(True,)]


def test_decisive_same_guard_short_circuit_replaces_later_demanding_read() -> None:
    state = Int("IntrascanShortCircuitState", external=True)
    poison = Int("IntrascanShortCircuitPoison", external=True)
    marker = Int("IntrascanShortCircuitMarker")
    stepper = Int("IntrascanShortCircuitStepper")
    with Program() as program:
        with rung(state == 2, poison == 1):
            copy(1, marker)
        with rung():
            copy(1, stepper)

    evidence_execution = PLC(program)
    evidence_execution.patch({state.name: 2, poison.name: 1})
    evidence_execution.step()
    projection = _projection(evidence_execution)
    state_read, poison_read = tuple(
        read for read in projection.reads if read.run.rung is program.rungs[0]
    )
    state_snapshot = occurrence_snapshot(state_read)
    poison_snapshot = occurrence_snapshot(poison_read)
    condition = GuardRequirementExpr(
        GuardLogic.ANY,
        (
            GuardRequirementAtom(
                Cmp(state.name, "!=", 2),
                (state_snapshot,),
                state_snapshot,
                (0,),
                demanding_rung=program.rungs[0],
            ),
            GuardRequirementAtom(
                Cmp(poison.name, "!=", 1),
                (poison_snapshot,),
                poison_snapshot,
                (1,),
                demanding_rung=program.rungs[0],
            ),
        ),
    )
    evidence = _guard_evidence(
        PLC(program),
        evidence_execution,
        condition,
        "same-guard-short-circuit",
        steerable=frozenset(),
        demanding_occurrence=poison_snapshot,
    )

    candidate = PLC(program)
    candidate.step()
    observation = observe_intrascan_requirement(evidence, _projection(candidate))

    assert observation.disposition is IntrascanRequirementDisposition.SATISFIED
    assert [read.tag for read in observation.observed_reads] == [state.name]
    assert observation.observed_reads[0].values == (0,)
