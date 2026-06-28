"""Tests for pilot investigation — hypothesis generation and bounded replay.

Coverage targets:
- build_replay_fn: bounded vs unbounded judgment
- investigate_deviation: hypothesis generation pipeline
- _cause_hypotheses, _latch_exposure_hypotheses, _liveness_hypotheses
- investigate_excursion: excursion diagnosis and retry
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import Bool, Program, Rung, Timer, on_delay, out
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.investigate import build_replay_fn
from pyrung.core.analysis.pilot.trace import compute_steerable
from pyrung.core.analysis.pilot.types import _Step
from pyrung.core.runner import PLC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_replay_context(prog: Program, plc: PLC, target_tag: str, target_value: Any):
    """Build the minimal keyword context for build_replay_fn."""
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)
    return {
        "resting": {t: False for t in steerable if isinstance(plc.state.tags.get(t), bool)},
        "edge_tags": set(),
        "target_tag": target_tag,
        "target_value": target_value,
        "pdg": pdg,
        "program": prog,
        "steerable": steerable,
        "opaque_loop": frozenset(),
        "pipeline_internal_tags": frozenset(),
        "choice": None,
    }


# ---------------------------------------------------------------------------
# Bounded replay — departure_scan / departure_bearing
# ---------------------------------------------------------------------------


def _watchdog_program() -> tuple[Program, Timer]:
    """Timer acts as a watchdog: Enable stays True, timer fires, Alarm goes True.

    Hold = True blocks the alarm output.  Use this to test bounded replay:
    without the hold, the bearing (Alarm=False) departs at the timer preset.
    """
    Enable = Bool("Enable", external=True)
    Hold = Bool("Hold", external=True)
    Tmr = Timer.clone("Tmr")
    Alarm = Bool("Alarm")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done, ~Hold):
            out(Alarm)
        with Rung(Enable, ~Alarm):
            out(Target)

    return prog, Tmr


class TestBoundedReplay:
    """build_replay_fn with departure_scan/departure_bearing bounds the coast
    and judges by bearing rather than target-reached."""

    def _setup(self):
        prog, tmr = _watchdog_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        cp = plc.fork()

        # Coast until the alarm fires
        for _ in range(20):
            plc.step()
        assert plc.state.tags["Alarm"] is True

        # Find the departure scan (when Alarm went True)
        departure_scan = None
        for scan in range(cp.state.scan_id, plc.state.scan_id + 1):
            s = plc.history.at(scan)
            if s.tags.get("Alarm") is True:
                departure_scan = scan
                break
        assert departure_scan is not None

        ctx = _make_replay_context(prog, plc, "Target", True)
        cp_trend = 1
        steps = [_Step(action={}, scan_before=cp.state.scan_id, scan_after=plc.state.scan_id)]
        return prog, plc, cp, cp_trend, steps, departure_scan, ctx

    def test_bounded_accepts_good_hold(self):
        """A hold that prevents the departure is accepted under bounded replay."""
        _prog, _plc, cp, cp_trend, steps, dep_scan, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            **ctx,
            departure_scan=dep_scan,
            departure_bearing=(("Alarm", False),),
        )
        outcome = replay((("Hold", True),))
        assert outcome.accepted
        assert "held" in outcome.reason

    def test_bounded_rejects_bad_hold(self):
        """A no-op hold that doesn't prevent the departure is rejected."""
        _prog, _plc, cp, cp_trend, steps, dep_scan, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            **ctx,
            departure_scan=dep_scan,
            departure_bearing=(("Alarm", False),),
        )
        outcome = replay(())
        assert not outcome.accepted
        assert "departed" in outcome.reason

    def test_unbounded_falls_through_to_trend_judgment(self):
        """Without departure info, replay uses the trace-back trend judgment."""
        _prog, _plc, cp, cp_trend, steps, _dep_scan, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            **ctx,
        )
        outcome = replay((("Hold", True),))
        assert "trend" in outcome.reason


# ---------------------------------------------------------------------------
# Hypothesis generation (stubs — fill in as cause()-based detection lands)
# ---------------------------------------------------------------------------


class TestCauseHypotheses:
    """_cause_hypotheses: recorded cause names transitioning steerable roots."""

    @pytest.mark.skip(reason="stub — needs cause()-based detection refactor")
    def test_single_departure_produces_hold(self):
        ...

    @pytest.mark.skip(reason="stub — needs cause()-based detection refactor")
    def test_non_steerable_departure_skipped(self):
        ...


class TestLatchExposureHypotheses:
    """_latch_exposure_hypotheses: alarm latches fired on state entry."""

    @pytest.mark.skip(reason="stub")
    def test_latch_guard_resolved_to_steerable(self):
        ...

    @pytest.mark.skip(reason="stub")
    def test_conjunction_proposed_when_multiple_latches(self):
        ...


class TestLivenessHypotheses:
    """_liveness_hypotheses: watchdog-driven oscillation holds."""

    @pytest.mark.skip(reason="stub — will change with incremental dwell learning")
    def test_complement_reset_watchdog_produces_liveness_hold(self):
        ...

    @pytest.mark.skip(reason="stub — will change with incremental dwell learning")
    def test_dwell_respects_shortest_preset(self):
        ...

    @pytest.mark.skip(reason="stub — will change with incremental dwell learning")
    def test_only_fired_watchdogs_proposed(self):
        ...


class TestInvestigateExcursion:
    """investigate_excursion: state-key excursion diagnosis and hold-based retry."""

    @pytest.mark.skip(reason="stub")
    def test_reverted_tags_diagnosed(self):
        ...

    @pytest.mark.skip(reason="stub")
    def test_confirmed_holds_fix_revert(self):
        ...
