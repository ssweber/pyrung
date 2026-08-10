"""Report or close one exact scan without choosing or retaining a future.

The execution projection is the semantic oracle.  The report path asks what
one already-executed assertion scan proves about an ``EffectExpectation`` and
interprets failed observations with the existing requirement derivations.  The
bounded closure path may execute disposable one-scan forks to verify complete
candidate overlays, but it cannot install a correction, mutate PILOT state,
restore or commit a live world, or retain navigation state.  Transitive
same-scan source walks remain typed evidence, including explicit
``INCOMPLETE`` results; they are never action choices.

``derive_recorded_observations`` is the compatibility seam for observations
already captured by steering.  Its filtering, projection selection, and
derivation order intentionally match the former inline implementation in
``pilot.py::_derive_attempt_requirements``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from pyrung.core.analysis.causal._rung_writes import ScanRungWriteProjection
from pyrung.core.analysis.pilot.advance import AdvanceIndex
from pyrung.core.analysis.pilot.avoid import _avoid_snap_names
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObservation,
    EffectObservationSnapshot,
    EffectOccurrenceSelector,
    EffectPolarity,
    observe_execution_window,
    observe_intrascan_expectation,
    occurrence_selector,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.intrascan_schedule import (
    compile_scalar_schedule,
    iter_guard_alternatives,
    satisfying_values,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _merged_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveCondition,
    ActiveRequirement,
    ActiveRequirementSnapshot,
    FailureExplanation,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementDerivation,
    RequirementSourceWalk,
    RequirementSourceWalkStatus,
    bind_guard_condition_operand_authorities,
    bind_guard_operand_authorities,
    classify_bound_operand_authority,
    derive_advance_requirement_from_effect,
    derive_guard_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
)
from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds


@dataclass(frozen=True)
class IntrascanQuestion:
    """Evidence and authority needed to inspect one exact assertion scan."""

    expectation: EffectExpectation | None
    execution: Any = field(compare=False, repr=False)
    assertion_scan: int
    source_checkpoint: Any = field(compare=False, repr=False)
    advance_index: AdvanceIndex | None = field(compare=False, repr=False)
    operand_authorities: Mapping[str, OperandAuthority] = field(compare=False, repr=False)
    steerable: frozenset[str]
    program_written: frozenset[str]
    configured_inputs: frozenset[str] = frozenset()
    projection_at: Callable[[int], ScanRungWriteProjection | None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    advance_index_factory: Callable[[], AdvanceIndex] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    operand_authorities_at: (
        Callable[[ScanRungWriteProjection], Mapping[str, OperandAuthority]] | None
    ) = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class IntrascanFinding:
    """One failed observation and its inert requirement derivation."""

    observation: EffectObservation = field(compare=False, repr=False)
    derivation: RequirementDerivation
    source_checkpoint: Any = field(compare=False, repr=False)

    @property
    def consumed_before_displacement(self) -> bool:
        """Whether the promised value reached one exact read before cleanup.

        This remains report-only evidence.  A caller may use it to distinguish
        an incomplete same-scan handoff from an unconsumed overwrite, but this
        service does not choose a replacement action or retain a continuation.
        """

        observation = self.observation
        requirement = self.derivation.requirement
        if (
            observation.disposition != "OVERWRITTEN"
            or observation.appeared is None
            or observation.displacement is None
            or requirement is None
            or requirement.provenance != "steer-overwriter"
        ):
            return False
        demanding = requirement.demanding_occurrence
        return (
            demanding.kind == "read"
            and demanding.tag == observation.obligation.tag
            and demanding.values == (observation.obligation.value,)
            and demanding.scan_id == observation.appeared.scan_id
            and demanding.scan_id == observation.displacement.scan_id
            and observation.appeared.ordinal < demanding.ordinal < observation.displacement.ordinal
        )

    def diagnostic_snapshot(self) -> IntrascanFindingSnapshot:
        """Detach observation, derivation, source, and causal identity evidence."""

        checkpoint_work = getattr(
            getattr(self.source_checkpoint, "world", None),
            "work",
            None,
        )
        requirement = self.derivation.requirement
        return IntrascanFindingSnapshot(
            observation=self.observation.diagnostic_snapshot(),
            explanation=self.derivation.explanation,
            requirement=(requirement.diagnostic_snapshot() if requirement is not None else None),
            selected_writer=self.observation.obligation.producer,
            source_world_key=self.source_checkpoint.key,
            source_scan=getattr(getattr(checkpoint_work, "state", None), "scan_id", None),
            causal_identity=(
                id(self.observation.execution_epoch),
                id(self.observation.execution_owner),
                id(self.source_checkpoint.owner),
            ),
            source_walk=self.derivation.source_walk,
            consumed_before_displacement=self.consumed_before_displacement,
        )


@dataclass(frozen=True)
class IntrascanFindingSnapshot:
    """Detached diagnostic view of one report-only finding."""

    observation: EffectObservationSnapshot
    explanation: FailureExplanation
    requirement: ActiveRequirementSnapshot | None
    selected_writer: Any
    source_world_key: Any
    source_scan: int | None
    causal_identity: tuple[int, int, int]
    source_walk: RequirementSourceWalk | None
    consumed_before_displacement: bool = False


@dataclass(frozen=True)
class IntrascanResult:
    """Factual observations and derivations for one report-only question."""

    observations: tuple[EffectObservation, ...] = field(compare=False, repr=False)
    findings: tuple[IntrascanFinding, ...]

    def diagnostic_snapshot(self) -> tuple[IntrascanFindingSnapshot, ...]:
        """Return complete detached findings in legacy receipt order."""

        return tuple(finding.diagnostic_snapshot() for finding in self.findings)


class IntrascanClosureStatus(StrEnum):
    """Terminal status of one bounded, production-inert closure question."""

    WITNESS = "witness"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INCOMPLETE = "incomplete"


class IntrascanRequirementDisposition(StrEnum):
    """Exact demanding-occurrence verdict for one logical requirement."""

    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntrascanAtomEvidence:
    """One requirement atom and its replay-relocatable read selector."""

    condition: Cmp
    selector: EffectOccurrenceSelector
    deadline: Any


@dataclass(frozen=True)
class IntrascanRequirementEvidence:
    """A complete logical requirement with exact original-read selectors."""

    requirement: ActiveRequirement = field(compare=False, repr=False)
    condition: ActiveCondition
    atoms: tuple[IntrascanAtomEvidence, ...]
    demanding_selector: EffectOccurrenceSelector | None = None
    complete: bool = True
    detail: str = ""

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.requirement.identity,
            self.condition,
            tuple((atom.condition, atom.selector, atom.deadline) for atom in self.atoms),
            self.demanding_selector,
            self.complete,
        )


@dataclass(frozen=True)
class IntrascanRequirementObservation:
    """Detached occurrence-relative proof for one candidate scan."""

    requirement_identity: tuple[Any, ...]
    disposition: IntrascanRequirementDisposition
    observed_reads: tuple[Any, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class IntrascanOverlay:
    """Complete executable data for one disposable assertion attempt."""

    assignments: tuple[tuple[str, Any], ...]
    steady_assignments: tuple[tuple[str, Any], ...]
    pilot_rungs: tuple[PilotRung, ...]
    expectation: EffectExpectation

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            tuple(sorted(((tag, _semantic_key(value)) for tag, value in self.assignments))),
            tuple(sorted(((tag, _semantic_key(value)) for tag, value in self.steady_assignments))),
            tuple(_rung_identity(rung) for rung in self.pilot_rungs),
            tuple(_effect_obligation_identity(item) for item in self.expectation.obligations),
        )


@dataclass(frozen=True)
class IntrascanAttempt:
    """Detached result of one admitted semantic attempt."""

    identity: tuple[Any, ...]
    overlay: IntrascanOverlay
    observations: tuple[EffectObservationSnapshot, ...]
    requirement_observations: tuple[IntrascanRequirementObservation, ...]
    witnessed: bool
    detail: str = ""
    execution_owner_token: tuple[Any, ...] = ()
    findings: tuple[IntrascanFinding, ...] = field(default=(), compare=False, repr=False)


@dataclass(frozen=True)
class IntrascanWitness:
    """Exact one-scan witness without retaining its disposable PLC world."""

    overlay: IntrascanOverlay
    assertion_scan: int
    observations: tuple[EffectObservationSnapshot, ...]
    requirement_observations: tuple[IntrascanRequirementObservation, ...]
    added_pilot_rungs: tuple[PilotRung, ...] = ()
    execution_owner_token: tuple[Any, ...] = ()


@dataclass(frozen=True)
class IntrascanDraftOverlayResult:
    """One finite draft overlay, or a fail-closed reason for omitting it."""

    assignments: tuple[tuple[str, Any], ...] | None
    detail: str = ""


@dataclass(frozen=True)
class IntrascanGuardOverlayResult:
    """Finite producer-guard alternatives, or one fail-closed reason."""

    overlays: tuple[tuple[tuple[str, Any], ...], ...] | None
    detail: str = ""


@dataclass(frozen=True)
class IntrascanClosureQuestion:
    """All fixed evidence and finite candidates for one exact source scan."""

    source_checkpoint: Any = field(compare=False, repr=False)
    expectation: EffectExpectation
    steady_guard: Any = field(compare=False, repr=False)
    requirements: tuple[IntrascanRequirementEvidence, ...] = ()
    draft_assignments: tuple[tuple[str, Any], ...] = ()
    fixed_pilot_rungs: tuple[PilotRung, ...] | None = None
    proposed_pilot_rungs: tuple[PilotRung, ...] = ()
    producer_guard_rungs: tuple[Any, ...] = field(default=(), compare=False, repr=False)
    producer_guard_steerable: frozenset[str] = frozenset()
    program: Any = field(default=None, compare=False, repr=False)
    steerable: frozenset[str] = frozenset()
    program_written: frozenset[str] = frozenset()
    configured_inputs: frozenset[str] = frozenset()
    avoid_predicate: Any = field(default=None, compare=False, repr=False)
    candidate_overlays: tuple[tuple[tuple[str, Any], ...], ...] = ((),)
    attempted_identities: frozenset[tuple[Any, ...]] = frozenset()
    budget: int = 1

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError("intrascan closure budget must be positive")


@dataclass(frozen=True)
class IntrascanClosureResult:
    """Bounded closure outcome; ``IMPOSSIBLE`` requires a future certificate."""

    status: IntrascanClosureStatus
    witness: IntrascanWitness | None = None
    attempts: tuple[IntrascanAttempt, ...] = ()
    attempted_identities: frozenset[tuple[Any, ...]] = frozenset()
    detail: str = ""


def draft_overlay_from_selected_actions(
    selected_assignments: tuple[tuple[str, Any], ...],
    expectation: EffectExpectation,
    *,
    steerable: frozenset[str],
    source_snapshot: Mapping[str, Any],
) -> IntrascanDraftOverlayResult:
    """Compose one finite action draft with sound steerable shape facts.

    Selected action assignments are already owned by the caller.  Additional
    ``required_shape`` facts may enter the draft only when their destination is
    steerable. A non-steerable fact is never synthesized: when the source does
    not yet supply it, the selected program may still establish it before the
    exact consumer occurrence in the assertion scan. The execution projection
    remains the oracle for that internal handoff. Conflicting selected facts
    and unowned steerable additions omit the whole draft.
    """

    assignments = _merge_assignments(selected_assignments)
    if assignments is None:
        return IntrascanDraftOverlayResult(
            None,
            "selected action assignments conflict",
        )

    selected = dict(assignments)
    additions: list[tuple[str, Any]] = []
    for obligation in expectation.obligations:
        for tag, value in obligation.required_shape:
            # The handoff value appears in the consumer-local receipt, but it
            # is the selected producer's promised result, not an input the
            # draft may synthesize.  The execution projection must witness it.
            if tag == obligation.tag and _values_match(value, obligation.value):
                continue
            if tag in selected:
                existing = selected[tag]
                if not _values_match(existing, value):
                    return IntrascanDraftOverlayResult(
                        None,
                        f"required shape conflicts with selected action for {tag!r}",
                    )
                continue
            if tag in source_snapshot and _values_match(source_snapshot[tag], value):
                continue
            if tag not in steerable:
                # Internal values are evidence to observe, not intervention
                # inputs. The selected action may produce them earlier in this
                # same scan; only its exact projection can certify that fact.
                continue
            selected[tag] = value
            additions.append((tag, value))

    return IntrascanDraftOverlayResult((*assignments, *additions))


def producer_guard_candidate_overlays(
    rungs: tuple[Any, ...],
    source: Any,
    *,
    selected_assignments: tuple[tuple[str, Any], ...] = (),
    steerable: frozenset[str],
    limit: int,
) -> IntrascanGuardOverlayResult:
    """Materialize complete local DNF alternatives for exact projection.

    The result is only a finite candidate generator. Unsupported atoms,
    ambiguous operands, or excessive local alternatives return no overlays;
    only :func:`close_intrascan` may certify an alternative by executing it.
    """

    from pyrung.core.analysis.simplified import And, Atom, Const, Or, _sp_to_expr

    if not rungs:
        return IntrascanGuardOverlayResult(None, "selected producer guard is unavailable")
    if limit <= 0:
        return IntrascanGuardOverlayResult(None, "producer guard budget is unavailable")

    expressions: list[Any] = []
    for rung in rungs:
        sp = rung.sp_tree()
        expressions.append(Const(True) if sp is None else _sp_to_expr(sp))
    expression = expressions[0] if len(expressions) == 1 else And(tuple(expressions))
    snapshot = dict(source.state.tags)
    selected = dict(selected_assignments)
    known = {**snapshot, **selected}
    tags = source._known_tags_by_name

    forms = {
        "eq": "==",
        "ne": "!=",
        "lt": "<",
        "le": "<=",
        "gt": ">",
        "ge": ">=",
    }

    def atom_options(atom: Any) -> tuple[tuple[tuple[str, Any], ...], ...] | None:
        if atom.unsupported is not None or atom.operand_is_tag or atom.tag not in tags:
            return None
        if atom.form in {"xic", "rise"}:
            constraints = (Cmp(atom.tag, "==", True),)
        elif atom.form in {"xio", "fall"}:
            constraints = (Cmp(atom.tag, "==", False),)
        elif atom.form == "truthy":
            constraints = (Cmp(atom.tag, "!=", 0),)
        elif atom.form in forms:
            constraints = (Cmp(atom.tag, forms[atom.form], atom.operand),)
        else:
            return None

        # Edge contacts still require the projected executor to witness their
        # previous-value semantics. Their post level alone is never proof.
        steady = atom.form not in {"rise", "fall"}
        if steady and all(constraint_holds(item, known) is True for item in constraints):
            return ((),)
        if atom.tag not in steerable:
            # A selected action may establish an internal producer guard before
            # the chart writer executes. Do not invent an assignment for it;
            # retain the branch and require the exact producer/consumer witness.
            return ((),)
        values = satisfying_values(tags[atom.tag], constraints, snapshot)
        if not values:
            return None
        return tuple((((atom.tag, value),)) for value in values)

    def alternatives(expr: Any) -> tuple[tuple[tuple[str, Any], ...], ...] | None:
        if isinstance(expr, Const):
            return ((),) if expr.value else ()
        if isinstance(expr, Atom):
            return atom_options(expr)
        if isinstance(expr, Or):
            result: list[tuple[tuple[str, Any], ...]] = []
            for term in expr.terms:
                branch = alternatives(term)
                if branch is None:
                    return None
                result.extend(branch)
                if len(result) > limit:
                    return None
            return tuple(result)
        if not isinstance(expr, And):
            return None
        result = [()]
        for term in expr.terms:
            branches = alternatives(term)
            if branches is None:
                return None
            result = [(*prefix, *branch) for prefix in result for branch in branches]
            if len(result) > limit:
                return None
        return tuple(result)

    overlays = alternatives(expression)
    if overlays is None:
        return IntrascanGuardOverlayResult(
            None,
            "selected producer guard is unsupported, ambiguous, or exceeds its local budget",
        )
    if not overlays:
        return IntrascanGuardOverlayResult(None, "selected producer guard has no local alternative")
    return IntrascanGuardOverlayResult(overlays)


def inspect_assertion_scan(question: IntrascanQuestion) -> IntrascanResult:
    """Inspect exactly the assertion scan named by ``question``.

    ``observe_execution_window`` owns scan selection and delegates the actual
    producer/consumer interpretation to ``observe_expectation``.  Missing or
    ambiguous projection evidence therefore remains ``UNKNOWN`` and produces
    no derived finding.
    """

    project = question.projection_at or question.execution._replay_rung_write_projection_at

    def exact_projection_at(scan_id: int) -> ScanRungWriteProjection | None:
        projection = project(scan_id)
        if projection is None or projection.scan_id != scan_id:
            return None
        return projection

    observations = observe_execution_window(
        question.expectation,
        question.execution,
        scan_before=question.assertion_scan - 1,
        kernel_scan_ids=(question.assertion_scan,),
        action_scan=question.assertion_scan,
        projection_at=exact_projection_at,
    )
    return derive_recorded_observations(question, observations)


def derive_recorded_observations(
    question: IntrascanQuestion,
    observations: Sequence[EffectObservation],
    *,
    fallback_scan: int | None = None,
) -> IntrascanResult:
    """Interpret already-recorded observations without making a decision.

    A surviving occurrence suppresses another failure for the same obligation
    object, just as the legacy adapter did.  Each remaining observation must
    carry an exact execution owner and epoch, and its selected projection must
    match the latest involved occurrence scan.  Replaying that exact scan is
    the sole fallback; unavailable evidence is ignored rather than guessed.
    """

    recorded = tuple(observations)
    fulfilled_obligations = {
        id(item.obligation) for item in recorded if item.disposition == "SURVIVED"
    }
    findings: list[IntrascanFinding] = []
    advance_index = question.advance_index
    for observation in recorded:
        if (
            observation.disposition == "SURVIVED"
            or id(observation.obligation) in fulfilled_obligations
        ):
            continue
        owner = observation.execution_owner
        epoch = observation.execution_epoch
        if owner is None or epoch is None:
            continue
        scans = tuple(
            occurrence.scan_id
            for occurrence in (
                observation.appeared,
                observation.consumer_read,
                observation.displacement,
                observation.displaced_read,
                *observation.observed_reads,
            )
            if occurrence is not None
        )
        scan = max(
            scans, default=question.assertion_scan if fallback_scan is None else fallback_scan
        )
        projection = observation.execution_projection
        if projection is None or projection.scan_id != scan:
            project = question.projection_at or owner._runner()._replay_rung_write_projection_at
            projection = project(scan)
        if projection is None or projection.scan_id != scan:
            continue
        if observation.disposition not in {"ABSENT", "STRANDED"} and advance_index is None:
            factory = question.advance_index_factory
            if factory is None:
                continue
            advance_index = factory()
        derivation = _derive_observation(
            question,
            observation,
            projection,
            epoch,
            owner,
            advance_index,
        )
        findings.append(IntrascanFinding(observation, derivation, question.source_checkpoint))
    return IntrascanResult(recorded, tuple(findings))


def _derive_observation(
    question: IntrascanQuestion,
    observation: EffectObservation,
    projection: ScanRungWriteProjection,
    epoch: Any,
    owner: Any,
    advance_index: AdvanceIndex | None,
) -> RequirementDerivation:
    """Apply the existing one-hop requirement derivations in legacy order."""

    checkpoint = question.source_checkpoint
    selected_writer = observation.obligation.producer
    if observation.disposition in {"ABSENT", "STRANDED"}:
        return _bind_guard_authority(
            question,
            derive_guard_requirement_from_effect(
                observation,
                projection,
                execution_epoch=epoch,
                execution_owner=owner,
                selected_writer=selected_writer,
                source_world_key=checkpoint.key,
                source_checkpoint=checkpoint,
                provenance="steer",
            ),
        )

    if advance_index is None:
        raise AssertionError("advance index is required for overwrite derivation")
    operand_authorities = (
        question.operand_authorities_at(projection)
        if question.operand_authorities_at is not None
        else question.operand_authorities
    )
    derivation = derive_advance_requirement_from_effect(
        advance_index,
        projection,
        observation,
        operand_authorities=operand_authorities,
        execution_epoch=epoch,
        execution_owner=owner,
        selected_writer=selected_writer,
        source_world_key=checkpoint.key,
        source_checkpoint=checkpoint,
        provenance="steer",
    )
    if derivation.requirement is not None:
        return derivation
    return _bind_guard_authority(
        question,
        derive_overwriter_guard_requirement_from_effect(
            observation,
            projection,
            execution_epoch=epoch,
            execution_owner=owner,
            selected_writer=selected_writer,
            source_world_key=checkpoint.key,
            source_checkpoint=checkpoint,
            provenance="steer-overwriter",
            preserved_values=(
                ((observation.obligation.tag, observation.obligation.value),)
                if observation.obligation.terminal_target
                else ()
            ),
        ),
    )


def _bind_guard_authority(
    question: IntrascanQuestion,
    derivation: RequirementDerivation,
) -> RequirementDerivation:
    """Classify exact guard atoms without granting execution authority."""

    requirement = bind_guard_operand_authorities(
        derivation.requirement,
        steerable=question.steerable,
        program_written=question.program_written,
        configured=question.configured_inputs,
    )
    return replace(derivation, requirement=requirement)


def build_intrascan_requirement_evidence(
    requirement: ActiveRequirement,
    projection: ScanRungWriteProjection,
    *,
    source_walk: RequirementSourceWalk | None = None,
    steerable: frozenset[str] = frozenset(),
    program_written: frozenset[str] = frozenset(),
    configured_inputs: frozenset[str] = frozenset(),
) -> IntrascanRequirementEvidence:
    """Relocate one retained requirement without retaining its old projection."""

    configured_inputs = configured_inputs | frozenset(
        getattr(requirement.source_checkpoint, "configured_inputs", frozenset())
    )
    condition = requirement.condition
    if source_walk is not None:
        if source_walk.status is not RequirementSourceWalkStatus.COMPLETE:
            return IntrascanRequirementEvidence(
                requirement,
                source_walk.condition,
                (),
                complete=False,
                detail=source_walk.detail or "same-scan requirement source walk is incomplete",
            )
        condition = source_walk.condition
    if isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
        condition = bind_guard_condition_operand_authorities(
            condition,
            steerable=steerable,
            program_written=program_written,
            configured=configured_inputs,
        )
        atoms = _guard_atoms(condition)
        targets = tuple((atom.condition, atom.deadline) for atom in atoms)
    elif isinstance(condition, Cmp):
        targets = ((condition, requirement.deadline),)
    else:
        return IntrascanRequirementEvidence(
            requirement,
            condition,
            (),
            complete=False,
            detail="requirement condition cannot be observed at one scalar occurrence",
        )

    evidence: list[IntrascanAtomEvidence] = []
    for atom_condition, deadline in targets:
        if not isinstance(atom_condition, Cmp):
            return IntrascanRequirementEvidence(
                requirement,
                condition,
                tuple(evidence),
                complete=False,
                detail="requirement atom is not an exact scalar comparison",
            )
        reads = tuple(read for read in projection.reads if occurrence_snapshot(read) == deadline)
        if len(reads) != 1:
            return IntrascanRequirementEvidence(
                requirement,
                condition,
                tuple(evidence),
                complete=False,
                detail="requirement deadline read is unavailable or ambiguous",
            )
        selector = occurrence_selector(projection, reads[0])
        if selector is None:
            return IntrascanRequirementEvidence(
                requirement,
                condition,
                tuple(evidence),
                complete=False,
                detail="requirement deadline has no relocatable occurrence selector",
            )
        evidence.append(IntrascanAtomEvidence(atom_condition, selector, deadline))
    demanding_reads = tuple(
        read
        for read in projection.reads
        if occurrence_snapshot(read) == requirement.demanding_occurrence
    )
    if len(demanding_reads) != 1:
        return IntrascanRequirementEvidence(
            requirement,
            condition,
            tuple(evidence),
            complete=False,
            detail="requirement demanding read is unavailable or ambiguous",
        )
    demanding_selector = occurrence_selector(projection, demanding_reads[0])
    if demanding_selector is None:
        return IntrascanRequirementEvidence(
            requirement,
            condition,
            tuple(evidence),
            complete=False,
            detail="requirement demanding read has no relocatable occurrence selector",
        )
    return IntrascanRequirementEvidence(
        requirement,
        condition,
        tuple(evidence),
        demanding_selector=demanding_selector,
    )


def close_intrascan(question: IntrascanClosureQuestion) -> IntrascanClosureResult:
    """Search a finite set of overlays on disposable one-scan forks only."""

    incomplete = tuple(item.detail for item in question.requirements if not item.complete)
    if incomplete:
        return IntrascanClosureResult(
            IntrascanClosureStatus.INCOMPLETE,
            attempted_identities=question.attempted_identities,
            detail="; ".join(dict.fromkeys(incomplete)),
        )
    source = getattr(getattr(question.source_checkpoint, "world", None), "work", None)
    if source is None:
        return IntrascanClosureResult(
            IntrascanClosureStatus.INCOMPLETE,
            attempted_identities=question.attempted_identities,
            detail="exact source checkpoint has no executable work",
        )
    if (
        getattr(question.source_checkpoint, "key", None) is None
        or getattr(question.source_checkpoint, "owner", None) is None
    ):
        return IntrascanClosureResult(
            IntrascanClosureStatus.INCOMPLETE,
            attempted_identities=question.attempted_identities,
            detail="exact source checkpoint identity is unavailable",
        )

    checkpoint_world = question.source_checkpoint.world
    checkpoint_rungs = getattr(checkpoint_world, "pilot_rungs", None)
    if checkpoint_rungs is None:
        fixed_pilot_rungs = question.fixed_pilot_rungs or ()
    else:
        checkpoint_rungs = tuple(checkpoint_rungs)
        if question.fixed_pilot_rungs is not None and tuple(
            _rung_identity(rung) for rung in question.fixed_pilot_rungs
        ) != tuple(_rung_identity(rung) for rung in checkpoint_rungs):
            return IntrascanClosureResult(
                IntrascanClosureStatus.INCOMPLETE,
                attempted_identities=question.attempted_identities,
                detail="supplied PilotRungs do not match the exact source checkpoint",
            )
        fixed_pilot_rungs = checkpoint_rungs

    scalar = tuple(
        item.requirement for item in question.requirements if isinstance(item.condition, Cmp)
    )
    schedule = None
    if scalar:
        compilation = compile_scalar_schedule(scalar, source, guard=question.steady_guard)
        if compilation.schedule is None:
            return IntrascanClosureResult(
                IntrascanClosureStatus.INCOMPLETE,
                attempted_identities=question.attempted_identities,
                detail=compilation.detail,
            )
        schedule = compilation.schedule
    steady_assignments = schedule.assignments if schedule is not None else ()
    scheduled_rungs = schedule.pilot_rungs if schedule is not None else ()
    source_rung_identities = frozenset(_rung_identity(rung) for rung in fixed_pilot_rungs)
    proposed_rungs = _merged_pilot_rungs(
        question.proposed_pilot_rungs,
        fixed_pilot_rungs,
    )
    pilot_rungs = tuple(_merged_pilot_rungs(scheduled_rungs, proposed_rungs))
    added_pilot_rungs = tuple(
        rung for rung in pilot_rungs if _rung_identity(rung) not in source_rung_identities
    )

    attempts: list[IntrascanAttempt] = []
    known = set(question.attempted_identities)
    source_snapshot = dict(source.state.tags)
    configured_inputs = question.configured_inputs | frozenset(
        getattr(question.source_checkpoint, "configured_inputs", frozenset())
    )
    candidate_overlays = question.candidate_overlays
    if question.producer_guard_rungs:
        guard_result = producer_guard_candidate_overlays(
            question.producer_guard_rungs,
            source,
            selected_assignments=question.draft_assignments,
            steerable=question.producer_guard_steerable - configured_inputs,
            limit=question.budget,
        )
        if guard_result.overlays is None:
            return IntrascanClosureResult(
                IntrascanClosureStatus.INCOMPLETE,
                attempted_identities=question.attempted_identities,
                detail=guard_result.detail,
            )
        candidate_overlays = tuple(
            (*candidate, *guard)
            for candidate in candidate_overlays
            for guard in guard_result.overlays
        )
    exhausted = False
    generated = False
    for guard_atoms in _iter_requirement_alternatives(question.requirements):
        alternatives, detail = _guard_assignment_options(
            guard_atoms,
            source,
            source_snapshot,
            configured_inputs=configured_inputs,
        )
        if detail:
            continue
        for requirement_assignments in alternatives:
            for candidate in candidate_overlays:
                generated = True
                assignments = _merge_assignments(
                    question.draft_assignments,
                    candidate,
                    requirement_assignments,
                )
                raw_assignments = (
                    *question.draft_assignments,
                    *candidate,
                    *requirement_assignments,
                )
                conflict = assignments is None
                configured = any(tag in configured_inputs for tag, _value in raw_assignments)
                if conflict or configured:
                    rejected = IntrascanOverlay(
                        raw_assignments,
                        steady_assignments,
                        pilot_rungs,
                        question.expectation,
                    )
                    identity = _closure_attempt_identity(question, rejected)
                    if identity in known:
                        continue
                    if len(attempts) >= question.budget:
                        exhausted = True
                        break
                    known.add(identity)
                    attempts.append(
                        IntrascanAttempt(
                            identity,
                            rejected,
                            (),
                            (),
                            False,
                            (
                                "composite assignments conflict"
                                if conflict
                                else "composite assignment changes a configured input"
                            ),
                        )
                    )
                    continue
                assert assignments is not None
                overlay = IntrascanOverlay(
                    assignments,
                    steady_assignments,
                    pilot_rungs,
                    question.expectation,
                )
                identity = _closure_attempt_identity(question, overlay)
                if identity in known:
                    continue
                if len(attempts) >= question.budget:
                    exhausted = True
                    break
                known.add(identity)
                attempt = _execute_closure_attempt(question, source, overlay, identity)
                attempts.append(attempt)
                if attempt.witnessed:
                    return IntrascanClosureResult(
                        IntrascanClosureStatus.WITNESS,
                        IntrascanWitness(
                            overlay,
                            attempt.observations[0].appeared.scan_id
                            if attempt.observations and attempt.observations[0].appeared is not None
                            else source.state.scan_id + 1,
                            attempt.observations,
                            attempt.requirement_observations,
                            added_pilot_rungs=added_pilot_rungs,
                            execution_owner_token=attempt.execution_owner_token,
                        ),
                        tuple(attempts),
                        frozenset(known),
                    )
            if exhausted:
                break
        if exhausted:
            break
    if exhausted:
        return IntrascanClosureResult(
            IntrascanClosureStatus.BUDGET_EXHAUSTED,
            attempts=tuple(attempts),
            attempted_identities=frozenset(known),
            detail="bounded one-scan closure exhausted its attempt budget",
        )
    return IntrascanClosureResult(
        IntrascanClosureStatus.INCOMPLETE,
        attempts=tuple(attempts),
        attempted_identities=frozenset(known),
        detail=(
            "no complete compatible overlay was generated"
            if not generated or not attempts
            else "finite candidate overlays produced no exact witness"
        ),
    )


def _guard_atoms(condition: GuardRequirementCondition) -> tuple[GuardRequirementAtom, ...]:
    if isinstance(condition, GuardRequirementAtom):
        return (condition,)
    return tuple(atom for term in condition.terms for atom in _guard_atoms(term))


def _iter_requirement_alternatives(
    requirements: tuple[IntrascanRequirementEvidence, ...],
) -> Iterator[tuple[GuardRequirementAtom, ...]]:
    guards = tuple(
        item.condition
        for item in requirements
        if isinstance(item.condition, GuardRequirementAtom | GuardRequirementExpr)
    )

    def combine(
        index: int,
        prefix: tuple[GuardRequirementAtom, ...],
    ) -> Iterator[tuple[GuardRequirementAtom, ...]]:
        if index == len(guards):
            yield prefix
            return
        for alternative in iter_guard_alternatives(guards[index]):
            yield from combine(index + 1, (*prefix, *alternative))

    yield from combine(0, ())


def _guard_assignment_options(
    atoms: tuple[GuardRequirementAtom, ...],
    source: Any,
    snapshot: dict[str, Any],
    *,
    configured_inputs: frozenset[str],
) -> tuple[Iterator[tuple[tuple[str, Any], ...]], str]:
    by_tag: dict[str, list[Cmp]] = {}
    for atom in atoms:
        condition = atom.condition
        if not isinstance(condition, Cmp) or condition.bound_is_tag:
            return iter(()), "guard alternative is not an exact literal scalar constraint"
        if constraint_holds(condition, snapshot) is True:
            continue
        if not atom.permits_assignment or condition.tag in configured_inputs:
            return iter(()), "guard alternative contains an unsatisfied authoritative operand"
        by_tag.setdefault(condition.tag, []).append(condition)

    names: list[str] = []
    values: list[tuple[Any, ...]] = []
    for name, constraints in sorted(by_tag.items()):
        tag = source._known_tags_by_name.get(name)
        if tag is None:
            return iter(()), f"unknown assignment destination {name!r}"
        candidates = satisfying_values(tag, tuple(constraints), snapshot)
        if not candidates:
            return iter(()), f"incompatible guard requirements for {name!r}"
        names.append(name)
        values.append(candidates)

    def assignments() -> Iterator[tuple[tuple[str, Any], ...]]:
        if not names:
            yield ()
            return

        def combine(index: int, prefix: tuple[tuple[str, Any], ...]) -> Iterator[Any]:
            if index == len(names):
                yield prefix
                return
            for value in values[index]:
                yield from combine(index + 1, (*prefix, (names[index], value)))

        yield from combine(0, ())

    return assignments(), ""


def _merge_assignments(
    *groups: tuple[tuple[str, Any], ...],
) -> tuple[tuple[str, Any], ...] | None:
    merged: dict[str, Any] = {}
    order: list[str] = []
    for group in groups:
        for tag, value in group:
            if tag in merged and not _values_match(merged[tag], value):
                return None
            if tag not in merged:
                order.append(tag)
                merged[tag] = value
    return tuple((tag, merged[tag]) for tag in order)


def _effect_obligation_identity(obligation: Any) -> tuple[Any, ...]:
    return (
        getattr(obligation, "polarity", EffectPolarity.PRODUCE),
        obligation.tag,
        _semantic_key(obligation.value),
        obligation.producer,
        obligation.consumer,
        tuple(obligation.required_shape),
        _semantic_key(obligation.boundary),
        getattr(obligation, "projected_consumer", False),
        getattr(obligation, "occurrence_selector", None),
    )


def _closure_attempt_identity(
    question: IntrascanClosureQuestion,
    overlay: IntrascanOverlay,
) -> tuple[Any, ...]:
    checkpoint = question.source_checkpoint
    configured_inputs = question.configured_inputs | frozenset(
        getattr(checkpoint, "configured_inputs", frozenset())
    )
    return (
        "intrascan",
        checkpoint.key,
        id(checkpoint.owner),
        overlay.identity,
        tuple(item.identity for item in question.requirements),
        tuple(sorted(configured_inputs)),
        _semantic_key(question.steady_guard),
        _semantic_key(question.avoid_predicate),
    )


def _execute_closure_attempt(
    question: IntrascanClosureQuestion,
    source: Any,
    overlay: IntrascanOverlay,
    identity: tuple[Any, ...],
) -> IntrascanAttempt:
    top = dict(source.state.tags)
    top.update(overlay.assignments)
    if _avoid_snap_names(question.avoid_predicate, top):
        return IntrascanAttempt(
            identity, overlay, (), (), False, "candidate overlay violates avoid"
        )
    try:
        fork = fork_with_pilot_rungs(source, overlay.pilot_rungs)
        if overlay.assignments:
            fork.patch(dict(overlay.assignments))
        fork.step()
        projection = fork._replay_pilot_rung_write_projection_at(fork.state.scan_id)
    except Exception as exc:  # noqa: BLE001 - unsupported disposable candidate fails closed
        return IntrascanAttempt(
            identity,
            overlay,
            (),
            (),
            False,
            f"disposable one-scan execution was unavailable: {type(exc).__name__}",
        )
    if projection is None or projection.scan_id != fork.state.scan_id:
        return IntrascanAttempt(
            identity,
            overlay,
            (),
            (),
            False,
            "exact assertion projection is unavailable",
        )
    owner_matches = tuple(
        (epoch, owner)
        for epoch, owner in fork._causal_lineage.seal_through(fork.state.scan_id)
        if epoch.first_scan <= fork.state.scan_id <= epoch.last_scan
    )
    observations = observe_intrascan_expectation(overlay.expectation, projection)
    if len(owner_matches) == 1:
        epoch, owner = owner_matches[0]
        observations = tuple(
            replace(
                item,
                execution_epoch=epoch,
                execution_owner=owner,
            )
            for item in observations
        )
    snapshots = tuple(item.diagnostic_snapshot() for item in observations)
    owner_pairs = {
        (id(item.execution_epoch), id(item.execution_owner))
        for item in observations
        if item.execution_epoch is not None and item.execution_owner is not None
    }
    execution_owner_token = (
        ("execution-owner", *next(iter(owner_pairs)))
        if len(observations) > 0
        and len(owner_pairs) == 1
        and all(
            item.execution_epoch is not None and item.execution_owner is not None
            for item in observations
        )
        else ()
    )
    requirement_observations = tuple(
        _observe_requirement(item, projection) for item in question.requirements
    )
    effects_hold = all(
        (
            len(related) == 1 and related[0].disposition == "PREVENTED"
            if obligation.polarity is EffectPolarity.PREVENT
            else len(tuple(item for item in related if item.disposition == "SURVIVED")) == 1
        )
        for obligation in overlay.expectation.obligations
        for related in (tuple(item for item in observations if item.obligation is obligation),)
    )
    requirements_hold = all(
        item.disposition is IntrascanRequirementDisposition.SATISFIED
        for item in requirement_observations
    )
    avoid_holds = not _avoid_snap_names(question.avoid_predicate, dict(fork.state.tags))
    witnessed = effects_hold and requirements_hold and avoid_holds and bool(execution_owner_token)
    detail = (
        ""
        if witnessed
        else "exact execution owner is unavailable or ambiguous"
        if effects_hold and requirements_hold and avoid_holds
        else "exact projection did not satisfy the complete artifact"
    )
    findings: tuple[IntrascanFinding, ...] = ()
    if not witnessed and question.program is not None:
        from pyrung.core.analysis.pilot.advance import build_advance_index

        source_tags = question.source_checkpoint.world.work.state.tags
        known = question.source_checkpoint.world.work._known_tags_by_name
        configured = question.configured_inputs | frozenset(
            getattr(question.source_checkpoint, "configured_inputs", frozenset())
        )

        def authorities_at(exact_projection: Any) -> Mapping[str, OperandAuthority]:
            return {
                read.occurrence.name: classify_bound_operand_authority(
                    read.occurrence.name,
                    source_value=source_tags.get(
                        read.occurrence.name,
                        getattr(known.get(read.occurrence.name), "default", None),
                    ),
                    declared_default=getattr(known.get(read.occurrence.name), "default", None),
                    steerable=question.steerable,
                    program_written=question.program_written,
                    configured=configured,
                )
                for read in exact_projection.reads
            }

        report = derive_recorded_observations(
            IntrascanQuestion(
                expectation=overlay.expectation,
                execution=fork,
                assertion_scan=projection.scan_id,
                source_checkpoint=question.source_checkpoint,
                advance_index=None,
                operand_authorities={},
                steerable=question.steerable,
                program_written=question.program_written,
                configured_inputs=configured,
                projection_at=lambda scan_id: projection if scan_id == projection.scan_id else None,
                advance_index_factory=lambda: build_advance_index(
                    question.program,
                    getattr(question.source_checkpoint.world.work, "_harness", None),
                ),
                operand_authorities_at=authorities_at,
            ),
            observations,
            fallback_scan=projection.scan_id,
        )
        findings = report.findings
    return IntrascanAttempt(
        identity,
        overlay,
        snapshots,
        requirement_observations,
        witnessed,
        detail,
        execution_owner_token,
        findings,
    )


def _observe_requirement(
    evidence: IntrascanRequirementEvidence,
    projection: ScanRungWriteProjection,
) -> IntrascanRequirementObservation:
    if not evidence.complete:
        return IntrascanRequirementObservation(
            evidence.identity,
            IntrascanRequirementDisposition.UNKNOWN,
            detail=evidence.detail or "requirement demanding occurrence is unavailable",
        )
    demanding_matches = (
        tuple(
            read
            for read in projection.reads
            if occurrence_selector(projection, read) == evidence.demanding_selector
        )
        if evidence.demanding_selector is not None
        else ()
    )
    demanding_read = demanding_matches[0] if len(demanding_matches) == 1 else None
    verdicts: list[tuple[IntrascanAtomEvidence, bool | None, Any | None]] = []
    for atom in evidence.atoms:
        matches = tuple(
            read
            for read in projection.reads
            if occurrence_selector(projection, read) == atom.selector
        )
        if len(matches) != 1 or atom.condition.bound_is_tag:
            verdicts.append((atom, None, None))
            continue
        read = matches[0]
        if read.occurrence.name != atom.condition.tag:
            verdicts.append((atom, None, read))
            continue
        verdict = constraint_holds(
            atom.condition,
            {atom.condition.tag: read.occurrence.value},
        )
        verdicts.append((atom, verdict, read))

    def atom_verdict(condition: Cmp, deadline: Any) -> bool | None:
        matching = tuple(
            verdict
            for atom, verdict, _read in verdicts
            if atom.condition == condition and atom.deadline == deadline
        )
        return matching[0] if len(matching) == 1 else None

    def evaluate(condition: ActiveCondition) -> bool | None:
        if isinstance(condition, GuardRequirementAtom):
            return (
                atom_verdict(condition.condition, condition.deadline)
                if isinstance(condition.condition, Cmp)
                else None
            )
        if isinstance(condition, GuardRequirementExpr):
            terms = tuple(evaluate(term) for term in condition.terms)
            if condition.logic.value == "all":
                if any(item is False for item in terms):
                    return False
                return True if all(item is True for item in terms) else None
            if condition.logic.value == "any":
                if any(item is True for item in terms):
                    return True
                return False if all(item is False for item in terms) else None
            return None
        if isinstance(condition, Cmp) and evidence.atoms:
            return atom_verdict(condition, evidence.atoms[0].deadline)
        return None

    logical_verdict = evaluate(evidence.condition)
    # A compound guard can decide before its final historical read.  For
    # example, the first false term of an AND prevents the remaining terms
    # from being read, while its complement already proves an ANY prevention
    # requirement.  Accept that decisive short-circuit proof only when an
    # exact relocated atom belongs to the same dynamic guard surface as the
    # demanding occurrence.  A true read from another rung/call remains
    # insufficient (and therefore UNKNOWN) when the demanding occurrence is
    # absent.
    demanding_surface = (
        (
            evidence.demanding_selector.static_address,
            evidence.demanding_selector.instruction_path,
            evidence.demanding_selector.execution_kind,
            evidence.demanding_selector.caller_rung,
            evidence.demanding_selector.call_stack,
            evidence.demanding_selector.depth,
            evidence.demanding_selector.call_invocation,
        )
        if evidence.demanding_selector is not None
        else None
    )
    exact_deciding_surface = any(
        read is not None
        and (
            atom.selector.static_address,
            atom.selector.instruction_path,
            atom.selector.execution_kind,
            atom.selector.caller_rung,
            atom.selector.call_stack,
            atom.selector.depth,
            atom.selector.call_invocation,
        )
        == demanding_surface
        for atom, _atom_verdict, read in verdicts
    )
    verdict = (
        logical_verdict
        if demanding_read is not None or (logical_verdict is not None and exact_deciding_surface)
        else None
    )
    disposition = (
        IntrascanRequirementDisposition.SATISFIED
        if verdict is True
        else IntrascanRequirementDisposition.VIOLATED
        if verdict is False
        else IntrascanRequirementDisposition.UNKNOWN
    )
    reads = [demanding_read] if demanding_read is not None else []
    reads.extend(read for _atom, _verdict, read in verdicts if read is not None)
    snapshots: list[Any] = []
    for read in reads:
        snapshot = occurrence_snapshot(read)
        if snapshot not in snapshots:
            snapshots.append(snapshot)
    return IntrascanRequirementObservation(
        evidence.identity,
        disposition,
        tuple(snapshots),
        (
            ""
            if verdict is not None
            else "demanding occurrence is unavailable or ambiguous"
            if demanding_read is None
            else "requirement deadline is unavailable or ambiguous"
        ),
    )
