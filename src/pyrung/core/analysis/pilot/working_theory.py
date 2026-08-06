"""Detached, immutable WorkingTheory knowledge and its pure reducer.

Stage 4 records the existing PILOT loop as shadow evidence.  Nothing in this
module owns a PLC world, chooses an action, or restores or promotes a
checkpoint.  All identities are supplied as detached semantic values so a
ledger can survive ordinary world rollback without retaining a future.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import StrEnum
from typing import Any, TypeAlias

from pyrsistent import PMap, pmap

TheoryId: TypeAlias = tuple[Any, ...]
TheoryVersionId: TypeAlias = tuple[Any, ...]
TheoryProgressId: TypeAlias = tuple[Any, ...]
TheoryReceiptId: TypeAlias = tuple[Any, ...]


class TheoryInvariantError(ValueError):
    """A fact conflicts with the immutable theory history."""


class TheoryStatus(StrEnum):
    OPEN = "open"
    PROVED = "proved"
    ABANDONED = "abandoned"


class TheoryAttemptDisposition(StrEnum):
    ACCEPTED_PROVISIONAL = "accepted-provisional"
    REJECTED_EXACT = "rejected-exact"
    REJECTED_EMPIRICAL = "rejected-empirical"
    WITNESS = "witness"
    INCOMPLETE = "incomplete"
    BUDGET_EXHAUSTED = "budget-exhausted"


class TheoryTermination(StrEnum):
    STUCK = "stuck"
    BUDGET = "budget"
    CONFLICT = "conflict"
    PROVED_IMPOSSIBLE = "proved-impossible"


@dataclass(frozen=True)
class TheoryBoundaryIdentity:
    """Detached identity of one exact observed execution boundary."""

    world_key: tuple[Any, ...]
    scan_id: int
    checkpoint_token: tuple[Any, ...]
    execution_owner_token: tuple[Any, ...] = ()
    occurrence_identity: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TheoryObjectiveSnapshot:
    """Predicate-free semantic form of a target-relative objective."""

    target_tag: str
    target_value: Any
    predicate_identity: tuple[Any, ...] | None = None
    frontier: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class TheoryObligationSnapshot:
    """Detached positive or negative occurrence obligation."""

    tag: str
    value: Any
    producer: tuple[Any, ...]
    consumer: tuple[Any, ...] | None
    required_shape: tuple[tuple[str, Any], ...]
    boundary: tuple[str, Any] | None
    terminal_target: bool
    polarity: str
    occurrence_selector: tuple[Any, ...] | None


@dataclass(frozen=True)
class TheoryRequirementSnapshot:
    """Detached exact requirement, including occurrence and deadline evidence."""

    semantic_identity: tuple[Any, ...]
    condition_identity: tuple[Any, ...]
    demanding_occurrence: tuple[Any, ...]
    deadline_occurrence: tuple[Any, ...]
    selected_writer: tuple[Any, ...]
    operand_authority: str
    source_world_key: tuple[Any, ...]
    source_scan: int | None
    checkpoint_token: tuple[Any, ...]
    execution_owner_token: tuple[Any, ...]
    phase: str
    status: str
    provenance: str
    scope: tuple[Any, ...]


@dataclass(frozen=True)
class TheoryClaim:
    """One selected producer-to-consumer claim at one exact source."""

    source: TheoryBoundaryIdentity
    objective: TheoryObjectiveSnapshot
    obligations: tuple[TheoryObligationSnapshot, ...]
    selected_boundary: TheoryBoundaryIdentity

    @property
    def identity(self) -> tuple[Any, ...]:
        return ("claim", self.source, self.objective, self.obligations, self.selected_boundary)


@dataclass(frozen=True)
class TheoryVersion:
    theory_id: TheoryId
    version_id: TheoryVersionId
    requirements: tuple[TheoryRequirementSnapshot, ...]
    source: TheoryBoundaryIdentity
    parent_version_id: TheoryVersionId | None = None


@dataclass(frozen=True)
class TheoryProgressSnapshot:
    theory_id: TheoryId
    progress_id: TheoryProgressId
    provisional_tip: TheoryBoundaryIdentity
    phase_receipts: tuple[tuple[Any, ...], ...]
    remaining_budget: int
    parent_progress_id: TheoryProgressId | None = None
    accepted_attempt_id: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class TheoryAttemptReceipt:
    theory_id: TheoryId
    version_id: TheoryVersionId
    attempt_id: tuple[Any, ...]
    source: TheoryBoundaryIdentity
    execution_owner_token: tuple[Any, ...]
    occurrence_evidence: tuple[Any, ...]
    act_identity: tuple[Any, ...]
    pilot_rung_identities: tuple[tuple[Any, ...], ...]
    disposition: TheoryAttemptDisposition
    evidence: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TheoryReceipt:
    receipt_id: TheoryReceiptId
    theory_id: TheoryId
    version_id: TheoryVersionId
    root: TheoryBoundaryIdentity
    promoted_landing: TheoryBoundaryIdentity
    fulfilled_obligations: tuple[Any, ...]
    requirement_observations: tuple[Any, ...]
    retained_pilot_rung_identities: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class TheoryTombstone:
    theory_id: TheoryId
    version_id: TheoryVersionId
    attempted_artifacts: tuple[tuple[Any, ...], ...]
    termination: TheoryTermination
    tombstone_id: tuple[Any, ...]


@dataclass(frozen=True)
class NogoodProof:
    """Proof-level negative evidence; shadow integration never manufactures one."""

    proof_id: tuple[Any, ...]
    executable_world_identity: tuple[Any, ...]
    claim_scope: tuple[Any, ...]
    finite_domain: tuple[Any, ...]
    completeness_evidence: tuple[Any, ...]
    rejected_artifacts: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class TheorySuccessor:
    parent_receipt_id: TheoryReceiptId
    successor_theory_id: TheoryId
    link_identity: tuple[Any, ...]


@dataclass(frozen=True)
class UnattributedTheoryEvidence:
    observation_id: tuple[Any, ...]
    boundary: TheoryBoundaryIdentity
    evidence: tuple[Any, ...]


@dataclass(frozen=True)
class WorkingTheory:
    theory_id: TheoryId
    claim_id: tuple[Any, ...]
    current_version_id: TheoryVersionId
    current_progress_id: TheoryProgressId
    status: TheoryStatus = TheoryStatus.OPEN
    attempt_ids: tuple[tuple[Any, ...], ...] = ()


@dataclass(frozen=True)
class TheoryLedger:
    claims: PMap[Any, TheoryClaim] = pmap()
    versions: PMap[Any, TheoryVersion] = pmap()
    progress: PMap[Any, TheoryProgressSnapshot] = pmap()
    theories: PMap[Any, WorkingTheory] = pmap()
    attempts: PMap[Any, TheoryAttemptReceipt] = pmap()
    receipts: PMap[Any, TheoryReceipt] = pmap()
    tombstones: PMap[Any, TheoryTombstone] = pmap()
    successors: PMap[Any, TheorySuccessor] = pmap()
    nogood_proofs: PMap[Any, NogoodProof] = pmap()
    unattributed: PMap[Any, UnattributedTheoryEvidence] = pmap()
    applied_facts: PMap[Any, TheoryFact] = pmap()


@dataclass(frozen=True)
class TheoryState:
    ledger: TheoryLedger = TheoryLedger()
    active_theory_id: TheoryId | None = None


@dataclass(frozen=True)
class OpenTheory:
    claim: TheoryClaim
    opening_identity: tuple[Any, ...]
    remaining_budget: int


@dataclass(frozen=True)
class RecordTheoryAttempt:
    theory_id: TheoryId
    version_id: TheoryVersionId
    attempt_identity: tuple[Any, ...]
    source: TheoryBoundaryIdentity
    execution_owner_token: tuple[Any, ...]
    occurrence_evidence: tuple[Any, ...]
    act_identity: tuple[Any, ...]
    pilot_rung_identities: tuple[tuple[Any, ...], ...]
    disposition: TheoryAttemptDisposition
    evidence: tuple[Any, ...] = ()


@dataclass(frozen=True)
class AdvanceTheory:
    theory_id: TheoryId
    version_id: TheoryVersionId
    accepted_attempt_id: tuple[Any, ...]
    source: TheoryBoundaryIdentity
    boundary: TheoryBoundaryIdentity
    advance_identity: tuple[Any, ...]
    phase_receipts: tuple[tuple[Any, ...], ...] = ()
    remaining_budget: int | None = None


@dataclass(frozen=True)
class RefineTheory:
    theory_id: TheoryId
    parent_version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    refined_source: TheoryBoundaryIdentity
    requirements: tuple[TheoryRequirementSnapshot, ...]
    refinement_identity: tuple[Any, ...]


@dataclass(frozen=True)
class ProveTheory:
    theory_id: TheoryId
    version_id: TheoryVersionId
    promoted_landing: TheoryBoundaryIdentity
    proof_identity: tuple[Any, ...]
    fulfilled_obligations: tuple[Any, ...] = ()
    requirement_observations: tuple[Any, ...] = ()
    retained_pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()


@dataclass(frozen=True)
class AbandonTheory:
    theory_id: TheoryId
    version_id: TheoryVersionId
    termination: TheoryTermination
    abandonment_identity: tuple[Any, ...]


@dataclass(frozen=True)
class OpenSuccessor:
    parent_receipt_id: TheoryReceiptId
    claim: TheoryClaim
    opening_identity: tuple[Any, ...]
    link_identity: tuple[Any, ...]
    remaining_budget: int


@dataclass(frozen=True)
class RecordUnattributedEvidence:
    observation: UnattributedTheoryEvidence


TheoryFact: TypeAlias = (
    OpenTheory
    | RecordTheoryAttempt
    | AdvanceTheory
    | RefineTheory
    | ProveTheory
    | AbandonTheory
    | OpenSuccessor
    | RecordUnattributedEvidence
)


_FORBIDDEN_TYPES = frozenset(
    {
        "Bearing",
        "CandidateRead",
        "OrientationRead",
        "OrientationWorld",
        "PLC",
        "PilotRung",
        "TraceChoice",
        "_CausalCheckpoint",
        "_World",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "bearing",
        "candidate_cursor",
        "checkpoint",
        "fork",
        "future_action",
        "navigation_act",
        "predicted_world",
        "route_suffix",
        "world",
    }
)


def assert_detached_theory_value(value: Any, *, path: str = "value") -> None:
    """Fail closed when a ledger fact contains a live or retained-future value."""

    if callable(value):
        raise TheoryInvariantError(f"{path} retains a callable")
    value_type = type(value)
    if value_type.__name__ in _FORBIDDEN_TYPES:
        raise TheoryInvariantError(f"{path} retains forbidden {value_type.__name__}")
    if value is None or isinstance(value, (str, bytes, int, float, bool, StrEnum)):
        return
    if isinstance(value, tuple | frozenset):
        for index, item in enumerate(value):
            assert_detached_theory_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, PMap):
        for key, item in value.items():
            assert_detached_theory_value(key, path=f"{path}.key")
            assert_detached_theory_value(item, path=f"{path}[{key!r}]")
        return
    if is_dataclass(value):
        for item in fields(value):
            if item.name in _FORBIDDEN_FIELDS:
                raise TheoryInvariantError(f"{path}.{item.name} is a retained-future field")
            assert_detached_theory_value(getattr(value, item.name), path=f"{path}.{item.name}")
        return
    raise TheoryInvariantError(f"{path} contains unsupported live type {value_type.__name__}")


def _put_unique(mapping: PMap[Any, Any], key: Any, value: Any, label: str) -> PMap[Any, Any]:
    current = mapping.get(key)
    if current is not None:
        if current == value:
            return mapping
        raise TheoryInvariantError(f"conflicting {label} identity {key!r}")
    return mapping.set(key, value)


def _active(state: TheoryState, theory_id: TheoryId) -> WorkingTheory:
    if state.active_theory_id != theory_id:
        raise TheoryInvariantError("fact does not address the active theory")
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        raise TheoryInvariantError("active theory is missing or closed")
    return theory


def _allowed_source_boundaries(
    state: TheoryState,
    theory: WorkingTheory,
) -> frozenset[TheoryBoundaryIdentity]:
    """Exact observed boundaries that may source the active lifecycle tip."""

    claim = state.ledger.claims[theory.claim_id]
    version = state.ledger.versions[theory.current_version_id]
    progress = state.ledger.progress[theory.current_progress_id]
    return frozenset((claim.source, version.source, progress.provisional_tip))


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


def _open(
    state: TheoryState,
    claim: TheoryClaim,
    opening_identity: tuple[Any, ...],
    remaining_budget: int,
    *,
    parent_receipt_id: TheoryReceiptId | None = None,
    link_identity: tuple[Any, ...] | None = None,
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
    if parent_receipt_id is not None:
        if parent_receipt_id not in ledger.receipts:
            raise TheoryInvariantError("successor parent receipt is missing")
        assert link_identity is not None
        successor = TheorySuccessor(parent_receipt_id, theory_id, link_identity)
        ledger = replace(
            ledger,
            successors=_put_unique(ledger.successors, link_identity, successor, "successor"),
        )
    return TheoryState(ledger, theory_id)


def _fact_identity(fact: TheoryFact) -> tuple[Any, ...]:
    if isinstance(fact, OpenTheory):
        return ("open", fact.opening_identity)
    if isinstance(fact, RecordTheoryAttempt):
        return ("attempt", fact.attempt_identity)
    if isinstance(fact, AdvanceTheory):
        return ("advance", fact.advance_identity)
    if isinstance(fact, RefineTheory):
        return ("refine", fact.refinement_identity)
    if isinstance(fact, ProveTheory):
        return ("prove", fact.proof_identity)
    if isinstance(fact, AbandonTheory):
        return ("abandon", fact.abandonment_identity)
    if isinstance(fact, OpenSuccessor):
        return ("successor", fact.link_identity)
    if isinstance(fact, RecordUnattributedEvidence):
        return ("unattributed", fact.observation.observation_id)
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
    if isinstance(fact, OpenSuccessor):
        return _open(
            state,
            fact.claim,
            fact.opening_identity,
            fact.remaining_budget,
            parent_receipt_id=fact.parent_receipt_id,
            link_identity=fact.link_identity,
        )
    if isinstance(fact, RecordUnattributedEvidence):
        observation = fact.observation
        updated = _put_unique(
            state.ledger.unattributed,
            observation.observation_id,
            observation,
            "unattributed observation",
        )
        return (
            state
            if updated is state.ledger.unattributed
            else replace(state, ledger=replace(state.ledger, unattributed=updated))
        )
    if isinstance(fact, RecordTheoryAttempt):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("attempt addresses a stale theory version")
        _require_allowed_source(state, theory, fact.source)
        if not fact.execution_owner_token:
            raise TheoryInvariantError("attempt execution owner evidence is missing")
        if not fact.occurrence_evidence:
            raise TheoryInvariantError("attempt occurrence evidence is missing")
        receipt = TheoryAttemptReceipt(
            fact.theory_id,
            fact.version_id,
            fact.attempt_identity,
            fact.source,
            fact.execution_owner_token,
            fact.occurrence_evidence,
            fact.act_identity,
            fact.pilot_rung_identities,
            fact.disposition,
            fact.evidence,
        )
        attempts = _put_unique(state.ledger.attempts, fact.attempt_identity, receipt, "attempt")
        if attempts is state.ledger.attempts:
            return state
        updated_theory = replace(theory, attempt_ids=(*theory.attempt_ids, fact.attempt_identity))
        ledger = replace(
            state.ledger,
            attempts=attempts,
            theories=state.ledger.theories.set(fact.theory_id, updated_theory),
        )
        return replace(state, ledger=ledger)
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
        remaining = (
            parent.remaining_budget if fact.remaining_budget is None else fact.remaining_budget
        )
        if remaining < 0 or remaining > parent.remaining_budget:
            raise TheoryInvariantError("advance cannot replenish or overdraw its budget")
        phases = (*parent.phase_receipts, *fact.phase_receipts)
        progress_id: TheoryProgressId = (
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
    if isinstance(fact, RefineTheory):
        theory = _active(state, fact.theory_id)
        if fact.parent_version_id != theory.current_version_id:
            raise TheoryInvariantError("refinement addresses a stale theory version")
        _require_allowed_source(state, theory, fact.source)
        if not fact.refined_source.checkpoint_token or (
            fact.refined_source.scan_id > 0 and not fact.refined_source.execution_owner_token
        ):
            raise TheoryInvariantError("refined source exact boundary evidence is missing")
        if fact.refined_source.scan_id < fact.source.scan_id:
            raise TheoryInvariantError("refined source cannot precede its source")
        parent = state.ledger.versions[fact.parent_version_id]
        novel = tuple(
            requirement
            for requirement in fact.requirements
            if requirement not in parent.requirements
        )
        if not novel:
            return state
        requirements = (*parent.requirements, *novel)
        version_id: TheoryVersionId = (
            "version",
            fact.theory_id,
            parent.version_id,
            fact.source,
            fact.refined_source,
            tuple(item.semantic_identity for item in requirements),
            fact.refinement_identity,
        )
        version = TheoryVersion(
            fact.theory_id,
            version_id,
            requirements,
            fact.refined_source,
            parent.version_id,
        )
        versions = _put_unique(state.ledger.versions, version_id, version, "version")
        updated = replace(theory, current_version_id=version_id)
        return replace(
            state,
            ledger=replace(
                state.ledger,
                versions=versions,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
        )
    if isinstance(fact, ProveTheory):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("proof addresses a stale theory version")
        claim = state.ledger.claims[theory.claim_id]
        receipt_id: TheoryReceiptId = ("receipt", fact.theory_id, fact.proof_identity)
        receipt = TheoryReceipt(
            receipt_id,
            fact.theory_id,
            fact.version_id,
            claim.source,
            fact.promoted_landing,
            fact.fulfilled_obligations,
            fact.requirement_observations,
            fact.retained_pilot_rung_identities,
        )
        receipts = _put_unique(state.ledger.receipts, receipt_id, receipt, "receipt")
        updated = replace(theory, status=TheoryStatus.PROVED)
        return TheoryState(
            replace(
                state.ledger,
                receipts=receipts,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
            None,
        )
    if isinstance(fact, AbandonTheory):
        theory = _active(state, fact.theory_id)
        if fact.version_id != theory.current_version_id:
            raise TheoryInvariantError("abandonment addresses a stale theory version")
        attempted = tuple(
            attempt_id
            for attempt_id in theory.attempt_ids
            if state.ledger.attempts[attempt_id].version_id == fact.version_id
        )
        tombstone_id = ("tombstone", fact.theory_id, fact.abandonment_identity)
        tombstone = TheoryTombstone(
            fact.theory_id,
            fact.version_id,
            attempted,
            fact.termination,
            tombstone_id,
        )
        tombstones = _put_unique(state.ledger.tombstones, tombstone_id, tombstone, "tombstone")
        updated = replace(theory, status=TheoryStatus.ABANDONED)
        return TheoryState(
            replace(
                state.ledger,
                tombstones=tombstones,
                theories=state.ledger.theories.set(fact.theory_id, updated),
            ),
            None,
        )
    raise AssertionError(f"unhandled theory fact {type(fact).__name__}")
