"""Classify an observed channel departure for post-commit recovery.

``classify_departure`` settles the landing under the active holds, compares
target-relative gauge evidence, and inspects static routes for reset boundaries
or already-discharged actions that would have to be repeated. It permits
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
from pyrung.core.analysis.pilot.gauge import GaugeReceipt
from pyrung.core.analysis.pilot.navigation import BearingObjective
from pyrung.core.analysis.pilot.navigation_evidence import NavigationEvidence, Reachable
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
    progress: GaugeReceipt = GaugeReceipt((), (), "unknown")
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
    progress: GaugeReceipt = GaugeReceipt((), (), "unknown")
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


def _discharged_actions(state: _PilotState, channel_tag: str) -> set[tuple[str, Any, Any]]:
    """Discharged obligations: ``(action_tag, value, channel_value_at_press)``.

    The committed steps are the work the march already did.  A forward plan that
    re-requires one of these presses *in the same channel context* is
    resurrected debt — the mechanical meaning of "undoes our progress".
    Context comes from the owning committed operation's before-snapshot; release
    and pulse steps therefore carry the same exact channel context."""
    out: set[tuple[str, Any, Any]] = set()
    for act in state.committed_acts:
        context = act.context.before_snap.get(channel_tag)
        for step in act.steps:
            for tag, value in step.inputs.items():
                out.add((tag, value, context))
    return out


def _reset_blocked_values(
    gauge: Any,
    anchor_snap: Any,
    channel_tag: str,
) -> tuple[frozenset[Any], bool]:
    """Channel values where a gauge reset is enabled, given the anchor.

    A reset is a literal load *behind* the anchor value in the component's
    earn direction (a load ahead of the anchor is a shortcut, not a reset).
    Returns ``(blocked_values, all_resolved)`` — an unresolved reset poisons
    the whole route analysis (the caller fails closed).
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


def _edge_resurrects(
    edge: Any,
    discharged: set[tuple[str, Any, Any]],
) -> bool:
    if edge.action is None:
        return False
    tag, value = edge.action
    for action_tag, action_value, action_context in discharged:
        if action_tag != tag or not _values_match(action_value, value):
            continue
        if action_context is None or edge.from_value is ANY_FROM:
            return True
        if _values_match(action_context, edge.from_value):
            return True
    return False


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
        else GaugeReceipt((), (), "unknown")
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

    if progress.effect == "behind":
        return _v("regression", "settled world is behind the exact source receipt")

    goals: list[Any] = [from_value]
    for value in objective.channel_goals(channel_tag):
        if not any(_values_match(value, goal) for goal in goals):
            goals.append(value)

    blocked_values, all_resolved = _reset_blocked_values(gauge, anchor_snap, channel_tag)
    if not all_resolved:
        return _v("unknown", "a gauge reset is unresolved")

    discharged = _discharged_actions(state, channel_tag)
    graphs = getattr(getattr(ctx, "compass", None), "graphs", ()) or ()
    route_allowed = getattr(ctx, "route_allowed", lambda _action: True)
    settled_snap = dict(fork.state.tags)
    settled_key = (
        _pilot_world_key(settled_snap, state.key_config, state.rungs)
        if state.key_config is not None
        else None
    )
    continuation = NavigationEvidence.channel_continuation(
        tuple(graphs),
        channel_tag,
        settled_value,
        tuple(goals),
        edge_allowed=lambda edge: (
            ctx.compass.knowledge.static_edge_status(edge, settled_key, settled_snap)
            not in {"contradicted", "no_change"}
            and not any(_values_match(edge.to_value, blocked) for blocked in blocked_values)
            and not _edge_resurrects(edge, discharged)
            and (
                edge.action is None
                or (
                    route_allowed(edge.action)
                    and not _avoid_forces(ctx, [edge.action], dict(fork.state.tags))
                )
            )
        ),
    )
    saw_graph = any(graph.role.channel_tag == channel_tag for graph in graphs)
    if isinstance(continuation, Reachable):
        return _v(
            "continue",
            "constrained navigation evidence has a clean forward continuation "
            "(no reset, no resurrected obligation)",
        )

    # A unique, non-avoided operator push that the program is waiting for is
    # affirmative continuation evidence too. This covers machines whose useful
    # progress is structural (state + command handshake) and exposes no gauge.
    from pyrung.core.analysis.pilot.currents import WorldView, current_readings

    current_context = ("pdg", "program", "steerable", "opaque_loop", "pipeline_roles")
    if not all(hasattr(ctx, name) for name in current_context):
        qualifier = "chart has no clean route" if saw_graph else "no chart or current evidence"
        return _v("unknown", qualifier)

    readings = current_readings(
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
    )
    legal_readings = tuple(
        reading
        for reading in readings
        if route_allowed(reading.action)
        and not _avoid_forces(ctx, [reading.action], dict(fork.state.tags))
    )
    if len(legal_readings) == 1:
        current = legal_readings[0]
        return _v("continue", current.note, current.to_state, (settled_value,))
    return _v(
        "unknown",
        "no clean route is currently proven"
        if saw_graph
        else "no transition structure for the channel",
    )
