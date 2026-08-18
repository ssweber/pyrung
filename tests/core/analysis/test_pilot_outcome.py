"""Focused truth table for PILOT post-act outcome classification."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung.core.analysis.pilot.earned_work import EarnedWorkReading, EarnedWorkReceipt
from pyrung.core.analysis.pilot.execution import ChannelMotion
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    ProgressEffect,
    TrialAssessment,
    assess_outcome,
)


def _bearing_coast(
    after: int,
    *,
    trend_before: int = 2,
    trend_after: int = 2,
    frontier: bool = False,
    credential: bool = False,
) -> TrialAssessment:
    trial = SimpleNamespace(snap={"State": after})
    frame = SimpleNamespace(snap={"State": 11}, distance_before=trend_before)
    ctx = SimpleNamespace(opaque_loop=frozenset())
    return assess_outcome(
        trial,
        (),
        frame,
        ctx,
        trend_after,
        frontier,
        lambda *_args, **_kwargs: (set(), []),
        route_prescribed=True,
        channel_motion=ChannelMotion(
            "State",
            16,
            stop_reason="reached" if after == 16 else "timeout" if after == 11 else "departed",
        ),
        earned_work_receipt=EarnedWorkReceipt(
            (EarnedWorkReading("Step", 0, 1, 1),) if credential else ()
        ),
    )


def test_bearing_coast_that_reaches_requested_channel_is_accepted() -> None:
    assessment = _bearing_coast(16)
    assert assessment.bearing is BearingEffect.SATISFIED
    assert assessment.accepted is True


def test_action_receipt_survives_later_program_departure() -> None:
    """Settlement cannot erase an action's observed operation boundary."""
    trial = SimpleNamespace(
        snap={"State": 11},
        timeline=(SimpleNamespace(transitions=(("State", 11, 16), ("State", 16, 11))),),
    )
    frame = SimpleNamespace(snap={"State": 11}, distance_before=2)
    ctx = SimpleNamespace(opaque_loop=frozenset())

    assessment = assess_outcome(
        trial,
        (("Resume", True),),
        frame,
        ctx,
        2,
        False,
        lambda *_args, **_kwargs: (set(), []),
        route_prescribed=False,
        channel_motion=ChannelMotion("State", 16, stop_reason="reached"),
        earned_work_receipt=EarnedWorkReceipt((EarnedWorkReading("Step", 0, 1, 1),)),
    )

    assert assessment.bearing is BearingEffect.SATISFIED
    assert assessment.agency is Agency.UNKNOWN
    assert assessment.accepted is True


def test_action_receipt_does_not_hide_a_different_landing() -> None:
    trial = SimpleNamespace(
        snap={"State": 9},
        timeline=(SimpleNamespace(transitions=(("State", 11, 16), ("State", 16, 9))),),
    )
    frame = SimpleNamespace(snap={"State": 11}, distance_before=2)
    ctx = SimpleNamespace(opaque_loop=frozenset())

    assessment = assess_outcome(
        trial,
        (("Resume", True),),
        frame,
        ctx,
        2,
        False,
        lambda *_args, **_kwargs: (set(), []),
        route_prescribed=False,
        channel_motion=ChannelMotion("State", 16, stop_reason="departed"),
        earned_work_receipt=EarnedWorkReceipt(),
    )

    assert assessment.bearing is BearingEffect.DEPARTED


def test_bearing_coast_that_really_departs_elsewhere_is_program_motion() -> None:
    assessment = _bearing_coast(10)
    assert assessment.agency is Agency.PROGRAM
    assert assessment.bearing is BearingEffect.DEPARTED
    assert assessment.accepted is True


def test_only_the_immediate_requested_value_satisfies_the_bearing() -> None:
    trial = SimpleNamespace(snap={"State": 3})
    frame = SimpleNamespace(snap={"State": 4}, distance_before=2)
    ctx = SimpleNamespace(opaque_loop=frozenset())

    assessment = assess_outcome(
        trial,
        (("Start", True),),
        frame,
        ctx,
        15,
        True,
        lambda *_args, **_kwargs: (set(), []),
        route_prescribed=True,
        channel_motion=ChannelMotion("State", 6, stop_reason="departed"),
        earned_work_receipt=EarnedWorkReceipt(),
    )

    assert assessment.agency is Agency.UNKNOWN
    assert assessment.bearing is BearingEffect.DEPARTED
    assert assessment.progress is ProgressEffect.BACKWARD
    assert assessment.new_frontier is True
    assert assessment.accepted is True


def test_second_applied_action_can_supply_positive_causal_attribution() -> None:
    trial = SimpleNamespace(
        fork=object(),
        action_scan=12,
        snap={"State": 3},
    )
    frame = SimpleNamespace(snap={"State": 4}, distance_before=2)
    ctx = SimpleNamespace(
        opaque_loop=frozenset({"State"}),
        steerable=frozenset({"Primary", "Support"}),
    )
    calls = []

    def roots(fork, tag, steerable, *, scan):
        calls.append((fork, tag, steerable, scan))
        return {"Support"}, []

    assessment = assess_outcome(
        trial,
        (("Primary", True), ("Support", True)),
        frame,
        ctx,
        3,
        True,
        roots,
        route_prescribed=True,
        channel_motion=ChannelMotion(),
        earned_work_receipt=EarnedWorkReceipt(),
    )

    assert assessment.agency is Agency.PILOT
    assert assessment.bearing is BearingEffect.EXPOSED
    assert assessment.progress is ProgressEffect.BACKWARD
    assert assessment.new_frontier is True
    assert assessment.accepted is True
    assert calls == [(trial.fork, "State", ctx.steerable, trial.action_scan)]


def test_unattributed_nonempty_regression_fails_closed() -> None:
    trial = SimpleNamespace(
        fork=object(),
        action_scan=12,
        snap={"State": 3},
    )
    frame = SimpleNamespace(snap={"State": 4}, distance_before=2)
    ctx = SimpleNamespace(
        opaque_loop=frozenset({"State"}),
        steerable=frozenset({"Primary", "Support"}),
    )

    assessment = assess_outcome(
        trial,
        (("Primary", True), ("Support", True)),
        frame,
        ctx,
        3,
        True,
        lambda *_args, **_kwargs: (set(), []),
        route_prescribed=True,
        channel_motion=ChannelMotion(),
        earned_work_receipt=EarnedWorkReceipt(),
    )

    assert assessment.agency is Agency.UNKNOWN
    assert assessment.bearing is BearingEffect.DEPARTED
    assert assessment.progress is ProgressEffect.BACKWARD
    assert assessment.new_frontier is True
    assert assessment.accepted is False


def test_empty_artifact_preserves_ambient_program_regression() -> None:
    trial = SimpleNamespace(snap={"State": 3})
    frame = SimpleNamespace(snap={"State": 4}, distance_before=2)
    ctx = SimpleNamespace(opaque_loop=frozenset({"State"}))

    assessment = assess_outcome(
        trial,
        (),
        frame,
        ctx,
        3,
        False,
        lambda *_args, **_kwargs: pytest.fail("empty work must not chase causes"),
        route_prescribed=False,
        channel_motion=ChannelMotion(),
        earned_work_receipt=EarnedWorkReceipt(),
    )

    assert assessment.agency is Agency.PROGRAM
    assert assessment.bearing is BearingEffect.DEPARTED
    assert assessment.progress is ProgressEffect.BACKWARD
    assert assessment.accepted is True


def test_bearing_coast_timeout_with_unchanged_channel_is_rejected() -> None:
    assessment = _bearing_coast(11)
    assert assessment.bearing is BearingEffect.UNCHANGED
    assert assessment.accepted is False


def test_unchanged_channel_can_still_earn_progress_inside_corridor() -> None:
    earned = _bearing_coast(11, credential=True)
    assert earned.bearing is BearingEffect.UNCHANGED
    assert earned.accepted is True

    exposed = _bearing_coast(11, frontier=True)
    assert exposed.bearing is BearingEffect.EXPOSED
    assert exposed.new_frontier is True
    assert exposed.accepted is True


def test_unchanged_channel_trend_drop_alone_is_rejected() -> None:
    """Earned-work-authoritative: trace-trend is coordinate-relative noise for a
    frozen channel (incidental sub-registers can drop the tree count while
    the channel sits stuck at its start value — the tumbler bearing-coast
    false-confirm).  Only earned work or a genuinely new frontier confirms."""
    assessment = _bearing_coast(11, trend_after=1)
    assert assessment.bearing is BearingEffect.UNCHANGED
    assert assessment.progress is ProgressEffect.FORWARD
    assert assessment.accepted is False
