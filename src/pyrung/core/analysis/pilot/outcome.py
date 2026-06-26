"""Verify — who moved what?

Post-act outcome classification for the PILOT loop.  After a candidate passes
the pre-act gates (SPIN, CYCLE, DEAD-END), this module decides which of the
five outcomes occurred:

  1. CONFIRMED     — I moved it where I wanted.
  2. AUTO_EDGE     — The PLC moved it where I wanted.
  3. BAD_EDGE      — I moved it wrong → correct the compass.
  4. AMBIENT_DRIFT — The PLC moved it wrong → learn both edges.
  5. FRONTIER      — Productive regression → new prereqs revealed.

The classifier replaces the old CAUSED-REGRESSION gate, which was too blunt:
it rejected *any* pilot-caused trend increase, including a route-prescribed
forward step that correctly enters a new corridor with more prerequisites.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pyrung.core.analysis.pilot.trace import _all_nodes
from pyrung.core.analysis.pilot.types import _ActionPair
from pyrung.core.analysis.sp_values import _values_match


class Outcome(Enum):
    """Which of the five verify outcomes occurred after a pilot action."""

    CONFIRMED = "confirmed"
    AUTO_EDGE = "auto_edge"
    BAD_EDGE = "bad_edge"
    AMBIENT_DRIFT = "ambient"
    FRONTIER = "frontier"


# ---------------------------------------------------------------------------
# Compass frontier detection
# ---------------------------------------------------------------------------


def _has_compass_frontier(
    tree: Any,
    snap: dict[str, Any],
    opaque_loop: frozenset[str],
) -> bool:
    """True if *tree* has a dead-end leaf that influence mapping can probe."""
    if not opaque_loop:
        return False
    for n in _all_nodes(tree):
        if n.children or n.satisfied or n.is_steerable:
            continue
        if getattr(n, "pipeline_internal", False):
            continue
        if n.tag in opaque_loop and not _values_match(snap.get(n.tag), n.value):
            return True
    return False


# ---------------------------------------------------------------------------
# Causal attribution — did the pilot or the program cause a change?
# ---------------------------------------------------------------------------


def _action_caused_regression(
    trial: Any,
    action_pairs: tuple[_ActionPair, ...],
    frame: Any,
    ctx: Any,
    chase_cause_roots: Any,
) -> bool:
    """True if a pulsed action causally drove an opaque-loop register backward.

    A trend regression the pilot's own control input produced (C_Abort driving
    S_StateCurrent to Aborted) is a self-inflicted misstep — distinct from an
    ambient regression (an alarm firing on its own).  The pilot should not
    commit to its own bad control input; ambient drift is handled elsewhere.
    """
    action_tags = {t for t, _ in action_pairs}
    for tag in ctx.opaque_loop:
        if _values_match(frame.snap.get(tag), trial.snap.get(tag)):
            continue
        roots, _holds = chase_cause_roots(trial.fork, tag, ctx.steerable, scan=trial.action_scan)
        if roots & action_tags:
            return True
    return False


# ---------------------------------------------------------------------------
# Outcome classifier
# ---------------------------------------------------------------------------


def classify_outcome(
    trial: Any,
    action_pairs: tuple[_ActionPair, ...],
    frame: Any,
    ctx: Any,
    new_trend: int,
    has_new_frontier: bool,
    chase_cause_roots: Any,
    *,
    route_prescribed: bool,
) -> Outcome:
    """Classify a post-gate trial into one of the five verify outcomes.

    Called after SPIN, CYCLE, and DEAD-END gates have passed — the trial
    produced a real state change with a non-empty frontier.

    The key distinction the old CAUSED-REGRESSION gate missed: a
    route-prescribed action that opens genuinely new frontier is FRONTIER
    (outcome #5), not BAD_EDGE.  The new prerequisites are the real work.
    """
    # Trend improved or flat → the action helped
    if new_trend <= frame.distance_before:
        return Outcome.CONFIRMED

    # Trend increased — who caused it?
    pilot_caused = _action_caused_regression(trial, action_pairs, frame, ctx, chase_cause_roots)

    if not pilot_caused:
        # The PLC caused the regression — the command was a no-op, the program
        # has its own agenda.  (Stub: for now we accept; full "learn both" is future work.)
        return Outcome.AMBIENT_DRIFT

    # Pilot caused regression — but is it productive?
    if route_prescribed and has_new_frontier:
        # The route says go here, and the move opened genuinely new actions.
        # This is "revealed new prerequisites" — accept the forward step.
        return Outcome.FRONTIER

    # Pilot-caused regression with no new frontier → destructive self-move
    return Outcome.BAD_EDGE
