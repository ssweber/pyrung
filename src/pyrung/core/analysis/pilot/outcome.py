"""Classify the evidence produced by one executed trial.

``assess_outcome`` attributes relevant motion to the pilot, program, or an
unknown source and records bearing, target-progress, and frontier effects in a
``TrialAssessment``. ``confirmed_entry`` constructs the only transition entry
eligible for CONFIRMED provenance.

This module classifies observations. ``verify.py`` applies the trial gates,
``pilot.py`` commits accepted worlds, and ``progress.py`` may later retain or
revert them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pyrung.core.analysis.pilot.compass import CompassEntry, Provenance, TransitionCause
from pyrung.core.analysis.pilot.types import ChannelMotion, _ActionPair
from pyrung.core.analysis.sp_values import _values_match


class Outcome(Enum):
    """Which of the four verify outcomes occurred after a pilot action."""

    CONFIRMED = "confirmed"
    BAD_EDGE = "bad_edge"
    AMBIENT_DRIFT = "ambient"
    FRONTIER = "frontier"


class Agency(Enum):
    """Best observed attribution for the trial's relevant motion."""

    PILOT = "pilot"
    PROGRAM = "program"
    UNKNOWN = "unknown"


class BearingEffect(Enum):
    """What the resulting world says about the immediate requested bearing."""

    SATISFIED = "satisfied"
    DEPARTED = "departed"
    UNCHANGED = "unchanged"
    EXPOSED = "exposed"


class ProgressEffect(Enum):
    """Target-relative evidence visible inside this one trial.

    ``progress.py`` owns checkpoint-relative promotion and regression. This
    value is narrower: it records only what the before/after target trace and
    progress gauge establish for this trial.
    """

    ADVANCED = "advanced"
    PRESERVED = "preserved"
    BEHIND = "behind"


@dataclass(frozen=True)
class TrialAssessment:
    """Orthogonal evidence returned by trial verification.

    ``Outcome`` is a compatibility projection. Policy should read these axes
    rather than infer semantics from the execution mode's label.
    """

    agency: Agency
    bearing: BearingEffect
    progress: ProgressEffect
    new_frontier: bool
    accepted: bool

    @property
    def legacy_outcome(self) -> Outcome:
        if not self.accepted:
            return Outcome.BAD_EDGE
        if self.bearing is BearingEffect.DEPARTED:
            return Outcome.AMBIENT_DRIFT
        if self.bearing is BearingEffect.EXPOSED:
            return Outcome.FRONTIER
        return Outcome.CONFIRMED


def confirmed_entry(
    tag: str,
    from_val: Any,
    cause: TransitionCause,
    to_val: Any,
) -> CompassEntry:
    """Build a CONFIRMED entry after causal attribution supports the action.

    Applying the returned evidence accepts only a prebuilt confirmed entry,
    making this the structural confirmation boundary.
    """
    return CompassEntry(
        tag=tag,
        from_val=from_val,
        cause=cause,
        to_val=to_val,
        provenance=Provenance.CONFIRMED,
    )


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


def assess_outcome(
    trial: Any,
    action_pairs: tuple[_ActionPair, ...],
    frame: Any,
    ctx: Any,
    new_trend: int,
    has_new_frontier: bool,
    chase_cause_roots: Any,
    *,
    route_prescribed: bool,
    channel_motion: ChannelMotion,
    channel_progressed: bool,
) -> TrialAssessment:
    """Judge a post-gate trial on independent evidence axes.

    Called after SPIN, CYCLE, and DEAD-END gates have passed — the trial
    produced a real state change with a non-empty frontier.

    Only the *immediate* requested channel value can satisfy a bearing.  A
    stored route suffix is intent, not evidence: landing on a later or earlier
    chart value is a departure and post-commit progress handling decides what
    that observed world means for target-relative progress.
    """
    if (channel_motion.active and channel_progressed) or new_trend < frame.distance_before:
        progress = ProgressEffect.ADVANCED
    elif new_trend == frame.distance_before:
        progress = ProgressEffect.PRESERVED
    else:
        progress = ProgressEffect.BEHIND

    if channel_motion.active:
        if channel_motion.reached:
            # The zoom achieved its channel subgoal (e.g. S_StateCurrent 3->6).
            # That is a confirmed advance even when the *global* target's onward
            # leg is another self-advancing dwell (HeatDelay timer -> Heat steps)
            # that trace_back cannot surface yet.  Do not fall through to the
            # trend/BAD_EDGE logic, which would discard a correct 800-scan coast.
            return TrialAssessment(
                Agency.PILOT if action_pairs else Agency.PROGRAM,
                BearingEffect.SATISFIED,
                progress,
                has_new_frontier,
                True,
            )
        if channel_motion.departed:
            # The channel moved, but not to the requested value.  Attribute the
            # move independently from its usefulness; post-commit handling may later prove the
            # resulting world advanced, regressed, or remains incomparable.
            pilot_caused = bool(action_pairs) and _action_caused_regression(
                trial, action_pairs, frame, ctx, chase_cause_roots
            )
            return TrialAssessment(
                Agency.PILOT if pilot_caused else Agency.PROGRAM,
                BearingEffect.DEPARTED,
                progress,
                has_new_frontier,
                True,
            )

        # The channel did not move.  That is not ambient drift.  Accept only
        # evidence of useful work during the motion: an event-earned
        # credential (the gauge) or genuinely new prerequisites.  Otherwise
        # this was a sterile timeout and must be rejected; treating
        # ``actual != requested`` alone as drift used to commit 10k-scan HELD
        # laps forever.
        #
        # Gauge-authoritative: trace-trend is a coordinate-relative count that
        # legitimately drops when the surrounding world shifts, so a frozen
        # channel must never be confirmed off an incidental trend drop — only
        # the gauge (``channel_progressed``) proves earned work here. The honest
        # rejection is what frees the escalation ladder (terminal let-run,
        # skiff) to earn the holds this coast actually needs.
        if channel_progressed:
            return TrialAssessment(
                Agency.PROGRAM,
                BearingEffect.UNCHANGED,
                progress,
                has_new_frontier,
                True,
            )
        if has_new_frontier:
            return TrialAssessment(
                Agency.PROGRAM,
                BearingEffect.EXPOSED,
                progress,
                True,
                True,
            )
        return TrialAssessment(
            Agency.PROGRAM,
            BearingEffect.UNCHANGED,
            progress,
            False,
            False,
        )

    # Trend improved or flat → the action helped
    if new_trend <= frame.distance_before:
        return TrialAssessment(
            Agency.PILOT if action_pairs else Agency.PROGRAM,
            BearingEffect.SATISFIED,
            progress,
            has_new_frontier,
            True,
        )

    # Trend increased — who caused it?
    pilot_caused = _action_caused_regression(trial, action_pairs, frame, ctx, chase_cause_roots)

    if not pilot_caused:
        # The PLC caused the regression — the command was a no-op, the program
        # has its own current.  (Stub: for now we accept; full "learn both" is future work.)
        return TrialAssessment(
            Agency.PROGRAM,
            BearingEffect.DEPARTED,
            progress,
            has_new_frontier,
            True,
        )

    # Pilot caused regression — but is it productive?
    if route_prescribed and has_new_frontier:
        # The route says go here, and the move opened genuinely new actions.
        # This is "revealed new prerequisites" — accept the forward step.
        return TrialAssessment(
            Agency.PILOT,
            BearingEffect.EXPOSED,
            progress,
            True,
            True,
        )

    # Pilot-caused regression with no new frontier → destructive self-move
    return TrialAssessment(
        Agency.PILOT,
        BearingEffect.DEPARTED,
        progress,
        has_new_frontier,
        False,
    )


def classify_outcome(*args: Any, **kwargs: Any) -> Outcome:
    """Compatibility projection for focused callers and external probes."""
    return assess_outcome(*args, **kwargs).legacy_outcome
