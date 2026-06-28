"""Tests for pilot investigation — hypothesis generation and bounded replay.

Coverage targets:
- build_replay_fn: bounded vs unbounded judgment
- investigate_deviation: hypothesis generation pipeline
- _cause_hypotheses, _latch_exposure_hypotheses, _liveness_hypotheses
- investigate_excursion: excursion diagnosis and retry
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import And, Bool, Or, Program, Rung, Timer, latch, on_delay, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import (
    LivenessHold,
    _pilot_state_key,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.investigate import (
    DeviationIncident,
    _changed_tags_in_window,
    _dedupe_pairs,
    _first_departure_scan,
    _hold_allowed,
    _latch_exposure_hypotheses,
    _liveness_hypotheses,
    build_deviation_incident,
    build_replay_fn,
    investigate_excursion,
)
from pyrung.core.analysis.pilot.trace import compute_steerable
from pyrung.core.analysis.pilot.types import _Step
from pyrung.core.runner import PLC


def _make_ctx(prog: Program, plc: PLC, **overrides: Any) -> SimpleNamespace:
    """Minimal duck-typed context for the hypothesis generators.

    The generators read ``pdg``, ``program``, ``steerable``, ``opaque_loop``,
    ``pipeline_internal_tags``, ``choice`` and ``compass.action_tags`` off the
    context via ``getattr`` — a SimpleNamespace satisfies all of them.
    """
    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, plc._known_tags_by_name, prog))
    ns: dict[str, Any] = {
        "pdg": pdg,
        "program": prog,
        "steerable": steerable,
        "opaque_loop": frozenset(),
        "pipeline_internal_tags": frozenset(),
        "choice": None,
        "compass": SimpleNamespace(action_tags=frozenset()),
    }
    ns.update(overrides)
    return SimpleNamespace(**ns)


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
# _cause_hypotheses — recorded cause names transitioning steerable roots
# ---------------------------------------------------------------------------


class TestCauseHypotheses:
    """_cause_hypotheses: recorded cause names transitioning steerable roots."""

    @pytest.mark.skip(reason="stub — needs cause()-based detection refactor")
    def test_single_departure_produces_hold(self): ...

    @pytest.mark.skip(reason="stub — needs cause()-based detection refactor")
    def test_non_steerable_departure_skipped(self): ...


# ---------------------------------------------------------------------------
# _latch_exposure_hypotheses — alarm latches that fired on state entry
# ---------------------------------------------------------------------------


class TestLatchExposureHypotheses:
    """_latch_exposure_hypotheses: alarm latches fired on state entry.

    A latch active after the move and gated by a state we were already in
    latched *because* of the move.  Its non-state guards are preconditions we
    failed to establish — flip each to the value that breaks the latch and
    resolve it to its steerable driver.
    """

    def test_latch_guard_resolved_to_steerable(self):
        Enter = Bool("Enter", external=True)
        Guard = Bool("Guard", external=True)
        State = Bool("State")
        Alarm = Bool("Alarm")
        with Program() as prog:
            with Rung(Enter):
                out(State)
            with Rung(State, ~Guard):
                latch(Alarm)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(("Enter", True),),
            bearing=(("Alarm", False),),
            before_snap={"State": True, "Guard": False},
            after_snap={"State": True, "Guard": False, "Alarm": True},
            changed_tags=("Alarm",),
            departures=(),
        )

        hyps = _latch_exposure_hypotheses(plc, incident, ctx)
        # The latch's non-state guard (Guard=False) flips to True to break it.
        assert len(hyps) == 1
        assert hyps[0].kind == "latch-exposure"
        assert hyps[0].holds == (("Guard", True),)
        assert "Alarm" in hyps[0].sources

    def test_conjunction_proposed_when_multiple_latches(self):
        Enter = Bool("Enter", external=True)
        G1 = Bool("G1", external=True)
        G2 = Bool("G2", external=True)
        State = Bool("State")
        A1 = Bool("A1")
        A2 = Bool("A2")
        with Program() as prog:
            with Rung(Enter):
                out(State)
            with Rung(State, ~G1):
                latch(A1)
            with Rung(State, ~G2):
                latch(A2)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc, opaque_loop=frozenset({"State"}))
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(("Enter", True),),
            bearing=(("A1", False), ("A2", False)),
            before_snap={"State": True, "G1": False, "G2": False},
            after_snap={"State": True, "G1": False, "G2": False, "A1": True, "A2": True},
            changed_tags=("A1", "A2"),
            departures=(),
        )

        hyps = _latch_exposure_hypotheses(plc, incident, ctx)
        # Two per-latch hypotheses plus one conjunction clearing both.
        assert len(hyps) == 3
        per_latch = [h for h in hyps if len(h.holds) == 1]
        conjunction = [h for h in hyps if len(h.holds) == 2]
        assert {h.holds for h in per_latch} == {(("G1", True),), (("G2", True),)}
        assert len(conjunction) == 1
        assert set(conjunction[0].holds) == {("G1", True), ("G2", True)}


# ---------------------------------------------------------------------------
# _liveness_hypotheses — complement-reset watchdog oscillation holds
# ---------------------------------------------------------------------------


class TestLivenessHypotheses:
    """_liveness_hypotheses: watchdog-driven oscillation holds.

    A complement-reset watchdog (``on_delay`` reset by an input edge) trips if
    the input sits at either polarity too long.  Only a *changing* input
    satisfies it — proposed as a :class:`LivenessHold`.
    """

    def test_complement_reset_watchdog_produces_liveness_hold(self):
        Sensor = Bool("Sensor", external=True)
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(WD, 30, "ms").reset(~Sensor)
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Sensor": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True  # watchdog fired

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("Sensor", True),),
            bearing=(("Err", False),),
            before_snap={"Sensor": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        hyps = _liveness_hypotheses(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].kind == "liveness"
        ((tag, val),) = hyps[0].holds
        assert tag == "Sensor"
        assert isinstance(val, LivenessHold)

    def test_dwell_respects_shortest_preset(self):
        # Two watchdogs read the same sensor; the toggle dwell must clear the
        # tightest one (40ms -> 4 scans -> dwell max(2, 4//2) = 2), not the
        # looser 100ms watchdog (which alone would give dwell 5).
        Sensor = Bool("Sensor", external=True)
        WDa = Timer.clone("WDa")
        WDb = Timer.clone("WDb")
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(WDa, 100, "ms").reset(~Sensor)
            with Rung():
                on_delay(WDb, 40, "ms").reset(~Sensor)
            with Rung(WDa.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Sensor": True})
        for _ in range(12):
            plc.step()

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("Sensor", True),),
            bearing=(("Err", False),),
            before_snap={"Sensor": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("WDa_Done",),  # only the loose watchdog fired
            departures=(),
        )

        hyps = _liveness_hypotheses(plc, incident, ctx)
        assert len(hyps) == 1
        ((_tag, lh),) = hyps[0].holds
        # Tightest preset (4 scans) governs the dwell, not the fired one (10).
        assert lh == LivenessHold(on_dwell=2, off_dwell=2)

    def test_only_fired_watchdogs_proposed(self):
        S1 = Bool("S1", external=True)
        S2 = Bool("S2", external=True)
        W1 = Timer.clone("W1")
        W2 = Timer.clone("W2")
        E = Bool("E")
        with Program() as prog:
            with Rung():
                on_delay(W1, 30, "ms").reset(~S1)
            with Rung():
                on_delay(W2, 30, "ms").reset(~S2)
            with Rung(W1.Done):
                out(E)

        plc = PLC(prog, dt=0.010)
        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=5,
            action=(),
            bearing=(("E", False),),
            before_snap={},
            after_snap={"S1": True, "S2": True},
            changed_tags=("W1_Done",),  # only W1 fired; W2 did not
            departures=(),
        )

        hyps = _liveness_hypotheses(plc, incident, ctx)
        proposed = {h.holds[0][0] for h in hyps}
        assert proposed == {"S1"}


# ---------------------------------------------------------------------------
# investigate_excursion — diagnose a state-key revert, replay-validate holds
# ---------------------------------------------------------------------------


def _seal_in_program() -> Program:
    """``Out`` fires on a rising ``Command`` edge but only *stays* up if sealed
    in by ``Hold``.  Pulsed (edge) without the hold, ``Out`` reverts — exactly
    the excursion shape verify detects.
    """
    Command = Bool("Command", external=True)
    Hold = Bool("Hold", external=True)
    Out = Bool("Out")
    with Program() as prog:
        with Rung(Or(rise(Command), And(Out, Hold))):
            out(Out)
    return prog


def _excursion_inputs():
    """Build the (work, fork, snaps, key, cfg, steerable) for an excursion.

    ``work`` rests at Out=False; ``fork`` reproduces the pulse where Out went
    True then reverted to False after the edge released (no hold installed).
    """
    prog = _seal_in_program()
    work = PLC(prog, dt=0.010)
    work.patch({"Command": False, "Hold": False})
    work.step()

    cfg = _StateKeyConfig(
        stateful_names=("Out",),
        done_specs=(),
        threshold_vector_specs=(),
        acc_indices=frozenset(),
    )
    pre_snap = dict(work.state.tags)
    pre_key = _pilot_state_key(pre_snap, cfg)

    fork = work.fork()
    fork.patch({"Command": False})
    fork.step()
    fork.patch({"Command": True})
    fork.step()
    post_pulse_snap = dict(fork.state.tags)
    for _ in range(4):
        fork.step()

    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, work._known_tags_by_name, prog))
    return work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable


class TestInvestigateExcursion:
    """investigate_excursion: state-key excursion diagnosis and hold-based retry."""

    def test_reverted_tags_diagnosed(self):
        work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable = _excursion_inputs()
        # Out was True at the end of the pulse but reverted to its pre value.
        assert post_pulse_snap["Out"] is True
        assert fork.state.tags["Out"] is False

        result = investigate_excursion(
            work,
            fork,
            pre_snap,
            post_pulse_snap,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            forced_holds={},
            resting={"Command": False},
            edge_tags={"Command"},
            scan_budget=50,
        )
        assert result.reverted == ["Out"]

    def test_confirmed_holds_fix_revert(self):
        work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable = _excursion_inputs()

        result = investigate_excursion(
            work,
            fork,
            pre_snap,
            post_pulse_snap,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            forced_holds={},
            resting={"Command": False},
            edge_tags={"Command"},
            scan_budget=50,
        )
        # Sealing Hold=True keeps Out latched across the edge release — the
        # retry key differs from the (reverted) pre key, so the hold is kept.
        assert ("Hold", True) in result.confirmed_holds
        assert result.retry_fork is not None


# ---------------------------------------------------------------------------
# Incident construction + internal helpers
# ---------------------------------------------------------------------------


class TestDedupePairs:
    def test_preserves_first_occurrence_order(self):
        pairs = [("a", 1), ("b", 2), ("a", 1), ("c", 3)]
        assert _dedupe_pairs(pairs) == [("a", 1), ("b", 2), ("c", 3)]


class TestHoldAllowed:
    def test_rejects_action_tags(self):
        ctx = SimpleNamespace(compass=SimpleNamespace(action_tags=frozenset({"x"})))
        assert _hold_allowed(ctx, ("x", True)) is False
        assert _hold_allowed(ctx, ("y", True)) is True

    def test_rejects_route_blocked(self):
        ctx = SimpleNamespace(
            compass=SimpleNamespace(action_tags=frozenset()),
            route_allowed=lambda pair: pair[0] != "blocked",
        )
        assert _hold_allowed(ctx, ("blocked", True)) is False
        assert _hold_allowed(ctx, ("ok", True)) is True


def _change_program() -> Program:
    A = Bool("A", external=True)
    B = Bool("B")
    with Program() as prog:
        with Rung(A):
            out(B)
    return prog


class TestWindowHelpers:
    def test_changed_tags_in_window(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.patch({"A": True})
        plc.step()  # A -> True, B -> True
        changed = _changed_tags_in_window(plc, anchor, plc.state.scan_id)
        assert "A" in changed and "B" in changed

    def test_first_departure_scan(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.patch({"A": True})
        plc.step()
        # B held False at the anchor and departed (-> True) the scan A latched it.
        dep = _first_departure_scan(plc, "B", False, anchor, plc.state.scan_id)
        assert dep == plc.state.scan_id

    def test_no_departure_returns_none(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.step()  # B never leaves False
        assert _first_departure_scan(plc, "B", False, anchor, plc.state.scan_id) is None


class TestBuildDeviationIncident:
    def test_captures_changes_and_departures(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.patch({"A": True})
        plc.step()
        incident = build_deviation_incident(
            plc,
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(("A", True),),
            bearing=(("B", False),),
            before_snap={"B": False},
            after_snap=dict(plc.state.tags),
        )
        assert "B" in incident.changed_tags
        # B departed from its bearing (False) inside the window.
        assert len(incident.departures) == 1
        assert incident.departures[0].tag == "B"
        assert incident.departure_scan == plc.state.scan_id

    def test_no_departure_when_bearing_held(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.step()  # B stays False — bearing held
        incident = build_deviation_incident(
            plc,
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("B", False),),
            before_snap={"B": False},
            after_snap=dict(plc.state.tags),
        )
        assert incident.departures == ()
        assert incident.departure_scan is None
