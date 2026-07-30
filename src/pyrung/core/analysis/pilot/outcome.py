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
from pyrung.core.analysis.pilot.earned_work import EarnedWorkReceipt
from pyrung.core.analysis.pilot.types import ChannelMotion, _ActionPair
from pyrung.core.analysis.sp_values import _values_match


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
    earned-work receipt establish for this trial.
    """

    FORWARD = "forward"
    UNCHANGED = "unchanged"
    BACKWARD = "backward"


@dataclass(frozen=True)
class TrialAssessment:
    """Orthogonal evidence returned by trial verification."""

    agency: Agency
    bearing: BearingEffect
    progress: ProgressEffect
    new_frontier: bool
    accepted: bool


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


def _motion_agency(
    trial: Any,
    applied_actions: tuple[_ActionPair, ...],
    frame: Any,
    ctx: Any,
    chase_cause_roots: Any,
) -> Agency:
    """Attribute relevant motion only from positive causal evidence.

    An empty physical artifact is program motion. Once PILOT applied anything,
    however, absence of a matching causal root is uncertainty rather than proof
    that the program owned the motion.
    """
    if not applied_actions:
        return Agency.PROGRAM

    action_tags = {tag for tag, _ in applied_actions}
    for tag in ctx.opaque_loop:
        if _values_match(frame.snap.get(tag), trial.snap.get(tag)):
            continue
        roots, _holds = chase_cause_roots(trial.fork, tag, ctx.steerable, scan=trial.action_scan)
        if roots & action_tags:
            return Agency.PILOT
    return Agency.UNKNOWN


# ---------------------------------------------------------------------------
# Trial assessment
# ---------------------------------------------------------------------------


def assess_outcome(
    trial: Any,
    applied_actions: tuple[_ActionPair, ...],
    frame: Any,
    ctx: Any,
    new_trend: int,
    has_new_frontier: bool,
    chase_cause_roots: Any,
    *,
    route_prescribed: bool,
    channel_motion: ChannelMotion,
    earned_work_receipt: EarnedWorkReceipt,
) -> TrialAssessment:
    """Judge a post-gate trial on independent evidence axes.

    Called after SPIN, CYCLE, and DEAD-END gates have passed — the trial
    produced a real state change, a non-empty frontier, or an owned channel
    landing that must reach post-commit handling.

    Only the *immediate* requested channel value can satisfy a bearing.  A
    stored route suffix is intent, not evidence: landing on a later or earlier
    chart value is a departure and post-commit progress handling decides what
    that observed world means for target-relative progress.
    """
    if (
        channel_motion.active and earned_work_receipt.any_forward
    ) or new_trend < frame.distance_before:
        progress = ProgressEffect.FORWARD
    elif new_trend == frame.distance_before:
        progress = ProgressEffect.UNCHANGED
    else:
        progress = ProgressEffect.BACKWARD

    agency = _motion_agency(
        trial,
        applied_actions,
        frame,
        ctx,
        chase_cause_roots,
    )

    if channel_motion.active:
        if channel_motion.reached:
            # The bearing coast achieved its channel subgoal (e.g. State 3->6).
            # That is a confirmed advance even when the *global* target's onward
            # leg is another self-advancing dwell (HeatDelay timer -> Heat steps)
            # that trace_back cannot surface yet.  Do not fall through to the
            # trend/BAD_EDGE logic, which would discard a correct 800-scan coast.
            return TrialAssessment(
                agency,
                BearingEffect.SATISFIED,
                progress,
                has_new_frontier,
                True,
            )
        if channel_motion.departed:
            # The channel moved, but not to the requested value.  Attribute the
            # move independently from its usefulness; post-commit handling may later prove the
            # resulting world advanced, regressed, or remains incomparable.
            return TrialAssessment(
                agency,
                BearingEffect.DEPARTED,
                progress,
                has_new_frontier,
                True,
            )

        # The channel did not move.  That is not ambient drift.  Accept only
        # evidence of useful work during the motion: an event-earned
        # credential (earned work) or genuinely new prerequisites.  Otherwise
        # this was a sterile timeout and must be rejected. ``actual !=
        # requested`` alone is not enough to commit a frozen-channel lap.
        #
        # Earned-work-authoritative: trace-trend is a coordinate-relative count that
        # legitimately drops when the surrounding world shifts, so a frozen
        # channel must never be confirmed off an incidental trend drop — only
        # the earned-work receipt proves earned work here. The honest
        # rejection is what frees the escalation ladder (terminal let-run,
        # skiff) to earn the holds this coast actually needs.
        if earned_work_receipt.any_forward:
            return TrialAssessment(
                agency,
                BearingEffect.UNCHANGED,
                progress,
                has_new_frontier,
                True,
            )
        if has_new_frontier:
            return TrialAssessment(
                agency,
                BearingEffect.EXPOSED,
                progress,
                True,
                True,
            )
        return TrialAssessment(
            agency,
            BearingEffect.UNCHANGED,
            progress,
            False,
            False,
        )

    # Trend improved or flat is useful even when its agency remains unknown.
    if new_trend <= frame.distance_before:
        return TrialAssessment(
            agency,
            BearingEffect.SATISFIED,
            progress,
            has_new_frontier,
            True,
        )

    # Empty applied work establishes ambient program motion. A non-empty
    # artifact without a matching causal root is unresolved and fails closed.
    if agency is Agency.PROGRAM:
        return TrialAssessment(
            Agency.PROGRAM,
            BearingEffect.DEPARTED,
            progress,
            has_new_frontier,
            True,
        )

    # A causally attributed PILOT regression may still be productive when the
    # prescribed route exposed genuinely new work.
    if agency is Agency.PILOT and route_prescribed and has_new_frontier:
        # The route says go here, and the move opened genuinely new actions.
        # This is "revealed new prerequisites" — accept the forward step.
        return TrialAssessment(
            Agency.PILOT,
            BearingEffect.EXPOSED,
            progress,
            True,
            True,
        )

    # PILOT-caused regression without the route exception, or unresolved
    # non-empty work, is not safe to commit.
    return TrialAssessment(
        agency,
        BearingEffect.DEPARTED,
        progress,
        has_new_frontier,
        False,
    )
