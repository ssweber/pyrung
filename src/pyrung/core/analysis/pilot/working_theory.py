"""Detached, immutable WorkingTheory knowledge and its pure reducer.

The ledger owns detached knowledge and typed temporal intent. It never owns a
PLC world, chooses an executable action, or restores or promotes a checkpoint.
The drive may resolve a typed request back to exact live receipts; all values
stored here remain semantic so the ledger survives rollback without retaining
a future.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from enum import StrEnum
from typing import Any, TypeAlias

from pyrsistent import PMap, pmap

from pyrung.core.analysis.pilot.world_key import _semantic_key

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


class TheoryTemporalIntent(StrEnum):
    """Typed next temporal move justified by one failed theory attempt."""

    SETUP_FIRST = "setup_first"
    RETRY_TOGETHER = "retry_together"


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
    projected_consumer: bool = False


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
    selected_artifact_identity: tuple[Any, ...] | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            "claim",
            self.source,
            self.objective,
            self.obligations,
            self.selected_boundary,
            self.selected_artifact_identity,
        )


@dataclass(frozen=True)
class TheoryVersion:
    theory_id: TheoryId
    version_id: TheoryVersionId
    requirements: tuple[TheoryRequirementSnapshot, ...]
    source: TheoryBoundaryIdentity
    parent_version_id: TheoryVersionId | None = None
    temporal_intent: TheoryTemporalIntent | None = None
    trigger_attempt_id: tuple[Any, ...] | None = None
    temporal_source: TheoryBoundaryIdentity | None = None
    temporal_requirements: tuple[TheoryRequirementSnapshot, ...] = ()


@dataclass(frozen=True)
class TheoryProgressSnapshot:
    theory_id: TheoryId
    progress_id: TheoryProgressId
    provisional_tip: TheoryBoundaryIdentity
    phase_receipts: tuple[tuple[Any, ...], ...]
    remaining_budget: int
    parent_progress_id: TheoryProgressId | None = None
    accepted_attempt_id: tuple[Any, ...] | None = None
    execution_source: TheoryBoundaryIdentity | None = None


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
    first_edge_identity: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class TheoryFirstEdgeExclusion:
    """One failed artifact scoped to an exact theory version and source."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    artifact_identity: tuple[Any, ...]
    attempt_id: tuple[Any, ...]
    disposition: TheoryAttemptDisposition


@dataclass(frozen=True)
class TheoryView:
    """Detached read-only projection of the active theory for navigation.

    The view contains no executable world or retained navigation decision.  Its
    attempts and first-edge exclusions are restricted to the current version
    and exact provisional source, so a failure cannot suppress the same move
    later in a different world or under a refined version.
    """

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    root: TheoryBoundaryIdentity
    claim: TheoryClaim
    requirements: tuple[TheoryRequirementSnapshot, ...]
    attempts: tuple[TheoryAttemptReceipt, ...]
    first_edge_exclusions: tuple[TheoryFirstEdgeExclusion, ...]
    temporal_intent: TheoryTemporalIntent | None = None
    trigger_attempt_id: tuple[Any, ...] | None = None
    trigger_act_identity: tuple[Any, ...] | None = None

    def excludes_first_edge(self, artifact_identity: tuple[Any, ...]) -> bool:
        """Whether this exact theory/version/source already rejected an artifact."""

        return any(
            exclusion.artifact_identity == artifact_identity
            for exclusion in self.first_edge_exclusions
        )


@dataclass(frozen=True)
class TemporalNeedRequest:
    """Detached request to reread one exact temporal need in the live world.

    The request deliberately carries requirement identities and occurrence
    evidence rather than a producer, satisfying value, Bearing, or branch
    cursor.  Compass resolves those choices afresh from the executable source.
    """

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    intent: TheoryTemporalIntent
    trigger_attempt_id: tuple[Any, ...]
    trigger_act_identity: tuple[Any, ...]
    requirements: tuple[TheoryRequirementSnapshot, ...]


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
    accepted_attempt_id: tuple[Any, ...] | None = None


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


def temporal_setup_rung_identities(state: TheoryState) -> frozenset[tuple[Any, ...]]:
    """Read exact rungs owned by accepted temporal setup phases.

    These detached receipts confer no navigation authority. They only let a
    later exact requirement distinguish Pilot's provisional setup value from
    user or program configuration.
    """

    owned: set[tuple[Any, ...]] = set()
    for progress in state.ledger.progress.values():
        for receipt in progress.phase_receipts:
            if len(receipt) < 4 or receipt[0] != "temporal-setup-established":
                continue
            identities = receipt[3]
            if isinstance(identities, tuple):
                owned.update(item for item in identities if isinstance(item, tuple))
    return frozenset(owned)


def theory_boundary_from_checkpoint(checkpoint: Any) -> TheoryBoundaryIdentity:
    """Detach one exact current-source checkpoint for a Compass claim."""

    key = getattr(checkpoint, "key", None)
    work = getattr(getattr(checkpoint, "world", None), "work", None)
    owner = getattr(checkpoint, "owner", None)
    if key is None or work is None or owner is None:
        raise TheoryInvariantError("claim source checkpoint evidence is incomplete")
    scan_id = work.state.scan_id
    world_key = tuple(key)
    owner_token: tuple[Any, ...] = ()
    lineage = getattr(work, "_causal_lineage", None)
    if lineage is not None:
        try:
            owners = tuple(
                (epoch, query)
                for epoch, query in lineage.seal_through(scan_id)
                if epoch.first_scan <= scan_id <= epoch.last_scan
            )
        except Exception as exc:  # noqa: BLE001 - malformed evidence fails closed below
            raise TheoryInvariantError("claim source execution evidence is unavailable") from exc
        if len(owners) > 1:
            raise TheoryInvariantError("claim source execution evidence is ambiguous")
        if owners:
            epoch, query = owners[0]
            owner_token = ("execution-owner", id(epoch), id(query))
    return TheoryBoundaryIdentity(
        world_key=world_key,
        scan_id=scan_id,
        checkpoint_token=(
            ("execution-boundary", world_key, scan_id, owner_token)
            if owner_token
            else ("checkpoint-owner", id(owner), world_key, scan_id)
        ),
        execution_owner_token=owner_token,
    )


def theory_claim(
    expectation: Any,
    objective: Any,
    source: TheoryBoundaryIdentity,
    *,
    selected_artifact_identity: tuple[Any, ...] | None = None,
) -> TheoryClaim:
    """Detach one selected producer claim without retaining its candidate world."""

    target = objective.target
    predicate = _semantic_key(target.predicate) if target.predicate is not None else None
    objective_snapshot = TheoryObjectiveSnapshot(
        target_tag=target.tag,
        target_value=_semantic_key(target.value),
        predicate_identity=(predicate if isinstance(predicate, tuple) else (predicate,))
        if predicate is not None
        else None,
        frontier=tuple((tag, _semantic_key(value)) for tag, value in objective.frontier),
    )
    obligations = tuple(
        TheoryObligationSnapshot(
            tag=obligation.tag,
            value=_semantic_key(obligation.value),
            producer=tuple(obligation.producer),
            consumer=(tuple(obligation.consumer) if obligation.consumer is not None else None),
            required_shape=tuple(
                (tag, _semantic_key(value)) for tag, value in obligation.required_shape
            ),
            boundary=(
                (obligation.boundary[0], _semantic_key(obligation.boundary[1]))
                if obligation.boundary is not None
                else None
            ),
            terminal_target=obligation.terminal_target,
            polarity=str(getattr(obligation, "polarity", "produce")),
            occurrence_selector=(selector if isinstance(selector, tuple) else (selector,))
            if (selector := _semantic_key(getattr(obligation, "occurrence_selector", None)))
            is not None
            else None,
            projected_consumer=getattr(obligation, "projected_consumer", False),
        )
        for obligation in expectation.obligations
    )
    claim = TheoryClaim(
        source=source,
        objective=objective_snapshot,
        obligations=obligations,
        selected_boundary=replace(
            source,
            occurrence_identity=(
                "selected-boundary",
                tuple(
                    (
                        item.producer,
                        item.consumer,
                        item.boundary,
                        item.polarity,
                        item.occurrence_selector,
                        item.projected_consumer,
                    )
                    for item in obligations
                ),
            ),
        ),
        selected_artifact_identity=selected_artifact_identity,
    )
    assert_detached_theory_value(claim, path="claim")
    return claim


def theory_occurrence_token(
    occurrence: Any,
    *,
    scan_id: int | None = None,
) -> tuple[Any, ...]:
    """Detach one exact dynamic read/write occurrence.

    Ordinary effect snapshots carry ``scan_id`` and ``dynamic_address``
    directly. Bootstrap snapshots share the same dynamic fields but inherit
    their one exact scan from the bootstrap receipt. Missing address fields
    fail closed instead of producing a weaker claim identity.
    """

    occurrence_scan = getattr(occurrence, "scan_id", scan_id)
    dynamic_address = getattr(occurrence, "dynamic_address", None)
    if dynamic_address is None:
        required = (
            "rung",
            "execution_kind",
            "caller_rung",
            "call_stack",
            "depth",
            "call_invocation",
            "run_order",
            "ordinal",
        )
        if any(not hasattr(occurrence, name) for name in required):
            raise TheoryInvariantError("dynamic occurrence address is incomplete")
        dynamic_address = (
            tuple(occurrence.rung),
            occurrence.execution_kind,
            occurrence.caller_rung,
            tuple(occurrence.call_stack),
            occurrence.depth,
            occurrence.call_invocation,
            occurrence.run_order,
            occurrence.ordinal,
        )
    if (
        getattr(occurrence, "kind", None) not in {"read", "write"}
        or not isinstance(getattr(occurrence, "tag", None), str)
        or not isinstance(occurrence_scan, int)
        or occurrence_scan < 0
        or not hasattr(occurrence, "values")
        or not hasattr(occurrence, "enabled")
    ):
        raise TheoryInvariantError("dynamic occurrence evidence is incomplete")
    token = (
        "dynamic-occurrence",
        occurrence.kind,
        occurrence.tag,
        tuple(_semantic_key(value) for value in occurrence.values),
        occurrence_scan,
        tuple(dynamic_address),
        occurrence.enabled,
    )
    assert_detached_theory_value(token, path="dynamic_occurrence")
    return token


def _claim_occurrence_token(occurrence: Any) -> tuple[Any, ...]:
    """Compatibility name for intrascan claim binding."""

    return theory_occurrence_token(occurrence)


def _observation_matches_claim_obligation(
    observation: Any,
    obligation: TheoryObligationSnapshot,
) -> bool:
    recorded = observation.obligation
    return (
        recorded.tag == obligation.tag
        and _semantic_key(recorded.value) == obligation.value
        and tuple(recorded.producer) == obligation.producer
        and (tuple(recorded.consumer) if recorded.consumer is not None else None)
        == obligation.consumer
        and tuple((tag, _semantic_key(value)) for tag, value in recorded.required_shape)
        == obligation.required_shape
        and (
            (recorded.boundary[0], _semantic_key(recorded.boundary[1]))
            if recorded.boundary is not None
            else None
        )
        == obligation.boundary
        and recorded.terminal_target == obligation.terminal_target
        and getattr(recorded, "projected_consumer", False) == obligation.projected_consumer
    )


def theory_claim_from_intrascan_witness(
    witness: Any,
    objective: Any,
    source: TheoryBoundaryIdentity,
    *,
    selected_artifact_identity: tuple[Any, ...] | None = None,
) -> TheoryClaim:
    """Bind a selected claim to one exact detached disposable execution.

    The witness contributes evidence only: its live fork has already been
    discarded.  A claim is admitted only when every obligation has one
    unambiguous satisfying observation owned by the same exact execution.
    """

    owner_token = tuple(getattr(witness, "execution_owner_token", ()))
    if len(owner_token) != 3 or owner_token[0] != "execution-owner":
        raise TheoryInvariantError("intrascan witness execution owner is unavailable")
    assertion_scan = getattr(witness, "assertion_scan", None)
    if not isinstance(assertion_scan, int) or assertion_scan <= source.scan_id:
        raise TheoryInvariantError("intrascan witness assertion boundary is invalid")

    claim = theory_claim(
        witness.overlay.expectation,
        objective,
        source,
        selected_artifact_identity=selected_artifact_identity,
    )
    observations = tuple(witness.observations)
    selected: list[tuple[Any, ...]] = []
    for obligation in claim.obligations:
        related = tuple(
            observation
            for observation in observations
            if _observation_matches_claim_obligation(observation, obligation)
        )
        if obligation.polarity == "prevent":
            satisfying = tuple(
                observation for observation in related if observation.disposition == "PREVENTED"
            )
        else:
            satisfying = tuple(
                observation for observation in related if observation.disposition == "SURVIVED"
            )
        if len(satisfying) != 1:
            raise TheoryInvariantError(
                "intrascan claim requires one unambiguous satisfying occurrence"
            )
        observation = satisfying[0]
        epoch = observation.execution_epoch
        if (
            epoch is None
            or not (epoch.first_scan <= assertion_scan <= epoch.last_scan)
            or (observation.appeared is not None and observation.appeared.scan_id != assertion_scan)
            or (
                observation.consumer_read is not None
                and observation.consumer_read.scan_id != assertion_scan
            )
        ):
            raise TheoryInvariantError("intrascan claim occurrence boundary is inconsistent")
        if obligation.polarity != "prevent" and observation.appeared is None:
            raise TheoryInvariantError("intrascan claim producer occurrence is unavailable")
        if obligation.consumer is not None and observation.consumer_read is None:
            raise TheoryInvariantError("intrascan claim consumer occurrence is unavailable")
        selected.append(
            (
                "obligation-occurrence",
                obligation.producer,
                obligation.consumer,
                obligation.occurrence_selector,
                observation.disposition,
                (
                    _claim_occurrence_token(observation.appeared)
                    if observation.appeared is not None
                    else None
                ),
                (
                    _claim_occurrence_token(observation.consumer_read)
                    if observation.consumer_read is not None
                    else None
                ),
                (epoch.first_scan, epoch.last_scan, epoch.initial_scan_id),
                _semantic_key(observation),
            )
        )

    requirement_evidence = tuple(
        (
            "requirement-observation",
            _semantic_key(item.requirement_identity),
            str(item.disposition),
            tuple(_claim_occurrence_token(read) for read in item.observed_reads),
            item.detail,
        )
        for item in witness.requirement_observations
    )
    bound = replace(
        claim,
        selected_boundary=replace(
            source,
            scan_id=assertion_scan,
            execution_owner_token=owner_token,
            occurrence_identity=(
                "intrascan-witness",
                tuple(selected),
                requirement_evidence,
            ),
        ),
    )
    assert_detached_theory_value(bound, path="intrascan_claim")
    return bound


def theory_boundary_claim(
    objective: Any,
    source: TheoryBoundaryIdentity,
    boundary: Any,
    *,
    selected_artifact_identity: tuple[Any, ...] | None = None,
) -> TheoryClaim:
    """Detach an already-owned cross-scan boundary before Stage 7 transfer."""

    if not source.execution_owner_token:
        raise TheoryInvariantError("cross-scan boundary owner is unavailable")

    target = objective.target
    predicate = _semantic_key(target.predicate) if target.predicate is not None else None
    objective_snapshot = TheoryObjectiveSnapshot(
        target_tag=target.tag,
        target_value=_semantic_key(target.value),
        predicate_identity=(predicate if isinstance(predicate, tuple) else (predicate,))
        if predicate is not None
        else None,
        frontier=tuple((tag, _semantic_key(value)) for tag, value in objective.frontier),
    )
    heading = getattr(boundary, "boundary", None)
    selected_boundary = replace(
        source,
        occurrence_identity=(
            "selected-cross-scan-boundary",
            getattr(boundary, "channel_tag", None),
            _semantic_key(getattr(boundary, "target_value", None)),
            _semantic_key(heading),
        ),
    )
    claim = TheoryClaim(
        source=source,
        objective=objective_snapshot,
        obligations=(),
        selected_boundary=selected_boundary,
        selected_artifact_identity=selected_artifact_identity,
    )
    assert_detached_theory_value(claim, path="boundary_claim")
    return claim


def theory_view(state: TheoryState) -> TheoryView | None:
    """Return the exact active navigation view, or ``None`` when no theory is open."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return None
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        raise TheoryInvariantError("active theory is missing or closed")
    claim = state.ledger.claims.get(theory.claim_id)
    version = state.ledger.versions.get(theory.current_version_id)
    progress = state.ledger.progress.get(theory.current_progress_id)
    if claim is None or version is None or progress is None:
        raise TheoryInvariantError("active theory projection is incomplete")

    attempts = tuple(
        attempt
        for attempt_id in theory.attempt_ids
        for attempt in (state.ledger.attempts.get(attempt_id),)
        if attempt is not None
        and attempt.version_id == version.version_id
        and attempt.source == progress.provisional_tip
    )
    rejected = frozenset(
        (
            TheoryAttemptDisposition.REJECTED_EXACT,
            TheoryAttemptDisposition.REJECTED_EMPIRICAL,
        )
    )
    exclusions = tuple(
        TheoryFirstEdgeExclusion(
            theory_id,
            version.version_id,
            attempt.source,
            attempt.first_edge_identity,
            attempt.attempt_id,
            attempt.disposition,
        )
        for attempt in attempts
        if attempt.disposition in rejected and attempt.first_edge_identity is not None
    )
    trigger = (
        state.ledger.attempts.get(version.trigger_attempt_id)
        if version.trigger_attempt_id is not None
        else None
    )
    view = TheoryView(
        theory_id=theory_id,
        version_id=version.version_id,
        source=progress.provisional_tip,
        root=version.source,
        claim=claim,
        requirements=version.requirements,
        attempts=attempts,
        first_edge_exclusions=exclusions,
        temporal_intent=version.temporal_intent,
        trigger_attempt_id=version.trigger_attempt_id,
        trigger_act_identity=(trigger.act_identity if trigger is not None else None),
    )
    assert_detached_theory_value(view, path="theory_view")
    return view


def temporal_need_request(state: TheoryState) -> TemporalNeedRequest | None:
    """Project the active temporal need without choosing how to satisfy it."""

    view = theory_view(state)
    if view is None or view.temporal_intent is None:
        return None
    trigger_id = view.trigger_attempt_id
    trigger = state.ledger.attempts.get(trigger_id) if trigger_id is not None else None
    if trigger is None:
        raise TheoryInvariantError("temporal intent has no triggering attempt")
    if trigger.theory_id != view.theory_id:
        raise TheoryInvariantError("temporal trigger belongs to another theory")
    if trigger.disposition is not TheoryAttemptDisposition.REJECTED_EXACT:
        raise TheoryInvariantError("temporal trigger is not an exact rejection")
    theory = state.ledger.theories[view.theory_id]
    version = state.ledger.versions[theory.current_version_id]
    if not version.temporal_requirements:
        raise TheoryInvariantError("temporal intent has no exact requirements")
    request = TemporalNeedRequest(
        theory_id=view.theory_id,
        version_id=view.version_id,
        # The trigger explains *why* this need exists; the provisional tip is
        # where the next scan must begin.  Replaying the trigger's old source
        # after an accepted setup/rearm scan would prove work and then perform
        # it a second time instead of following conductivity forward.
        source=version.temporal_source or view.source,
        intent=view.temporal_intent,
        trigger_attempt_id=trigger.attempt_id,
        trigger_act_identity=trigger.act_identity,
        # A version's requirements are the immutable evidence accumulated by
        # the theory.  Only the requirements attached to this temporal
        # transition are executable work for Compass.  Exporting the whole
        # ledger here makes discharged history masquerade as a live handoff.
        requirements=version.temporal_requirements,
    )
    assert_detached_theory_value(request, path="temporal_need_request")
    return request


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
    first_edge_identity: tuple[Any, ...] | None = None


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
    execution_source: TheoryBoundaryIdentity | None = None


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
    promoted_landing: TheoryBoundaryIdentity
    proof_identity: tuple[Any, ...]
    fulfilled_obligations: tuple[Any, ...] = ()
    requirement_observations: tuple[Any, ...] = ()
    retained_pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()
    accepted_attempt_id: tuple[Any, ...] | None = None


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
    """Exact observed boundaries that may source the active lifecycle tip.

    The parent progress boundary is the retained source of the just-accepted
    adjacent scan.  Its monitor receipt may establish the next temporal need
    only after progress has advanced to the landing, so it remains admissible
    for that one lifecycle handoff.
    """

    claim = state.ledger.claims[theory.claim_id]
    version = state.ledger.versions[theory.current_version_id]
    progress = state.ledger.progress[theory.current_progress_id]
    prior = (
        state.ledger.progress[progress.parent_progress_id].provisional_tip
        if progress.parent_progress_id is not None
        else None
    )
    return frozenset(
        boundary
        for boundary in (
            claim.source,
            version.source,
            progress.provisional_tip,
            progress.execution_source,
            prior,
        )
        if boundary is not None
    )


def theory_source_is_retained(
    state: TheoryState,
    source: TheoryBoundaryIdentity,
) -> bool:
    """Whether an exact boundary may still source the active theory's next fact."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return False
    theory = state.ledger.theories.get(theory_id)
    return bool(
        theory is not None
        and theory.status is TheoryStatus.OPEN
        and source in _allowed_source_boundaries(state, theory)
    )


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
            fact.first_edge_identity,
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
        if fact.execution_source is not None:
            if fact.execution_source.scan_id != fact.source.scan_id:
                raise TheoryInvariantError(
                    "advance execution source is not the source scan boundary"
                )
            if not fact.execution_source.checkpoint_token or (
                fact.execution_source.scan_id > 0
                and not fact.execution_source.execution_owner_token
            ):
                raise TheoryInvariantError("advance execution source evidence is missing")
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
    if isinstance(fact, RefineTheory):
        theory = _active(state, fact.theory_id)
        if fact.parent_version_id != theory.current_version_id:
            raise TheoryInvariantError("refinement addresses a stale theory version")
        _require_allowed_source(state, theory, fact.source)
        if not fact.refined_source.checkpoint_token or (
            fact.refined_source.scan_id > 0 and not fact.refined_source.execution_owner_token
        ):
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
            if fact.temporal_source not in (fact.source, fact.refined_source):
                raise TheoryInvariantError(
                    "temporal source is not an exact refinement boundary"
                )
        if fact.trigger_attempt_id is not None:
            trigger = state.ledger.attempts.get(fact.trigger_attempt_id)
            if trigger is None:
                raise TheoryInvariantError("refinement triggering attempt is missing")
            if (
                trigger.theory_id != fact.theory_id
                or trigger.version_id != parent.version_id
                or trigger.source != fact.source
            ):
                raise TheoryInvariantError(
                    "refinement triggering attempt does not match its theory version and source"
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
        ):
            return state
        requirements = (*parent.requirements, *novel)
        version_id: TheoryVersionId = (
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
                current_progress_id = (
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
                    (),
                    parent_progress.remaining_budget,
                    parent_progress.progress_id,
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
        claim = state.ledger.claims[theory.claim_id]
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
            fact.accepted_attempt_id,
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
