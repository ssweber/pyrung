"""Observe and classify a channel departure for post-commit recovery.

``observe_departure`` settles the landing under the active holds, compares
target-relative earned-work evidence, and inspects static routes for reset boundaries
or completed channel actions that would have to be repeated. The immutable
``DepartureObservation`` keeps those exact source receipts together, and the
pure ``classify_departure`` interpretation adds only a classification and reason.

No route suffix is retained as a plan. ``progress.py`` owns the policy for a
clean continuation, regression, or unknown result.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.awaited_actions import (
    AwaitedAction,
    unique_legal_awaited_action,
)
from pyrung.core.analysis.pilot.causal import (
    _shared_cause,
    occurrence_external_supports,
)
from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastSession
from pyrung.core.analysis.pilot.compass import (
    CompassKnowledge,
)
from pyrung.core.analysis.pilot.constrained_reachability import (
    FrontierStatus,
    NavigationEvidence,
    Reachable,
    StaticEdgeAdmission,
    Unknown,
)
from pyrung.core.analysis.pilot.earned_work import EarnedWorkMovement, EarnedWorkReceipt
from pyrung.core.analysis.pilot.execution import (
    ChannelMotion,
    ExecutionReceipt,
    capture_execution_spans,
)
from pyrung.core.analysis.pilot.navigation_contracts import EvidenceScope
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.pipeline_graph import ANY_FROM
from pyrung.core.analysis.pilot.world_key import _pilot_world_key
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import (
        _AcceptedTrial,
        _PilotContext,
        _PilotState,
    )
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

# Settlement policy (window/cap) lives in coast.LIMITS — landing_confirm_scans
# / landing_cap; the mechanism is CoastSession.settle_landing.


class DepartureDisposition(StrEnum):
    """Target-relative meaning of the exact producer that caused a departure."""

    OWNED = "owned"
    REACTIVE = "reactive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DepartureReading:
    """A thin interpretation of one recorded channel occurrence.

    ``cause`` remains the sole owner of causal reconstruction.  This receipt
    only names the exact request producer(s) already present in that chain,
    partitions their supports at target-owned EarnedWork accomplishments.
    """

    disposition: DepartureDisposition
    occurrence_scan: int | None
    source_scan: int | None
    producer_rungs: tuple[int, ...] = ()
    external_supports: tuple[tuple[str, Any], ...] = ()
    reason: str = ""

    @property
    def target_owned(self) -> bool:
        return self.disposition is DepartureDisposition.OWNED


class DepartureClassification(StrEnum):
    """What the immutable departure evidence establishes."""

    CLEAN_CONTINUATION = "clean_continuation"
    REGRESSION = "regression"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContinuationEvidence:
    """Constrained continuation evidence actually consulted at the landing.

    ``channel_status`` is the typed static-chart result. For classifications
    that short-circuit before that read, it is an honest ``Unknown`` naming why
    it was not inspected. ``awaited_action_inspected`` distinguishes an
    unsuccessful awaited-action read from one that was never attempted.
    """

    channel_status: FrontierStatus
    awaited_action_inspected: bool = False
    awaited_action: AwaitedAction | None = None
    observed_adjacent: bool = False

    def __post_init__(self) -> None:
        if self.awaited_action is not None and not self.awaited_action_inspected:
            raise ValueError("an awaited action requires an inspected awaited action")


@dataclass(frozen=True)
class DepartureObservation:
    """Immutable facts observed while one channel departure settled."""

    channel_tag: str
    from_value: Any
    settled_value: Any
    landing_receipt: CoastReceipt | None
    progress: EarnedWorkReceipt
    reading: DepartureReading
    continuation: ContinuationEvidence
    execution: ExecutionReceipt | None = None
    settlement_execution: ExecutionReceipt | None = None

    @property
    def logical_scans(self) -> int:
        """Exact observed landing span, whether produced by coast or an act."""

        if self.landing_receipt is not None:
            return self.landing_receipt.logical_scans
        progress = self.execution.scan_progress if self.execution is not None else None
        if progress is not None:
            return progress.landing_scan - progress.source_scan
        return 0


@dataclass(frozen=True)
class DepartureResult:
    """The pure policy interpretation of one immutable observation."""

    observation: DepartureObservation
    classification: DepartureClassification
    reason: str


def _settle_departure(
    state: _PilotState,
    channel_tag: str,
) -> tuple[PLC, ExecutionReceipt]:
    """Ride a rung-driven fork to the departure's stable landing (bounded).

    The departure trigger lands at the *first* departure scan — mid-transition
    (Holding, Aborting).  Classification needs the landing, so let the
    departure's own chain complete with the installed pilot rungs active,
    exactly as a coast would — ``settle_landing`` rides every hop and the
    receipt records the chain.  A ``"timeout"`` receipt means the cap-hit
    value may be mid-transition; the caller must not trust it as settled.
    """
    source_scan = state.work.state.scan_id
    source_snap = dict(state.work.state.tags)
    fork = fork_with_pilot_rungs(state.work, state.pilot_rungs)
    session = CoastSession(fork, kind="departure-settle")
    receipt = session.settle_landing(channel_tag)
    if receipt.kernel_scans != len(session.kernel_scan_ids):
        raise RuntimeError("settlement kernel accounting does not match its session")
    return (
        fork,
        ExecutionReceipt(
            before_snap=source_snap,
            after_snap=dict(fork.state.tags),
            channel_motion=ChannelMotion(
                channel_tag=channel_tag,
                target_value=fork.state.tags.get(channel_tag),
                stop_reason=receipt.stop_reason,
            ),
            coast_receipt=receipt,
            timeline=session.events,
            spans=capture_execution_spans(fork, session.kernel_scan_ids),
            source_scan=source_scan,
        ),
    )


def _completed_channel_actions(
    state: _PilotState,
    channel_tag: str,
) -> set[tuple[str, Any, Any]]:
    """Completed channel actions: ``(tag, value, channel_value_at_press)``.

    The committed steps are the work the march already did.  A forward plan that
    re-requires one of these presses *in the same channel context* reopens work
    that was already completed.
    Context comes from the owning committed operation's before-snapshot; release
    and pulse steps therefore carry the same exact channel context."""
    out: set[tuple[str, Any, Any]] = set()
    for act in state.committed_acts:
        context = act.context.execution.before_snap.get(channel_tag)
        for step in act.steps:
            for tag, value in step.inputs.items():
                out.add((tag, value, context))
    return out


def _progress_erasing_values(
    earned_work: Any,
    anchor_snap: Any,
    channel_tag: str,
) -> tuple[frozenset[Any], bool]:
    """Channel values where the route would erase progress earned at the anchor.

    A reset is a literal load *behind* the anchor value in the component's
    earn direction (a load ahead of the anchor is a shortcut, not a reset).
    Returns ``(progress_erasing_values, all_resolved)``. When reset behavior is
    unresolved, the caller cannot safely classify the route.
    """
    blocked: set[Any] = set()
    all_resolved = True
    for component in getattr(earned_work, "components", ()) or ():
        anchor_value = anchor_snap.get(component.tag)
        for reset in component.resets:
            if reset.init_only:
                continue
            if not reset.resolved:
                all_resolved = False
                continue
            if reset.channel_tag is not None and reset.channel_tag != channel_tag:
                continue
            if (
                reset.value is not None
                and isinstance(anchor_value, (int, float))
                and not isinstance(anchor_value, bool)
                and (reset.value - anchor_value) * component.direction >= 0
            ):
                continue  # at-or-ahead of the anchor — not destroying anything
            blocked.update(reset.enabling_channel_values)
    return frozenset(blocked), all_resolved


def _reopens_completed_work(
    edge: Any,
    completed_actions: set[tuple[str, Any, Any]],
) -> bool:
    if edge.action is None:
        return False
    tag, value = edge.action
    for action_tag, action_value, action_context in completed_actions:
        if action_tag != tag or not _values_match(action_value, value):
            continue
        if action_context is None or edge.from_value is ANY_FROM:
            return True
        if _values_match(action_context, edge.from_value):
            return True
    return False


def _erases_earned_progress(
    edge: Any,
    progress_erasing_values: frozenset[Any],
) -> bool:
    """Whether this edge enters a channel value that enables a proved reset."""

    return any(_values_match(edge.to_value, value) for value in progress_erasing_values)


@dataclass(frozen=True)
class ContinuationSafety:
    """Static admission plus recovery-only protection of already-earned work."""

    admission: StaticEdgeAdmission
    erases_earned_progress: bool
    reopens_completed_work: bool

    @property
    def allowed(self) -> bool:
        """Boolean projection consumed by the static channel search."""

        return (
            self.admission.allowed
            and not self.erases_earned_progress
            and not self.reopens_completed_work
        )


def _continuation_safety(
    edge: Any,
    ctx: Any,
    *,
    settled_key: tuple[Any, ...] | None,
    settled_snap: dict[str, Any],
    evidence_scope: EvidenceScope | None,
    blocked_actions: frozenset[tuple[str, Any]],
    progress_erasing_values: frozenset[Any],
    completed_actions: set[tuple[str, Any, Any]],
) -> ContinuationSafety:
    """Whether one admitted edge also preserves work recovery already earned."""

    admission = NavigationEvidence.static_edge_admission(
        edge,
        world_key=settled_key,
        snapshot=settled_snap,
        knowledge=ctx.compass.knowledge,
        context=ctx,
        evidence_scope=evidence_scope,
        blocked_actions=blocked_actions,
    )
    return ContinuationSafety(
        admission=admission,
        erases_earned_progress=_erases_earned_progress(edge, progress_erasing_values),
        reopens_completed_work=_reopens_completed_work(edge, completed_actions),
    )


def _awaited_action_allowed(
    action: tuple[str, Any],
    *,
    settled_key: tuple[Any, ...] | None,
    knowledge: CompassKnowledge,
    blocked_actions: frozenset[tuple[str, Any]],
) -> bool:
    """Whether one program-awaited action may support this settled world."""

    if action in blocked_actions:
        return False
    return settled_key is None or action not in knowledge.nogood_pairs(settled_key)


def _request_producer_rungs(chain: Any, role: Any) -> tuple[int, ...]:
    """Read exact initiating producers from an existing ``cause()`` chain.

    The channel pipeline's request transition is the attribution boundary.
    Its recorded triggers point at the command-register writes that initiated
    this occurrence.  Following those already-recorded links does not search
    writers, replay guards, or infer a second cause graph.
    """
    if chain is None or role is None:
        return ()
    request_step = next(
        (step for step in chain.steps if step.transition.tag_name in role.request_tags),
        None,
    )
    if request_step is None:
        return ()
    triggers = {
        (trigger.tag_name, trigger.scan_id, repr(trigger.to_value))
        for trigger in request_step.triggers
        if trigger.tag_name not in role.participating_tags
    }
    if not triggers:
        return ()
    producers = {
        step.rung_index
        for step in chain.steps
        if (
            step.transition.tag_name,
            step.transition.scan_id,
            repr(step.transition.to_value),
        )
        in triggers
    }
    return tuple(sorted(producers))


def _is_hold_landing(ctx: _PilotContext, channel_tag: str, value: Any) -> bool:
    """Whether the channel's declared value label identifies a Hold transaction."""
    tag_ref = getattr(getattr(ctx, "pdg", None), "tags", {}).get(channel_tag)
    choices = getattr(tag_ref, "choices", None) if tag_ref is not None else None
    label = choices.get(value) if choices else None
    normalized = "".join(character for character in str(label).casefold() if character.isalnum())
    if normalized in {"holding", "held"}:
        return True

    # Some imported projects express PackML values through reference constants
    # rather than ``choices=``.  The already-built static channel edge still
    # carries the command-family action that names the transaction.
    graphs = getattr(getattr(ctx, "compass", None), "graphs", ()) or ()
    for graph in graphs:
        if graph.role.channel_tag != channel_tag:
            continue
        hold_values: list[Any] = []
        for edge in graph.edges:
            if edge.action is None:
                continue
            action_name = "".join(
                character for character in edge.action[0].casefold() if character.isalnum()
            )
            if "hold" in action_name and "unhold" not in action_name:
                hold_values.append(edge.to_value)
        # The command edge commonly lands on transitional ``Holding`` and an
        # actionless program edge completes it to ``Held``.
        for current in hold_values:
            if _values_match(current, value):
                return True
            if any(
                edge.action is None
                and _values_match(edge.from_value, current)
                and _values_match(edge.to_value, value)
                for edge in graph.edges
            ):
                return True
    return False


def _departure_reading(
    chain: Any,
    ctx: _PilotContext,
    channel_tag: str,
    settled_value: Any,
    occurrence_scan: int | None,
    earned_work: Any,
) -> DepartureReading:
    """Interpret exact cause identity against the selected target work."""
    source_scan = chain.effect.scan_id - 1 if chain is not None else None
    if not _is_hold_landing(ctx, channel_tag, settled_value):
        return DepartureReading(
            disposition=DepartureDisposition.UNKNOWN,
            occurrence_scan=(chain.effect.scan_id if chain is not None else occurrence_scan),
            source_scan=source_scan,
            reason="the landing is outside Held occurrence policy",
        )
    role = next(
        (
            candidate
            for candidate in getattr(ctx, "pipeline_roles", ())
            if candidate.channel_tag == channel_tag
        ),
        None,
    )
    producer_rungs = _request_producer_rungs(chain, role)
    accomplishments = frozenset(
        component.tag for component in getattr(earned_work, "components", ()) or ()
    )
    has_target_earned_work = bool(accomplishments)
    external_supports = (
        occurrence_external_supports(
            chain,
            frozenset(producer_rungs),
            getattr(ctx, "steerable", frozenset()),
            accomplishments,
        )
        if has_target_earned_work
        else ()
    )
    if producer_rungs and has_target_earned_work and not external_supports:
        disposition = DepartureDisposition.OWNED
        reason = "exact departure producer is bounded by target earned-work accomplishments"
    elif external_supports:
        disposition = DepartureDisposition.REACTIVE
        reason = (
            "exact departure producer is conditional on external support, not target earned work"
        )
    else:
        disposition = DepartureDisposition.UNKNOWN
        reason = "exact departure producer could not be attributed to selected target work"
    return DepartureReading(
        disposition=disposition,
        occurrence_scan=(chain.effect.scan_id if chain is not None else occurrence_scan),
        source_scan=source_scan,
        producer_rungs=producer_rungs,
        external_supports=external_supports,
        reason=reason,
    )


def observe_departure(
    state: _PilotState,
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    channel_tag: str,
    from_value: Any,
) -> tuple[DepartureObservation, PLC]:
    """Observe one accepted trial's exact channel-departure receipt."""
    objective = trial.attempt.bearing.objective
    execution = trial.execution
    source_snap = execution.before_snap
    landing_receipt = execution.coast_receipt
    matching_occurrences = tuple(
        event
        for event in execution.timeline
        for tag, before, after in event.transitions
        if tag == channel_tag
        and _values_match(before, from_value)
        and not _values_match(after, from_value)
    )
    receipt_owned_occurrences = tuple(
        event
        for event in matching_occurrences
        if landing_receipt is not None
        and event.kind == "departure"
        and event.name in landing_receipt.fired
    )
    selected_occurrences = (
        receipt_owned_occurrences if receipt_owned_occurrences else matching_occurrences
    )
    occurrence_scan = selected_occurrences[0].scan if len(selected_occurrences) == 1 else None
    occurrence_ambiguous = len(selected_occurrences) > 1
    work = getattr(state, "work", None)
    cause_scan = (
        occurrence_scan
        if occurrence_scan is not None
        else (
            None if occurrence_ambiguous else getattr(getattr(work, "state", None), "scan_id", None)
        )
    )
    # Execution's pen timeline already names the exact occurrence scan.  Use
    # its predecessor for the earned-work receipt before asking for a deep
    # causal explanation.  On a long folded run, rebuilding that explanation
    # merely to discover an already-proven clean forward continuation turns
    # this classification into a history replay.
    if work is not None and cause_scan is not None and cause_scan > work.history.oldest_scan_id:
        anchor_snap = dict(work.history.at(cause_scan - 1).tags)
    else:
        anchor_snap = dict(source_snap)
    earned_work = getattr(state, "earned_work", None)

    # An exact one-scan departure may itself be an ordinary actionless chart
    # edge.  In that case do not run past it looking for global quiescence:
    # retain the observed landing and let the next Compass lifecycle read own
    # the next edge. Static charts corroborate this already-executed adjacency;
    # they still neither select nor execute an action.
    immediate_value = work.state.tags.get(channel_tag) if work is not None else None
    immediate_snap = dict(work.state.tags) if work is not None else {}
    immediate_progress = (
        earned_work.receipt(anchor_snap, immediate_snap)
        if earned_work is not None and getattr(earned_work, "components", ())
        else EarnedWorkReceipt()
    )
    goals = list(objective.channel_goals(channel_tag))
    progress_receipt = execution.scan_progress if execution is not None else None
    exact_execution_window = progress_receipt is not None and progress_receipt.kind == (
        "selected-producer"
    )
    if (landing_receipt is not None or exact_execution_window) and work is not None and goals:
        progress_erasing_values, all_resolved = _progress_erasing_values(
            earned_work,
            anchor_snap,
            channel_tag,
        )
        completed_actions = _completed_channel_actions(state, channel_tag)
        immediate_key = (
            _pilot_world_key(
                immediate_snap,
                state.key_config,
                state.pilot_rungs,
                state.active_requirements,
            )
            if state.key_config is not None
            else None
        )
        immediate_scope = EvidenceScope.capture(immediate_key, immediate_snap.items())

        def _adjacent_edge_allowed(edge: Any) -> bool:
            return _continuation_safety(
                edge,
                ctx,
                settled_key=immediate_key,
                settled_snap=immediate_snap,
                evidence_scope=immediate_scope,
                blocked_actions=ctx.blocked_actions,
                progress_erasing_values=progress_erasing_values,
                completed_actions=completed_actions,
            ).allowed

        compass = getattr(ctx, "compass", None)
        charts = (
            *(getattr(compass, "graphs", ()) or ()),
            *(getattr(compass, "chart_graphs", ()) or ()),
        )
        exact_edges = tuple(
            edge
            for graph in charts
            if graph.role.channel_tag == channel_tag
            for edge in graph.edges
            if edge.from_value is not ANY_FROM
            and _values_match(edge.from_value, from_value)
            and _values_match(edge.to_value, immediate_value)
            and edge.action is None
            and _adjacent_edge_allowed(edge)
        )
        continuation = NavigationEvidence.channel_continuation(
            charts,
            channel_tag,
            immediate_value,
            tuple(goals),
            edge_allowed=_adjacent_edge_allowed,
        )
        if (
            all_resolved
            and len(exact_edges) == 1
            and immediate_progress.movement is not EarnedWorkMovement.BACKWARD
            and isinstance(continuation, Reachable)
        ):
            return (
                DepartureObservation(
                    channel_tag=channel_tag,
                    from_value=from_value,
                    settled_value=immediate_value,
                    landing_receipt=landing_receipt,
                    progress=immediate_progress,
                    reading=DepartureReading(
                        disposition=DepartureDisposition.UNKNOWN,
                        occurrence_scan=occurrence_scan,
                        source_scan=(occurrence_scan - 1 if occurrence_scan is not None else None),
                        reason="exact actionless chart edge was observed",
                    ),
                    continuation=ContinuationEvidence(
                        continuation,
                        observed_adjacent=True,
                    ),
                    execution=execution,
                ),
                work,
            )

    fork, settlement_execution = _settle_departure(state, channel_tag)
    receipt = settlement_execution.coast_receipt
    assert receipt is not None
    settled_value = fork.state.tags.get(channel_tag)
    progress = (
        earned_work.receipt(anchor_snap, dict(fork.state.tags))
        if earned_work is not None and getattr(earned_work, "components", ())
        else EarnedWorkReceipt()
    )

    def _reading(*, explain: bool) -> DepartureReading:
        chain = (
            _shared_cause(work, channel_tag, cause_scan)
            if explain and work is not None and cause_scan is not None
            else None
        )
        return _departure_reading(
            chain,
            ctx,
            channel_tag,
            settled_value,
            occurrence_scan,
            earned_work,
        )

    def _observation(
        continuation: ContinuationEvidence,
        *,
        explain: bool = False,
    ) -> tuple[DepartureObservation, PLC]:
        return (
            DepartureObservation(
                channel_tag=channel_tag,
                from_value=from_value,
                settled_value=settled_value,
                landing_receipt=receipt,
                progress=progress,
                reading=_reading(explain=explain),
                continuation=continuation,
                execution=execution,
                settlement_execution=settlement_execution,
            ),
            fork,
        )

    def _not_inspected(reason: str) -> ContinuationEvidence:
        return ContinuationEvidence(Unknown(f"continuation not inspected because {reason}"))

    if receipt.stop_reason != "quiescent":
        # A cap-hit value may be mid-transition; refuse to classify it as a
        # landing. A timeout is not a settlement.
        reason = f"landing did not settle within cap ({receipt.stop_reason})"
        return _observation(_not_inspected(reason))

    if progress.movement is EarnedWorkMovement.BACKWARD:
        reason = "settled world is behind the exact source receipt"
        return _observation(_not_inspected(reason))

    goals: list[Any] = [from_value]
    for value in objective.channel_goals(channel_tag):
        if not any(_values_match(value, goal) for goal in goals):
            goals.append(value)

    progress_erasing_values, all_resolved = _progress_erasing_values(
        earned_work,
        anchor_snap,
        channel_tag,
    )
    if not all_resolved:
        reason = "cannot determine whether a route would erase earned progress"
        return _observation(_not_inspected(reason))

    completed_actions = _completed_channel_actions(state, channel_tag)
    graphs = getattr(getattr(ctx, "compass", None), "graphs", ()) or ()
    settled_snap = dict(fork.state.tags)
    settled_key = (
        _pilot_world_key(
            settled_snap,
            state.key_config,
            state.pilot_rungs,
            state.active_requirements,
        )
        if state.key_config is not None
        else None
    )
    evidence_scope = EvidenceScope.capture(settled_key, settled_snap.items())

    def _safe_continuation_edge(edge: Any) -> bool:
        return _continuation_safety(
            edge,
            ctx,
            settled_key=settled_key,
            settled_snap=settled_snap,
            evidence_scope=evidence_scope,
            blocked_actions=ctx.blocked_actions,
            progress_erasing_values=progress_erasing_values,
            completed_actions=completed_actions,
        ).allowed

    continuation = NavigationEvidence.channel_continuation(
        tuple(graphs),
        channel_tag,
        settled_value,
        tuple(goals),
        edge_allowed=_safe_continuation_edge,
    )
    if isinstance(continuation, Reachable):
        return _observation(
            ContinuationEvidence(continuation),
            explain=progress.movement is not EarnedWorkMovement.FORWARD,
        )

    # A unique, non-avoided operator push that the program is waiting for is
    # affirmative continuation evidence too. This covers machines whose useful
    # progress is structural (state + command handshake) and exposes no earned work.
    from pyrung.core.analysis.pilot.trace_read import WorldView

    awaited_action_context = ("pdg", "program", "steerable", "opaque_loop", "pipeline_roles")
    if not all(hasattr(ctx, name) for name in awaited_action_context):
        return _observation(ContinuationEvidence(continuation))

    def _legal_awaited_action(action: tuple[str, Any]) -> bool:
        return _awaited_action_allowed(
            action,
            settled_key=settled_key,
            knowledge=ctx.compass.knowledge,
            blocked_actions=ctx.blocked_actions,
        )

    awaited_action = unique_legal_awaited_action(
        WorldView(
            snapshot=dict(fork.state.tags),
            pdg=ctx.pdg,
            program=ctx.program,
            steerable=ctx.steerable,
            opaque_loop=ctx.opaque_loop,
            prior=getattr(ctx, "domain_prior", None),
        ),
        channel_tag,
        ctx.pipeline_roles,
        action_allowed=_legal_awaited_action,
        action_avoided=lambda action: _avoid_forces(
            ctx,
            [action],
            dict(fork.state.tags),
        ),
    )
    if awaited_action is not None:
        return _observation(
            ContinuationEvidence(
                continuation,
                awaited_action_inspected=True,
                awaited_action=awaited_action,
            ),
            explain=progress.movement is not EarnedWorkMovement.FORWARD,
        )
    return _observation(
        ContinuationEvidence(continuation, awaited_action_inspected=True),
        # With neither a chart route nor an awaited action the classification
        # is UNKNOWN regardless of producer ancestry. Investigation owns the
        # next question and can try exact local evidence before selectively
        # following older supports; a deep walk here cannot change the verdict.
        explain=False,
    )


def classify_departure(observation: DepartureObservation) -> DepartureResult:
    """Purely classify one immutable departure observation."""
    receipt = observation.landing_receipt
    progress = observation.progress
    continuation = observation.continuation
    status = continuation.channel_status

    if progress.movement is EarnedWorkMovement.BACKWARD:
        classification = DepartureClassification.REGRESSION
        reason = "settled world is behind the exact source receipt"
    elif continuation.observed_adjacent and isinstance(status, Reachable):
        classification = DepartureClassification.CLEAN_CONTINUATION
        reason = "exact observed actionless chart edge has a clean forward continuation"
    elif receipt is None:
        classification = DepartureClassification.UNKNOWN
        reason = "departure has no exact settled landing receipt"
    elif receipt.stop_reason != "quiescent":
        classification = DepartureClassification.UNKNOWN
        reason = f"landing did not settle within cap ({receipt.stop_reason})"
    elif isinstance(status, Reachable) or continuation.awaited_action is not None:
        classification = DepartureClassification.CLEAN_CONTINUATION
        reason = (
            continuation.awaited_action.note
            if continuation.awaited_action is not None
            else "constrained navigation evidence has a clean forward continuation "
            "that preserves earned progress and does not reopen completed work"
        )
    elif isinstance(status, Unknown) and status.reason.startswith(
        "continuation not inspected because "
    ):
        classification = DepartureClassification.UNKNOWN
        reason = status.reason.removeprefix("continuation not inspected because ")
    elif not continuation.awaited_action_inspected:
        classification = DepartureClassification.UNKNOWN
        reason = (
            "chart has no clean route"
            if not isinstance(status, Unknown)
            else "no chart or awaited-action evidence"
        )
    else:
        classification = DepartureClassification.UNKNOWN
        reason = (
            "no clean route is currently proven"
            if not isinstance(status, Unknown)
            else "no transition structure for the channel"
        )

    if (
        classification is DepartureClassification.CLEAN_CONTINUATION
        and observation.reading.disposition is DepartureDisposition.REACTIVE
    ):
        classification = DepartureClassification.UNKNOWN
        reason = f"{observation.reading.reason}; {reason}"

    logger.debug(
        "departure: %s %r->%r (%d settle scans, %s): %s; %s",
        observation.channel_tag,
        observation.from_value,
        observation.settled_value,
        observation.logical_scans,
        receipt.stop_reason if receipt is not None else "execution-window",
        classification,
        reason,
    )
    return DepartureResult(observation, classification, reason)
