"""VERIFY — who moved what?

The classification half of the PILOT loop's VERIFY phase (the gate pipeline is
``verify.py``).  Post-act outcome classification: after a candidate passes
the pre-act gates (SPIN, CYCLE, DEAD-END), this module decides which of the
four outcomes occurred:

  1. CONFIRMED     — I moved it where I wanted.
  2. BAD_EDGE      — I moved it wrong → correct the compass.
  3. AMBIENT_DRIFT — The PLC moved it wrong → learn both edges.
  4. FRONTIER      — Productive regression → new prereqs revealed.

The classifier replaces the old CAUSED-REGRESSION gate, which was too blunt:
it rejected *any* pilot-caused trend increase, including a route-prescribed
forward step that correctly enters a new corridor with more prerequisites.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pyrung.core.analysis.pilot.compass import CompassEntry, Provenance, TransitionCause
from pyrung.core.analysis.pilot.trace import _all_nodes
from pyrung.core.analysis.pilot.types import _ActionPair
from pyrung.core.analysis.sp_values import _values_match


class Outcome(Enum):
    """Which of the four verify outcomes occurred after a pilot action."""

    CONFIRMED = "confirmed"
    BAD_EDGE = "bad_edge"
    AMBIENT_DRIFT = "ambient"
    FRONTIER = "frontier"


def confirmed_entry(
    tag: str,
    from_val: Any,
    cause: TransitionCause,
    to_val: Any,
) -> CompassEntry:
    """Mint a CONFIRMED compass entry — the sole constructor of that provenance.

    Verify is the sole source of CONFIRMED: an entry earns it only when the
    outcome pipeline in this module has judged that *we* moved the register
    where we wanted (outcome #1, "I moved it where I wanted").  ``Compass.record``
    rejects the CONFIRMED provenance and ``Compass.commit_confirmed`` accepts
    only a prebuilt entry, so this factory — owned by the module that assigns
    ``Outcome.CONFIRMED`` — is structurally the only place CONFIRMED can be
    built.  Grep ``Provenance.CONFIRMED``: it appears only in the enum and here.
    """
    return CompassEntry(
        tag=tag,
        from_val=from_val,
        cause=cause,
        to_val=to_val,
        provenance=Provenance.CONFIRMED,
    )


# ---------------------------------------------------------------------------
# Compass frontier detection
# ---------------------------------------------------------------------------


def _has_compass_frontier(
    tree: Any,
    snap: dict[str, Any],
    opaque_loop: frozenset[str],
    compass: Any,
) -> bool:
    """True if the compass still has a route toward some unmet channel node.

    Asks the compass directly — does a route plan exist from ``snap`` to an
    unsatisfied ``opaque_loop`` node the tree still needs — rather than inferring
    it from a trace dead-end leaf.  A jump-table drive (``how(COMPLETED)``) reaches
    its goal entirely through the compass while trace legitimately dead-ends, so
    the dead-end gate must not stall it while a route genuinely remains.
    """
    if not opaque_loop or not compass.graphs:
        return False
    from pyrung.core.analysis.pilot.charts import best_compass_plan

    seen: set[tuple[str, Any]] = set()
    for n in _all_nodes(tree):
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        if n.tag not in opaque_loop or _values_match(snap.get(n.tag), n.value):
            continue
        key = (n.tag, n.value)
        if key in seen:
            continue
        seen.add(key)
        if best_compass_plan(n.tag, n.value, snap, compass.graphs) is not None:
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
    zoom_channel_tag: str | None = None,
    zoom_target_value: Any = None,
) -> Outcome:
    """Classify a post-gate trial into one of the five verify outcomes.

    Called after SPIN, CYCLE, and DEAD-END gates have passed — the trial
    produced a real state change with a non-empty frontier.

    The key distinction the old CAUSED-REGRESSION gate missed: a
    route-prescribed action that opens genuinely new frontier is FRONTIER
    (outcome #5), not BAD_EDGE.  The new prerequisites are the real work.
    """
    if zoom_channel_tag is not None:
        chan_actual = trial.snap.get(zoom_channel_tag)
        if not _values_match(chan_actual, zoom_target_value):
            return Outcome.AMBIENT_DRIFT
        # The zoom achieved its channel subgoal (e.g. S_StateCurrent 3->6).
        # That is a confirmed advance even when the *global* target's onward
        # leg is another self-advancing dwell (HeatDelay timer -> Heat steps)
        # that trace_back cannot surface yet.  Do not fall through to the
        # trend/BAD_EDGE logic, which would discard a correct 800-scan coast.
        return Outcome.CONFIRMED

    # Trend improved or flat → the action helped
    if new_trend <= frame.distance_before:
        return Outcome.CONFIRMED

    # Trend increased — who caused it?
    pilot_caused = _action_caused_regression(trial, action_pairs, frame, ctx, chase_cause_roots)

    if not pilot_caused:
        # The PLC caused the regression — the command was a no-op, the program
        # has its own current.  (Stub: for now we accept; full "learn both" is future work.)
        return Outcome.AMBIENT_DRIFT

    # Pilot caused regression — but is it productive?
    if route_prescribed and has_new_frontier:
        # The route says go here, and the move opened genuinely new actions.
        # This is "revealed new prerequisites" — accept the forward step.
        return Outcome.FRONTIER

    # Pilot-caused regression with no new frontier → destructive self-move
    return Outcome.BAD_EDGE
