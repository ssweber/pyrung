"""Focused truth table for PILOT post-act outcome classification."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung.core.analysis.pilot.gauge import GaugeReading, GaugeReceipt
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    Outcome,
    ProgressEffect,
    assess_outcome,
    classify_outcome,
)
from pyrung.core.analysis.pilot.types import ChannelMotion


def _zoom(
    after: int,
    *,
    trend_before: int = 2,
    trend_after: int = 2,
    frontier: bool = False,
    credential: bool = False,
) -> Outcome:
    trial = SimpleNamespace(snap={"State": after})
    frame = SimpleNamespace(snap={"State": 11}, distance_before=trend_before)
    ctx = SimpleNamespace(opaque_loop=frozenset())
    return classify_outcome(
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
        gauge_receipt=GaugeReceipt((GaugeReading("Step", 0, 1, 1),) if credential else ()),
    )


def test_zoom_that_reaches_requested_channel_is_confirmed() -> None:
    assert _zoom(16) is Outcome.CONFIRMED


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
        gauge_receipt=GaugeReceipt((GaugeReading("Step", 0, 1, 1),)),
    )

    assert assessment.bearing is BearingEffect.SATISFIED
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
        gauge_receipt=GaugeReceipt(),
    )

    assert assessment.bearing is BearingEffect.DEPARTED


def test_zoom_that_really_departs_elsewhere_is_ambient_drift() -> None:
    assert _zoom(10) is Outcome.AMBIENT_DRIFT


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
        gauge_receipt=GaugeReceipt(),
    )

    assert assessment.agency is Agency.PROGRAM
    assert assessment.bearing is BearingEffect.DEPARTED
    assert assessment.progress is ProgressEffect.BEHIND
    assert assessment.new_frontier is True
    assert assessment.legacy_outcome is Outcome.AMBIENT_DRIFT


def test_zoom_timeout_with_unchanged_channel_is_rejected() -> None:
    assert _zoom(11) is Outcome.BAD_EDGE


def test_unchanged_channel_can_still_earn_progress_inside_corridor() -> None:
    assert _zoom(11, credential=True) is Outcome.CONFIRMED
    assert _zoom(11, frontier=True) is Outcome.FRONTIER


def test_unchanged_channel_trend_drop_alone_is_rejected() -> None:
    """Gauge-authoritative: trace-trend is coordinate-relative noise for a
    frozen channel (incidental sub-registers can drop the tree count while
    the channel sits stuck at its start value — the tumbler zoom
    false-confirm).  Only the gauge or a genuinely new frontier confirms."""
    assert _zoom(11, trend_after=1) is Outcome.BAD_EDGE
