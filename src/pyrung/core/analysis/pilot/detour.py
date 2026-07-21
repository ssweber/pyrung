"""Classify an observed channel departure for post-commit recovery.

``classify_departure`` settles the landing under the active holds, compares
target-relative gauge evidence, and inspects static routes for reset boundaries
or already-discharged actions that would have to be repeated. It returns
provisional motion only when a clean continuation is supported, regression
when earned work is known to have moved backward, and unknown otherwise.

The returned route evidence is not retained as a plan. ``progress.py`` is the
consumer and applies the conservative policy for unknown departures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _avoid_forces,
    fork_with_rungs,
)
from pyrung.core.analysis.pilot.charts import ANY_FROM
from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastSession
from pyrung.core.analysis.pilot.gauge import GaugeReceipt
from pyrung.core.analysis.pilot.navigation_evidence import NavigationEvidence, Reachable
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import _PilotContext, _PilotState

logger = logging.getLogger(__name__)

# Settlement policy (window/cap) lives in coast.LIMITS — landing_confirm_scans
# / landing_cap; the mechanism is CoastSession.settle_landing.


@dataclass(frozen=True)
class DepartureVerdict:
    """The classification of one channel departure, with its receipts."""

    verdict: str  # "provisional" | "unknown" | "regression"
    reason: str
    settled_fork: Any  # PLC — the settled landing (pilot rungs active)
    settled_value: Any
    settle_scans: int
    reentry_value: Any = None  # where the clean route re-enters, if found
    route: tuple[Any, ...] = ()  # channel values along the clean route
    progress: GaugeReceipt = GaugeReceipt((), (), "unknown")

    @property
    def is_provisional(self) -> bool:
        # Unknown is an epistemic classification, not permission to wander.
        # Operationally it follows the conservative rollback/investigation arm.
        return self.verdict == "provisional"


@dataclass(frozen=True)
class Provisional:
    """A bounded attempt awaiting target-relative progress evidence.

    ``gauge_at_source`` is captured at the observed settled landing.
    ``checkpoint_depth`` identifies the exact rollback boundary. ``started_at``
    and ``expires_at`` use PILOT's search-scan coordinate, so instruction-owned
    waiting does not consume the bounded exploration lifetime.
    """

    channel_tag: str
    from_value: Any
    gauge_at_source: tuple[tuple[str, Any], ...]
    checkpoint_depth: int
    started_at: int
    expires_at: int
    entry_progress: GaugeReceipt = GaugeReceipt((), (), "unknown")
    entry_banked: bool = False


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
    Context comes from each step's before-snapshot (``step_contexts``); a
    missing context records ``None``, which matches any (conservative)."""
    before_by_scan = {sc.scan_before: sc.before_snap for sc in state.step_contexts}
    out: set[tuple[str, Any, Any]] = set()
    for step in state.steps:
        before = before_by_scan.get(step.scan_before) or {}
        context = before.get(channel_tag)
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


def classify_departure(
    state: _PilotState,
    ctx: _PilotContext,
    channel_tag: str,
    from_value: Any,
    source_snap: Any,
) -> DepartureVerdict:
    """Classify the channel departure the work fork is currently paused in."""
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

    def _v(verdict: str, reason: str, reentry: Any = None, route: tuple = ()) -> DepartureVerdict:
        logger.debug(
            "departure: %s %r->%r (%d settle scans, %s): %s; %s",
            channel_tag,
            from_value,
            settled_value,
            settle_scans,
            receipt.stop_reason,
            verdict,
            reason,
        )
        return DepartureVerdict(
            verdict=verdict,
            reason=reason,
            settled_fork=fork,
            settled_value=settled_value,
            settle_scans=settle_scans,
            reentry_value=reentry,
            route=route,
            progress=progress,
        )

    if receipt.stop_reason != "quiescent":
        # A cap-hit value may be mid-transition; refuse to classify it as a
        # landing (the receipt names the distinction the old stable-counter
        # could not — a timeout is not a settlement).
        return _v("unknown", f"landing did not settle within cap ({receipt.stop_reason})")

    if progress.effect == "behind":
        return _v("regression", "settled world is behind the exact source receipt")

    goals: list[Any] = [from_value]
    target_tag = getattr(ctx, "target_tag", None)
    target_value = getattr(ctx, "target_value", None)
    if target_tag == channel_tag and not any(_values_match(target_value, g) for g in goals):
        goals.append(target_value)

    blocked_values, all_resolved = _reset_blocked_values(gauge, anchor_snap, channel_tag)
    if not all_resolved:
        return _v("unknown", "a gauge reset is unresolved")

    discharged = _discharged_actions(state, channel_tag)
    graphs = getattr(getattr(ctx, "compass", None), "graphs", ()) or ()
    route_allowed = getattr(ctx, "route_allowed", lambda _action: True)
    continuation = NavigationEvidence.channel_continuation(
        tuple(graphs),
        channel_tag,
        settled_value,
        tuple(goals),
        edge_allowed=lambda edge: (
            ctx.compass.knowledge.static_overlays.get(edge.identity)
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
            "provisional",
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
        return _v("provisional", current.note, current.to_state, (settled_value,))
    return _v(
        "unknown",
        "no clean route is currently proven"
        if saw_graph
        else "no transition structure for the channel",
    )
