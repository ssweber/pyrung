"""Detached, immutable WorkingTheory knowledge and read API.

The ledger owns detached knowledge and typed temporal intent. It never owns a
PLC world, chooses an executable action, or restores or promotes a checkpoint.
The drive may resolve a typed request back to exact live receipts; all values
stored here remain semantic so the ledger survives rollback without retaining
a future. Typed lifecycle facts and their pure state transition live in
theory_reducer.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from functools import cached_property
from hashlib import sha256
from typing import TYPE_CHECKING, Any, TypeAlias

from pyrsistent import PMap, pmap

from pyrung.core.analysis.pilot.effects import (
    ConsumerBoundary,
    EffectObligationSnapshot,
    EffectObservationSnapshot,
    EffectOccurrenceSnapshot,
)
from pyrung.core.analysis.pilot.execution import (
    CheckpointRef,
    ScanEntryConfiguration,
)
from pyrung.core.analysis.pilot.world_key import _semantic_key
from pyrung.core.runner import EpochRef

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.intrascan_research import (
        IntrascanBoundaryRealization,
        IntrascanProducerGoal,
        IntrascanTracebackWitness,
    )

TheoryId: TypeAlias = tuple[Any, ...]
TheoryVersionId: TypeAlias = tuple[Any, ...]
TheoryProgressId: TypeAlias = tuple[Any, ...]


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
    CONSUMER_STOP = "consumer_stop"
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
    configurations: tuple[ScanEntryConfiguration, ...] = ()
    superseded_configuration_identities: tuple[tuple[Any, ...], ...] = ()
    execution_source: TheoryBoundaryIdentity | None = None
    execution_tip: TheoryBoundaryIdentity | None = None


@dataclass(frozen=True)
class TheoryBoundaryIdentity:
    """Detached identity of one exact observed execution boundary."""

    world_key: tuple[Any, ...]
    scan_id: int
    owner_ref: CheckpointRef | EpochRef
    occurrence_identity: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.owner_ref, CheckpointRef | EpochRef):
            raise TheoryInvariantError("boundary owner must be a CheckpointRef or EpochRef")
        if self.scan_id > 0 and not isinstance(self.owner_ref, EpochRef):
            raise TheoryInvariantError("an executed boundary must be owned by an EpochRef")

    @property
    def execution_ref(self) -> EpochRef | None:
        """Return physical execution ownership when this is an executed boundary."""

        return self.owner_ref if isinstance(self.owner_ref, EpochRef) else None


@dataclass(frozen=True)
class TheoryObjectiveSnapshot:
    """Predicate-free semantic form of a target-relative objective."""

    target_tag: str
    target_value: Any
    predicate_identity: tuple[Any, ...] | None = None
    frontier: tuple[tuple[str, Any], ...] = ()


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
    execution_ref: EpochRef
    phase: str
    status: str
    provenance: str
    scope: tuple[Any, ...]
    obstruction_occurrence: tuple[Any, ...] | None = None
    corrective_pilot_rung_identities: tuple[tuple[Any, ...], ...] = ()


@dataclass(frozen=True)
class TheoryClaim:
    """One selected producer-to-consumer claim at one exact source."""

    source: TheoryBoundaryIdentity
    objective: TheoryObjectiveSnapshot
    obligations: tuple[EffectObligationSnapshot, ...]
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
    consumer_stop: TheoryBoundaryIdentity | None = None
    transaction_rearmed: bool = False
    retry_act_identity: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class TheoryAttemptReceipt:
    theory_id: TheoryId
    version_id: TheoryVersionId
    attempt_id: tuple[Any, ...]
    source: TheoryBoundaryIdentity
    execution_ref: EpochRef
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
    configurations: tuple[ScanEntryConfiguration, ...] = ()


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
    overlay_identities: frozenset[tuple[Any, ...]] = frozenset()
    pending_overlay_identities: frozenset[tuple[Any, ...]] = frozenset()
    configurations: tuple[ScanEntryConfiguration, ...] = ()
    pending_configuration_identities: frozenset[tuple[Any, ...]] = frozenset()

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
    applied_facts: PMap[Any, object] = pmap()


@dataclass(frozen=True)
class TheoryState:
    ledger: TheoryLedger = TheoryLedger()
    active_theory_id: TheoryId | None = None

    @cached_property
    def view(self) -> TheoryView | None:
        """Build and validate this immutable state's detached view once."""

        return _build_theory_view(self)


def active_theory(state: TheoryState) -> WorkingTheory | None:
    """Return the exact active case, or ``None`` when no case is open."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return None
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        raise TheoryInvariantError("active theory is missing or closed")
    return theory


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


def temporal_setup_configuration_tags(state: TheoryState) -> frozenset[str]:
    """Read tags whose configuration came from a Working Theory phase."""

    owned: set[str] = set()
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
            owned.update(
                tag
                for configuration in receipt.configurations
                for tag, _value in configuration.assignments
            )
    return frozenset(owned)


def active_theory_configurations(
    state: TheoryState,
) -> tuple[ScanEntryConfiguration, ...]:
    """Return the desired scan-entry configuration owned by the active case."""

    theory_id = state.active_theory_id
    if theory_id is None:
        return ()
    theory = state.ledger.theories.get(theory_id)
    if theory is None or theory.status is not TheoryStatus.OPEN:
        return ()
    progress = state.ledger.progress.get(theory.current_progress_id)
    if progress is None:
        raise TheoryInvariantError("active theory configuration history is incomplete")
    active: dict[tuple[Any, ...], ScanEntryConfiguration] = {}
    for receipt in progress.phase_receipts:
        for identity in receipt.superseded_configuration_identities:
            active.pop(identity, None)
        for configuration in receipt.configurations:
            active[configuration.identity] = configuration
    return tuple(active.values())


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


def theory_view(state: TheoryState) -> TheoryView | None:
    """Return the exact active navigation view, or ``None`` when no theory is open."""

    return state.view


def _build_theory_view(state: TheoryState) -> TheoryView | None:
    """Project one immutable state for :attr:`TheoryState.view`."""

    theory = active_theory(state)
    if theory is None:
        return None
    theory_id = theory.theory_id
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
    pending_overlays: set[tuple[Any, ...]] = set()
    pending_configurations: set[tuple[Any, ...]] = set()
    transaction_attempt: TheoryAttemptReceipt | None = None
    boundary_attempt: TheoryAttemptReceipt | None = None
    transaction_execution_source = _transaction_execution_source(state, theory, progress)
    transaction_execution_tip: TheoryBoundaryIdentity | None = None
    transaction_rearmed = False
    for receipt in progress.phase_receipts:
        pending_overlays.difference_update(receipt.superseded_pilot_rung_identities)
        pending_configurations.difference_update(receipt.superseded_configuration_identities)
        if receipt.kind is TheoryPhaseKind.CORRECTION_COMPOSITION:
            pending_overlays.update(receipt.pilot_rung_identities)
            pending_configurations.update(
                configuration.identity for configuration in receipt.configurations
            )
        elif receipt.kind is TheoryPhaseKind.CORRECTION_INSTALL:
            installed = pending_overlays.intersection(receipt.pilot_rung_identities)
            pending_overlays.difference_update(installed)
            pending_overlays.update(
                identity for identity in receipt.pilot_rung_identities if identity not in installed
            )
            pending_configurations.update(
                configuration.identity for configuration in receipt.configurations
            )
        elif receipt.kind in {
            TheoryPhaseKind.TEMPORAL_SETUP,
            TheoryPhaseKind.REARM,
            TheoryPhaseKind.TRANSACTION_ATTEMPT,
            TheoryPhaseKind.CONSUMER_STOP,
        }:
            pending_overlays.difference_update(receipt.pilot_rung_identities)
            pending_configurations.difference_update(
                configuration.identity for configuration in receipt.configurations
            )
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
        elif receipt.kind is TheoryPhaseKind.CONSUMER_STOP:
            if receipt.execution_tip is None:
                raise TheoryInvariantError("consumer stop phase lost its exact tip")
            transaction_execution_tip = receipt.execution_tip
        elif receipt.kind is TheoryPhaseKind.REARM and transaction_attempt is not None:
            transaction_rearmed = True
    consumer_stop = (
        transaction_execution_tip
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
            consumer_stop=consumer_stop,
            transaction_rearmed=transaction_rearmed,
            retry_act_identity=(
                transaction_attempt.act_identity
                if transaction_attempt is not None
                and consumer_stop is not None
                and trigger is not None
                and trigger.source
                == (
                    transaction_execution_source
                    if transaction_execution_source is not None
                    else transaction_attempt.source
                )
                and trigger.observation_boundary is not None
                and trigger.observation_boundary == consumer_stop
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
        overlay_identities=active_theory_pilot_rung_identities(state),
        pending_overlay_identities=frozenset(pending_overlays),
        configurations=active_theory_configurations(state),
        pending_configuration_identities=frozenset(pending_configurations),
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
