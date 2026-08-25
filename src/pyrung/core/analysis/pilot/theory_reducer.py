"""Apply typed lifecycle facts to immutable WorkingTheory state.

This module owns lifecycle commands, their validation, and the pure reducer.
It may depend on the detached WorkingTheory model; the model never depends on
reducer commands.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, TypeAlias

from pyrsistent import PMap

from pyrung.core.analysis.pilot.execution import ScanEntryConfiguration
from pyrung.core.analysis.pilot.working_theory import (
    ConductivityResearchFinding,
    IntrascanOrdinarySteerFinding,
    IntrascanTracebackFinding,
    IntrascanTracebackFrontier,
    TheoryAttemptDisposition,
    TheoryAttemptReceipt,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryId,
    TheoryInvariantError,
    TheoryLedger,
    TheoryPhaseKind,
    TheoryPhaseReceipt,
    TheoryProgressId,
    TheoryProgressSnapshot,
    TheoryRequirementSnapshot,
    TheoryState,
    TheoryStatus,
    TheoryTemporalIntent,
    TheoryTermination,
    TheoryVersion,
    TheoryVersionId,
    WorkingTheory,
    _allowed_source_boundaries,
    _transaction_execution_source,
    assert_detached_theory_value,
    theory_boundary_overlay_delta,
    theory_source_is_retained,
)
from pyrung.core.runner import EpochRef


@dataclass(frozen=True)
class OpenTheory:
    claim: TheoryClaim
    opening_identity: tuple[Any, ...]
    remaining_budget: int


@dataclass(frozen=True)
class RecordTheoryAttempt:
    receipt: TheoryAttemptReceipt


@dataclass(frozen=True)
class RecordConductivityResearch:
    """Record one completed evidence-only research question without scanning."""

    finding: ConductivityResearchFinding


@dataclass(frozen=True)
class RecordIntrascanTraceback:
    """Retain one completed occurrence research result without choosing an action."""

    finding: IntrascanTracebackFinding | IntrascanOrdinarySteerFinding


@dataclass(frozen=True)
class RecordIntrascanTracebackFrontier:
    """Retain one open backward hop without granting execution authority."""

    frontier: IntrascanTracebackFrontier


@dataclass(frozen=True)
class AdvanceTheory:
    theory_id: TheoryId
    version_id: TheoryVersionId
    accepted_attempt_id: tuple[Any, ...]
    source: TheoryBoundaryIdentity
    boundary: TheoryBoundaryIdentity
    advance_identity: tuple[Any, ...]
    phase_receipts: tuple[TheoryPhaseReceipt, ...] = ()
    remaining_budget: int | None = None
    execution_source: TheoryBoundaryIdentity | None = None


@dataclass(frozen=True)
class RebaseTheoryWorld:
    """Move one same-owner progress tip across already-owned overlay facts."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    rebased_source: TheoryBoundaryIdentity
    retained_pilot_rung_identities: tuple[tuple[Any, ...], ...]
    rebase_identity: tuple[Any, ...]
    superseded_pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()


@dataclass(frozen=True)
class ComposeTheoryCorrection:
    """Record one no-scan correction composed into the current theory world."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    composed_source: TheoryBoundaryIdentity
    requirement_identities: tuple[tuple[Any, ...], ...]
    composition_identity: tuple[Any, ...]
    configuration: ScanEntryConfiguration | None = None
    pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()
    superseded_configuration_identities: tuple[tuple[Any, ...], ...] = ()
    superseded_pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()
    research_finding_identity: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class RefineTheory:
    theory_id: TheoryId
    parent_version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    refined_source: TheoryBoundaryIdentity
    requirements: tuple[TheoryRequirementSnapshot, ...]
    refinement_identity: tuple[Any, ...]
    temporal_intent: TheoryTemporalIntent | None = None
    trigger_attempt_id: tuple[Any, ...] | None = None
    temporal_source: TheoryBoundaryIdentity | None = None


@dataclass(frozen=True)
class ProveTheory:
    theory_id: TheoryId
    version_id: TheoryVersionId
    proof_identity: tuple[Any, ...]
    accepted_attempt_id: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class AbandonTheory:
    theory_id: TheoryId
    version_id: TheoryVersionId
    termination: TheoryTermination
    abandonment_identity: tuple[Any, ...]


TheoryFact: TypeAlias = (
    OpenTheory
    | RecordTheoryAttempt
    | RecordConductivityResearch
    | RecordIntrascanTraceback
    | RecordIntrascanTracebackFrontier
    | AdvanceTheory
    | RebaseTheoryWorld
    | ComposeTheoryCorrection
    | RefineTheory
    | ProveTheory
    | AbandonTheory
)


def _put_unique(mapping: PMap[Any, Any], key: Any, value: Any, label: str) -> PMap[Any, Any]:
    current = mapping.get(key)
    if current is not None:
        if current == value:
            return mapping
        raise TheoryInvariantError(f"conflicting {label} identity {key!r}")
    return mapping.set(key, value)


def _ledger_identity(kind: str, *components: Any) -> tuple[Any, ...]:
    """Return a compact content handle for one immutable ledger row.

    Parent/version/progress relationships live in explicit fields and maps.
    Embedding the complete parent row identity into every new key turns an
    ordinary lookup into a recursive hash walk as the investigation grows.
    """

    digest = sha256(repr(components).encode("utf-8")).hexdigest()
    return (kind, digest)


def _active(state: TheoryState, theory_id: TheoryId) -> WorkingTheory:
    if state.active_theory_id != theory_id:
        raise TheoryInvariantError("fact does not address the active theory")
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        raise TheoryInvariantError("active theory is missing or closed")
    return theory


def _require_allowed_source(
    state: TheoryState,
    theory: WorkingTheory,
    source: TheoryBoundaryIdentity,
) -> None:
    if source not in _allowed_source_boundaries(state, theory):
        raise TheoryInvariantError(
            "fact source is not an active root, version, or progress boundary"
        )


def _version_descends_from(
    ledger: TheoryLedger,
    version_id: TheoryVersionId,
    ancestor_id: TheoryVersionId,
) -> bool:
    current: TheoryVersionId | None = version_id
    while current is not None:
        if current == ancestor_id:
            return True
        version = ledger.versions.get(current)
        if version is None:
            return False
        current = version.parent_version_id
    return False


def _require_traceback_parent(
    state: TheoryState,
    theory: WorkingTheory,
    item: (IntrascanTracebackFinding | IntrascanOrdinarySteerFinding | IntrascanTracebackFrontier),
) -> None:
    """Validate the exact attempted frontier goal which opened a child hop."""

    parent_fields = (
        item.parent_frontier_id,
        item.parent_producer_goal_id,
        item.parent_attempt_id,
    )
    if not any(value is not None for value in parent_fields):
        return
    if any(value is None for value in parent_fields):
        raise TheoryInvariantError("traceback parent ownership is incomplete")
    parent = state.ledger.traceback_frontiers.get(item.parent_frontier_id)
    attempt = state.ledger.attempts.get(item.parent_attempt_id)
    progress = state.ledger.progress[theory.current_progress_id]
    same_source = parent is not None and parent.source == item.source
    advanced_source = bool(
        parent is not None
        and parent.source != item.source
        and progress.provisional_tip == item.source
        and progress.accepted_attempt_id == item.parent_attempt_id
        and attempt is not None
        and attempt.disposition is TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
    )
    if (
        parent is None
        or parent.theory_id != item.theory_id
        or parent.identity not in theory.traceback_frontier_ids
        or not (same_source or advanced_source)
        or not _version_descends_from(
            state.ledger,
            item.version_id,
            parent.version_id,
        )
    ):
        raise TheoryInvariantError("traceback parent frontier is not owned")
    if item.parent_producer_goal_id not in tuple(goal.identity for goal in parent.producer_goals):
        raise TheoryInvariantError("traceback parent producer goal is not owned")
    if (
        attempt is None
        or attempt.theory_id != item.theory_id
        or attempt.source != parent.source
        or attempt.investigation_frontier_id != item.parent_frontier_id
        or attempt.producer_goal_id != item.parent_producer_goal_id
    ):
        raise TheoryInvariantError("traceback parent attempt did not select that goal")


def _open(
    state: TheoryState,
    claim: TheoryClaim,
    opening_identity: tuple[Any, ...],
    remaining_budget: int,
) -> TheoryState:
    if remaining_budget < 0:
        raise TheoryInvariantError("remaining theory budget cannot be negative")
    theory_id: TheoryId = ("theory", opening_identity)
    version_id: TheoryVersionId = ("version", theory_id, None, ())
    progress_id: TheoryProgressId = (
        "progress",
        theory_id,
        None,
        claim.source,
        (),
        remaining_budget,
    )
    working = WorkingTheory(theory_id, claim.identity, version_id, progress_id)
    version = TheoryVersion(theory_id, version_id, (), claim.source)
    progress = TheoryProgressSnapshot(theory_id, progress_id, claim.source, (), remaining_budget)
    existing = state.ledger.theories.get(theory_id)
    if existing is not None:
        if (
            existing != working
            or state.ledger.claims.get(claim.identity) != claim
            or state.ledger.versions.get(version_id) != version
            or state.ledger.progress.get(progress_id) != progress
        ):
            raise TheoryInvariantError("conflicting theory opening identity")
        if state.active_theory_id not in (None, theory_id):
            raise TheoryInvariantError("another theory is already active")
        return (
            state
            if state.active_theory_id == theory_id
            else replace(state, active_theory_id=theory_id)
        )
    if state.active_theory_id is not None:
        raise TheoryInvariantError("another theory is already active")
    ledger = replace(
        state.ledger,
        claims=_put_unique(state.ledger.claims, claim.identity, claim, "claim"),
        versions=_put_unique(state.ledger.versions, version_id, version, "version"),
        progress=_put_unique(state.ledger.progress, progress_id, progress, "progress"),
        theories=_put_unique(state.ledger.theories, theory_id, working, "theory"),
    )
    return TheoryState(ledger, theory_id)


def _fact_identity(fact: TheoryFact) -> tuple[Any, ...]:
    if isinstance(fact, OpenTheory):
        return ("open", fact.opening_identity)
    if isinstance(fact, RecordTheoryAttempt):
        return ("attempt", fact.receipt.attempt_id)
    if isinstance(fact, RecordConductivityResearch):
        return ("conductivity-research", fact.finding.identity)
    if isinstance(fact, RecordIntrascanTraceback):
        return ("intrascan-traceback", fact.finding.identity)
    if isinstance(fact, RecordIntrascanTracebackFrontier):
        return ("intrascan-traceback-frontier", fact.frontier.identity)
    if isinstance(fact, AdvanceTheory):
        return ("advance", fact.advance_identity)
    if isinstance(fact, RebaseTheoryWorld):
        return ("world-rebase", fact.rebase_identity)
    if isinstance(fact, ComposeTheoryCorrection):
        return ("compose", fact.composition_identity)
    if isinstance(fact, RefineTheory):
        return ("refine", fact.refinement_identity)
    if isinstance(fact, ProveTheory):
        return ("prove", fact.proof_identity)
    if isinstance(fact, AbandonTheory):
        return ("abandon", fact.abandonment_identity)
    raise AssertionError(f"unhandled theory fact {type(fact).__name__}")


def reduce_theory(state: TheoryState, fact: TheoryFact) -> TheoryState:
    """Apply one typed lifecycle fact without producing any executable decision."""

    assert_detached_theory_value(fact, path="fact")
    fact_identity = _fact_identity(fact)
    prior = state.ledger.applied_facts.get(fact_identity)
    if prior is not None:
        if prior == fact:
            return state
        raise TheoryInvariantError(f"conflicting lifecycle fact {fact_identity!r}")
    updated = _reduce_new_theory_fact(state, fact)
    return replace(
        updated,
        ledger=replace(
            updated.ledger,
            applied_facts=updated.ledger.applied_facts.set(fact_identity, fact),
        ),
    )


def _reduce_new_theory_fact(state: TheoryState, fact: TheoryFact) -> TheoryState:
    if isinstance(fact, OpenTheory):
        return _open(state, fact.claim, fact.opening_identity, fact.remaining_budget)
    if isinstance(fact, RecordTheoryAttempt):
        receipt = fact.receipt
        theory = _active(state, receipt.theory_id)
        if receipt.version_id != theory.current_version_id:
            raise TheoryInvariantError("attempt addresses a stale theory version")
        _require_allowed_source(state, theory, receipt.source)
        observation_boundary = receipt.observation_boundary
        if observation_boundary is None:
            raise TheoryInvariantError("attempt observation boundary is missing")
        _require_allowed_source(state, theory, observation_boundary)
        if observation_boundary != receipt.source:
            progress = state.ledger.progress[theory.current_progress_id]
            if (
                _transaction_execution_source(state, theory, progress) != receipt.source
                or progress.provisional_tip != observation_boundary
                or progress.accepted_attempt_id is None
            ):
                raise TheoryInvariantError(
                    "attempt observation is outside its active investigation scope"
                )
        if not isinstance(receipt.execution_ref, EpochRef):
            raise TheoryInvariantError("attempt execution owner evidence is missing")
        if not receipt.occurrence_evidence:
            raise TheoryInvariantError("attempt occurrence evidence is missing")
        if receipt.execution_source is not None and (
            receipt.execution_source.scan_id != receipt.source.scan_id
            or (
                receipt.execution_source.scan_id > 0
                and receipt.execution_source.execution_ref is None
            )
        ):
            raise TheoryInvariantError("attempt execution source evidence is inconsistent")
        if (receipt.investigation_frontier_id is None) != (receipt.producer_goal_id is None):
            raise TheoryInvariantError("attempt investigation ownership is incomplete")
        if receipt.investigation_frontier_id is not None:
            frontier = state.ledger.traceback_frontiers.get(receipt.investigation_frontier_id)
            if (
                frontier is None
                or frontier.theory_id != receipt.theory_id
                or frontier.identity not in theory.traceback_frontier_ids
                or frontier.source != observation_boundary
                or not _version_descends_from(
                    state.ledger,
                    receipt.version_id,
                    frontier.version_id,
                )
            ):
                raise TheoryInvariantError(
                    "attempt investigation frontier is not current theory work"
                )
            version = state.ledger.versions[receipt.version_id]
            owned_requirements = frozenset(
                requirement.semantic_identity for requirement in version.requirements
            )
            if not frozenset(frontier.requirement_identities) <= owned_requirements:
                raise TheoryInvariantError(
                    "attempt investigation frontier lost requirement ownership"
                )
            if receipt.producer_goal_id not in tuple(
                goal.identity for goal in frontier.producer_goals
            ):
                raise TheoryInvariantError("attempt producer goal does not belong to its frontier")
        attempts = _put_unique(state.ledger.attempts, receipt.attempt_id, receipt, "attempt")
        if attempts is state.ledger.attempts:
            return state
        updated_theory = replace(theory, attempt_ids=(*theory.attempt_ids, receipt.attempt_id))
        ledger = replace(
            state.ledger,
            attempts=attempts,
            theories=state.ledger.theories.set(receipt.theory_id, updated_theory),
        )
        return replace(state, ledger=ledger)
    if isinstance(fact, RecordConductivityResearch):
        finding = fact.finding
        theory = _active(state, finding.theory_id)
        if finding.version_id != theory.current_version_id:
            raise TheoryInvariantError("research addresses a stale theory version")
        _require_allowed_source(state, theory, finding.source)
        progress = state.ledger.progress[theory.current_progress_id]
        if finding.source != progress.provisional_tip:
            raise TheoryInvariantError("research source is not the current same-scan World")
        if not finding.enabling_reads or not finding.requirement_drift_identities:
            raise TheoryInvariantError("research finding lacks its stopping evidence")
        if finding.displacement.kind != "write" or any(
            read.kind != "read" for read in finding.enabling_reads
        ):
            raise TheoryInvariantError("research finding has malformed occurrence evidence")
        earlier_id, later_id = finding.compared_attempt_ids
        if (
            len(finding.comparison_identity) < 3
            or finding.comparison_identity[0] != "conductivity-comparison"
            or finding.comparison_identity[1:3] != (earlier_id, later_id)
        ):
            raise TheoryInvariantError("research comparison identity is inconsistent")
        earlier = state.ledger.attempts.get(earlier_id)
        later = state.ledger.attempts.get(later_id)
        if earlier is None or later is None:
            raise TheoryInvariantError("research comparison attempt is missing")
        if (
            earlier.theory_id != finding.theory_id
            or later.theory_id != finding.theory_id
            or earlier_id not in theory.attempt_ids
            or later_id not in theory.attempt_ids
        ):
            raise TheoryInvariantError("research comparison belongs to another theory")
        stopped_observations = tuple(
            observation
            for observation in later.conductivity_observations
            if observation.displacement == finding.displacement
        )
        retained_stopping_reads = tuple(
            read
            for observation in stopped_observations
            for read in (observation.displacement_enabling_reads or observation.observed_reads)
        )
        if not stopped_observations or any(
            read not in retained_stopping_reads for read in finding.enabling_reads
        ):
            raise TheoryInvariantError("research evidence is not owned by its later attempt")
        findings = _put_unique(
            state.ledger.research_findings,
            finding.identity,
            finding,
            "conductivity research finding",
        )
        if findings is state.ledger.research_findings:
            return state
        updated_theory = replace(
            theory,
            research_finding_ids=(*theory.research_finding_ids, finding.identity),
        )
        return replace(
            state,
            ledger=replace(
                state.ledger,
                research_findings=findings,
                theories=state.ledger.theories.set(finding.theory_id, updated_theory),
            ),
        )
    if isinstance(fact, RecordIntrascanTraceback):
        finding = fact.finding
        theory = _active(state, finding.theory_id)
        if finding.version_id != theory.current_version_id:
            raise TheoryInvariantError("traceback addresses a stale theory version")
        _require_allowed_source(state, theory, finding.source)
        progress = state.ledger.progress[theory.current_progress_id]
        if finding.source != progress.provisional_tip:
            raise TheoryInvariantError("traceback source is not the current same-scan World")
        if not finding.requirement_identities:
            raise TheoryInvariantError("traceback finding has no owned requirement")
        if not finding.hop_identity:
            raise TheoryInvariantError("traceback finding has no physical hop identity")
        version = state.ledger.versions[theory.current_version_id]
        owned_requirements = frozenset(
            requirement.semantic_identity for requirement in version.requirements
        )
        if not frozenset(finding.requirement_identities) <= owned_requirements:
            raise TheoryInvariantError("traceback finding requirement is not theory-owned")
        _require_traceback_parent(state, theory, finding)
        if finding.witness.request_identity != finding.request_identity:
            raise TheoryInvariantError("traceback witness answers another request")
        if isinstance(finding, IntrascanOrdinarySteerFinding):
            if (
                not finding.witness.applied_exactly_once
                or finding.witness.traceback_step is not None
                or finding.witness.blocked_edges
                or not finding.consumer_assignments
            ):
                raise TheoryInvariantError(
                    "ordinary-steer finding is not an exact exhausted hypothetical"
                )
            findings = _put_unique(
                state.ledger.traceback_findings,
                finding.identity,
                finding,
                "intrascan ordinary-steer finding",
            )
            if findings is state.ledger.traceback_findings:
                return state
            updated_theory = replace(
                theory,
                traceback_finding_ids=(*theory.traceback_finding_ids, finding.identity),
            )
            return replace(
                state,
                ledger=replace(
                    state.ledger,
                    traceback_findings=findings,
                    theories=state.ledger.theories.set(finding.theory_id, updated_theory),
                ),
            )
        realization = finding.realization
        natural_horizon = bool(
            finding.witness.consumer_stop_reached
            and finding.witness.consumer_horizon_read is not None
            and realization.consumer_stop_reached
            and realization.consumer_horizon_read == finding.witness.consumer_horizon_read
            and realization.consumer_scan == finding.source.scan_id + 1
            and realization.consumer_assignments
        )
        if finding.witness.traceback_step is None and not natural_horizon:
            raise TheoryInvariantError("traceback finding has no exact backward hop")
        direct = bool(
            realization.direct
            and realization.consumer_scan == finding.source.scan_id + 1
            and realization.consumer_write is not None
            and realization.consumer_assignments
        )
        staged = bool(
            realization.staged
            and realization.stage_scan == finding.source.scan_id + 1
            and realization.consumer_scan == finding.source.scan_id + 2
            and realization.consumer_write is not None
            and realization.consumer_assignments
        )
        if not (direct or staged or natural_horizon):
            raise TheoryInvariantError("traceback finding has no exact boundary realization")
        findings = _put_unique(
            state.ledger.traceback_findings,
            finding.identity,
            finding,
            "intrascan traceback finding",
        )
        if findings is state.ledger.traceback_findings:
            return state
        updated_theory = replace(
            theory,
            traceback_finding_ids=(*theory.traceback_finding_ids, finding.identity),
        )
        return replace(
            state,
            ledger=replace(
                state.ledger,
                traceback_findings=findings,
                theories=state.ledger.theories.set(finding.theory_id, updated_theory),
            ),
        )
    if isinstance(fact, RecordIntrascanTracebackFrontier):
        frontier = fact.frontier
        theory = _active(state, frontier.theory_id)
        if frontier.version_id != theory.current_version_id:
            raise TheoryInvariantError("traceback frontier addresses a stale theory version")
        _require_allowed_source(state, theory, frontier.source)
        progress = state.ledger.progress[theory.current_progress_id]
        if frontier.source != progress.provisional_tip:
            raise TheoryInvariantError(
                "traceback frontier source is not the current same-scan World"
            )
        if not frontier.requirement_identities:
            raise TheoryInvariantError("traceback frontier has no owned requirement")
        if not frontier.hop_identity:
            raise TheoryInvariantError("traceback frontier has no physical hop identity")
        version = state.ledger.versions[theory.current_version_id]
        owned_requirements = frozenset(
            requirement.semantic_identity for requirement in version.requirements
        )
        if not frozenset(frontier.requirement_identities) <= owned_requirements:
            raise TheoryInvariantError("traceback frontier requirement is not theory-owned")
        _require_traceback_parent(state, theory, frontier)
        if frontier.witness.request_identity != frontier.request_identity:
            raise TheoryInvariantError("traceback frontier witness answers another request")
        if frontier.witness.traceback_step is None:
            raise TheoryInvariantError("traceback frontier has no exact backward hop")
        if not frontier.producer_goals or any(
            goal.node_index < 0 for goal in frontier.producer_goals
        ):
            raise TheoryInvariantError("traceback frontier has no exact producer goal")
        if not frontier.consumer_assignments:
            raise TheoryInvariantError("traceback frontier lost its consumer boundary steer")
        frontiers = _put_unique(
            state.ledger.traceback_frontiers,
            frontier.identity,
            frontier,
            "intrascan traceback frontier",
        )
        if frontiers is state.ledger.traceback_frontiers:
            return state
        updated_theory = replace(
            theory,
            traceback_frontier_ids=(*theory.traceback_frontier_ids, frontier.identity),
        )
        return replace(
            state,
            ledger=replace(
                state.ledger,
                traceback_frontiers=frontiers,
                theories=state.ledger.theories.set(frontier.theory_id, updated_theory),
            ),
        )
    if isinstance(fact, AdvanceTheory):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("advance addresses a stale theory version")
        _require_allowed_source(state, theory, fact.source)
        attempt = state.ledger.attempts.get(fact.accepted_attempt_id)
        if attempt is None:
            raise TheoryInvariantError("advance has no linked attempt receipt")
        if attempt.theory_id != fact.theory_id or attempt.source != fact.source:
            raise TheoryInvariantError("advance does not match its attempt source")
        if attempt.disposition is not TheoryAttemptDisposition.ACCEPTED_PROVISIONAL:
            raise TheoryInvariantError("advance is not linked to an accepted attempt")
        if not _version_descends_from(state.ledger, fact.version_id, attempt.version_id):
            raise TheoryInvariantError("advance version does not descend from its attempt")
        parent = state.ledger.progress[theory.current_progress_id]
        if fact.source != parent.provisional_tip:
            raise TheoryInvariantError("advance source is not the current progress boundary")
        if fact.boundary == fact.source or fact.boundary.scan_id < fact.source.scan_id:
            raise TheoryInvariantError("advance boundary is not monotonic from its source")
        if fact.execution_source is not None:
            if fact.execution_source.scan_id != fact.source.scan_id:
                raise TheoryInvariantError(
                    "advance execution source is not the source scan boundary"
                )
            if fact.execution_source.scan_id > 0 and fact.execution_source.execution_ref is None:
                raise TheoryInvariantError("advance execution source evidence is missing")
        remaining = (
            parent.remaining_budget if fact.remaining_budget is None else fact.remaining_budget
        )
        if remaining < 0 or remaining > parent.remaining_budget:
            raise TheoryInvariantError("advance cannot replenish or overdraw its budget")
        for index, receipt in enumerate(fact.phase_receipts):
            if receipt.kind is TheoryPhaseKind.TRANSACTION_ATTEMPT:
                transaction = state.ledger.attempts.get(receipt.evidence_identity)
                if (
                    transaction is None
                    or receipt.execution_source is None
                    or transaction.execution_source != receipt.execution_source
                    or fact.execution_source != receipt.execution_source
                ):
                    raise TheoryInvariantError("transaction phase lacks its exact execution source")
            if receipt.kind is not TheoryPhaseKind.CONSUMER_STOP:
                continue
            preceding = (*parent.phase_receipts, *fact.phase_receipts[:index])
            if (
                receipt.execution_tip != fact.boundary
                or not any(item.kind is TheoryPhaseKind.TRANSACTION_ATTEMPT for item in preceding)
                or not any(item.kind is TheoryPhaseKind.CONSUMER_BOUNDARY for item in preceding)
            ):
                raise TheoryInvariantError(
                    "consumer stop lacks its exact tip, transaction, or consumer boundary"
                )
        phases = (*parent.phase_receipts, *fact.phase_receipts)
        progress_id: TheoryProgressId = _ledger_identity(
            "progress",
            fact.theory_id,
            parent.progress_id,
            fact.boundary,
            phases,
            remaining,
            fact.advance_identity,
            fact.accepted_attempt_id,
        )
        progress = TheoryProgressSnapshot(
            fact.theory_id,
            progress_id,
            fact.boundary,
            phases,
            remaining,
            parent.progress_id,
            fact.accepted_attempt_id,
            fact.execution_source,
        )
        progress_map = _put_unique(state.ledger.progress, progress_id, progress, "progress")
        if theory.current_progress_id == progress_id:
            return state
        updated = replace(theory, current_progress_id=progress_id)
        return replace(
            state,
            ledger=replace(
                state.ledger,
                progress=progress_map,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
        )
    if isinstance(fact, RebaseTheoryWorld):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("world rebase addresses a stale theory version")
        _require_allowed_source(state, theory, fact.source)
        parent = state.ledger.progress[theory.current_progress_id]
        if fact.source != parent.provisional_tip:
            raise TheoryInvariantError("world rebase source is not the current progress boundary")
        if fact.rebased_source == fact.source:
            raise TheoryInvariantError("world rebase changed its physical execution boundary")
        overlay_delta = theory_boundary_overlay_delta(fact.source, fact.rebased_source)
        if overlay_delta is None:
            raise TheoryInvariantError("world rebase changed its physical execution boundary")
        if not fact.retained_pilot_rung_identities and not fact.superseded_pilot_rung_identities:
            raise TheoryInvariantError("world rebase has no overlay change evidence")
        added, removed = overlay_delta
        if set(added) != set(fact.retained_pilot_rung_identities):
            raise TheoryInvariantError("world rebase overlay delta lacks exact ownership")
        if set(removed) != set(fact.superseded_pilot_rung_identities):
            raise TheoryInvariantError("world rebase overlay removal lacks exact ownership")
        owned: set[tuple[Any, ...]] = set()
        for receipt in parent.phase_receipts:
            owned.difference_update(receipt.superseded_pilot_rung_identities)
            if receipt.kind in {
                TheoryPhaseKind.TEMPORAL_SETUP,
                TheoryPhaseKind.REARM,
                TheoryPhaseKind.TRANSACTION_ATTEMPT,
                TheoryPhaseKind.CORRECTION_INSTALL,
            }:
                owned.update(receipt.pilot_rung_identities)
            elif receipt.kind is TheoryPhaseKind.CORRECTION_COMPOSITION:
                owned.update(receipt.pilot_rung_identities)
            elif receipt.kind is TheoryPhaseKind.WORLD_REBASE:
                owned.update(receipt.pilot_rung_identities)
        if not set(added) <= owned:
            unowned_identities = tuple(identity for identity in added if identity not in owned)
            unowned = tuple((identity[0], identity[1]) for identity in unowned_identities)
            related_owned = tuple(
                identity
                for identity in owned
                if any(identity[:2] == missing[:2] for missing in unowned_identities)
            )
            phases = tuple(
                (
                    receipt.kind.value,
                    tuple((identity[0], identity[1]) for identity in receipt.pilot_rung_identities),
                )
                for receipt in parent.phase_receipts
            )
            raise TheoryInvariantError(
                "world rebase uses an overlay not owned by this theory: "
                f"{unowned!r}; exact={unowned_identities!r}; "
                f"related_owned={related_owned!r}; phases={phases!r}"
            )
        superseded = {
            rung_identity
            for phase in parent.phase_receipts
            for rung_identity in phase.superseded_pilot_rung_identities
        }
        if not set(removed) <= superseded:
            raise TheoryInvariantError("world rebase removed an overlay without supersession")
        receipt = TheoryPhaseReceipt(
            kind=TheoryPhaseKind.WORLD_REBASE,
            evidence_identity=fact.rebase_identity,
            pilot_rung_identities=tuple(added),
            superseded_pilot_rung_identities=tuple(removed),
            execution_source=fact.rebased_source,
        )
        phases = (*parent.phase_receipts, receipt)
        progress_id: TheoryProgressId = _ledger_identity(
            "progress-rebase",
            fact.theory_id,
            parent.progress_id,
            fact.rebased_source,
            phases,
            fact.rebase_identity,
        )
        progress = TheoryProgressSnapshot(
            fact.theory_id,
            progress_id,
            fact.rebased_source,
            phases,
            parent.remaining_budget,
            parent.progress_id,
            parent.accepted_attempt_id,
            parent.execution_source,
        )
        progress_map = _put_unique(state.ledger.progress, progress_id, progress, "progress")
        updated = replace(theory, current_progress_id=progress_id)
        return replace(
            state,
            ledger=replace(
                state.ledger,
                progress=progress_map,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
        )
    if isinstance(fact, ComposeTheoryCorrection):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("composition addresses a stale theory version")
        _require_allowed_source(state, theory, fact.source)
        if not fact.requirement_identities:
            raise TheoryInvariantError("composition lacks its exact requirement identity")
        parent = state.ledger.progress[theory.current_progress_id]
        if fact.source != parent.provisional_tip:
            raise TheoryInvariantError("composition source is not the current progress boundary")
        if fact.composed_source.scan_id != fact.source.scan_id:
            raise TheoryInvariantError("no-scan composition advanced the physical scan")
        if fact.composed_source != fact.source:
            raise TheoryInvariantError("scan-entry composition changed the physical World")
        if fact.composed_source.scan_id > 0 and fact.composed_source.execution_ref is None:
            raise TheoryInvariantError("composition boundary evidence is incomplete")
        if (fact.configuration is None) == (not fact.pilot_rung_identities):
            raise TheoryInvariantError(
                "composition must contain exactly one configuration or PilotRung form"
            )
        prior_configurations = {
            configuration.identity
            for phase in parent.phase_receipts
            if phase.kind is TheoryPhaseKind.CORRECTION_COMPOSITION
            for configuration in phase.configurations
        }
        prior_superseded = {
            configuration_identity
            for phase in parent.phase_receipts
            for configuration_identity in phase.superseded_configuration_identities
        }
        active_prior = prior_configurations - prior_superseded
        if not set(fact.superseded_configuration_identities) <= active_prior:
            raise TheoryInvariantError("composition supersedes an unowned correction")
        if (
            fact.configuration is not None
            and fact.configuration.identity in fact.superseded_configuration_identities
        ):
            raise TheoryInvariantError("composition cannot supersede its new correction")
        prior_rungs: set[tuple[Any, ...]] = set()
        prior_superseded_rungs: set[tuple[Any, ...]] = set()
        for phase in parent.phase_receipts:
            prior_superseded_rungs.update(phase.superseded_pilot_rung_identities)
            if phase.kind in {
                TheoryPhaseKind.TEMPORAL_SETUP,
                TheoryPhaseKind.REARM,
                TheoryPhaseKind.TRANSACTION_ATTEMPT,
                TheoryPhaseKind.CORRECTION_COMPOSITION,
                TheoryPhaseKind.CORRECTION_INSTALL,
                TheoryPhaseKind.WORLD_REBASE,
            }:
                prior_rungs.update(phase.pilot_rung_identities)
        active_prior_rungs = prior_rungs - prior_superseded_rungs
        if not set(fact.superseded_pilot_rung_identities) <= active_prior_rungs:
            raise TheoryInvariantError("composition supersedes an unowned PilotRung")
        if set(fact.pilot_rung_identities) & set(fact.superseded_pilot_rung_identities):
            raise TheoryInvariantError("composition cannot supersede its new PilotRung")
        if fact.research_finding_identity is not None:
            finding = state.ledger.research_findings.get(fact.research_finding_identity)
            if finding is None:
                raise TheoryInvariantError("composition research finding is missing")
            if (
                finding.theory_id != fact.theory_id
                or finding.version_id != fact.version_id
                or finding.source != fact.source
            ):
                raise TheoryInvariantError("composition research finding has stale ownership")
        receipt = TheoryPhaseReceipt(
            kind=TheoryPhaseKind.CORRECTION_COMPOSITION,
            evidence_identity=fact.composition_identity,
            requirement_identities=fact.requirement_identities,
            pilot_rung_identities=fact.pilot_rung_identities,
            superseded_pilot_rung_identities=fact.superseded_pilot_rung_identities,
            configurations=((fact.configuration,) if fact.configuration is not None else ()),
            superseded_configuration_identities=(fact.superseded_configuration_identities),
        )
        progress_id: TheoryProgressId = _ledger_identity(
            "progress-compose",
            fact.theory_id,
            parent.progress_id,
            fact.source,
            fact.composed_source,
            fact.composition_identity,
        )
        progress = TheoryProgressSnapshot(
            fact.theory_id,
            progress_id,
            fact.composed_source,
            (*parent.phase_receipts, receipt),
            parent.remaining_budget,
            parent.progress_id,
            parent.accepted_attempt_id,
            (
                fact.composed_source
                if parent.execution_source == fact.source
                else parent.execution_source
            ),
        )
        progress_map = _put_unique(state.ledger.progress, progress_id, progress, "progress")
        updated = replace(theory, current_progress_id=progress_id)
        return replace(
            state,
            ledger=replace(
                state.ledger,
                progress=progress_map,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
        )
    if isinstance(fact, RefineTheory):
        theory = _active(state, fact.theory_id)
        if fact.parent_version_id != theory.current_version_id:
            raise TheoryInvariantError("refinement addresses a stale theory version")
        _require_allowed_source(state, theory, fact.source)
        if fact.refined_source.scan_id > 0 and fact.refined_source.execution_ref is None:
            raise TheoryInvariantError("refined source exact boundary evidence is missing")
        parent = state.ledger.versions[fact.parent_version_id]
        if fact.refined_source.scan_id < fact.source.scan_id and not (
            fact.requirements
            and all(
                requirement.provenance == "program-guard-rebase"
                and requirement.source_scan == fact.refined_source.scan_id
                and requirement.source_world_key == fact.refined_source.world_key
                for requirement in fact.requirements
            )
        ):
            raise TheoryInvariantError(
                "backward refinement lacks an exact program-guard rebase source"
            )
        temporal_intent = fact.temporal_intent
        trigger_attempt_id = fact.trigger_attempt_id
        if (temporal_intent is None) != (trigger_attempt_id is None):
            raise TheoryInvariantError(
                "temporal intent and triggering attempt must travel together"
            )
        if fact.temporal_source is not None:
            if temporal_intent is None:
                raise TheoryInvariantError("temporal source has no temporal intent")
            active_progress = state.ledger.progress[theory.current_progress_id]
            retained_transaction_source = (
                active_progress.execution_source
                if active_progress.accepted_attempt_id is not None
                else None
            )
            if fact.temporal_source not in (
                fact.source,
                fact.refined_source,
                retained_transaction_source,
            ):
                raise TheoryInvariantError(
                    "temporal source is not an exact refinement boundary: "
                    f"source={fact.source!r}; refined={fact.refined_source!r}; "
                    f"temporal={fact.temporal_source!r}; "
                    f"transaction={retained_transaction_source!r}"
                )
        if fact.trigger_attempt_id is not None:
            trigger = state.ledger.attempts.get(fact.trigger_attempt_id)
            carried_trigger = parent.trigger_attempt_id == fact.trigger_attempt_id
            if trigger is None:
                raise TheoryInvariantError("refinement triggering attempt is missing")
            if (
                trigger.theory_id != fact.theory_id
                or not _version_descends_from(
                    state.ledger,
                    parent.version_id,
                    trigger.version_id,
                )
                or not (carried_trigger or theory_source_is_retained(state, trigger.source))
            ):
                raise TheoryInvariantError(
                    "refinement trigger is not retained in its theory ancestry"
                )
            if trigger.disposition is not TheoryAttemptDisposition.REJECTED_EXACT:
                raise TheoryInvariantError("temporal refinement requires an exact rejected attempt")
        novel = tuple(
            requirement
            for requirement in fact.requirements
            if requirement not in parent.requirements
        )
        if (
            not novel
            and temporal_intent == parent.temporal_intent
            and trigger_attempt_id == parent.trigger_attempt_id
            and (
                temporal_intent is None
                or (
                    fact.temporal_source == parent.temporal_source
                    and fact.refined_source == parent.source
                    and tuple(fact.requirements) == parent.temporal_requirements
                )
            )
        ):
            return state
        requirements = (*parent.requirements, *novel)
        version_id: TheoryVersionId = _ledger_identity(
            "version",
            fact.theory_id,
            parent.version_id,
            fact.source,
            fact.refined_source,
            tuple(item.semantic_identity for item in requirements),
            temporal_intent,
            trigger_attempt_id,
            fact.temporal_source,
            tuple(item.semantic_identity for item in fact.requirements)
            if temporal_intent is not None
            else (),
            fact.refinement_identity,
        )
        version = TheoryVersion(
            fact.theory_id,
            version_id,
            requirements,
            fact.refined_source,
            parent.version_id,
            temporal_intent,
            trigger_attempt_id,
            fact.temporal_source,
            fact.requirements if temporal_intent is not None else (),
        )
        versions = _put_unique(state.ledger.versions, version_id, version, "version")
        progress_map = state.ledger.progress
        current_progress_id = theory.current_progress_id
        if fact.temporal_source is not None:
            parent_progress = state.ledger.progress[theory.current_progress_id]
            if fact.temporal_source != parent_progress.provisional_tip:
                current_progress_id = _ledger_identity(
                    "progress-refine",
                    fact.theory_id,
                    parent_progress.progress_id,
                    fact.temporal_source,
                    fact.refinement_identity,
                )
                rewound = TheoryProgressSnapshot(
                    fact.theory_id,
                    current_progress_id,
                    fact.temporal_source,
                    parent_progress.phase_receipts,
                    parent_progress.remaining_budget,
                    parent_progress.progress_id,
                    parent_progress.accepted_attempt_id,
                    parent_progress.execution_source,
                )
                progress_map = _put_unique(
                    progress_map,
                    current_progress_id,
                    rewound,
                    "progress",
                )
        updated = replace(
            theory,
            current_version_id=version_id,
            current_progress_id=current_progress_id,
        )
        return replace(
            state,
            ledger=replace(
                state.ledger,
                versions=versions,
                progress=progress_map,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
        )
    if isinstance(fact, ProveTheory):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("proof addresses a stale theory version")
        if fact.accepted_attempt_id is not None:
            accepted = state.ledger.attempts.get(fact.accepted_attempt_id)
            if accepted is None:
                raise TheoryInvariantError("proof's accepted attempt is missing")
            if (
                accepted.theory_id != theory.theory_id
                or accepted.version_id != fact.version_id
                or accepted.disposition is not TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
            ):
                raise TheoryInvariantError(
                    "proof's accepted attempt does not match its current theory version"
                )
        updated = replace(theory, status=TheoryStatus.PROVED)
        return TheoryState(
            replace(
                state.ledger,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
            None,
        )
    if isinstance(fact, AbandonTheory):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("abandonment addresses a stale theory version")
        updated = replace(theory, status=TheoryStatus.ABANDONED)
        return TheoryState(
            replace(
                state.ledger,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
            None,
        )
    raise AssertionError(f"unhandled theory fact {type(fact).__name__}")
