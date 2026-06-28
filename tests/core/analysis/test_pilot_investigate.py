"""Tests for pilot investigation — hypothesis generation and bounded replay.

Coverage targets:
- build_replay_fn: bounded vs unbounded judgment
- investigate_deviation: hypothesis generation pipeline
- _cause_hypotheses, _latch_exposure_hypotheses, _done_boundary_hypotheses
- investigate_excursion: excursion diagnosis and retry
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import And, Bool, Int, Or, Program, Rung, Timer, copy, latch, on_delay, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import (
    ConditionalHold,
    _coast_holding_state,
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
    _done_boundary_hypotheses,
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
# Zoom incident — governing register reaches its corridor target
# ---------------------------------------------------------------------------


def _zoom_corridor_program() -> tuple[Program, Timer, Any]:
    """``State`` advances 3 -> 6 after a watchdog timer, but ejects to 8 (Aborting)
    if the door (``Guard``) is open at completion.  Holding the door closed lets
    the coast reach the corridor target (6); leaving it open ejects (8).

    The timer is long (50 scans) on purpose: the corridor target is reachable
    only by an *unbounded* coast, so a coast bounded to the departure window
    would never get there — that is the regression this guards.
    """
    Guard = Bool("Guard", external=True)
    Tmr = Timer.clone("Tmr")
    State = Int("State")
    with Program() as prog:
        with Rung(State == 3):
            on_delay(Tmr, 500, "ms")
        with Rung(Tmr.Done, ~Guard):
            copy(8, State)  # door open at completion -> eject to Aborting
        with Rung(Tmr.Done, Guard):
            copy(6, State)  # door closed -> advance to Execute
    return prog, Tmr, State


class TestZoomReplay:
    """build_replay_fn for a zoom incident.

    Judged by the governing register reaching its corridor target over an
    *unbounded*, ejection-guarded coast — never by the bounded bearing-held test
    (the bearing carries the far-off corridor target as a conjunct, which a
    bounded coast can never restore, so it would reject every hold).
    """

    def _setup(self):
        prog, _tmr, _state = _zoom_corridor_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"State": 3})
        plc.step()
        assert plc.state.tags["State"] == 3
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "State", 6)
        steps = [_Step(action={}, scan_before=cp.state.scan_id, scan_after=cp.state.scan_id)]
        # A deliberately *tiny* departure window carrying the unreachable corridor
        # target as a bearing conjunct: if the zoom coast were (wrongly) bounded
        # by it, State could never reach 6 and the good hold would be rejected.
        return cp, steps, ctx

    def _build(self, cp, steps, ctx):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            **ctx,
            zoom_governing_tag="State",
            zoom_target_value=6,
            departure_scan=cp.state.scan_id + 1,
            departure_bearing=(("State", 6),),
        )

    def test_zoom_accepts_hold_that_reaches_corridor_target(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay((("Guard", True),))  # close the door
        assert outcome.accepted
        assert outcome.snapshot["State"] == 6
        assert "State -> 6" in outcome.reason

    def test_zoom_rejects_hold_that_ejects(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay(())  # door rests open -> ejects to 8
        assert not outcome.accepted
        assert outcome.snapshot["State"] == 8


# ---------------------------------------------------------------------------
# Terminal let-run incident — governing register *maintained* at its held value
# ---------------------------------------------------------------------------


def _letrun_hold_program() -> tuple[Program, Timer, Any]:
    """``Phase`` sits at 6 (Execute).  A watchdog ejects it to 8 (Aborting) at its
    preset unless ``Guard`` is held.  ``Goal`` (the global target) is never
    reached inside the window, so the only signal of a good hold is whether
    ``Phase`` *stayed* at 6 — the maintained-macro-state judgment.
    """
    Guard = Bool("Guard", external=True)
    Tmr = Timer.clone("Tmr")
    Phase = Int("Phase")
    Goal = Bool("Goal")
    with Program() as prog:
        with Rung(Phase == 6):
            on_delay(Tmr, 200, "ms")
        with Rung(Tmr.Done, ~Guard):
            copy(8, Phase)  # eject Execute -> Aborting
        with Rung(Phase == 99):
            out(Goal)  # Phase never 99 -> Goal stays a known-but-unreached tag
    return prog, Tmr, Phase


class TestTerminalLetrunReplay:
    """build_replay_fn for a terminal let-run incident.

    The coast is *bounded* to the departure window (its global target is far
    off), but the judgment is the governing register being *maintained* at its
    held value — not the bounded bearing-held conjunction, which would over-
    reject the very liveness/precondition hold that keeps the state from ejecting.
    """

    def _setup(self):
        prog, _tmr, _phase = _letrun_hold_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Phase": 6})
        plc.step()
        assert plc.state.tags["Phase"] == 6
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "Goal", True)
        steps = [_Step(action={}, scan_before=cp.state.scan_id, scan_after=cp.state.scan_id)]
        return cp, steps, ctx

    def _build(self, cp, steps, ctx):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            **ctx,
            zoom_governing_tag="Phase",
            zoom_target_value=6,
            terminal_letrun_role_tags=("Phase",),
            # Window covers the watchdog eject (~20 scans) so the bad hold ejects
            # inside the bounded coast.
            departure_scan=cp.state.scan_id + 25,
            departure_bearing=(("Phase", 6),),
        )

    def test_letrun_accepts_hold_that_maintains_state(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay((("Guard", True),))  # keep the watchdog satisfied
        assert outcome.accepted
        assert outcome.snapshot["Phase"] == 6
        assert "Phase -> 6" in outcome.reason

    def test_letrun_rejects_hold_that_ejects(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay(())  # watchdog trips -> Phase ejects to 8
        assert not outcome.accepted
        assert outcome.snapshot["Phase"] == 8


def _letrun_global_program() -> tuple[Program, Timer]:
    """No macro-state register: ``Goal`` latches at the watchdog preset only if
    ``Hold`` keeps ``Alarm`` clear.  Exercises the let-run fallback that judges
    the *global* target when there is no governing register to maintain.
    """
    Enable = Bool("Enable", external=True)
    Hold = Bool("Hold", external=True)
    Tmr = Timer.clone("Tmr")
    Alarm = Bool("Alarm")
    Goal = Bool("Goal")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done, ~Hold):
            latch(Alarm)
        with Rung(Tmr.Done, ~Alarm):
            latch(Goal)
    return prog, Tmr


class TestTerminalLetrunNoGoverningRegister:
    """A let-run with no recognized state machine (empty role tags, no governing
    register) falls back to judging the global target at the bounded point."""

    def _setup(self):
        prog, _tmr = _letrun_global_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "Goal", True)
        steps = [_Step(action={}, scan_before=cp.state.scan_id, scan_after=cp.state.scan_id)]
        return cp, steps, ctx

    def _build(self, cp, steps, ctx):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            **ctx,
            terminal_letrun_role_tags=(),  # no recognized state machine
            departure_scan=cp.state.scan_id + 15,
            departure_bearing=(("Goal", True),),
        )

    def test_fallback_accepts_hold_that_reaches_global_target(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay((("Hold", True),))  # keep Alarm clear -> Goal latches
        assert outcome.accepted
        assert outcome.snapshot["Goal"] is True
        assert "Goal -> True" in outcome.reason

    def test_fallback_rejects_hold_that_misses_global_target(self):
        cp, steps, ctx = self._setup()
        replay = self._build(cp, steps, ctx)
        outcome = replay(())  # Alarm latches -> Goal never reached
        assert not outcome.accepted
        assert outcome.snapshot["Goal"] is not True


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
# _done_boundary_hypotheses — complement-reset watchdog oscillation holds
# ---------------------------------------------------------------------------


class TestLivenessHypotheses:
    """_done_boundary_hypotheses: watchdog-driven oscillation holds.

    A complement-reset watchdog (``on_delay`` reset by an input edge) trips if
    the input sits at either polarity too long.  Only a *changing* input
    satisfies it — proposed structurally (no dwell) as a :class:`ConditionalHold`
    carrying one guarded rule per resetting polarity.
    """

    def test_complement_reset_watchdog_produces_conditional_hold(self):
        # One watchdog resets on ~Sensor (counts while True): the only resetting
        # polarity is False, so the hold drives Sensor->False while it is != False.
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

        hyps = _done_boundary_hypotheses(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].kind == "liveness"
        ((tag, val),) = hyps[0].holds
        assert tag == "Sensor"
        assert isinstance(val, ConditionalHold)
        assert [(r.value, r.guard_op, r.guard_value) for r in val.rules] == [(False, "ne", False)]

    def test_complement_pair_yields_both_polarity_rules(self):
        # Two watchdogs on one sensor reset on OPPOSITE edges — held at either
        # polarity, one trips.  The hold must carry BOTH polarity rules so the
        # input oscillates; a single steady value would trip the other watchdog.
        Sensor = Bool("Sensor", external=True)
        OffWD = Timer.clone("OffWD")  # resets on Sensor -> counts while False
        OnWD = Timer.clone("OnWD")  # resets on ~Sensor -> counts while True
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(OffWD, 30, "ms").reset(Sensor)
            with Rung():
                on_delay(OnWD, 30, "ms").reset(~Sensor)
            with Rung(Or(OffWD.Done, OnWD.Done)):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Sensor": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["OnWD_Done"] is True  # the on-watchdog fired

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("Sensor", True),),
            bearing=(("Err", False),),
            before_snap={"Sensor": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("OnWD_Done", "Err"),
            departures=(),
        )

        hyps = _done_boundary_hypotheses(plc, incident, ctx)
        assert len(hyps) == 1
        ((tag, val),) = hyps[0].holds
        assert tag == "Sensor"
        assert isinstance(val, ConditionalHold)
        # Both polarities, each guarded by "drive v while != v" — the oscillation.
        assert {(r.value, r.guard_op, r.guard_value) for r in val.rules} == {
            (False, "ne", False),
            (True, "ne", True),
        }

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

        hyps = _done_boundary_hypotheses(plc, incident, ctx)
        proposed = {h.holds[0][0] for h in hyps}
        assert proposed == {"S1"}


def _shaft_rotate_program() -> Program:
    """A shaft-rotate feedback that must keep *pulsing* while a delay counts up.

    The canonical liveness shape, self-contained: a rotation sensor guarded by
    two complement-reset watchdogs, and a run delay that only advances while no
    watchdog has faulted.

    - ``x_Rotate`` (external): the shaft-rotation feedback bit.
    - ``SensorOffWD``: resets on ``x_Rotate`` -> counts while the sensor is
      *False* -> trips if rotation stalls off.
    - ``SensorOnWD``: resets on ``~x_Rotate`` -> counts while the sensor is
      *True* -> trips if rotation sticks on.
    - either Done -> ``Fault`` latches -> ``RunDelay`` (gated by ``~Fault``)
      resets and can never complete.
    - ``RunDelay`` Done -> ``Running`` (the target).

    Steady at either polarity faults within 50 ms; only a sensor that oscillates
    faster than 50 ms keeps both watchdogs reset long enough for the 200 ms
    ``RunDelay`` to reach ``Running``.
    """
    Rotate = Bool("x_Rotate", external=True)
    SensorOffWD = Timer.clone("SensorOffWD")
    SensorOnWD = Timer.clone("SensorOnWD")
    RunDelay = Timer.clone("RunDelay")
    Fault = Bool("Fault")
    Running = Bool("Running")
    with Program() as prog:
        with Rung():
            on_delay(SensorOffWD, 50, "ms").reset(Rotate)
        with Rung():
            on_delay(SensorOnWD, 50, "ms").reset(~Rotate)
        with Rung(Or(SensorOffWD.Done, SensorOnWD.Done)):
            latch(Fault)
        with Rung(~Fault):
            on_delay(RunDelay, 200, "ms")
        with Rung(RunDelay.Done):
            out(Running)
    return prog


def _coast_holding_to_trip(plc: PLC, polarity: bool, limit: int = 60) -> DeviationIncident:
    """Hold ``x_Rotate`` steady at *polarity* and step until a sensor watchdog
    fires, then return the bounded incident over that coast span — the faithful
    analogue of a terminal let-run that ejects on a watchdog."""
    wd = ("SensorOffWD_Done", "SensorOnWD_Done")
    anchor = plc.state.scan_id
    before = dict(plc.state.tags)
    for _ in range(limit):
        plc.force("x_Rotate", polarity)
        plc.step()
        if any(plc.state.tags.get(n) for n in wd):
            break
    return build_deviation_incident(
        plc,
        anchor_scan=anchor,
        end_scan=plc.state.scan_id,
        action=(),
        bearing=(("Running", True),),
        before_snap=before,
        after_snap=dict(plc.state.tags),
    )


class TestShaftRotateLiveness:
    """The shaft-rotate scenario end to end: a bit that must keep pulsing while a
    delay counts up, driven by a structurally-synthesized :class:`ConditionalHold`.
    """

    def test_delay_needs_pulsing(self):
        # The premise: a steady sensor faults and the delay never completes;
        # only an oscillating sensor lets RunDelay count up to Running.
        prog = _shaft_rotate_program()
        plc = PLC(prog, dt=0.010)
        plc.force("x_Rotate", False)
        for _ in range(30):
            plc.step()
        assert plc.state.tags["Fault"] is True
        assert plc.state.tags["Running"] is False

        prog2 = _shaft_rotate_program()
        plc2 = PLC(prog2, dt=0.010)
        val = True
        for i in range(40):
            if i % 3 == 0:  # flip every 3 scans (30 ms) — faster than the 50 ms WDs
                val = not val
            plc2.force("x_Rotate", val)
            plc2.step()
        assert plc2.state.tags["Fault"] is False
        assert plc2.state.tags["Running"] is True

    def test_ejection_synthesizes_both_polarity_hold(self):
        # Park the sensor off and let it eject (SensorOffWD trips); from that one
        # incident, _done_boundary_hypotheses reads BOTH watchdogs structurally and
        # synthesizes an oscillating ConditionalHold — no dwell, no second round.
        prog = _shaft_rotate_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        ctx = _make_ctx(prog, plc)
        incident = _coast_holding_to_trip(plc, False)
        assert "SensorOffWD_Done" in incident.changed_tags

        hyps = _done_boundary_hypotheses(plc, incident, ctx)
        assert len(hyps) == 1
        ((tag, val),) = hyps[0].holds
        assert tag == "x_Rotate"
        assert isinstance(val, ConditionalHold)
        assert {(r.value, r.guard_op, r.guard_value) for r in val.rules} == {
            (True, "ne", True),
            (False, "ne", False),
        }

    def test_synthesized_hold_oscillates_to_target(self):
        # Drive the coast with the synthesized hold: the two complementary rules
        # alternate x_Rotate every scan, keeping both watchdogs reset, so RunDelay
        # counts up and Running is reached with no fault.
        prog = _shaft_rotate_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        ctx = _make_ctx(prog, plc)
        incident = _coast_holding_to_trip(plc, False)
        ((tag, hold),) = _done_boundary_hypotheses(plc, incident, ctx)[0].holds

        fresh = PLC(_shaft_rotate_program(), dt=0.010)
        fresh.step()
        reached = _coast_holding_state(
            fresh, "Running", True, (), conditional={tag: hold}, budget=200
        )
        assert reached is True
        assert fresh.state.tags["Running"] is True
        assert fresh.state.tags["Fault"] is False


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
