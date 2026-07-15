"""Departure classification for ordinary PILOT motion.

ASSESS's ejection arm used to treat every channel departure as a regression:
investigate, revert, re-march. But useful program-owned motion (the machine
issuing its own Hold mid-recipe) destroys nothing — the recipe gauge
(``Internal__Step``, event-earned counters) survives and the machine's own
transition structure offers a forward route back.  Reverting it throws away the
whole march, and investigation honestly confirms nothing.

The law (state names like HELD/ABORTED are instances, never the rule):

**Regression is resurrected work, not channel displacement.**  A departure is
classified at its settled landing by the routes back:

* a route is *dirty* when it must pass through a channel value where a
  gauge **reset** is enabled (``S_Resetting`` guards
  ``Internal__Step := 101`` — writing behind the anchor destroys earned work),
  or when one of its command edges **resurrects a discharged obligation** (the
  route from ABORTED re-requires the very ``C_Clear``/``C_Reset``/``C_Start``
  presses the march already committed, in the same channel contexts);
* a clean route existing → **provisional**: useful static evidence supports
  continued piloting;
* missing or inconclusive route evidence → **unknown**: no regression fact is
  minted, but operational policy remains conservative (investigate/revert);
* an observed gauge move behind the exact source receipt → **regression**.

The route is evidence, not a contract and not carried state. Progress later
settles the provisional attempt whenever gauge comparability returns; it does
not wait for a stored channel value to recur.

Classifying the landing runs the ship with pilot rungs active, so this is an
ASSESS-side helper; ``progress.py`` is its only consumer.
Falsified-and-replaced proofs (see
``scratchpad/burner/detour_recognition.md``): raw ``_pilot_state_key`` novelty
(accepts destructive landings; threshold-aliases event-earned work) and
committed-channel-history membership (a sampled shadow of the reset test).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _avoid_forces,
    fork_with_rungs,
)
from pyrung.core.analysis.pilot.charts import ANY_FROM
from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastSession
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

    @property
    def is_provisional(self) -> bool:
        # Unknown is an epistemic classification, not permission to wander.
        # Operationally it follows the conservative rollback/investigation arm.
        return self.verdict == "provisional"


@dataclass(frozen=True)
class Provisional:
    """A bounded attempt awaiting target-relative progress evidence.

    ``gauge_at_source`` is captured at the observed settled landing.
    ``checkpoint_depth`` identifies the exact rollback boundary. ``expires_at``
    bounds exploration when the gauge remains incomparable or merely preserved.
    """

    channel_tag: str
    from_value: Any
    gauge_at_source: tuple[tuple[str, Any], ...]
    checkpoint_depth: int
    started_at: int
    expires_at: int
    classification: str


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


def _clean_route(
    graph: Any,
    start: Any,
    goals: tuple[Any, ...],
    blocked_values: frozenset[Any],
    discharged: set[tuple[str, Any, Any]],
    edge_allowed: Callable[[Any], bool] | None = None,
) -> tuple[tuple[Any, ...], Any] | None:
    """BFS for a route to a goal avoiding reset values and resurrected debt.

    An edge is unusable when its destination hosts an enabled reset, or when
    its command action replicates a discharged ``(action, context)`` pair.
    Returns ``(route_values, reentry_value)`` or None.
    """

    def _edge_resurrects(edge: Any, at_value: Any) -> bool:
        if edge.action is None:
            return False
        tag, value = edge.action
        for a_tag, a_value, a_context in discharged:
            if a_tag != tag or not _values_match(a_value, value):
                continue
            if a_context is None or edge.from_value is ANY_FROM:
                return True
            if _values_match(a_context, at_value):
                return True
        return False

    def _key(v: Any) -> str:
        return f"{type(v).__name__}:{v!r}"

    # Settlement can itself land on a goal (the terminal coast pauses at
    # Completing, then the departure settle reaches Completed).  That is the
    # strongest possible clean road: zero remaining edges, hence no eraser or
    # resurrected obligation to cross.
    if any(_values_match(start, goal) for goal in goals):
        return (start,), start

    queue: deque[tuple[Any, tuple[Any, ...]]] = deque([(start, (start,))])
    visited = {_key(start)}
    while queue:
        value, path = queue.popleft()
        for edge in graph.edges:
            if edge_allowed is not None and not edge_allowed(edge):
                continue
            if edge.from_value is not ANY_FROM and not _values_match(edge.from_value, value):
                continue
            if any(_values_match(edge.to_value, b) for b in blocked_values):
                continue
            if _edge_resurrects(edge, value):
                continue
            k = _key(edge.to_value)
            if k in visited:
                continue
            next_path = (*path, edge.to_value)
            if any(_values_match(edge.to_value, g) for g in goals):
                return next_path, edge.to_value
            visited.add(k)
            queue.append((edge.to_value, next_path))
    return None


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

    def _v(verdict: str, reason: str, reentry: Any = None, route: tuple = ()) -> DepartureVerdict:
        logger.debug(
            "departure: %s %r->%r (%d settle scans, %s): %s — %s",
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
        )

    if receipt.stop_reason != "quiescent":
        # A cap-hit value may be mid-transition; refuse to classify it as a
        # landing (the receipt names the distinction the old stable-counter
        # could not — a timeout is not a settlement).
        return _v("unknown", f"landing did not settle within cap ({receipt.stop_reason})")

    gauge = getattr(state, "gauge", None)
    if gauge is not None and getattr(gauge, "components", ()):
        observed = gauge.compare(anchor_snap, dict(fork.state.tags))
        if observed == "behind":
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
    saw_graph = False
    for graph in graphs:
        if graph.role.channel_tag != channel_tag:
            continue
        saw_graph = True
        found = _clean_route(
            graph,
            settled_value,
            tuple(goals),
            blocked_values,
            discharged,
            edge_allowed=lambda edge: (
                edge.action is None
                or (
                    route_allowed(edge.action)
                    and not _avoid_forces(ctx, [edge.action], dict(fork.state.tags))
                )
            ),
        )
        if found is not None:
            route, reentry = found
            return _v(
                "provisional",
                f"clean forward route {' -> '.join(repr(v) for v in route)} "
                "(no reset, no resurrected obligation)",
                reentry,
                route,
            )

    # A unique, non-avoided operator push that the program is waiting for is
    # affirmative continuation evidence too. This covers machines whose useful
    # progress is structural (state + command handshake) and exposes no gauge.
    from pyrung.core.analysis.pilot.currents import WorldView, operator_action_for_state

    current_context = ("pdg", "program", "steerable", "opaque_loop", "pipeline_roles")
    if not all(hasattr(ctx, name) for name in current_context):
        qualifier = "chart has no clean route" if saw_graph else "no chart or current evidence"
        return _v("unknown", qualifier)

    current = operator_action_for_state(
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
        avoid_pred=getattr(ctx, "avoid_pred", None),
    )
    if current is not None:
        return _v("provisional", current.note, current.to_state, (settled_value,))
    return _v(
        "unknown",
        "no clean route is currently proven"
        if saw_graph
        else "no transition structure for the channel",
    )
