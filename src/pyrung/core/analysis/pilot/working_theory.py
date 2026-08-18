"""Detached, immutable WorkingTheory knowledge and its pure reducer.

The ledger owns detached knowledge and typed temporal intent. It never owns a
PLC world, chooses an executable action, or restores or promotes a checkpoint.
The drive may resolve a typed request back to exact live receipts; all values
stored here remain semantic so the ledger survives rollback without retaining
a future.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Any, TypeAlias

from pyrsistent import PMap, pmap

from pyrung.core.analysis.pilot.effects import (
    ConsumerBoundary,
    EffectObservationSnapshot,
    EffectOccurrenceSnapshot,
)
from pyrung.core.analysis.pilot.world_key import _semantic_key

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.intrascan import (
        IntrascanBoundaryRealization,
        IntrascanProducerGoal,
        IntrascanTracebackWitness,
    )

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
    RETRY_THROUGH_DEADLINE = "retry_through_deadline"


class TheoryPhaseKind(StrEnum):
    """Typed meaning of one immutable progress-phase receipt."""

    SCAN_PROGRESS = "scan_progress"
    TEMPORAL_SETUP = "temporal_setup"
    REARM = "rearm"
    TRANSACTION_ATTEMPT = "transaction_attempt"
    CONSUMER_BOUNDARY = "consumer_boundary"
    CONSUMER_EXECUTION_HORIZON = "consumer_execution_horizon"
    CORRECTION_COMPOSITION = "correction_composition"
    CORRECTION_INSTALL = "correction_install"
    WORLD_REBASE = "world_rebase"


@dataclass(frozen=True)
class TheoryPhaseReceipt:
    """Named evidence retained when a theory phase changes its working world."""

    kind: TheoryPhaseKind
    evidence_identity: tuple[Any, ...]
    requirement_identities: tuple[tuple[Any, ...], ...] = ()
    pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()
    superseded_pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()
    execution_source: TheoryBoundaryIdentity | None = None
    execution_tip: TheoryBoundaryIdentity | None = None


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
    obstruction_occurrence: tuple[Any, ...] | None = None


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
    phase_receipts: tuple[TheoryPhaseReceipt, ...]
    remaining_budget: int
    parent_progress_id: TheoryProgressId | None = None
    accepted_attempt_id: tuple[Any, ...] | None = None
    execution_source: TheoryBoundaryIdentity | None = None


@dataclass(frozen=True)
class ConsumerExecutionHorizon:
    """Furthest accepted World still owned by one consumer-bound transaction."""

    transaction_attempt_id: tuple[Any, ...]
    source: TheoryBoundaryIdentity
    consumer_boundary_attempt_id: tuple[Any, ...]
    consumer_boundary: ConsumerBoundary
    tip: TheoryBoundaryIdentity


@dataclass(frozen=True)
class ProgramTransaction:
    """Detached identity of one program-owned channel transition.

    Navigation may describe the same transition first as an inner heading
    wrapped by an outer route and later as a direct heading.  Preserve the
    effective outer channel/effect so WorkingTheory can correlate those two
    observations without retaining an executable Act or decoding its identity.
    """

    channel_tag: str
    source_value: Any
    target_value: Any
    effect_tag: str
    effect_value: Any

    @classmethod
    def from_heading(
        cls,
        heading: Any,
        snapshot: Mapping[str, Any],
    ) -> ProgramTransaction | None:
        if heading is None:
            return None
        route = getattr(heading, "route", None)
        channel_tag = route.channel_tag if route is not None else heading.channel_tag
        target_value = route.target_value if route is not None else heading.target_value
        source_value = route.from_value if route is not None else snapshot.get(channel_tag)
        effect_tag = (
            route.effect_tag if route is not None and route.effect_tag is not None else channel_tag
        )
        effect_value = (
            route.effect_value
            if route is not None and route.effect_tag is not None
            else target_value
        )
        return cls(
            channel_tag=channel_tag,
            source_value=_semantic_key(source_value),
            target_value=_semantic_key(target_value),
            effect_tag=effect_tag,
            effect_value=_semantic_key(effect_value),
        )

    @classmethod
    def from_effect_observation(
        cls,
        observation: EffectObservationSnapshot,
        *,
        channel_tag: str,
        target_value: Any,
    ) -> ProgramTransaction | None:
        """Name an unheaded transaction from its exact physical target write."""

        appeared = observation.appeared
        values = tuple(appeared.values) if appeared is not None else ()
        if (
            appeared is None
            or appeared.kind != "write"
            or appeared.tag != channel_tag
            or len(values) != 2
            or _semantic_key(values[-1]) != _semantic_key(target_value)
        ):
            return None
        return cls(
            channel_tag=channel_tag,
            source_value=_semantic_key(values[0]),
            target_value=_semantic_key(target_value),
            effect_tag=channel_tag,
            effect_value=_semantic_key(target_value),
        )


@dataclass(frozen=True)
class TheoryInvestigationScope:
    """Exact transaction receipt paired with its current proved frontier.

    ``execution_source`` is the boundary from which the accepted temporal
    transaction actually ran. ``frontier`` is the latest World proved by that
    execution. ``transaction_act_pairs`` are immutable facts about that
    execution, not a retained navigation decision; Compass must authorize a
    new attempt against ``frontier`` before they can run again.
    """

    theory_id: TheoryId
    version_id: TheoryVersionId
    execution_source: TheoryBoundaryIdentity
    frontier: TheoryBoundaryIdentity
    source_progress_id: TheoryProgressId
    frontier_progress_id: TheoryProgressId
    accepted_attempt_id: tuple[Any, ...]
    transaction_attempt_id: tuple[Any, ...] | None = None
    transaction_act_identity: tuple[Any, ...] | None = None
    transaction_act_pairs: tuple[tuple[str, Any], ...] = ()
    transaction_selected_pairs: tuple[tuple[str, Any], ...] = ()
    consumer_boundary: ConsumerBoundary | None = None
    consumer_boundary_attempt_id: tuple[Any, ...] | None = None
    consumer_execution_horizon: ConsumerExecutionHorizon | None = None
    transaction_rearmed: bool = False
    retry_act_identity: tuple[Any, ...] | None = None


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
    # Exact values observed at execution. Unlike ``act_identity`` these are
    # not semantic-key encodings, so Compass never has to decode an identity
    # into an executable value.
    act_pairs: tuple[tuple[str, Any], ...] = ()
    selected_act_pairs: tuple[tuple[str, Any], ...] = ()
    evidence: tuple[Any, ...] = ()
    first_edge_identity: tuple[Any, ...] | None = None
    # Exact immutable effect observations pass through from the execution
    # boundary. WorkingTheory stores the facts; Compass may derive a
    # conductivity/front view without the ledger retaining that conclusion.
    conductivity_observations: tuple[EffectObservationSnapshot, ...] = ()
    consumer_boundary: ConsumerBoundary | None = None
    execution_source: TheoryBoundaryIdentity | None = None
    investigation_frontier_id: tuple[Any, ...] | None = None
    producer_goal_id: tuple[Any, ...] | None = None
    observation_boundary: TheoryBoundaryIdentity | None = None
    program_transaction: ProgramTransaction | None = None


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
class ConductivityResearchFinding:
    """Frozen evidence that one exact repeated conductivity stop was researched.

    This receipt deliberately contains no executable answer. Compass may use
    it to acknowledge a completed question, then must reread the current World
    before choosing any correction or steer.
    """

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    comparison_identity: tuple[Any, ...]
    compared_attempt_ids: tuple[tuple[Any, ...], tuple[Any, ...]]
    displacement: EffectOccurrenceSnapshot
    enabling_reads: tuple[EffectOccurrenceSnapshot, ...]
    requirement_drift_identities: tuple[tuple[Any, ...], ...]

    @property
    def request_identity(self) -> tuple[Any, ...]:
        """The exact evidence question answered by this finding."""

        return (
            "conductivity-research-request",
            self.theory_id,
            self.version_id,
            self.source,
            self.comparison_identity,
            self.displacement,
            self.enabling_reads,
        )

    @property
    def identity(self) -> tuple[Any, ...]:
        """Compact handle for the exact finding retained in the ledger.

        The finding itself remains full pass-through evidence. Receipts carry
        this deterministic handle so a later version does not recursively
        embed its entire ancestral World and theory identity.
        """

        payload = (
            self.request_identity,
            self.compared_attempt_ids,
            self.requirement_drift_identities,
        )
        digest = sha256(repr(payload).encode("utf-8")).hexdigest()
        return ("conductivity-research-finding", digest)


@dataclass(frozen=True)
class IntrascanTracebackFinding:
    """Detached proof of a real or naturally reached consumer realization."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    request_identity: tuple[Any, ...]
    hop_identity: tuple[Any, ...]
    requirement_identities: tuple[tuple[Any, ...], ...]
    witness: IntrascanTracebackWitness
    realization: IntrascanBoundaryRealization
    parent_frontier_id: tuple[Any, ...] | None = None
    parent_producer_goal_id: tuple[Any, ...] | None = None
    parent_attempt_id: tuple[Any, ...] | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        payload = (
            self.theory_id,
            self.version_id,
            self.source,
            self.request_identity,
            self.hop_identity,
            self.requirement_identities,
            self.witness,
            self.realization,
            self.parent_frontier_id,
            self.parent_producer_goal_id,
            self.parent_attempt_id,
        )
        digest = sha256(repr(payload).encode("utf-8")).hexdigest()
        return ("intrascan-traceback-finding", digest)


@dataclass(frozen=True)
class IntrascanOrdinarySteerFinding:
    """Exact hypothetical found no intrascan handoff; retry as user configuration."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    request_identity: tuple[Any, ...]
    hop_identity: tuple[Any, ...]
    requirement_identities: tuple[tuple[Any, ...], ...]
    witness: IntrascanTracebackWitness
    consumer_assignments: tuple[tuple[str, Any], ...]
    parent_frontier_id: tuple[Any, ...] | None = None
    parent_producer_goal_id: tuple[Any, ...] | None = None
    parent_attempt_id: tuple[Any, ...] | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        payload = (
            self.theory_id,
            self.version_id,
            self.source,
            self.request_identity,
            self.hop_identity,
            self.requirement_identities,
            self.witness,
            self.consumer_assignments,
            self.parent_frontier_id,
            self.parent_producer_goal_id,
            self.parent_attempt_id,
        )
        digest = sha256(repr(payload).encode("utf-8")).hexdigest()
        return ("intrascan-ordinary-steer-finding", digest)


@dataclass(frozen=True)
class IntrascanTracebackFrontier:
    """Detached backward hop whose real producer still needs navigation.

    Unlike :class:`IntrascanTracebackFinding`, this receipt grants no scan
    authority.  It keeps the exact hypothetical and its bounded static writer
    goals alive while Compass derives one ordinary act from the current World.
    """

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    request_identity: tuple[Any, ...]
    hop_identity: tuple[Any, ...]
    requirement_identities: tuple[tuple[Any, ...], ...]
    witness: IntrascanTracebackWitness
    producer_goals: tuple[IntrascanProducerGoal, ...]
    consumer_assignments: tuple[tuple[str, Any], ...] = ()
    parent_frontier_id: tuple[Any, ...] | None = None
    parent_producer_goal_id: tuple[Any, ...] | None = None
    parent_attempt_id: tuple[Any, ...] | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        payload = (
            self.theory_id,
            self.version_id,
            self.source,
            self.request_identity,
            self.hop_identity,
            self.requirement_identities,
            self.witness,
            self.producer_goals,
            self.consumer_assignments,
            self.parent_frontier_id,
            self.parent_producer_goal_id,
            self.parent_attempt_id,
        )
        digest = sha256(repr(payload).encode("utf-8")).hexdigest()
        return ("intrascan-traceback-frontier", digest)


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
    investigation_scope: TheoryInvestigationScope | None
    claim: TheoryClaim
    requirements: tuple[TheoryRequirementSnapshot, ...]
    # Root-to-tip immutable version chain. This supplies historical context
    # for evidence readers without broadening current-version route authority.
    version_history: tuple[TheoryVersion, ...]
    attempts: tuple[TheoryAttemptReceipt, ...]
    # Full attempt lineage for explicitly selected investigation work.  Unlike
    # ordinary attempts, an accepted producer stage must remain visible after
    # it advances the provisional World so the retained frontier can discharge
    # its consumer from that new boundary.
    investigation_attempts: tuple[TheoryAttemptReceipt, ...]
    # Full ordered effect history for this open theory. Unlike ``attempts``,
    # this is not narrowed to the current version/tip because earlier physical
    # scans explain how the latest temporal need was reached.
    conductivity_attempts: tuple[TheoryAttemptReceipt, ...]
    # Full ordered research history. Exact request identity prevents an old
    # finding from suppressing a changed version, World, stop, or requirement.
    research_findings: tuple[ConductivityResearchFinding, ...]
    first_edge_exclusions: tuple[TheoryFirstEdgeExclusion, ...]
    current_progress_attempt_id: tuple[Any, ...] | None = None
    traceback_findings: tuple[IntrascanTracebackFinding | IntrascanOrdinarySteerFinding, ...] = ()
    traceback_frontiers: tuple[IntrascanTracebackFrontier, ...] = ()
    temporal_intent: TheoryTemporalIntent | None = None
    trigger_attempt_id: tuple[Any, ...] | None = None
    trigger_act_identity: tuple[Any, ...] | None = None
    trigger_consumer_boundary: ConsumerBoundary | None = None
    trigger_program_transaction: ProgramTransaction | None = None
    correction_rung_identities: frozenset[tuple[Any, ...]] = frozenset()
    pending_correction_rung_identities: frozenset[tuple[Any, ...]] = frozenset()

    def excludes_first_edge(self, artifact_identity: tuple[Any, ...]) -> bool:
        """Whether this exact theory/version/source already rejected an artifact."""

        return any(
            exclusion.artifact_identity == artifact_identity
            for exclusion in self.first_edge_exclusions
        )

    def has_research_finding(self, request_identity: tuple[Any, ...]) -> bool:
        """Whether this theory already researched this exact evidence question."""

        return self.research_finding(request_identity) is not None

    def research_finding(
        self,
        request_identity: tuple[Any, ...],
    ) -> ConductivityResearchFinding | None:
        """Return the exact completed finding, without broadening its scope."""

        return next(
            (
                finding
                for finding in self.research_findings
                if finding.request_identity == request_identity
            ),
            None,
        )

    def traceback_finding(
        self,
        request_identity: tuple[Any, ...],
    ) -> IntrascanTracebackFinding | IntrascanOrdinarySteerFinding | None:
        """Return a still-supported finding for this occurrence question."""

        return next(
            (
                finding
                for finding in self.traceback_findings
                if finding.request_identity == request_identity
                and self._traceback_scope_is_current(
                    finding.version_id,
                    finding.source,
                    finding.requirement_identities,
                )
            ),
            None,
        )

    def has_traceback_finding(self, request_identity: tuple[Any, ...]) -> bool:
        return self.traceback_finding(request_identity) is not None

    def traceback_frontier(
        self,
        request_identity: tuple[Any, ...],
    ) -> IntrascanTracebackFrontier | None:
        """Return an open hop still supported at this exact physical source."""

        return next(
            (
                frontier
                for frontier in self.traceback_frontiers
                if frontier.request_identity == request_identity
                and self.traceback_frontier_is_current(frontier)
            ),
            None,
        )

    def _traceback_scope_is_current(
        self,
        version_id: TheoryVersionId,
        source: TheoryBoundaryIdentity,
        requirement_identities: tuple[tuple[Any, ...], ...],
    ) -> bool:
        """Whether immutable traceback evidence survived later refinement.

        Refinement appends requirements to one WorkingTheory.  That does not
        invalidate an exact earlier rung observation when its physical source
        is unchanged and every requirement which justified it is still owned.
        A World rebase or a dropped requirement fails this check and requires
        fresh research.
        """

        ancestry = frozenset(version.version_id for version in self.version_history)
        current_requirements = frozenset(
            requirement.semantic_identity for requirement in self.requirements
        )
        return (
            version_id in ancestry
            and source == self.source
            and frozenset(requirement_identities) <= current_requirements
        )

    def traceback_frontier_is_current(
        self,
        frontier: IntrascanTracebackFrontier,
    ) -> bool:
        """Whether an open frontier remains evidence in the refined theory."""

        return frontier.theory_id == self.theory_id and self._traceback_scope_is_current(
            frontier.version_id,
            frontier.source,
            frontier.requirement_identities,
        )

    def current_traceback_frontiers(self) -> tuple[IntrascanTracebackFrontier, ...]:
        """Actionable leaf hops which remain supported by the current version.

        A child hop is append-only evidence that its exact parent goal was
        selected and investigated.  The parent remains queryable as history,
        but it is no longer open work for Compass; otherwise an older producer
        can repeatedly win orientation over the newly exposed obstruction.
        """

        supported = tuple(
            frontier
            for frontier in self.traceback_frontiers
            if self.traceback_frontier_is_current(frontier)
        )
        superseded = frozenset(
            item.parent_frontier_id
            for item in (*supported, *self.traceback_findings)
            if item.parent_frontier_id is not None
            and (
                isinstance(item, IntrascanTracebackFrontier)
                or self._traceback_scope_is_current(
                    item.version_id,
                    item.source,
                    item.requirement_identities,
                )
            )
        )
        return tuple(frontier for frontier in supported if frontier.identity not in superseded)

    def realized_traceback_frontier(
        self,
    ) -> (
        tuple[
            IntrascanTracebackFrontier,
            IntrascanProducerGoal,
            TheoryAttemptReceipt,
        ]
        | None
    ):
        """The exact producer stage which advanced into the current World."""

        if self.current_progress_attempt_id is None:
            return None
        attempts = tuple(
            attempt
            for attempt in self.investigation_attempts
            if attempt.attempt_id == self.current_progress_attempt_id
            and attempt.disposition is TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
            and attempt.investigation_frontier_id is not None
            and attempt.producer_goal_id is not None
        )
        if len(attempts) != 1:
            return None
        attempt = attempts[0]
        frontiers = tuple(
            frontier
            for frontier in self.traceback_frontiers
            if frontier.identity == attempt.investigation_frontier_id
            and frontier.theory_id == self.theory_id
            and frontier.source == attempt.source
        )
        if len(frontiers) != 1 or attempt.source == self.source:
            return None
        frontier = frontiers[0]
        ancestry = frozenset(version.version_id for version in self.version_history)
        current_requirements = frozenset(
            requirement.semantic_identity for requirement in self.requirements
        )
        if (
            frontier.version_id not in ancestry
            or not frozenset(frontier.requirement_identities) <= current_requirements
        ):
            return None
        goals = tuple(
            goal for goal in frontier.producer_goals if goal.identity == attempt.producer_goal_id
        )
        if len(goals) != 1:
            return None
        already_realized = any(
            finding.parent_frontier_id == frontier.identity
            and finding.parent_producer_goal_id == goals[0].identity
            and finding.parent_attempt_id == attempt.attempt_id
            and self._traceback_scope_is_current(
                finding.version_id,
                finding.source,
                finding.requirement_identities,
            )
            for finding in self.traceback_findings
        )
        return None if already_realized else (frontier, goals[0], attempt)

    def has_traceback_result(self, request_identity: tuple[Any, ...]) -> bool:
        """Whether this World already researched the exact occurrence question."""

        return self.has_traceback_finding(request_identity) or (
            self.traceback_frontier(request_identity) is not None
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
    """Proof-level negative evidence; attempt recording never manufactures one."""

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
    research_finding_ids: tuple[tuple[Any, ...], ...] = ()
    traceback_finding_ids: tuple[tuple[Any, ...], ...] = ()
    traceback_frontier_ids: tuple[tuple[Any, ...], ...] = ()


@dataclass(frozen=True)
class TheoryLedger:
    claims: PMap[Any, TheoryClaim] = pmap()
    versions: PMap[Any, TheoryVersion] = pmap()
    progress: PMap[Any, TheoryProgressSnapshot] = pmap()
    theories: PMap[Any, WorkingTheory] = pmap()
    attempts: PMap[Any, TheoryAttemptReceipt] = pmap()
    research_findings: PMap[Any, ConductivityResearchFinding] = pmap()
    traceback_findings: PMap[Any, IntrascanTracebackFinding | IntrascanOrdinarySteerFinding] = (
        pmap()
    )
    traceback_frontiers: PMap[Any, IntrascanTracebackFrontier] = pmap()
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
            if receipt.kind not in {
                TheoryPhaseKind.TEMPORAL_SETUP,
                TheoryPhaseKind.REARM,
                TheoryPhaseKind.TRANSACTION_ATTEMPT,
                TheoryPhaseKind.CORRECTION_COMPOSITION,
                TheoryPhaseKind.CORRECTION_INSTALL,
            }:
                continue
            owned.update(receipt.pilot_rung_identities)
    return frozenset(owned)


def active_theory_correction_rung_identities(
    state: TheoryState,
) -> frozenset[tuple[Any, ...]]:
    """Read the correction rungs still owned by the active theory tip."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return frozenset()
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        return frozenset()
    progress = state.ledger.progress.get(theory.current_progress_id)
    if progress is None:
        raise TheoryInvariantError("active theory correction history is incomplete")
    active: set[tuple[Any, ...]] = set()
    for receipt in progress.phase_receipts:
        if receipt.kind not in {
            TheoryPhaseKind.CORRECTION_COMPOSITION,
            TheoryPhaseKind.CORRECTION_INSTALL,
        }:
            continue
        active.difference_update(receipt.superseded_pilot_rung_identities)
        active.update(receipt.pilot_rung_identities)
    return frozenset(active)


def active_theory_superseded_correction_rung_identities(
    state: TheoryState,
) -> frozenset[tuple[Any, ...]]:
    """Read correction identities explicitly replaced on the active tip."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return frozenset()
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        return frozenset()
    progress = state.ledger.progress.get(theory.current_progress_id)
    if progress is None:
        raise TheoryInvariantError("active theory correction history is incomplete")
    superseded: set[tuple[Any, ...]] = set()
    for receipt in progress.phase_receipts:
        if receipt.kind is not TheoryPhaseKind.CORRECTION_COMPOSITION:
            continue
        superseded.update(receipt.superseded_pilot_rung_identities)
        superseded.difference_update(receipt.pilot_rung_identities)
    return frozenset(superseded)


def active_theory_superseded_pilot_rung_identities(
    state: TheoryState,
) -> frozenset[tuple[Any, ...]]:
    """Read every exact overlay explicitly replaced on the active theory tip."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return frozenset()
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        return frozenset()
    progress = state.ledger.progress.get(theory.current_progress_id)
    if progress is None:
        raise TheoryInvariantError("active theory overlay history is incomplete")
    superseded: set[tuple[Any, ...]] = set()
    for receipt in progress.phase_receipts:
        superseded.update(receipt.superseded_pilot_rung_identities)
        superseded.difference_update(receipt.pilot_rung_identities)
    return frozenset(superseded)


def active_theory_pilot_rung_identities(
    state: TheoryState,
) -> frozenset[tuple[Any, ...]]:
    """Read every exact overlay identity owned by the active theory tip."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return frozenset()
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        return frozenset()
    progress = state.ledger.progress.get(theory.current_progress_id)
    if progress is None:
        raise TheoryInvariantError("active theory overlay history is incomplete")
    active: set[tuple[Any, ...]] = set()
    for receipt in progress.phase_receipts:
        active.difference_update(receipt.superseded_pilot_rung_identities)
        if receipt.kind in {
            TheoryPhaseKind.TEMPORAL_SETUP,
            TheoryPhaseKind.REARM,
            TheoryPhaseKind.TRANSACTION_ATTEMPT,
            TheoryPhaseKind.CORRECTION_COMPOSITION,
            TheoryPhaseKind.CORRECTION_INSTALL,
            TheoryPhaseKind.WORLD_REBASE,
        }:
            active.update(receipt.pilot_rung_identities)
    return frozenset(active)


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

    version_history_reversed: list[TheoryVersion] = []
    seen_version_ids: set[TheoryVersionId] = set()
    historical_version: TheoryVersion | None = version
    while historical_version is not None:
        if historical_version.version_id in seen_version_ids:
            raise TheoryInvariantError("active theory version history contains a cycle")
        seen_version_ids.add(historical_version.version_id)
        version_history_reversed.append(historical_version)
        parent_id = historical_version.parent_version_id
        if parent_id is None:
            break
        parent = state.ledger.versions.get(parent_id)
        if parent is None or parent.theory_id != theory_id:
            raise TheoryInvariantError("active theory version history is incomplete")
        historical_version = parent
    version_history = tuple(reversed(version_history_reversed))

    all_attempts = tuple(
        attempt
        for attempt_id in theory.attempt_ids
        for attempt in (state.ledger.attempts.get(attempt_id),)
        if attempt is not None
    )
    attempts = tuple(
        attempt
        for attempt in all_attempts
        if attempt.version_id == version.version_id and attempt.source == progress.provisional_tip
    )
    conductivity_attempts = tuple(
        attempt for attempt in all_attempts if attempt.conductivity_observations
    )
    investigation_attempts = tuple(
        attempt for attempt in all_attempts if attempt.investigation_frontier_id is not None
    )
    research_findings = tuple(
        finding
        for finding_id in theory.research_finding_ids
        for finding in (state.ledger.research_findings.get(finding_id),)
        if finding is not None
    )
    traceback_findings = tuple(
        finding
        for finding_id in theory.traceback_finding_ids
        for finding in (state.ledger.traceback_findings.get(finding_id),)
        if finding is not None
    )
    traceback_frontiers = tuple(
        frontier
        for frontier_id in theory.traceback_frontier_ids
        for frontier in (state.ledger.traceback_frontiers.get(frontier_id),)
        if frontier is not None
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
    pending_corrections: set[tuple[Any, ...]] = set()
    transaction_attempt: TheoryAttemptReceipt | None = None
    boundary_attempt: TheoryAttemptReceipt | None = None
    transaction_execution_source = _transaction_execution_source(state, theory, progress)
    transaction_execution_tip: TheoryBoundaryIdentity | None = None
    transaction_rearmed = False
    for receipt in progress.phase_receipts:
        pending_corrections.difference_update(receipt.superseded_pilot_rung_identities)
        if receipt.kind in {
            TheoryPhaseKind.CORRECTION_COMPOSITION,
            TheoryPhaseKind.CORRECTION_INSTALL,
        }:
            pending_corrections.update(receipt.pilot_rung_identities)
        elif receipt.kind in {
            TheoryPhaseKind.TEMPORAL_SETUP,
            TheoryPhaseKind.REARM,
            TheoryPhaseKind.TRANSACTION_ATTEMPT,
            TheoryPhaseKind.CONSUMER_EXECUTION_HORIZON,
        }:
            pending_corrections.difference_update(receipt.pilot_rung_identities)
        if receipt.kind is TheoryPhaseKind.TRANSACTION_ATTEMPT:
            candidate = state.ledger.attempts.get(receipt.evidence_identity)
            if (
                candidate is None
                or candidate.theory_id != theory_id
                or candidate.disposition is not TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
            ):
                raise TheoryInvariantError(
                    "investigation transaction phase lost its accepted attempt"
                )
            transaction_attempt = candidate
            boundary_attempt = candidate if candidate.consumer_boundary is not None else None
            transaction_rearmed = False
        elif receipt.kind is TheoryPhaseKind.CONSUMER_BOUNDARY:
            candidate = state.ledger.attempts.get(receipt.evidence_identity)
            if (
                transaction_attempt is None
                or candidate is None
                or candidate.theory_id != theory_id
                or candidate.disposition is not TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
                or candidate.consumer_boundary is None
            ):
                raise TheoryInvariantError(
                    "consumer boundary phase lost its accepted transaction receipt"
                )
            boundary_attempt = candidate
        elif receipt.kind is TheoryPhaseKind.CONSUMER_EXECUTION_HORIZON:
            if receipt.execution_tip is None:
                raise TheoryInvariantError("execution horizon phase lost its exact tip")
            transaction_execution_tip = receipt.execution_tip
        elif receipt.kind is TheoryPhaseKind.REARM and transaction_attempt is not None:
            transaction_rearmed = True
    consumer_execution_horizon = (
        ConsumerExecutionHorizon(
            transaction_attempt_id=transaction_attempt.attempt_id,
            source=(
                transaction_execution_source
                if transaction_execution_source is not None
                else transaction_attempt.source
            ),
            consumer_boundary_attempt_id=boundary_attempt.attempt_id,
            consumer_boundary=boundary_attempt.consumer_boundary,
            tip=transaction_execution_tip,
        )
        if transaction_attempt is not None
        and boundary_attempt is not None
        and boundary_attempt.consumer_boundary is not None
        and transaction_execution_tip is not None
        else None
    )
    investigation_scope = (
        TheoryInvestigationScope(
            theory_id=theory_id,
            version_id=version.version_id,
            execution_source=(
                transaction_execution_source
                if transaction_execution_source is not None
                else progress.execution_source
            ),
            frontier=progress.provisional_tip,
            source_progress_id=(
                progress.parent_progress_id
                if progress.parent_progress_id is not None
                else progress.progress_id
            ),
            frontier_progress_id=progress.progress_id,
            accepted_attempt_id=progress.accepted_attempt_id,
            transaction_attempt_id=(
                transaction_attempt.attempt_id if transaction_attempt is not None else None
            ),
            transaction_act_identity=(
                transaction_attempt.act_identity if transaction_attempt is not None else None
            ),
            transaction_act_pairs=(
                transaction_attempt.act_pairs if transaction_attempt is not None else ()
            ),
            transaction_selected_pairs=(
                transaction_attempt.selected_act_pairs if transaction_attempt is not None else ()
            ),
            consumer_boundary=(
                boundary_attempt.consumer_boundary if boundary_attempt is not None else None
            ),
            consumer_boundary_attempt_id=(
                boundary_attempt.attempt_id if boundary_attempt is not None else None
            ),
            consumer_execution_horizon=consumer_execution_horizon,
            transaction_rearmed=transaction_rearmed,
            retry_act_identity=(
                transaction_attempt.act_identity
                if transaction_attempt is not None
                and consumer_execution_horizon is not None
                and trigger is not None
                and trigger.source == consumer_execution_horizon.source
                and trigger.observation_boundary is not None
                and trigger.observation_boundary == consumer_execution_horizon.tip
                else None
            ),
        )
        if progress.execution_source is not None and progress.accepted_attempt_id is not None
        else None
    )
    view = TheoryView(
        theory_id=theory_id,
        version_id=version.version_id,
        source=progress.provisional_tip,
        root=version.source,
        investigation_scope=investigation_scope,
        claim=claim,
        requirements=version.requirements,
        version_history=version_history,
        attempts=attempts,
        investigation_attempts=investigation_attempts,
        conductivity_attempts=conductivity_attempts,
        research_findings=research_findings,
        first_edge_exclusions=exclusions,
        current_progress_attempt_id=progress.accepted_attempt_id,
        traceback_findings=traceback_findings,
        traceback_frontiers=traceback_frontiers,
        temporal_intent=version.temporal_intent,
        trigger_attempt_id=version.trigger_attempt_id,
        trigger_act_identity=(trigger.act_identity if trigger is not None else None),
        trigger_consumer_boundary=(trigger.consumer_boundary if trigger is not None else None),
        trigger_program_transaction=(trigger.program_transaction if trigger is not None else None),
        correction_rung_identities=active_theory_correction_rung_identities(state),
        pending_correction_rung_identities=frozenset(pending_corrections),
    )
    assert_detached_theory_value(view, path="theory_view")
    return view


def assert_temporal_need_current(
    state: TheoryState,
    request: TemporalNeedRequest,
) -> None:
    """Fail closed when a detached temporal request is used outside its world.

    The request is immutable evidence, so it cannot validate itself against a
    changing ledger.  Every executable consumer must establish that its theory,
    version, progress boundary, triggering attempt, and scoped requirements are
    still the active ones before deriving work from it.
    """

    if state.active_theory_id != request.theory_id:
        raise TheoryInvariantError("temporal need does not belong to the active theory")
    theory = state.ledger.theories.get(request.theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        raise TheoryInvariantError("temporal need theory is missing or closed")
    if theory.current_version_id != request.version_id:
        raise TheoryInvariantError("temporal need addresses a stale theory version")
    progress = state.ledger.progress.get(theory.current_progress_id)
    if progress is None or progress.provisional_tip != request.source:
        raise TheoryInvariantError("temporal need source is not the current progress boundary")
    trigger = state.ledger.attempts.get(request.trigger_attempt_id)
    if (
        trigger is None
        or trigger.theory_id != request.theory_id
        or trigger.act_identity != request.trigger_act_identity
        or trigger.disposition is not TheoryAttemptDisposition.REJECTED_EXACT
    ):
        raise TheoryInvariantError("temporal need trigger is not its exact rejected attempt")
    version = state.ledger.versions[request.version_id]
    if request.requirements != version.temporal_requirements:
        raise TheoryInvariantError("temporal need requirements are not the current temporal scope")


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
        source=view.source,
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
    assert_temporal_need_current(state, request)
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
    act_pairs: tuple[tuple[str, Any], ...] = ()
    selected_act_pairs: tuple[tuple[str, Any], ...] = ()
    evidence: tuple[Any, ...] = ()
    first_edge_identity: tuple[Any, ...] | None = None
    conductivity_observations: tuple[EffectObservationSnapshot, ...] = ()
    consumer_boundary: ConsumerBoundary | None = None
    execution_source: TheoryBoundaryIdentity | None = None
    investigation_frontier_id: tuple[Any, ...] | None = None
    producer_goal_id: tuple[Any, ...] | None = None
    observation_boundary: TheoryBoundaryIdentity | None = None
    program_transaction: ProgramTransaction | None = None


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
class RetainedCorrectionReceipt:
    """Detached proof that the trend monitor owns a retained overlay."""

    receipt_id: int
    correction_identity: tuple[tuple[Any, ...], ...]
    pilot_rung_identities: tuple[tuple[Any, ...], ...]
    origin_world_key: tuple[Any, ...]
    status: str


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
    retained_correction_receipts: tuple[RetainedCorrectionReceipt, ...] = ()


@dataclass(frozen=True)
class ComposeTheoryCorrection:
    """Record one no-scan correction composed into the current theory world."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    composed_source: TheoryBoundaryIdentity
    requirement_identities: tuple[tuple[Any, ...], ...]
    pilot_rung_identities: tuple[tuple[Any, ...], ...]
    composition_identity: tuple[Any, ...]
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
    | RecordConductivityResearch
    | RecordIntrascanTraceback
    | RecordIntrascanTracebackFrontier
    | AdvanceTheory
    | RebaseTheoryWorld
    | ComposeTheoryCorrection
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
    """Fail closed when a ledger fact contains a live or retained-future value.

    Theory identities form an immutable DAG: a later identity may refer to the
    same ancestral tuple through several evidence edges. Validate each object
    once so shared ancestry does not turn this safety check into an exponential
    tree walk. A distinct ``visiting`` state still rejects a genuine cycle.
    """

    pending: list[tuple[bool, Any, str]] = [(False, value, path)]
    states: dict[int, bool] = {}
    while pending:
        leaving, current, current_path = pending.pop()
        if leaving:
            states[id(current)] = True
            continue

        if callable(current):
            raise TheoryInvariantError(f"{current_path} retains a callable")
        value_type = type(current)
        if value_type.__name__ in _FORBIDDEN_TYPES:
            raise TheoryInvariantError(f"{current_path} retains forbidden {value_type.__name__}")
        if current is None or isinstance(current, (str, bytes, int, float, bool, StrEnum)):
            continue

        compound = isinstance(current, tuple | frozenset | PMap) or is_dataclass(current)
        if not compound:
            raise TheoryInvariantError(
                f"{current_path} contains unsupported live type {value_type.__name__}"
            )
        prior_state = states.get(id(current))
        if prior_state is False:
            raise TheoryInvariantError(f"{current_path} contains an object cycle")
        if prior_state is True:
            continue
        states[id(current)] = False
        pending.append((True, current, current_path))

        children: list[tuple[Any, str]] = []
        if isinstance(current, tuple | frozenset):
            children.extend(
                (item, f"{current_path}[{index}]") for index, item in enumerate(current)
            )
        elif isinstance(current, PMap):
            for key, item in current.items():
                children.append((key, f"{current_path}.key"))
                children.append((item, f"{current_path}[{key!r}]"))
        else:
            for item in fields(current):
                if item.name in _FORBIDDEN_FIELDS:
                    raise TheoryInvariantError(
                        f"{current_path}.{item.name} is a retained-future field"
                    )
                children.append((getattr(current, item.name), f"{current_path}.{item.name}"))
        pending.extend((False, item, item_path) for item, item_path in reversed(children))


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


def _transaction_execution_source(
    state: TheoryState,
    theory: WorkingTheory,
    progress: TheoryProgressSnapshot,
) -> TheoryBoundaryIdentity | None:
    """Resolve only the root owned by the latest active transaction phases."""

    source: TheoryBoundaryIdentity | None = None
    for receipt in progress.phase_receipts:
        if receipt.kind is TheoryPhaseKind.TRANSACTION_ATTEMPT:
            attempt = state.ledger.attempts.get(receipt.evidence_identity)
            if (
                attempt is None
                or attempt.theory_id != theory.theory_id
                or attempt.disposition is not TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
            ):
                raise TheoryInvariantError(
                    "investigation transaction phase lost its accepted attempt"
                )
            source = attempt.execution_source
        elif (
            receipt.kind is TheoryPhaseKind.WORLD_REBASE
            and source is not None
            and receipt.execution_source is not None
            and receipt.execution_source.scan_id == source.scan_id
        ):
            source = receipt.execution_source
    return source


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
    transaction_source = _transaction_execution_source(state, theory, progress)
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
            transaction_source,
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
        observation_boundary = fact.observation_boundary or fact.source
        _require_allowed_source(state, theory, observation_boundary)
        if observation_boundary != fact.source:
            progress = state.ledger.progress[theory.current_progress_id]
            if (
                _transaction_execution_source(state, theory, progress) != fact.source
                or progress.provisional_tip != observation_boundary
                or progress.accepted_attempt_id is None
            ):
                raise TheoryInvariantError(
                    "attempt observation is outside its active investigation scope"
                )
        if not fact.execution_owner_token:
            raise TheoryInvariantError("attempt execution owner evidence is missing")
        if not fact.occurrence_evidence:
            raise TheoryInvariantError("attempt occurrence evidence is missing")
        if fact.execution_source is not None and (
            fact.execution_source.scan_id != fact.source.scan_id
            or not fact.execution_source.checkpoint_token
            or (
                fact.execution_source.scan_id > 0
                and not fact.execution_source.execution_owner_token
            )
        ):
            raise TheoryInvariantError("attempt execution source evidence is inconsistent")
        if (fact.investigation_frontier_id is None) != (fact.producer_goal_id is None):
            raise TheoryInvariantError("attempt investigation ownership is incomplete")
        if fact.investigation_frontier_id is not None:
            frontier = state.ledger.traceback_frontiers.get(fact.investigation_frontier_id)
            if (
                frontier is None
                or frontier.theory_id != fact.theory_id
                or frontier.identity not in theory.traceback_frontier_ids
                or frontier.source != observation_boundary
                or not _version_descends_from(
                    state.ledger,
                    fact.version_id,
                    frontier.version_id,
                )
            ):
                raise TheoryInvariantError(
                    "attempt investigation frontier is not current theory work"
                )
            version = state.ledger.versions[fact.version_id]
            owned_requirements = frozenset(
                requirement.semantic_identity for requirement in version.requirements
            )
            if not frozenset(frontier.requirement_identities) <= owned_requirements:
                raise TheoryInvariantError(
                    "attempt investigation frontier lost requirement ownership"
                )
            if fact.producer_goal_id not in tuple(
                goal.identity for goal in frontier.producer_goals
            ):
                raise TheoryInvariantError("attempt producer goal does not belong to its frontier")
        receipt = TheoryAttemptReceipt(
            theory_id=fact.theory_id,
            version_id=fact.version_id,
            attempt_id=fact.attempt_identity,
            source=fact.source,
            execution_owner_token=fact.execution_owner_token,
            occurrence_evidence=fact.occurrence_evidence,
            act_identity=fact.act_identity,
            pilot_rung_identities=fact.pilot_rung_identities,
            disposition=fact.disposition,
            act_pairs=fact.act_pairs,
            selected_act_pairs=fact.selected_act_pairs,
            evidence=fact.evidence,
            first_edge_identity=fact.first_edge_identity,
            conductivity_observations=fact.conductivity_observations,
            consumer_boundary=fact.consumer_boundary,
            execution_source=fact.execution_source,
            investigation_frontier_id=fact.investigation_frontier_id,
            producer_goal_id=fact.producer_goal_id,
            observation_boundary=observation_boundary,
            program_transaction=fact.program_transaction,
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
            finding.witness.consumer_execution_horizon_reached
            and finding.witness.consumer_horizon_read is not None
            and realization.consumer_execution_horizon_reached
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
            if receipt.kind is not TheoryPhaseKind.CONSUMER_EXECUTION_HORIZON:
                continue
            preceding = (*parent.phase_receipts, *fact.phase_receipts[:index])
            if (
                receipt.execution_tip != fact.boundary
                or not any(item.kind is TheoryPhaseKind.TRANSACTION_ATTEMPT for item in preceding)
                or not any(item.kind is TheoryPhaseKind.CONSUMER_BOUNDARY for item in preceding)
            ):
                raise TheoryInvariantError(
                    "execution horizon lacks its exact tip, transaction, or consumer boundary"
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
        if fact.rebased_source == fact.source or (
            fact.rebased_source.scan_id != fact.source.scan_id
            or fact.rebased_source.execution_owner_token != fact.source.execution_owner_token
            or fact.rebased_source.occurrence_identity != fact.source.occurrence_identity
        ):
            raise TheoryInvariantError("world rebase changed its physical execution boundary")
        if not fact.retained_pilot_rung_identities and not fact.superseded_pilot_rung_identities:
            raise TheoryInvariantError("world rebase has no overlay change evidence")
        source_key = fact.source.world_key
        rebased_key = fact.rebased_source.world_key
        if len(source_key) != 2 or len(rebased_key) != 2 or source_key[0] != rebased_key[0]:
            raise TheoryInvariantError("world rebase changed its physical state")
        source_rungs = tuple(source_key[1])
        rebased_rungs = tuple(rebased_key[1])
        added = tuple(rung for rung in rebased_rungs if rung not in source_rungs)
        removed = tuple(rung for rung in source_rungs if rung not in rebased_rungs)
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
        correction_owned: set[tuple[Any, ...]] = set()
        for receipt in fact.retained_correction_receipts:
            if (
                receipt.receipt_id <= 0
                or not receipt.origin_world_key
                or receipt.status not in {"probationary", "active"}
                or not receipt.pilot_rung_identities
                or tuple(sorted(receipt.pilot_rung_identities, key=repr))
                != receipt.correction_identity
                or not set(receipt.pilot_rung_identities) & set(added)
            ):
                raise TheoryInvariantError("retained correction receipt is malformed or inactive")
            correction_owned.update(set(receipt.pilot_rung_identities) & set(added))
        allowed = owned | correction_owned
        if not set(added) <= allowed:
            unowned_identities = tuple(identity for identity in added if identity not in allowed)
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
        if not fact.requirement_identities or not fact.pilot_rung_identities:
            raise TheoryInvariantError("composition lacks its exact requirement or rung identity")
        parent = state.ledger.progress[theory.current_progress_id]
        if fact.source != parent.provisional_tip:
            raise TheoryInvariantError("composition source is not the current progress boundary")
        if fact.composed_source.scan_id != fact.source.scan_id:
            raise TheoryInvariantError("no-scan composition advanced the physical scan")
        if not fact.composed_source.checkpoint_token or (
            fact.composed_source.scan_id > 0 and not fact.composed_source.execution_owner_token
        ):
            raise TheoryInvariantError("composition boundary evidence is incomplete")
        prior_corrections = {
            rung_identity
            for phase in parent.phase_receipts
            if phase.kind is TheoryPhaseKind.CORRECTION_COMPOSITION
            for rung_identity in phase.pilot_rung_identities
        }
        prior_superseded = {
            rung_identity
            for phase in parent.phase_receipts
            for rung_identity in phase.superseded_pilot_rung_identities
        }
        active_prior = prior_corrections - prior_superseded
        if not set(fact.superseded_pilot_rung_identities) <= active_prior:
            raise TheoryInvariantError("composition supersedes an unowned correction")
        if set(fact.superseded_pilot_rung_identities) & set(fact.pilot_rung_identities):
            raise TheoryInvariantError("composition cannot supersede its new correction")
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
