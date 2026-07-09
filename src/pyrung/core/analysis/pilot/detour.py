"""Departure classification — "bumped out of the corridor: page turned, or work destroyed?"

ASSESS's ejection arm used to treat every channel departure as a regression:
investigate, revert, re-march.  But a program-intended detour (the machine
issuing its own Hold mid-recipe) destroys nothing — the recipe credential
(``Internal__Step``, event-earned counters) survives and the machine's own
transition structure offers a forward road back.  Reverting it throws away the
whole march, and investigation honestly confirms nothing.

The law (state names like HELD/ABORTED are instances, never the rule):

**Regression is resurrected work, not channel displacement.**  A departure is
classified at its settled landing by the roads back:

* a road is *dirty* when it must pass through a channel value where a
  credential **eraser** is enabled (``S_Resetting`` guards
  ``Internal__Step := 101`` — writing behind the anchor destroys earned work),
  or when one of its command edges **resurrects a discharged obligation** (the
  route from ABORTED re-requires the very ``C_Clear``/``C_Reset``/``C_Start``
  presses the march already committed, in the same channel contexts);
* a clean road existing → **stopover**: keep the world, keep the pre-departure
  checkpoint as a bailout, skip investigation, and take a **detour loan** —
  the verdict is provisional until the corridor is rejoined;
* no clean road → **regression** now: investigate and revert, exactly as
  before this module existed (fail-closed: no graph, unresolved erasers, or
  nothing reachable all land here).

The loan settles at corridor rejoin by comparing credential marks
(``credential.py``): *advanced* → promote the landing to a real checkpoint;
anything else → the departure gained nothing — revert to the bailout, remember
the failed signature, and let the re-ejection classify as regression so
investigation runs on a tight, fresh incident window.

Settlement runs the ship (holds animated), so this is an ASSESS-side helper;
``progress.py`` is its only consumer.  Falsified-and-replaced proofs (see
``scratchpad/burner/detour_recognition.md``): raw ``_pilot_state_key`` novelty
(accepts destructive landings; threshold-aliases event-earned work) and
committed-channel-history membership (a sampled shadow of the eraser test).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _add_conditional_hold_rungs,
    _split_holds,
    fork_with_holds,
)
from pyrung.core.analysis.pilot.charts import ANY_FROM
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.types import _PilotContext, _PilotState

logger = logging.getLogger(__name__)

# Settlement bounds: a departure's own transition chain (Holding -> Held,
# Aborting -> Aborted) completes in scans-to-seconds; the channel must sit
# still for a full second of scans before we trust the landing.
_SETTLE_STABLE_FOR = 100
_SETTLE_CAP = 2000


@dataclass(frozen=True)
class DepartureVerdict:
    """The classification of one channel departure, with its receipts."""

    verdict: str  # "stopover" | "regression"
    reason: str
    settled_fork: Any  # PLC — the settled landing (holds animated)
    settled_value: Any
    settle_scans: int
    reentry_value: Any = None  # where the clean road re-enters, if found
    road: tuple[Any, ...] = ()  # channel values along the clean road

    @property
    def is_stopover(self) -> bool:
        return self.verdict == "stopover"


@dataclass(frozen=True)
class DetourLoan:
    """A provisional stopover: the departure is accepted on credit.

    ``anchor_mark`` is the credential receipt at the ejection scan;
    settlement compares it against the rejoin.  ``bailout_len`` is the
    checkpoint-stack depth at loan time — a failed loan reverts there.
    ``signature`` remembers a failed loan so the same departure classifies
    as regression next time (investigation then gets a tight fresh window).
    """

    channel_tag: str
    from_value: Any
    anchor_mark: tuple[tuple[str, Any], ...]
    bailout_len: int
    taken_at_scan: int
    signature: tuple[Any, ...]


def loan_signature(channel_tag: str, from_value: Any, settled_value: Any) -> tuple[Any, ...]:
    return (channel_tag, from_value, settled_value)


def _settle_departure(state: _PilotState, channel_tag: str) -> tuple[Any, int]:
    """Run a holds-animated fork until the channel stops moving (bounded).

    The ejection guard pauses at the *first* departure scan — mid-transition
    (Holding, Aborting).  Classification needs the landing, so let the
    departure's own chain complete: steady holds as rungs, conditional holds
    (the oscillation correctives) animated, exactly as a coast would.
    """
    fork = fork_with_holds(state.work, state.forced_holds)
    _steady, conditional = _split_holds(list(state.forced_holds.items()))
    if conditional:
        _add_conditional_hold_rungs(fork, conditional)
    last = fork.state.tags.get(channel_tag)
    stable = 0
    n = 0
    while n < _SETTLE_CAP and stable < _SETTLE_STABLE_FOR:
        fork.step()
        n += 1
        cur = fork.state.tags.get(channel_tag)
        if _values_match(cur, last):
            stable += 1
        else:
            stable = 0
            last = cur
    return fork, n


def _discharged_actions(state: _PilotState, channel_tag: str) -> set[tuple[str, Any, Any]]:
    """Discharged obligations: ``(action_tag, value, channel_value_at_press)``.

    The committed steps are the work the march already did.  A route edge that
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


def _eraser_blocked_values(
    cut: Any,
    anchor_snap: Any,
    channel_tag: str,
) -> tuple[frozenset[Any], bool]:
    """Channel values where a credential eraser is enabled, given the anchor.

    An eraser is a literal load *behind* the anchor value in the component's
    earn direction (a load ahead of the anchor is a shortcut, not an eraser).
    Returns ``(blocked_values, all_resolved)`` — an unresolved eraser poisons
    the whole road analysis (the caller fails closed).
    """
    blocked: set[Any] = set()
    all_resolved = True
    for component in getattr(cut, "components", ()) or ():
        anchor_value = anchor_snap.get(component.tag)
        for eraser in component.erasers:
            if eraser.init_only:
                continue
            if not eraser.resolved:
                all_resolved = False
                continue
            if eraser.channel_tag is not None and eraser.channel_tag != channel_tag:
                continue
            if (
                eraser.value is not None
                and isinstance(anchor_value, (int, float))
                and not isinstance(anchor_value, bool)
                and (eraser.value - anchor_value) * component.direction >= 0
            ):
                continue  # at-or-ahead of the anchor — not destroying anything
            blocked.update(eraser.enabling_channel_values)
    return frozenset(blocked), all_resolved


def _clean_road(
    graph: Any,
    start: Any,
    goals: tuple[Any, ...],
    blocked_values: frozenset[Any],
    discharged: set[tuple[str, Any, Any]],
) -> tuple[tuple[Any, ...], Any] | None:
    """BFS for a road to a goal avoiding eraser values and resurrected debt.

    An edge is unusable when its destination hosts an enabled eraser, or when
    its command action replicates a discharged ``(action, context)`` pair.
    Returns ``(road_values, reentry_value)`` or None.
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

    queue: deque[tuple[Any, tuple[Any, ...]]] = deque([(start, (start,))])
    visited = {_key(start)}
    while queue:
        value, path = queue.popleft()
        for edge in graph.edges:
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
) -> DepartureVerdict:
    """Classify the channel departure the work fork is currently paused in."""
    anchor_snap = dict(state.work.state.tags)
    fork, settle_scans = _settle_departure(state, channel_tag)
    settled_value = fork.state.tags.get(channel_tag)

    def _v(verdict: str, reason: str, reentry: Any = None, road: tuple = ()) -> DepartureVerdict:
        logger.debug(
            "detour: %s %r->%r (%d settle scans): %s — %s",
            channel_tag,
            from_value,
            settled_value,
            settle_scans,
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
            road=road,
        )

    # A departure whose loan already failed once is a known regression —
    # investigation gets its tight window on this fresh incident.
    signature = loan_signature(channel_tag, from_value, settled_value)
    if signature in state.failed_loans:
        return _v("regression", "a detour loan for this departure already failed")

    cut = getattr(state, "credential_cut", None)
    if cut is None or not getattr(cut, "components", ()):
        return _v("regression", "no progress credential — cannot prove a stopover")

    goals: list[Any] = [from_value]
    target_tag = getattr(ctx, "target_tag", None)
    target_value = getattr(ctx, "target_value", None)
    if target_tag == channel_tag and not any(_values_match(target_value, g) for g in goals):
        goals.append(target_value)

    blocked_values, all_resolved = _eraser_blocked_values(cut, anchor_snap, channel_tag)
    if not all_resolved:
        return _v("regression", "an unresolved credential eraser poisons the road analysis")

    discharged = _discharged_actions(state, channel_tag)
    graphs = getattr(getattr(ctx, "compass", None), "graphs", ()) or ()
    for graph in graphs:
        if graph.role.channel_tag != channel_tag:
            continue
        found = _clean_road(graph, settled_value, tuple(goals), blocked_values, discharged)
        if found is not None:
            road, reentry = found
            return _v(
                "stopover",
                f"clean forward road {' -> '.join(repr(v) for v in road)} "
                "(no eraser, no resurrected obligation)",
                reentry,
                road,
            )
        return _v(
            "regression",
            "every forward road crosses a credential eraser or resurrects a discharged obligation",
        )
    return _v("regression", "no transition structure for the channel")
