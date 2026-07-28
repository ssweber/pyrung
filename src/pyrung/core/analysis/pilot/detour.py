"""Classify an observed channel departure for post-commit recovery.

``classify_departure`` settles the landing under the active holds, compares
target-relative gauge evidence, and inspects static routes for reset boundaries
or completed channel actions that would have to be repeated. It permits
continued motion only when a clean continuation is supported, reports regression
when earned work moved backward, and returns unknown otherwise.

The returned route evidence is not retained as a plan. ``progress.py`` is the
consumer and applies the conservative policy for unknown departures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _avoid_forces,
    _pilot_world_key,
    fork_with_rungs,
)
from pyrung.core.analysis.pilot.causal import (
    _shared_cause,
    occurrence_external_supports,
)
from pyrung.core.analysis.pilot.charts import ANY_FROM
from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastSession
from pyrung.core.analysis.pilot.compass import CompassKnowledge, unique_legal_current_reading
from pyrung.core.analysis.pilot.gauge import GaugeMovement, GaugeReceipt
from pyrung.core.analysis.pilot.navigation import BearingObjective
from pyrung.core.analysis.pilot.navigation_evidence import (
    NavigationEvidence,
    Reachable,
    StaticEdgeAdmission,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import _PilotContext, _PilotState

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
    partitions their supports at target-owned Gauge accomplishments, and
    carries the occurrence-local Gauge receipt used as durable tenure.
    """

    disposition: DepartureDisposition
    occurrence_scan: int | None
    source_scan: int | None
    producer_rungs: tuple[int, ...] = ()
    external_supports: tuple[tuple[str, Any], ...] = ()
    progress: GaugeReceipt = GaugeReceipt()
    reason: str = ""

    @property
    def target_owned(self) -> bool:
        return self.disposition is DepartureDisposition.OWNED


@dataclass(frozen=True)
class DepartureVerdict:
    """The classification of one channel departure, with its receipts."""

    decision: str  # "continue" | "unknown" | "regression"
    reason: str
    settled_fork: Any  # PLC — the settled landing (pilot rungs active)
    settled_value: Any
    settle_scans: int
    reentry_value: Any = None  # where the clean route re-enters, if found
    route: tuple[Any, ...] = ()  # channel values along the clean route
    progress: GaugeReceipt = GaugeReceipt()
    reading: DepartureReading = DepartureReading(
        DepartureDisposition.UNKNOWN,
        None,
        None,
    )

    @property
    def can_continue(self) -> bool:
        # Unknown is an epistemic classification, not permission to wander.
        # Operationally it follows the conservative rollback/investigation arm.
        return self.decision == "continue"


def _settle_departure(state: _PilotState, channel_tag: str) -> tuple[Any, CoastReceipt]:
    """Ride a rung-driven fork to the departure's stable landing (bounded).

    The departure bump lands at the *first* departure scan — mid-transition
    (Holding, Aborting).  Classification needs the landing, so let the
    departure's own chain complete with the installed pilot rungs active,
    exactly as a coast would — ``settle_landing`` rides every hop and the
    receipt records the chain.  A ``"timeout"`` receipt means the cap-hit
    value may be mid-transition; the caller must not trust it as settled.
    """
    fork = fork_with_rungs(state.work, state.rungs)
    receipt = CoastSession(fork, kind="departure-settle").settle_landing(channel_tag)
    return fork, receipt


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
        context = act.context.before_snap.get(channel_tag)
        for step in act.steps:
            for tag, value in step.inputs.items():
                out.add((tag, value, context))
    return out


def _progress_erasing_values(
    gauge: Any,
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
    for component in getattr(gauge, "components", ()) or ():
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
        blocked_actions=blocked_actions,
    )
    return ContinuationSafety(
        admission=admission,
        erases_earned_progress=_erases_earned_progress(edge, progress_erasing_values),
        reopens_completed_work=_reopens_completed_work(edge, completed_actions),
    )


def _current_action_allowed(
    action: tuple[str, Any],
    *,
    settled_key: tuple[Any, ...] | None,
    knowledge: CompassKnowledge,
    blocked_actions: frozenset[tuple[str, Any]],
) -> bool:
    """Whether one structural current may support this settled world."""

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
    progress: GaugeReceipt,
    gauge: Any,
) -> DepartureReading:
    """Interpret exact cause identity against the selected target work."""
    source_scan = chain.effect.scan_id - 1 if chain is not None else None
    if not _is_hold_landing(ctx, channel_tag, settled_value):
        return DepartureReading(
            disposition=DepartureDisposition.UNKNOWN,
            occurrence_scan=(chain.effect.scan_id if chain is not None else occurrence_scan),
            source_scan=source_scan,
            progress=progress,
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
        component.tag for component in getattr(gauge, "components", ()) or ()
    )
    has_target_gauge = bool(accomplishments)
    external_supports = (
        occurrence_external_supports(
            chain,
            frozenset(producer_rungs),
            getattr(ctx, "steerable", frozenset()),
            accomplishments,
        )
        if has_target_gauge
        else ()
    )
    if producer_rungs and has_target_gauge and not external_supports:
        disposition = DepartureDisposition.OWNED
        reason = "exact departure producer is bounded by target Gauge accomplishments"
    elif external_supports:
        disposition = DepartureDisposition.REACTIVE
        reason = (
            "exact departure producer is conditional on external support, not target Gauge work"
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
        progress=progress,
        reason=reason,
    )


def classify_departure(
    state: _PilotState,
    ctx: _PilotContext,
    objective: BearingObjective,
    channel_tag: str,
    from_value: Any,
    source_snap: Any,
    *,
    occurrence_scan: int | None = None,
) -> DepartureVerdict:
    """Classify the channel departure the work fork is currently paused in."""
    work = getattr(state, "work", None)
    cause_scan = (
        occurrence_scan
        if occurrence_scan is not None
        else getattr(getattr(work, "state", None), "scan_id", None)
    )
    chain = _shared_cause(work, channel_tag, cause_scan) if work is not None else None
    if (
        work is not None
        and chain is not None
        and chain.effect.scan_id > work.history.oldest_scan_id
    ):
        anchor_snap = dict(work.history.at(chain.effect.scan_id - 1).tags)
    else:
        anchor_snap = dict(source_snap)
    fork, receipt = _settle_departure(state, channel_tag)
    settled_value = fork.state.tags.get(channel_tag)
    settle_scans = receipt.end_scan - receipt.start_scan
    gauge = getattr(state, "gauge", None)
    progress = (
        gauge.receipt(anchor_snap, dict(fork.state.tags))
        if gauge is not None and getattr(gauge, "components", ())
        else GaugeReceipt()
    )
    reading = _departure_reading(
        chain,
        ctx,
        channel_tag,
        settled_value,
        occurrence_scan,
        progress,
        gauge,
    )

    def _v(decision: str, reason: str, reentry: Any = None, route: tuple = ()) -> DepartureVerdict:
        if decision == "continue" and reading.disposition is DepartureDisposition.REACTIVE:
            decision = "unknown"
            reason = f"{reading.reason}; {reason}"
        logger.debug(
            "departure: %s %r->%r (%d settle scans, %s): %s; %s",
            channel_tag,
            from_value,
            settled_value,
            settle_scans,
            receipt.stop_reason,
            decision,
            reason,
        )
        return DepartureVerdict(
            decision=decision,
            reason=reason,
            settled_fork=fork,
            settled_value=settled_value,
            settle_scans=settle_scans,
            reentry_value=reentry,
            route=route,
            progress=progress,
            reading=reading,
        )

    if receipt.stop_reason != "quiescent":
        # A cap-hit value may be mid-transition; refuse to classify it as a
        # landing (the receipt names the distinction the old stable-counter
        # could not — a timeout is not a settlement).
        return _v("unknown", f"landing did not settle within cap ({receipt.stop_reason})")

    if progress.movement is GaugeMovement.BACKWARD:
        return _v("regression", "settled world is behind the exact source receipt")

    goals: list[Any] = [from_value]
    for value in objective.channel_goals(channel_tag):
        if not any(_values_match(value, goal) for goal in goals):
            goals.append(value)

    progress_erasing_values, all_resolved = _progress_erasing_values(
        gauge,
        anchor_snap,
        channel_tag,
    )
    if not all_resolved:
        return _v(
            "unknown",
            "cannot determine whether a route would erase earned progress",
        )

    completed_actions = _completed_channel_actions(state, channel_tag)
    graphs = getattr(getattr(ctx, "compass", None), "graphs", ()) or ()
    settled_snap = dict(fork.state.tags)
    settled_key = (
        _pilot_world_key(settled_snap, state.key_config, state.rungs)
        if state.key_config is not None
        else None
    )

    def _safe_continuation_edge(edge: Any) -> bool:
        return _continuation_safety(
            edge,
            ctx,
            settled_key=settled_key,
            settled_snap=settled_snap,
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
    saw_graph = any(graph.role.channel_tag == channel_tag for graph in graphs)
    if isinstance(continuation, Reachable):
        return _v(
            "continue",
            "constrained navigation evidence has a clean forward continuation "
            "that preserves earned progress and does not reopen completed work",
        )

    # A unique, non-avoided operator push that the program is waiting for is
    # affirmative continuation evidence too. This covers machines whose useful
    # progress is structural (state + command handshake) and exposes no gauge.
    from pyrung.core.analysis.pilot.types import WorldView

    current_context = ("pdg", "program", "steerable", "opaque_loop", "pipeline_roles")
    if not all(hasattr(ctx, name) for name in current_context):
        qualifier = "chart has no clean route" if saw_graph else "no chart or current evidence"
        return _v("unknown", qualifier)

    def _legal_current_action(action: tuple[str, Any]) -> bool:
        return _current_action_allowed(
            action,
            settled_key=settled_key,
            knowledge=ctx.compass.knowledge,
            blocked_actions=ctx.blocked_actions,
        )

    current = unique_legal_current_reading(
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
        action_allowed=_legal_current_action,
        action_avoided=lambda action: _avoid_forces(
            ctx,
            [action],
            dict(fork.state.tags),
        ),
    )
    if current is not None:
        return _v("continue", current.note, current.to_state, (settled_value,))
    return _v(
        "unknown",
        "no clean route is currently proven"
        if saw_graph
        else "no transition structure for the channel",
    )
