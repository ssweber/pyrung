"""Tests for pilot investigation — hypothesis generation and bounded replay.

Coverage targets:
- build_replay_fn: bounded vs unbounded judgment
- investigate_deviation: hypothesis generation pipeline
- _precise_cause, _latch_exposure_hypotheses, _done_boundary_hypotheses
- investigate_excursion: excursion diagnosis and retry
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import And, Bool, Int, Or, Program, Rung, Timer, calc, copy, latch, on_delay, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _coast_holding_state,
    _pilot_state_key,
    _set_rungs,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.coast import BumpEvent, CoastSession
from pyrung.core.analysis.pilot.corrections import correct_enablers
from pyrung.core.analysis.pilot.investigate import (
    DeviationIncident,
    InvestigationHypothesis,
    ReplayJustification,
    ReplayOutcome,
    ReplayStep,
    _dedupe_pairs,
    _first_timeline_departure,
    _hold_allowed,
    _hold_is_noop,
    _precise_cause,
    _precise_causes,
    _regression_cause_replayed,
    build_deviation_incident,
    build_replay_fn,
    correction_identity,
    incident_regression_witness,
    investigate_deviation,
    investigate_excursion,
)
from pyrung.core.analysis.pilot.types import BearingDeparture
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.runner import PLC


def _make_ctx(prog: Program, plc: PLC, **overrides: Any) -> SimpleNamespace:
    """Minimal duck-typed context for the hypothesis generators.

    The generators read ``pdg``, ``program``, ``steerable``, ``opaque_loop``,
    ``pipeline_internal_tags``, ``route`` and ``compass.action_tags`` off the
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
        "route": None,
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
        "route": None,
    }


def _ground_test_incident(plc: PLC) -> DeviationIncident:
    snap = dict(plc.state.tags)
    return DeviationIncident(
        anchor_scan=plc.state.scan_id,
        departure_scan=plc.state.scan_id,
        end_scan=plc.state.scan_id,
        action=(),
        bearing=(),
        before_snap=snap,
        after_snap=snap,
        changed_tags=(),
        departures=(),
    )


def test_investigation_rejections_carry_raw_and_guarded_replay_grounds(monkeypatch):
    """Replay reasons survive into the result instead of disappearing into DEBUG."""
    A = Bool("GroundA", external=True)
    B = Bool("GroundB", external=True)
    with Program(strict=False) as prog:
        with Rung(A):
            out(B)
    plc = PLC(prog, dt=0.010)
    ctx = _make_ctx(prog, plc)
    raw_reject = InvestigationHypothesis("raw", (("GroundA", True),))
    guarded_reject = InvestigationHypothesis("guarded", (("GroundB", True),))

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([raw_reject, guarded_reject], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._scoped_correction_rungs",
        lambda _plc, holds, *_args: tuple(PilotRung(t, v, A == A) for t, v in holds),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )

    def replay(holds):
        first = holds[0]
        if isinstance(first, PilotRung):
            return ReplayOutcome(False, None, {}, "guard released before landing")
        if first[0] == "GroundA":
            return ReplayOutcome(False, None, {}, "watchdog still fired")
        return ReplayOutcome(True, None, {}, "incident silenced")

    result = investigate_deviation(plc, _ground_test_incident(plc), ctx, replay)

    assert result.rejected == (
        (raw_reject, "raw replay rejected: watchdog still fired"),
        (guarded_reject, "guarded replay rejected: guard released before landing"),
    )
    # Each rejection carries an index-aligned machine-readable ground slug.
    assert result.rejection_slugs == (
        "exploratory-replay-failed",
        "guarded-replay-failed",
    )


def test_investigation_static_rejections_carry_their_grounds(monkeypatch):
    A = Bool("StaticGround", external=True)
    with Program(strict=False) as prog:
        with Rung(A):
            out(Bool("StaticOut"))
    plc = PLC(prog, dt=0.010)
    ctx = _make_ctx(prog, plc)
    empty = InvestigationHypothesis("empty", ())
    noop = InvestigationHypothesis("noop", (("StaticGround", False),))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([empty, noop], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: True,
    )

    result = investigate_deviation(
        plc,
        _ground_test_incident(plc),
        ctx,
        lambda _holds: pytest.fail("static rejection must not replay"),
    )

    assert result.rejected[0] == (empty, "no holds proposed")
    assert result.rejected[1][0] == noop
    assert result.rejected[1][1].startswith("vacuous no-op hold")
    assert result.rejection_slugs == ("no-holds", "vacuous-hold")


def test_revoked_correction_is_skipped_and_runner_up_is_replayed(monkeypatch):
    """Correction nogoods select the next explanation, not an opposite overlay."""
    Bad = Bool("Revoked_Bad", external=True)
    Good = Bool("Revoked_Good", external=True)
    with Program(strict=False) as prog:
        with Rung(Bad):
            out(Bool("Revoked_BadOut"))
        with Rung(Good):
            out(Bool("Revoked_GoodOut"))
    plc = PLC(prog)
    ctx = _make_ctx(
        prog,
        plc,
        target_tag="Revoked_GoodOut",
        target_value=True,
        target_predicate=None,
    )
    bad = InvestigationHypothesis("bad", ((Bad.name, True),), sources=(Bad.name,))
    good = InvestigationHypothesis("good", ((Good.name, True),), sources=(Good.name,))
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._absence_root_correctives",
        lambda *_args, **_kwargs: ([bad, good], set()),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._precise_causes",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate.correct_enablers",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._rank_hypotheses",
        lambda _plc, hypotheses, *_args, **_kwargs: hypotheses,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._hold_is_noop",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.investigate._active_rungs_defeat_needed",
        lambda *_args, **_kwargs: False,
    )

    replayed = []

    def replay(holds):
        replayed.append(tuple((h.dest, h.value) if isinstance(h, PilotRung) else h for h in holds))
        return ReplayOutcome(True, None, dict(plc.state.tags), "incident solved")

    result = investigate_deviation(
        plc,
        _ground_test_incident(plc),
        ctx,
        replay,
        excluded_corrections=frozenset((correction_identity(bad.holds),)),
    )

    assert result.confirmed and result.confirmed[0].kind == "good"
    assert replayed
    assert all(Bad.name not in {tag for tag, _value in attempt} for attempt in replayed)
    assert result.rejection_slugs == ("correction-revoked",)


def test_noop_check_uses_recorded_incident_motion_not_pilot_ownership():
    Bool("RecordedMover", external=True)
    with Program(strict=False) as prog:
        with Rung():
            out(Bool("RecordedMoverReader"))
    plc = PLC(prog, dt=0.010)
    ctx = _make_ctx(prog, plc)
    snap = {"RecordedMover": False}

    assert _hold_allowed(ctx, ("RecordedMover", False))
    assert _hold_is_noop("RecordedMover", False, snap, ctx.pdg, prog)
    assert not _hold_is_noop(
        "RecordedMover",
        False,
        snap,
        ctx.pdg,
        prog,
        frozenset({"RecordedMover"}),
    )
    assert not _hold_is_noop(
        "RecordedMover",
        False,
        snap,
        ctx.pdg,
        prog,
        after_snap={"RecordedMover": True},
    )
    assert not _hold_is_noop(
        "RecordedMover",
        False,
        snap,
        ctx.pdg,
        prog,
        synthesis_rungs=(PilotRung("RecordedMover", True, Bool("SynthesisGuard")),),
    )


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
    """build_replay_fn with departure_bearing judges a command incident by
    bearing rather than target-reached; the coast is the recorded dwell span."""

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

        # The recorded coast span: a plain dwell reproduces the same scans the
        # live coast rode (the timer fires ~10 scans in, so 20 covers it).
        span = plc.state.scan_id - cp.state.scan_id
        ctx = _make_replay_context(prog, plc, "Target", True)
        cp_trend = 1
        steps = [ReplayStep(inputs=(), scans=span, kind="dwell")]
        return prog, plc, cp, cp_trend, steps, ctx

    def test_bounded_accepts_good_hold(self):
        """A hold that prevents the departure is accepted under bounded replay."""
        _prog, _plc, cp, cp_trend, steps, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            **ctx,
            departure_bearing=(("Alarm", False),),
        )
        outcome = replay((("Hold", True),))
        assert outcome.accepted
        assert "held" in outcome.reason

    def test_bounded_rejects_bad_hold(self):
        """A no-op hold that doesn't prevent the departure is rejected."""
        _prog, _plc, cp, cp_trend, steps, ctx = self._setup()

        replay = build_replay_fn(
            cp,
            cp_trend,
            {},
            steps,
            **ctx,
            departure_bearing=(("Alarm", False),),
        )
        outcome = replay(())
        assert not outcome.accepted
        assert "departed" in outcome.reason

    def test_unbounded_falls_through_to_trend_judgment(self):
        """Without departure info, replay uses the trace-back trend judgment."""
        _prog, _plc, cp, cp_trend, steps, ctx = self._setup()

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
# Zoom incident — channel register reaches its requested value
# ---------------------------------------------------------------------------


def _zoom_transition_program() -> tuple[Program, Timer, Any]:
    """``State`` advances 3 -> 6 after a watchdog timer, but ejects to 8 (Aborting)
    if the door (``Guard``) is open at completion.  Holding the door closed lets
    the coast reach the requested value (6); leaving it open ejects (8).

    The timer is long (50 scans) on purpose: the requested value is reachable
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

    Judged by the channel register reaching its requested value over an
    *unbounded*, ejection-guarded coast — never by the bounded bearing-held test
    (the bearing carries the far-off requested value as a conjunct, which a
    bounded coast can never restore, so it would reject every hold).
    """

    def _setup(self):
        prog, _tmr, _state = _zoom_transition_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"State": 3})
        plc.step()
        assert plc.state.tags["State"] == 3
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "State", 6)
        # A recorded zoom step: the coast re-arms State -> 6 under the ejection
        # guard, unbounded — the requested value is a full coast away, so no
        # departure-window bound may cut it short.
        steps = [ReplayStep(inputs=(), scans=0, kind="zoom", channel_tag="State", channel_target=6)]
        return cp, steps, ctx

    def _build(self, cp, steps, ctx):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            **ctx,
            zoom_channel_tag="State",
            zoom_target_value=6,
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


def test_route_replay_accepts_local_neutralization_without_reaching_frontier():
    """A correction owns the recorded regression, not the whole remaining route."""
    Guard = Bool("Neutralize_Guard", external=True)
    Detour = Bool("Neutralize_Detour", external=True)
    Watchdog = Timer.clone("Neutralize_Watchdog")
    State = Int("Neutralize_State", default=6)

    with Program() as prog:
        with Rung(State == 6):
            on_delay(Watchdog, 100, "ms").reset(Guard)
        with Rung(State == 6, Detour):
            copy(13, State)
        with Rung(Watchdog.Done):
            copy(8, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    incident_session.dwell(20)
    assert recorded.state.tags[State.name] == 8
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=(),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None
    assert (witness.source, witness.departed) == (6, 8)

    ctx = _make_replay_context(prog, plc, State.name, 17)
    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=(), scans=20, kind="dwell"),),
        **ctx,
        zoom_channel_tag=State.name,
        zoom_target_value=16,
        regression_witness=witness,
    )

    neutralized = replay(((Guard.name, True),))
    assert neutralized.accepted
    assert neutralized.justification is ReplayJustification.NEUTRALIZED
    assert neutralized.snapshot[State.name] == 6
    assert "recorded regression neutralized" in neutralized.reason

    harmful = replay(((Detour.name, True),))
    assert not harmful.accepted
    assert harmful.snapshot[State.name] == 13
    assert harmful.justification is None


def test_non_timer_regression_witness_distinguishes_suppression_from_masking():
    """Neutralization owns the recorded cause, not a behavior class."""
    Trip = Bool("Witness_Trip", external=True)
    Inhibit = Bool("Witness_Inhibit", external=True)
    Mask = Bool("Witness_Mask", external=True)
    State = Int("Witness_State", default=6)

    with Program() as prog:
        with Rung(Trip, ~Inhibit):
            copy(8, State)
        with Rung(Trip, Mask):
            copy(6, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    recorded.patch({Trip.name: True})
    recorded.step()
    incident_session.note_pens()
    assert recorded.state.tags[State.name] == 8
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=((Trip.name, True),),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None

    ctx = _make_replay_context(prog, plc, State.name, 17)
    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=((Trip.name, True),), scans=1, kind="pulse"),),
        **ctx,
        zoom_channel_tag=State.name,
        zoom_target_value=16,
        regression_witness=witness,
    )

    suppressed = replay(((Inhibit.name, True),))
    assert suppressed.snapshot[State.name] == 6
    assert suppressed.accepted
    assert suppressed.justification is ReplayJustification.NEUTRALIZED
    assert "suppressed its" in suppressed.reason

    masked_probe = cp.fork()
    _set_rungs(masked_probe, (PilotRung(Mask.name, True, State != 17),))
    masked_start = masked_probe.state.scan_id
    masked_probe.patch({Trip.name: True})
    masked_probe.step()
    assert masked_probe.state.tags[State.name] == 6
    assert _regression_cause_replayed(
        masked_probe,
        witness,
        start_scan=masked_start,
        end_scan=masked_probe.state.scan_id,
    ), masked_probe.rung_firings(masked_probe.state.scan_id)

    masked = replay(((Mask.name, True),))
    assert not masked.accepted
    assert masked.snapshot[State.name] == 6
    assert "cause replayed" in masked.reason


def test_regression_witness_does_not_confuse_a_shared_executor_with_its_owner():
    """A different cause may reuse the same response pipeline."""
    Fault = Bool("WitnessOwner_Fault", external=True)
    Alternate = Bool("WitnessOwner_Alternate", external=True)
    Request = Int("WitnessOwner_Request")
    State = Int("WitnessOwner_State", default=6)

    with Program() as prog:
        with Rung(Fault):
            copy(8, Request)
        with Rung(Alternate):
            copy(8, Request)
        with Rung(Request == 8):
            copy(Request, State)
            copy(0, Request)

    plc = PLC(prog)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    recorded.patch({Fault.name: True})
    recorded.step()
    incident_session.note_pens()
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=((Fault.name, True),),
        bearing=((State.name, 6),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None
    assert {item.rung.rung_index for item in witness.cause} == {0, 2}

    alternate = cp.fork()
    start_scan = alternate.state.scan_id
    alternate.patch({Alternate.name: True})
    alternate.step()
    assert alternate.state.tags[State.name] == 8
    assert not _regression_cause_replayed(
        alternate,
        witness,
        start_scan=start_scan,
        end_scan=alternate.state.scan_id,
    )


def test_latch_silencing_replay_observes_the_stable_landing_after_a_waypoint():
    """Correction scope comes from automatic motion beyond the incident window."""
    DoorA = Bool("Landing_DoorA", external=True)
    DoorB = Bool("Landing_DoorB", external=True)
    AlarmA = Bool("Landing_AlarmA")
    AlarmB = Bool("Landing_AlarmB")
    State = Int("Landing_State", default=3)
    AlarmTmr = Timer.clone("Landing_AlarmTmr")
    MotionTmr = Timer.clone("Landing_MotionTmr")

    with Program() as prog:
        with Rung(State == 3):
            on_delay(AlarmTmr, 100, "ms")
            on_delay(MotionTmr, 500, "ms")
        with Rung(AlarmTmr.Done, ~DoorA):
            latch(AlarmA)
        with Rung(AlarmTmr.Done, ~DoorB):
            latch(AlarmB)
        with Rung(Or(AlarmA, AlarmB)):
            copy(8, State)
        with Rung(MotionTmr.Done, DoorA, DoorB):
            copy(6, State)

    plc = PLC(prog, dt=0.010)
    plc.step()
    cp = plc.fork()
    recorded = cp.fork()
    incident_session = CoastSession(recorded, kind="recorded-regression")
    incident_session.arm_pens((State.name,))
    incident_session.dwell(12)
    assert recorded.state.tags[State.name] == 8
    incident = build_deviation_incident(
        anchor_scan=cp.state.scan_id,
        end_scan=recorded.state.scan_id,
        action=(),
        bearing=((State.name, 3),),
        before_snap=dict(cp.state.tags),
        after_snap=dict(recorded.state.tags),
        timeline=incident_session.events,
        channel_tag=State.name,
    )
    witness = incident_regression_witness(recorded, incident)
    assert witness is not None

    ctx = _make_replay_context(prog, plc, State.name, 17)
    replay = build_replay_fn(
        cp,
        99,
        (),
        (ReplayStep(inputs=(), scans=12, kind="letrun"),),
        **ctx,
        zoom_channel_tag=State.name,
        zoom_target_value=3,
        terminal_letrun_role_tags=(State.name,),
        replay_watch_roles=(State.name,),
        regression_witness=witness,
    )

    outcome = replay(((DoorA.name, True), (DoorB.name, True)))

    assert outcome.accepted
    assert outcome.snapshot[State.name] == 6

    guarded = replay(
        (
            PilotRung(DoorA.name, True, State != 6),
            PilotRung(DoorB.name, True, State != 6),
        )
    )
    assert guarded.accepted
    assert guarded.snapshot[State.name] == 6


# ---------------------------------------------------------------------------
# Terminal let-run incident — channel register *maintained* at its held value
# ---------------------------------------------------------------------------


def _letrun_hold_program() -> tuple[Program, Timer, Any]:
    """``Phase`` sits at 6 (Execute).  A watchdog ejects it to 8 (Aborting) at its
    preset unless ``Guard`` is held.  ``Goal`` (the global target) is never
    reached inside the window, so replay must prove both that ``Phase`` stayed
    at 6 and that the recorded 6 -> 8 cause stopped executing.
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
    off). Judgment requires source preservation plus suppression of the exact
    recorded cause, so a channel override cannot masquerade as maintenance.
    """

    def _setup(self):
        prog, _tmr, _phase = _letrun_hold_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Phase": 6})
        plc.step()
        assert plc.state.tags["Phase"] == 6
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "Goal", True)
        # A recorded let-run step whose span covers the watchdog eject (~20 scans)
        # so the bad hold ejects inside the bounded coast — the recorded coast
        # span replaces the old departure-window bound.
        steps = [ReplayStep(inputs=(), scans=25, kind="letrun")]
        recorded = cp.fork()
        incident_session = CoastSession(recorded, kind="recorded-regression")
        incident_session.arm_pens(("Phase",))
        incident_session.dwell(25)
        incident = build_deviation_incident(
            anchor_scan=cp.state.scan_id,
            end_scan=recorded.state.scan_id,
            action=(),
            bearing=(("Phase", 6),),
            before_snap=dict(cp.state.tags),
            after_snap=dict(recorded.state.tags),
            timeline=incident_session.events,
            channel_tag="Phase",
        )
        witness = incident_regression_witness(recorded, incident)
        assert witness is not None
        return cp, steps, ctx, witness

    def _build(self, cp, steps, ctx, witness):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            **ctx,
            zoom_channel_tag="Phase",
            zoom_target_value=6,
            terminal_letrun_role_tags=("Phase",),
            replay_watch_roles=("Phase",),
            regression_witness=witness,
        )

    def test_letrun_accepts_hold_that_maintains_state(self):
        cp, steps, ctx, witness = self._setup()
        replay = self._build(cp, steps, ctx, witness)
        outcome = replay((("Guard", True),))  # keep the watchdog satisfied
        assert outcome.accepted
        assert outcome.snapshot["Phase"] == 6
        assert "suppressed its" in outcome.reason

    def test_letrun_rejects_hold_that_ejects(self):
        cp, steps, ctx, witness = self._setup()
        replay = self._build(cp, steps, ctx, witness)
        outcome = replay(())  # watchdog trips -> Phase ejects to 8
        assert not outcome.accepted
        assert outcome.snapshot["Phase"] == 8


def _letrun_global_program() -> tuple[Program, Timer]:
    """No macro-state register: ``Goal`` latches at the watchdog preset only if
    ``Hold`` keeps ``Alarm`` clear.  Exercises the let-run fallback that judges
    the *global* target when there is no channel register to maintain.
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


class TestTerminalLetrunNoChannelRegister:
    """A let-run with no recognized state machine (empty role tags, no channel
    register) falls back to judging the global target at the bounded point."""

    def _setup(self):
        prog, _tmr = _letrun_global_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        cp = plc.fork()
        ctx = _make_replay_context(prog, plc, "Goal", True)
        # A recorded let-run step; its span covers the watchdog eject (~10 scans)
        # so a missed global target is judged at the bounded point.
        steps = [ReplayStep(inputs=(), scans=15, kind="letrun")]
        return cp, steps, ctx

    def _build(self, cp, steps, ctx):
        return build_replay_fn(
            cp,
            99,
            {},
            steps,
            **ctx,
            terminal_letrun_role_tags=(),  # no recognized state machine
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
# _precise_cause — single cause()-chain walk from first departure
# ---------------------------------------------------------------------------


class TestPreciseCause:
    """_precise_cause: single cause walk to steerable input, early exit."""

    def test_guard_expiry_keeps_external_destinations_on_frontier(self):
        """Exact PILOT authorship must not erase the released field levers."""
        Door = Bool("Release_DoorClosed", external=True)
        LintDoor = Bool("Release_LintDoorClosed", external=True)
        DoorImage = Bool("Release_DoorImage")
        LintDoorImage = Bool("Release_LintDoorImage")
        Enter = Bool("Release_Enter", external=True)
        State = Int("Release_State", default=3)

        with Program() as prog:
            with Rung(Door):
                out(DoorImage)
            with Rung(LintDoor):
                out(LintDoorImage)
            with Rung(Enter):
                copy(6, State)
            with Rung(State == 6, Or(~DoorImage, ~LintDoorImage)):
                copy(10, State)

        plc = PLC(prog, dt=0.010)
        _set_rungs(
            plc,
            [
                PilotRung(Door.name, True, State != 6),
                PilotRung(LintDoor.name, True, State != 6),
            ],
        )
        plc.patch({Enter.name: True})
        plc.step()
        assert plc.state.tags[State.name] == 6
        before = dict(plc.state.tags)
        anchor = plc.state.scan_id

        plc.patch({Enter.name: False})
        plc.step()
        departure_scan = plc.state.scan_id
        assert plc.state.tags[State.name] == 10

        incident = DeviationIncident(
            anchor_scan=anchor,
            departure_scan=departure_scan,
            end_scan=departure_scan,
            action=(),
            bearing=((State.name, 6),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
            changed_tags=(Door.name, LintDoor.name, State.name),
            departures=(BearingDeparture(State.name, 6, departure_scan),),
            channel_tag=State.name,
        )

        hypothesis = _precise_cause(plc, incident, _make_ctx(prog, plc))

        assert hypothesis is not None
        assert set(hypothesis.holds) == {
            (Door.name, True),
            (LintDoor.name, True),
        }

    def test_newly_conductive_enablers_break_actual_writer(self):
        Enter = Bool("Precise_EnterExecute", external=True)
        Door = Bool("Precise_DoorClosed", external=True)
        LintDoor = Bool("Precise_LintDoorClosed", external=True)
        FirstScan = Bool("Precise_FirstScan", external=True)
        Execute = Bool("Precise_StateExecute")
        State = Int("Precise_State")
        Cmd = Int("Precise_Command")
        Requested = Int("Precise_StateRequested")
        Unrelated = Int("Precise_Unrelated")

        with Program() as prog:
            with Rung(State == 6):
                out(Execute)
            with Rung(Enter):
                copy(6, State)
            with Rung(Execute, Or(~Door, ~LintDoor)):
                copy(4, Cmd)
            with Rung(Cmd == 4):
                copy(10, Requested)
            with Rung(Requested != 0):
                copy(Requested, State)
                copy(0, Requested)
                copy(0, Cmd)
            # A steady false default elsewhere in the program is support for
            # nothing on the fired causal path and must never become a hold.
            with Rung(~FirstScan):
                copy(0, Unrelated)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Precise_EnterExecute": True})
        plc.step()
        before = dict(plc.state.tags)
        anchor = plc.state.scan_id
        plc.patch({"Precise_EnterExecute": False})
        plc.step()
        departure_scan = plc.state.scan_id

        incident = DeviationIncident(
            anchor_scan=anchor,
            departure_scan=departure_scan,
            end_scan=departure_scan,
            action=(),
            bearing=(("Precise_State", 6),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
            changed_tags=("Precise_State",),
            departures=(BearingDeparture("Precise_State", 6, departure_scan),),
            channel_tag="Precise_State",
        )

        hypothesis = _precise_cause(plc, incident, _make_ctx(prog, plc))

        assert hypothesis is not None
        assert hypothesis.kind == "precise-cause"
        assert set(hypothesis.holds) == {
            ("Precise_DoorClosed", True),
            ("Precise_LintDoorClosed", True),
        }
        assert all(tag != "Precise_FirstScan" for tag, _value in hypothesis.holds)
        assert "R3 fired" in hypothesis.detail
        assert "minimal conductive cut" in hypothesis.detail

    def test_disabled_out_writer_is_not_treated_as_suppressed(self):
        """OUT writes False when its guard is false; false is not suppression."""
        Door = Bool("OutCut_Door", external=True)
        Init = Bool("OutCut_Init", external=True)
        Image = Bool("OutCut_Image")
        State = Int("OutCut_State")

        with Program() as prog:
            with Rung(Door):
                out(Image)
            with Rung(Init):
                copy(6, State)
            with Rung(~Image):
                copy(10, State)

        plc = PLC(prog, dt=0.010)
        plc.patch({"OutCut_Door": True})
        plc.step()
        plc.patch({"OutCut_Init": True})
        plc.step()
        before = dict(plc.state.tags)
        plc.patch({"OutCut_Door": False, "OutCut_Init": False})
        plc.step()
        scan = plc.state.scan_id
        incident = DeviationIncident(
            anchor_scan=scan - 1,
            departure_scan=scan,
            end_scan=scan,
            action=(),
            bearing=(("OutCut_State", 6),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
            changed_tags=("OutCut_Door", "OutCut_Image", "OutCut_State"),
            departures=(BearingDeparture("OutCut_State", 6, scan),),
            channel_tag="OutCut_State",
        )

        hypotheses = _precise_causes(
            plc,
            incident,
            _make_ctx(prog, plc, steerable=frozenset({"OutCut_Door"})),
        )

        chain = plc.cause("OutCut_State", scan=scan)
        assert chain is not None
        assert any(
            step.transition.tag_name == "OutCut_Image" and step.transition.to_value is False
            for step in chain.steps
        )
        assert all(("OutCut_Door", False) not in h.holds for h in hypotheses)


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

        hyps = correct_enablers(plc, incident, ctx)
        # The latch's non-state guard (Guard=False) flips to True to break it.
        # This hand-built incident carries no recorded activation chain, so no
        # channel scope is invented.
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
            # Deliberately reverse latch insertion order: hypothesis order is
            # semantic/deterministic, not an artifact of snapshot construction.
            after_snap={"State": True, "G1": False, "G2": False, "A2": True, "A1": True},
            changed_tags=("A1", "A2"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        # Two per-latch hypotheses plus one conjunction clearing both.
        assert len(hyps) == 3
        per_latch = [h for h in hyps if len(h.holds) == 1]
        conjunction = [h for h in hyps if len(h.holds) == 2]

        def _pairs(holds):
            return {(h.dest, h.value) if isinstance(h, PilotRung) else h for h in holds}

        assert {frozenset(_pairs(h.holds)) for h in per_latch} == {
            frozenset({("G1", True)}),
            frozenset({("G2", True)}),
        }
        assert [h.sources[0] for h in per_latch] == ["A1", "A2"]
        assert len(conjunction) == 1
        assert _pairs(conjunction[0].holds) == {("G1", True), ("G2", True)}


# ---------------------------------------------------------------------------
# _done_boundary_hypotheses — complement-reset watchdog oscillation holds
# ---------------------------------------------------------------------------


class TestLivenessHypotheses:
    """_done_boundary_hypotheses: watchdog-driven oscillation holds.

    A complement-reset watchdog (``on_delay`` reset by an input edge) trips if
    the input sits at either polarity too long.  Only a *changing* input
    satisfies it — proposed structurally (no dwell) as a :class:`PilotRung`
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

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].kind == "liveness"
        (proposal,) = hyps[0].holds
        assert isinstance(proposal, PilotRung)
        assert (proposal.dest, proposal.value) == ("Sensor", False)

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

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert all(isinstance(r, PilotRung) for r in hyps[0].holds)
        assert {(r.dest, r.value) for r in hyps[0].holds} == {
            ("Sensor", False),
            ("Sensor", True),
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

        hyps = correct_enablers(plc, incident, ctx)
        proposed = {h.holds[0].dest for h in hyps}
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
        anchor_scan=anchor,
        end_scan=plc.state.scan_id,
        action=(),
        bearing=(("Running", True),),
        before_snap=before,
        after_snap=dict(plc.state.tags),
    )


class TestShaftRotateLiveness:
    """The shaft-rotate scenario end to end: a bit that must keep pulsing while a
    delay counts up, driven by a structurally-synthesized :class:`PilotRung`.
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
        # synthesizes an oscillating PilotRung — no dwell, no second round.
        prog = _shaft_rotate_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        ctx = _make_ctx(prog, plc)
        incident = _coast_holding_to_trip(plc, False)
        assert "SensorOffWD_Done" in incident.changed_tags

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert all(isinstance(r, PilotRung) for r in hyps[0].holds)
        assert {(r.dest, r.value) for r in hyps[0].holds} == {
            ("x_Rotate", True),
            ("x_Rotate", False),
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
        rungs = correct_enablers(plc, incident, ctx)[0].holds

        fresh = PLC(_shaft_rotate_program(), dt=0.010)
        fresh.step()
        _set_rungs(fresh, list(rungs))
        reached = _coast_holding_state(fresh, "Running", True, (), budget=200)
        assert reached.reached is True
        assert fresh.state.tags["Running"] is True
        assert fresh.state.tags["Fault"] is False


# ---------------------------------------------------------------------------
# Multi-read reset/advance conditions — coordinated-hold generalization
#
# A reset/advance guard that is a *conjunction* of inputs used to be skipped
# ("no single unambiguous lever").  ``correct_enablers`` now evaluates the real
# Condition over its reads' value spaces to find the minimal lever assignment
# producing the needed polarity, and proposes coordinated holds.
# ---------------------------------------------------------------------------


def _conj_reset_target_program() -> Program:
    """A watchdog reset by ``And(A, B)`` gates a run-delay toward ``Running``.

    Only holding *both* A and B True keeps the watchdog reset long enough for the
    200 ms ``RunDelay`` to complete — the coordinated-conjunction analogue of the
    complement-reset shaft-rotate scenario.
    """
    A = Bool("A", external=True)
    B = Bool("B", external=True)
    WD = Timer.clone("WD")
    RunDelay = Timer.clone("RunDelay")
    Fault = Bool("Fault")
    Running = Bool("Running")
    with Program() as prog:
        with Rung():
            on_delay(WD, 50, "ms").reset(And(A, B))
        with Rung(WD.Done):
            latch(Fault)
        with Rung(~Fault):
            on_delay(RunDelay, 200, "ms")
        with Rung(RunDelay.Done):
            out(Running)
    return prog


class TestMultiReadCorrections:
    """Conjunctive reset/advance guards yield coordinated multi-tag corrections."""

    def test_conjunction_reset_yields_coordinated_oscillation(self):
        # (a) Two-Bool conjunction reset: only A AND B True resets the watchdog,
        # so a single lever can never satisfy it — the correction pairs both.
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(WD, 30, "ms").reset(And(A, B))
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"A": True, "B": False})  # reset never satisfied -> WD counts
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(("A", True),),
            bearing=(("Err", False),),
            before_snap={"A": True, "B": False},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        assert hyps[0].kind == "liveness"
        held = {r.dest: r for r in hyps[0].holds}
        assert set(held) == {"A", "B"}
        for tag in ("A", "B"):
            assert isinstance(held[tag], PilotRung)
            assert held[tag].value is True

    def test_bool_int_conjunction_reset_resolved_via_choices(self):
        # (b) Bool+int conjunction: the int lever's domain comes from the tag's
        # declared choices, so ``Mode == 2`` resolves to the concrete value 2.
        Enable = Bool("Enable", external=True)
        Mode = Int("Mode", external=True, choices={1: "Idle", 2: "Run", 3: "Stop"})
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung():
                on_delay(WD, 30, "ms").reset(And(Enable, Mode == 2))
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True, "Mode": 1})  # Mode != 2 -> reset unsatisfied
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Err", False),),
            before_snap={"Enable": True, "Mode": 1},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        hyps = correct_enablers(plc, incident, ctx)
        assert len(hyps) == 1
        held = {r.dest: r for r in hyps[0].holds}
        assert set(held) == {"Enable", "Mode"}
        assert held["Enable"].value is True
        assert held["Mode"].value == 2

    def test_conjunction_with_undrivable_read_declined(self):
        # (c) A conjunct that resolves to no steerable driver makes the whole
        # coordinated hold undrivable — decline exactly as the single-read path did.
        A = Bool("A", external=True)
        Locked = Bool("Locked", readonly=True)  # a constant PILOT cannot steer
        Internal = Bool("Internal")
        WD = Timer.clone("WD")
        Err = Bool("Err")
        with Program() as prog:
            with Rung(Locked):
                out(Internal)  # Internal only rises via the unsteerable Locked
            with Rung():
                on_delay(WD, 30, "ms").reset(And(A, Internal))
            with Rung(WD.Done):
                out(Err)

        plc = PLC(prog, dt=0.010)
        plc.patch({"A": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["WD_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Err", False),),
            before_snap={"A": True, "Internal": False},
            after_snap=dict(plc.state.tags),
            changed_tags=("WD_Done", "Err"),
            departures=(),
        )

        assert correct_enablers(plc, incident, ctx) == []

    def test_conjunction_advance_yields_single_lever_freeze(self):
        # (d) A conjunction *advance* stops as soon as ONE conjunct breaks, so the
        # cannot-hold correction is a single cheapest lever — not both.
        run1 = Bool("run1", external=True)
        run2 = Bool("run2", external=True)
        T = Timer.clone("T")
        Out = Bool("Out")
        with Program() as prog:
            with Rung(And(run1, run2)):
                on_delay(T, 50, "ms")
            with Rung(T.Done):
                out(Out)

        plc = PLC(prog, dt=0.010)
        plc.patch({"run1": True, "run2": True})
        for _ in range(8):
            plc.step()
        assert plc.state.tags["T_Done"] is True

        ctx = _make_ctx(prog, plc)
        incident = DeviationIncident(
            anchor_scan=0,
            departure_scan=None,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Out", False),),
            before_snap={"run1": True, "run2": True},
            after_snap=dict(plc.state.tags),
            changed_tags=("T_Done", "Out"),
            departures=(),
        )

        done_boundary = [
            h for h in correct_enablers(plc, incident, ctx) if h.kind == "done-boundary"
        ]
        assert len(done_boundary) == 1
        ((tag, value),) = done_boundary[0].holds
        assert tag in {"run1", "run2"}
        assert value is False

    def test_coordinated_conjunction_hold_reaches_target(self):
        # End to end: the synthesized coordinated pair, installed together on a
        # coast, keeps the And(A, B) watchdog reset so RunDelay reaches Running.
        prog = _conj_reset_target_program()
        plc = PLC(prog, dt=0.010)
        plc.step()
        anchor = plc.state.scan_id
        before = dict(plc.state.tags)
        for _ in range(80):
            plc.force("A", True)
            plc.force("B", False)  # one conjunct low -> watchdog trips
            plc.step()
            if plc.state.tags.get("WD_Done"):
                break
        incident = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("Running", True),),
            before_snap=before,
            after_snap=dict(plc.state.tags),
        )
        assert "WD_Done" in incident.changed_tags

        ctx = _make_ctx(prog, plc)
        rungs = correct_enablers(plc, incident, ctx)[0].holds
        assert {r.dest for r in rungs} == {"A", "B"}

        fresh = PLC(_conj_reset_target_program(), dt=0.010)
        fresh.step()
        _set_rungs(fresh, list(rungs))
        reached = _coast_holding_state(fresh, "Running", True, (), budget=300)
        assert reached.reached is True
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
            rungs=[],
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
            rungs=[],
            resting={"Command": False},
            edge_tags={"Command"},
            scan_budget=50,
        )
        # Sealing Hold=True keeps Out latched across the edge release — the
        # retry key differs from the (reverted) pre key, so the hold is kept.
        assert ("Hold", True) in result.confirmed_holds
        assert result.retry_fork is not None


# ---------------------------------------------------------------------------
# Generalized antagonist dispatch — any causally-implicated clobbering writer
#
# The old excursion path only recognized ``ResetInstruction`` antagonists.  The
# dispatch is now by causal implication (``cause()``) + producibility, so a plain
# clobbering ``copy`` is suppressed by forcing its guard FALSE — and a live-word
# guard escalates to the skiff.  Both flow through the same replay-retry gate.
# ---------------------------------------------------------------------------


def _run_excursion(
    prog: Program,
    *,
    setup_patch: dict[str, Any],
    action_tag: str,
    stateful: tuple[str, ...],
    extra_resting: dict[str, Any] | None = None,
):
    """Drive one pulse-then-revert excursion and return investigate_excursion inputs.

    ``work`` rests at the pre-state; ``fork`` reproduces the pulse where the
    stateful register moved (post_pulse) then was clobbered back after settling.
    """
    work = PLC(prog, dt=0.010)
    work.patch(setup_patch)
    work.step()
    cfg = _StateKeyConfig(
        stateful_names=stateful,
        done_specs=(),
        threshold_vector_specs=(),
        acc_indices=frozenset(),
    )
    pre_snap = dict(work.state.tags)
    pre_key = _pilot_state_key(pre_snap, cfg)

    fork = work.fork()
    fork.patch({action_tag: False})
    fork.step()
    fork.patch({action_tag: True})
    fork.step()
    post_pulse_snap = dict(fork.state.tags)
    for _ in range(4):
        fork.step()

    pdg = build_program_graph(prog)
    steerable = frozenset(compute_steerable(pdg, work._known_tags_by_name, prog))
    resting = {action_tag: False, **(extra_resting or {})}
    return work, fork, pre_snap, post_pulse_snap, pre_key, cfg, steerable, pdg, resting


def _clobber_copy_program() -> Program:
    """A non-Reset clobbering copy with a compound int guard.

    ``State`` is set to 5 on a rising ``Command`` edge, but ``copy(0, State)``
    gated by ``And(Internal, Mode == 2)`` clobbers it back every scan.
    ``Internal`` rides an unsteerable ``readonly`` latch, so the only drivable
    lever is the int ``Mode``.  The old bool-only fallback cannot flip an int
    comparison (this excursion is *unresolved* today); the generalized dispatch
    suppresses the copy by forcing its guard FALSE via the int-domain forcing
    enumeration (``Mode -> 1``), a value the ``copy`` can never turn into a 5.
    """
    Command = Bool("Command", external=True)
    Locked = Bool("Locked", readonly=True)
    Internal = Bool("Internal")
    Mode = Int("Mode", external=True, choices={1: "Idle", 2: "Run", 3: "Stop"})
    State = Int("State")
    with Program() as prog:
        with Rung(Locked):
            out(Internal)
        with Rung(And(Internal, Mode == 2)):
            copy(0, State)
        with Rung(rise(Command)):
            copy(5, State)
    return prog


def _liveword_clobber_program() -> Program:
    """A clobbering copy gated by a genuinely-live (calc-computed) word.

    ``Sel`` selects a raw mask (4 or 0), ``Mask`` is ``RawMask & 4`` — a *calc*
    output, so its finite domain is unreadable and the guard-force enumeration
    **punts**.  The clobber ``copy(0, State)`` fires while ``Mask != 0``.  Only the
    skiff can find the suppressing lever: a bounded isolated probe holding the
    condition-read Bool ``Sel`` False clears the mask, so the antagonist stops
    firing — a nomination the replay-retry gate then confirms.
    """
    Command = Bool("Command", external=True)
    Sel = Bool("Sel", external=True)
    RawMask = Int("RawMask")
    Mask = Int("Mask")
    State = Int("State")
    with Program() as prog:
        with Rung(Sel):
            copy(4, RawMask)
        with Rung(~Sel):
            copy(0, RawMask)
        with Rung():
            calc(RawMask & 4, Mask)
        with Rung(Mask != 0):
            copy(0, State)
        with Rung(rise(Command)):
            copy(5, State)
    return prog


class TestGeneralizedAntagonistExcursion:
    """investigate_excursion suppresses any causally-implicated clobbering writer,
    not just ``ResetInstruction`` — via guard-force enumeration, with a skiff
    escalation for a live-word guard.  Every hold rides the existing retry gate."""

    def test_non_reset_copy_clobber_now_corrected(self):
        # Compound int guard: unresolved under the old ResetInstruction dispatch,
        # corrected by forcing the copy's guard FALSE (Mode -> 1).
        prog = _clobber_copy_program()
        (work, fork, pre, post, pre_key, cfg, steerable, pdg, resting) = _run_excursion(
            prog,
            setup_patch={"Command": False, "Mode": 2, "Locked": True},
            action_tag="Command",
            stateful=("State",),
        )
        assert post["State"] == 5  # pulse established the value
        assert fork.state.tags["State"] == 0  # then it was clobbered back

        result = investigate_excursion(
            work,
            fork,
            pre,
            post,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            rungs=[],
            resting=resting,
            edge_tags={"Command"},
            scan_budget=50,
            pdg=pdg,
            program=prog,
        )
        assert result.reverted == ["State"]
        assert ("Mode", 1) in result.confirmed_holds
        assert result.retry_fork is not None
        # The suppression preserved the pulse-established value across the settle.
        assert result.retry_fork.state.tags["State"] == 5

    def test_live_word_guard_uses_skiff_probe(self):
        # The clobber's guard reads a calc-computed word: guard-force enumeration
        # punts, and the skiff's isolated probe nominates the condition-read Bool
        # Sel=False, which the retry gate confirms.
        prog = _liveword_clobber_program()
        (work, fork, pre, post, pre_key, cfg, steerable, pdg, resting) = _run_excursion(
            prog,
            setup_patch={"Command": False, "Sel": True},
            action_tag="Command",
            stateful=("State",),
            extra_resting={"Sel": False},
        )
        assert post["State"] == 5
        assert fork.state.tags["State"] == 0

        result = investigate_excursion(
            work,
            fork,
            pre,
            post,
            pre_key,
            [("Command", True)],
            cfg=cfg,
            steerable=steerable,
            rungs=[],
            resting=resting,
            edge_tags={"Command"},
            scan_budget=50,
            pdg=pdg,
            program=prog,
        )
        assert result.reverted == ["State"]
        assert ("Sel", False) in result.confirmed_holds
        assert result.retry_fork is not None
        assert result.retry_fork.state.tags["State"] == 5


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


class TestFirstTimelineDeparture:
    """``_first_timeline_departure`` reads the departure scan straight off the
    recorded receipt timeline — the pen mark IS the departure scan, never a
    history re-scan."""

    def test_finds_first_transition_off_value(self):
        timeline = (
            BumpEvent("pen", "pen", 5, (("B", False, True),)),
            BumpEvent("pen", "pen", 9, (("B", True, False),)),
        )
        assert _first_timeline_departure(timeline, "B", False) == 5

    def test_departure_is_relative_to_the_queried_value(self):
        # A single True -> False transition is a departure off True (scan 3),
        # not off False (which it lands on).
        timeline = (BumpEvent("pen", "pen", 3, (("B", True, False),)),)
        assert _first_timeline_departure(timeline, "B", True) == 3
        assert _first_timeline_departure(timeline, "B", False) is None

    def test_returns_the_first_of_several(self):
        timeline = (
            BumpEvent("pen", "pen", 4, (("B", False, True),)),
            BumpEvent("pen", "pen", 8, (("B", False, True),)),
        )
        assert _first_timeline_departure(timeline, "B", False) == 4

    def test_no_matching_tag_returns_none(self):
        timeline = (BumpEvent("pen", "pen", 7, (("A", False, True),)),)
        assert _first_timeline_departure(timeline, "B", False) is None

    def test_empty_timeline_returns_none(self):
        assert _first_timeline_departure((), "B", False) is None


def _oscillating_done_program() -> Program:
    """A complement-reset timer whose Done bit *pulses* — False -> True -> False
    each period — plus a latch that fires (and stays) the first time it does.
    The pens must record both Done transitions and the latch's single rise."""
    T = Timer.clone("T")
    Latched = Bool("Latched")
    with Program() as prog:
        with Rung(~T.Done):
            on_delay(T, 30, "ms")  # Done oscillates: ~3 scans off, 1 scan on
        with Rung(T.Done):
            latch(Latched)  # the first Done rise latches Latched permanently
    return prog


class TestPens:
    """CoastSession pens record mid-coast transitions onto the timeline so a
    fire-then-reset watchdog pulse is two recorded events, and incident
    construction reads changed tags + departure scans straight off them."""

    def test_pens_capture_fire_and_reset_onto_the_timeline(self):
        plc = PLC(_oscillating_done_program(), dt=0.010)
        session = CoastSession(plc, kind="test")
        session.arm_pens(("T_Done", "Latched"))
        session.dwell(20)

        pens = [e for e in session.events if e.kind == "pen"]
        rises = [
            e.scan
            for e in pens
            for t, b, a in e.transitions
            if t == "T_Done" and b is False and a is True
        ]
        falls = [
            e.scan
            for e in pens
            for t, b, a in e.transitions
            if t == "T_Done" and b is True and a is False
        ]
        # Both edges of the pulse landed as recorded pen marks with exact scans.
        assert rises and falls
        latched_scan = next(
            e.scan for e in pens for t, _b, a in e.transitions if t == "Latched" and a is True
        )

        # The Done bit fired and reset inside the window, so its endpoint diff is
        # a net no-op (before == after == False) — only the timeline carries it.
        incident = build_deviation_incident(
            anchor_scan=0,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("T_Done", False), ("Latched", False)),
            before_snap={"T_Done": False, "Latched": False},
            after_snap={"T_Done": False, "Latched": True},
            timeline=tuple(session.events),
        )
        assert "T_Done" in incident.changed_tags  # recovered from the timeline
        assert "Latched" in incident.changed_tags
        # The departure scan comes off the timeline, not a history re-diff.
        dep = {d.tag: d.scan for d in incident.departures}
        assert dep["Latched"] == latched_scan
        assert incident.departure_scan == latched_scan


class TestBuildDeviationIncident:
    def test_captures_changes_and_departures(self):
        plc = PLC(_change_program(), dt=0.010)
        anchor = plc.state.scan_id
        plc.step()
        plc.patch({"A": True})
        plc.step()
        # The recorded evidence: B departed False -> True the scan A latched it.
        timeline = (BumpEvent("pen", "pen", plc.state.scan_id, (("B", False, True),)),)
        incident = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(("A", True),),
            bearing=(("B", False),),
            before_snap={"B": False},
            after_snap=dict(plc.state.tags),
            timeline=timeline,
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
            anchor_scan=anchor,
            end_scan=plc.state.scan_id,
            action=(),
            bearing=(("B", False),),
            before_snap={"B": False},
            after_snap=dict(plc.state.tags),
        )
        assert incident.departures == ()
        assert incident.departure_scan is None

    def test_program_does_not_narrow_factual_changed_tags(self):
        """Incident evidence stays complete when a Program is supplied.

        Timer consumers select their Done/accumulator profile tags locally;
        constructing the incident must not erase unrelated recorded movement.
        """
        from pyrung.core.analysis.pilot.advance import iter_advance_owners

        prog, _tmr = _watchdog_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        anchor = plc.state.scan_id
        for _ in range(20):
            plc.step()  # timer fires: Tmr.Done, Alarm, Target all change
        assert plc.state.tags["Alarm"] is True

        before = dict(plc.history.at(anchor).tags)
        after = dict(plc.state.tags)
        end = plc.state.scan_id
        dones = {
            owner.profile.done.name
            for owner in iter_advance_owners(prog)
            if owner.profile.done is not None
        }
        # Recorded evidence: the watchdog's Done bit fired in the window.
        timeline = tuple(
            BumpEvent("pen", "pen", end, ((name, False, after.get(name)),))
            for name in sorted(dones)
            if after.get(name) is True
        )
        full = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=end,
            action=(),
            bearing=(("Target", True),),
            before_snap=before,
            after_snap=after,
            timeline=timeline,
        )
        restricted = build_deviation_incident(
            anchor_scan=anchor,
            end_scan=end,
            action=(),
            bearing=(("Target", True),),
            before_snap=before,
            after_snap=after,
            timeline=timeline,
            program=prog,
        )

        profile_tags = {
            tag.name
            for owner in iter_advance_owners(prog)
            for tag in (owner.profile.done, owner.profile.accumulator)
            if tag is not None
        }
        # Program metadata does not change the factual incident.
        assert "Alarm" in full.changed_tags
        assert restricted.changed_tags == full.changed_tags
        # The watchdog's Done bit actually fired in the window, so it survives.
        assert dones & set(restricted.changed_tags) & profile_tags


# ---------------------------------------------------------------------------
# Terminal coast receipts are ordinary durable Compass knowledge.
# ---------------------------------------------------------------------------


def test_terminal_coast_receipt_is_typed_navigation_knowledge() -> None:
    from pyrung.core.analysis.pilot.compass import CoastObservation, Compass

    key = ("world-key",)
    compass, changed = Compass().apply((CoastObservation(key, "quiescent"),))
    assert changed
    assert compass.knowledge.coast_receipt(key) == "quiescent"
