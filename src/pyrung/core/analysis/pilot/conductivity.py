"""Pure read model for value propagation through recorded intrascan history."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from pyrung.core.analysis.pilot.effects import (
    EffectObligationSnapshot,
    EffectObservationSnapshot,
    EffectOccurrenceSnapshot,
)
from pyrung.core.analysis.pilot.working_theory import (
    TheoryAttemptReceipt,
    TheoryBoundaryIdentity,
    TheoryId,
    TheoryRequirementSnapshot,
    TheoryTemporalIntent,
    TheoryVersionId,
    TheoryView,
)
from pyrung.core.analysis.pilot.world_key import _semantic_key


class ConductivityReach(StrEnum):
    """Furthest factual boundary reached by one produced value."""

    NONE = "none"
    PRODUCER = "producer"
    CONSUMER = "consumer"
    SCAN_EXIT = "scan_exit"


@dataclass(frozen=True)
class ConductivityFlow:
    """One occurrence-ordered value flow reconstructed without losing receipts."""

    attempt_id: tuple[Any, ...]
    obligations: tuple[EffectObligationSnapshot, ...]
    observations: tuple[EffectObservationSnapshot, ...]
    reach: ConductivityReach
    appeared: EffectOccurrenceSnapshot | None
    consumer_reads: tuple[EffectOccurrenceSnapshot, ...]
    displacement: EffectOccurrenceSnapshot | None
    displaced_read: EffectOccurrenceSnapshot | None

    @property
    def front_occurrence(self) -> EffectOccurrenceSnapshot | None:
        """Exact occurrence at the front, when the boundary is an occurrence."""

        if self.reach is ConductivityReach.CONSUMER:
            return self.consumer_reads[-1]
        if self.reach is ConductivityReach.PRODUCER:
            return self.appeared
        return None


@dataclass(frozen=True)
class ConductivityAttemptFront:
    """All value flows observed during one immutable theory attempt."""

    attempt_id: tuple[Any, ...]
    source: TheoryBoundaryIdentity
    flows: tuple[ConductivityFlow, ...]
    temporal_intent: TheoryTemporalIntent | None = None
    requirements: tuple[TheoryRequirementSnapshot, ...] = ()


class ConductivityProgress(StrEnum):
    """Structural relation between two consecutive attempt fronts."""

    INCOMPARABLE = "incomparable"
    SAME_STOP = "same_stop"
    STOP_CHANGED = "stop_changed"
    STOP_CLEARED = "stop_cleared"


@dataclass(frozen=True)
class ConductivityRequirementDrift:
    """One stable requirement boundary whose demanded condition changed."""

    boundary_identity: tuple[Any, ...]
    earlier: TheoryRequirementSnapshot
    later: TheoryRequirementSnapshot

    @property
    def identity(self) -> tuple[Any, ...]:
        """Stable detached identity of this exact changed demand."""

        return (
            "conductivity-requirement-drift",
            self.boundary_identity,
            self.earlier.semantic_identity,
            self.earlier.condition_identity,
            self.later.semantic_identity,
            self.later.condition_identity,
        )


@dataclass(frozen=True)
class ConductivityComparison:
    """Evidence-only comparison of two consecutive physical attempts."""

    earlier_attempt_id: tuple[Any, ...]
    later_attempt_id: tuple[Any, ...]
    progress: ConductivityProgress
    common_stop_identity: tuple[Any, ...] | None = None
    requirement_drifts: tuple[ConductivityRequirementDrift, ...] = ()

    @property
    def identity(self) -> tuple[Any, ...]:
        """Stable detached identity of the two compared physical attempts."""

        return (
            "conductivity-comparison",
            self.earlier_attempt_id,
            self.later_attempt_id,
            self.progress,
            self.common_stop_identity,
            tuple(drift.identity for drift in self.requirement_drifts),
        )


@dataclass(frozen=True)
class ConductivityResearchRequest:
    """Compass request to research a repeated stop instead of chasing a literal."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    comparison: ConductivityComparison
    displacement: EffectOccurrenceSnapshot
    enabling_reads: tuple[EffectOccurrenceSnapshot, ...]
    reason: str

    @property
    def identity(self) -> tuple[Any, ...]:
        """Identity of the exact evidence question, not of a future answer."""

        return (
            "conductivity-research-request",
            self.theory_id,
            self.version_id,
            self.source,
            self.comparison.identity,
            self.displacement,
            self.enabling_reads,
        )


@dataclass(frozen=True)
class ConductivityFront:
    """Complete ordered conductivity history exposed to one Compass read."""

    theory_id: TheoryId
    version_id: TheoryVersionId
    source: TheoryBoundaryIdentity
    attempts: tuple[ConductivityAttemptFront, ...]

    @property
    def flows(self) -> tuple[ConductivityFlow, ...]:
        return tuple(flow for attempt in self.attempts for flow in attempt.flows)

    @property
    def comparisons(self) -> tuple[ConductivityComparison, ...]:
        return tuple(
            _compare_attempts(earlier, later)
            for earlier, later in zip(self.attempts, self.attempts[1:], strict=False)
        )


def _occurrence_order(occurrence: EffectOccurrenceSnapshot) -> tuple[int, int]:
    # Projection ordinals are total within a scan. Avoid comparing structural
    # address members here: they can legitimately mix ``None`` and strings.
    return (occurrence.scan_id, occurrence.ordinal)


def _ordered_unique(
    occurrences: tuple[EffectOccurrenceSnapshot, ...],
) -> tuple[EffectOccurrenceSnapshot, ...]:
    result: list[EffectOccurrenceSnapshot] = []
    for occurrence in sorted(occurrences, key=_occurrence_order):
        if occurrence not in result:
            result.append(occurrence)
    return tuple(result)


def _stopping_reads(flow: ConductivityFlow) -> tuple[EffectOccurrenceSnapshot, ...]:
    if flow.displacement is None:
        return ()
    return _ordered_unique(
        tuple(
            read
            for observation in flow.observations
            if observation.displacement == flow.displacement
            for read in (observation.displacement_enabling_reads or observation.observed_reads)
        )
    )


def _stop_identity(occurrence: EffectOccurrenceSnapshot) -> tuple[Any, ...]:
    """Structural writer identity stable across physical scan occurrences."""

    return (
        occurrence.kind,
        occurrence.rung,
        occurrence.execution_kind,
        occurrence.caller_rung,
        occurrence.call_stack,
        occurrence.depth,
        occurrence.tag,
    )


def _front_identity(flow: ConductivityFlow) -> tuple[Any, ...]:
    """Structural produced-value front, independent of physical scan number.

    The front is the exact writer and value that entered the hose.  Entry
    state and later consumer annotation can vary between otherwise identical
    retries; neither changes which produced front reached the stopping writer.
    """

    appeared = flow.appeared
    if appeared is not None:
        return (
            appeared.kind,
            appeared.rung,
            appeared.execution_kind,
            appeared.caller_rung,
            appeared.call_stack,
            appeared.depth,
            appeared.tag,
            _semantic_key(appeared.values[-1] if appeared.values else None),
        )
    return (
        "unappeared",
        tuple(
            (
                obligation.tag,
                _semantic_key(obligation.value),
                obligation.producer,
            )
            for obligation in flow.obligations
        ),
    )


def _effect_occurrence_identity(
    occurrence: EffectOccurrenceSnapshot,
) -> tuple[Any, ...]:
    return (
        occurrence.kind,
        occurrence.tag,
        (
            occurrence.rung,
            occurrence.execution_kind,
            occurrence.caller_rung,
            occurrence.call_stack,
            occurrence.depth,
            occurrence.call_invocation,
        ),
    )


def _requirement_occurrence_identity(occurrence: tuple[Any, ...]) -> tuple[Any, ...]:
    if len(occurrence) < 4:
        return occurrence
    dynamic_address = occurrence[3]
    structural_address = (
        dynamic_address[:6]
        if isinstance(dynamic_address, tuple) and len(dynamic_address) >= 8
        else dynamic_address
    )
    return (occurrence[0], occurrence[1], structural_address)


def _requirement_boundary_identity(
    requirement: TheoryRequirementSnapshot,
) -> tuple[Any, ...]:
    return (
        _requirement_occurrence_identity(requirement.deadline_occurrence),
        _requirement_occurrence_identity(requirement.demanding_occurrence),
        requirement.operand_authority,
        requirement.phase,
    )


def _requirement_drifts(
    earlier: ConductivityAttemptFront,
    later: ConductivityAttemptFront,
) -> tuple[ConductivityRequirementDrift, ...]:
    result: list[ConductivityRequirementDrift] = []
    for earlier_requirement in earlier.requirements:
        boundary = _requirement_boundary_identity(earlier_requirement)
        for later_requirement in later.requirements:
            if _requirement_boundary_identity(later_requirement) != boundary:
                continue
            if earlier_requirement.condition_identity == later_requirement.condition_identity:
                continue
            result.append(
                ConductivityRequirementDrift(
                    boundary_identity=boundary,
                    earlier=earlier_requirement,
                    later=later_requirement,
                )
            )
    return tuple(result)


def _compare_attempts(
    earlier: ConductivityAttemptFront,
    later: ConductivityAttemptFront,
) -> ConductivityComparison:
    earlier_stops = tuple(
        ((_stop_identity(flow.displacement), _front_identity(flow)), flow)
        for flow in earlier.flows
        if flow.displacement is not None
    )
    later_stops = tuple(
        ((_stop_identity(flow.displacement), _front_identity(flow)), flow)
        for flow in later.flows
        if flow.displacement is not None
    )
    common_pair = next(
        (
            identity
            for identity, _flow in later_stops
            if any(identity == earlier_identity for earlier_identity, _ in earlier_stops)
        ),
        None,
    )
    common = next(
        (
            stop_identity
            for (stop_identity, _front), _flow in later_stops
            if any(
                stop_identity == earlier_stop
                for (earlier_stop, _earlier_front), _earlier_flow in earlier_stops
            )
        ),
        None,
    )
    if common_pair is not None:
        progress = ConductivityProgress.SAME_STOP
    elif earlier_stops and later_stops:
        progress = ConductivityProgress.STOP_CHANGED
    elif earlier_stops and not later_stops:
        progress = ConductivityProgress.STOP_CLEARED
    else:
        progress = ConductivityProgress.INCOMPARABLE
    return ConductivityComparison(
        earlier_attempt_id=earlier.attempt_id,
        later_attempt_id=later.attempt_id,
        progress=progress,
        common_stop_identity=common,
        requirement_drifts=_requirement_drifts(earlier, later),
    )


def _after(
    occurrence: EffectOccurrenceSnapshot,
    earlier: EffectOccurrenceSnapshot,
) -> bool:
    return _occurrence_order(occurrence) > _occurrence_order(earlier)


def _flow(
    attempt_id: tuple[Any, ...],
    observations: tuple[EffectObservationSnapshot, ...],
) -> ConductivityFlow:
    first = observations[0]
    obligations: list[EffectObligationSnapshot] = []
    for observation in observations:
        if observation.obligation not in obligations:
            obligations.append(observation.obligation)
    appeared = first.appeared
    displacements = _ordered_unique(
        tuple(
            observation.displacement
            for observation in observations
            if observation.displacement is not None
            and (appeared is None or _after(observation.displacement, appeared))
        )
    )
    displacement = displacements[0] if displacements else None
    consumer_reads = _ordered_unique(
        tuple(
            observation.consumer_read
            for observation in observations
            if observation.consumer_read is not None
            and appeared is not None
            and _after(observation.consumer_read, appeared)
            and (
                displacement is None
                or _occurrence_order(observation.consumer_read) < _occurrence_order(displacement)
            )
        )
    )
    displaced_reads = _ordered_unique(
        tuple(
            observation.displaced_read
            for observation in observations
            if observation.displaced_read is not None
            and displacement is not None
            and _after(observation.displaced_read, displacement)
        )
    )
    if appeared is None:
        reach = ConductivityReach.NONE
    elif consumer_reads:
        reach = ConductivityReach.CONSUMER
    elif displacement is None and any(
        observation.disposition == "SURVIVED" for observation in observations
    ):
        reach = ConductivityReach.SCAN_EXIT
    else:
        reach = ConductivityReach.PRODUCER
    return ConductivityFlow(
        attempt_id=attempt_id,
        obligations=tuple(obligations),
        observations=observations,
        reach=reach,
        appeared=appeared,
        consumer_reads=consumer_reads,
        displacement=displacement,
        displaced_read=displaced_reads[0] if displaced_reads else None,
    )


def _attempt_front(
    attempt: TheoryAttemptReceipt,
    view: TheoryView,
) -> ConductivityAttemptFront:
    groups: list[list[EffectObservationSnapshot]] = []
    for observation in attempt.conductivity_observations:
        matching = next(
            (
                group
                for group in groups
                if (group[0].appeared is not None and group[0].appeared == observation.appeared)
                or (
                    group[0].appeared is None
                    and observation.appeared is None
                    and group[0].obligation == observation.obligation
                )
            ),
            None,
        )
        if matching is None:
            groups.append([observation])
        else:
            matching.append(observation)
    triggered_version = next(
        (
            version
            for version in view.version_history
            if version.trigger_attempt_id == attempt.attempt_id
        ),
        None,
    )
    return ConductivityAttemptFront(
        attempt_id=attempt.attempt_id,
        source=attempt.source,
        flows=tuple(_flow(attempt.attempt_id, tuple(group)) for group in groups),
        temporal_intent=(
            triggered_version.temporal_intent if triggered_version is not None else None
        ),
        requirements=(
            triggered_version.temporal_requirements if triggered_version is not None else ()
        ),
    )


def conductivity_front(view: TheoryView | None) -> ConductivityFront | None:
    """Derive a read-only front from the full immutable history in ``view``."""

    if view is None:
        return None
    return ConductivityFront(
        theory_id=view.theory_id,
        version_id=view.version_id,
        source=view.source,
        attempts=tuple(_attempt_front(attempt, view) for attempt in view.conductivity_attempts),
    )


def charted_front_extends_current(
    view: TheoryView,
    observations: tuple[EffectObservationSnapshot, ...],
) -> bool:
    """Whether a fallback chart front may join the current exact history.

    A no-effect setup scan may repeat the retained stopped front.  It may move
    to a different chart front only after research has completed for the
    latest retained attempt; otherwise the fallback would skip an unresolved
    physical obstruction merely because the latest setup act lacked its own
    effect expectation.
    """

    front = conductivity_front(view)
    if front is None or not front.attempts:
        return True
    latest = front.attempts[-1]
    groups: list[list[EffectObservationSnapshot]] = []
    for observation in observations:
        matching = next(
            (
                group
                for group in groups
                if (group[0].appeared is not None and group[0].appeared == observation.appeared)
                or (
                    group[0].appeared is None
                    and observation.appeared is None
                    and group[0].obligation == observation.obligation
                )
            ),
            None,
        )
        if matching is None:
            groups.append([observation])
        else:
            matching.append(observation)
    candidate = ConductivityAttemptFront(
        attempt_id=("charted-front-candidate",),
        source=view.source,
        flows=tuple(_flow(("charted-front-candidate",), tuple(group)) for group in groups),
    )
    if _compare_attempts(latest, candidate).progress is ConductivityProgress.SAME_STOP:
        return True
    return any(
        finding.compared_attempt_ids[-1] == latest.attempt_id for finding in view.research_findings
    )


def conductivity_research_request(
    front: ConductivityFront | None,
) -> ConductivityResearchRequest | None:
    """Request research when the latest retry repeats a drifting stopped flow."""

    if front is None or not front.comparisons:
        return None
    comparison = front.comparisons[-1]
    if (
        comparison.progress
        not in {ConductivityProgress.SAME_STOP, ConductivityProgress.STOP_CHANGED}
        or comparison.common_stop_identity is None
        or not comparison.requirement_drifts
    ):
        return None
    later = front.attempts[-1]
    stopped_flow = next(
        (
            flow
            for flow in later.flows
            if flow.displacement is not None
            and _stop_identity(flow.displacement) == comparison.common_stop_identity
        ),
        None,
    )
    if stopped_flow is None or stopped_flow.displacement is None:
        return None
    stopping_reads = _stopping_reads(stopped_flow)
    stopping_read_identities = {_effect_occurrence_identity(read) for read in stopping_reads}
    relevant_drifts = tuple(
        drift
        for drift in comparison.requirement_drifts
        if _requirement_occurrence_identity(drift.later.demanding_occurrence)
        in stopping_read_identities
    )
    if not relevant_drifts:
        return None
    comparison = replace(comparison, requirement_drifts=relevant_drifts)
    return ConductivityResearchRequest(
        theory_id=front.theory_id,
        version_id=front.version_id,
        source=front.source,
        comparison=comparison,
        displacement=stopped_flow.displacement,
        enabling_reads=stopping_reads,
        reason=(
            "the conductivity front stopped at the same writer while its "
            "requirement changed; research the stopping rung before another steer"
        ),
    )
