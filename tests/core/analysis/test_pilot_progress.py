"""Tests for pilot progress — trend monitoring, checkpoints, regression recovery.

Coverage targets:
- _monitor_trend: checkpoint creation, flat checkpoint, frontier baseline,
  regression detection, letrun-ejection interception
- _investigate_and_revert: revert mechanics, nogood recording, hold reinstatement,
  investigation trigger

Strategy note
-------------
The checkpoint *stream* is exercised end-to-end through ``pilot_events`` on a
multi-step program (``TestCheckpointStream``).  The individual ``_monitor_trend``
branches — flat checkpoint, frontier, regression, letrun-ejection — cannot be
forced deterministically from a small program (PILOT's gates reject worsening
moves; real regressions arise from AMBIENT_DRIFT in large state machines like the
burner).  Those branches are therefore driven with controlled ``_TrialResult``
objects over real PLC forks, which is both deterministic and precise.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from pyrsistent import pvector

from pyrung import And, Bool, Or, Program, Rung, latch, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.detour import DepartureVerdict
from pyrung.core.analysis.pilot.gauge import Gauge, GaugeComponent, GaugeReceipt
from pyrung.core.analysis.pilot.navigation import BearingObjective, TargetSpec
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    Outcome,
    ProgressEffect,
    TrialAssessment,
)
from pyrung.core.analysis.pilot.progress import (
    _anchor_bearing_receipt,
    _anchor_frame_receipt,
    _apply_departure_decision,
    _channel_recovery_origin,
    _deviation_bearing,
    _investigate_and_revert,
    _monitor_trend,
    _open_pending_departure,
)
from pyrung.core.analysis.pilot.types import (
    DepartureAction,
    DepartureDecision,
    PendingDeparture,
    PilotEvent,
    _Checkpoint,
    _CommittedAct,
    _PilotState,
    _Step,
    _StepContext,
    _TrialResult,
    _World,
)
from pyrung.core.analysis.steerable import compute_steerable
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Fixtures — controlled trial / state / frame builders
# ---------------------------------------------------------------------------


def _oneshot_plc() -> PLC:
    """A trivial PLC whose forks stand in for checkpoint / work forks."""
    A = Bool("A", external=True)
    B = Bool("B")
    with Program() as prog:
        with Rung(A):
            out(B)
    return PLC(prog, dt=0.010)


def _cp(key: Any, fork: PLC, trend: int, frontier: tuple = ()) -> _Checkpoint:
    """A checkpoint pointing at a world that wraps ``fork`` (empty step path)."""
    return _Checkpoint(
        key,
        _World(
            work=fork,
            committed_acts=pvector([]),
            best_trend=trend,
            rungs=pvector([]),
            dwell_scans=0,
        ),
        trend,
        BearingObjective(TargetSpec("State", 17), frontier),
    )


def _make_state(best_trend: int, checkpoints: list, **over: Any) -> _PilotState:
    steps = tuple(over.pop("steps", ()))
    committed_acts = tuple(over.pop("committed_acts", ())) or tuple(
        _CommittedAct(
            steps=(step,),
            context=_StepContext(candidate=dict(step.inputs)),
        )
        for step in steps
    )
    world = _World(
        work=over.pop("work", None) or _oneshot_plc(),
        committed_acts=pvector(committed_acts),
        best_trend=best_trend,
        rungs=pvector(over.pop("rungs", [])),
        dwell_scans=over.pop("dwell_scans", 0),
    )
    base: dict[str, Any] = {
        "world": world,
        "key_config": None,
        "seen_keys": set(),
        "checkpoints": checkpoints,
        "watch_tags": [],
    }
    base.update(over)
    return _PilotState(**base)


def _make_trial(trend: int, outcome: Outcome, **over: Any) -> _TrialResult:
    base: dict[str, Any] = {
        "fork": _oneshot_plc(),
        "scan_before": 0,
        "candidate": {},
        "applied": (),
        "before_snap": {},
        "post_pulse_snap": {},
        "fork_snap": {},
        "observe_label": "pulse",
        "bearing_objective": BearingObjective(TargetSpec("State", 17)),
        "new_key": ("k",),
        "trend": trend,
        "outcome": outcome,
    }
    base.update(over)
    return _TrialResult(**base)


def _pending_departure(
    state: _PilotState,
    *,
    progress_mark: tuple[tuple[str, Any], ...] = (),
    expires_at: int = 2000,
    opening_progress: GaugeReceipt | None = None,
    from_value: Any = 9,
    rollback_owner: Any = None,
) -> PendingDeparture:
    return PendingDeparture(
        channel_tag="State",
        from_value=from_value,
        progress_mark=progress_mark,
        rollback_owner=rollback_owner or state.checkpoints[-1].owner,
        expires_at=expires_at,
        opening_progress=opening_progress or GaugeReceipt((), (), "unknown"),
    )


def _frame() -> SimpleNamespace:
    return SimpleNamespace(
        snap={},
        tree=SimpleNamespace(
            children=(),
            satisfied=True,
            is_steerable=False,
            ordered_actions=lambda: [],
        ),
        key=("f",),
        distance_before=5,
    )


# ---------------------------------------------------------------------------
# Trend monitoring — checkpoints
# ---------------------------------------------------------------------------


class TestCheckpoints:
    """Trend improvement creates checkpoints; flat CONFIRMED does too."""

    def test_trend_improvement_creates_checkpoint(self):
        state = _make_state(best_trend=5, checkpoints=[])
        objective = BearingObjective(
            TargetSpec("Completed", True),
            (("State", 17),),
        )
        trial = _make_trial(3, Outcome.CONFIRMED, bearing_objective=objective)
        events = tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))

        assert [e.kind for e in events] == ["trend_checkpoint"]
        assert events[0].data["trend"] == 3
        assert events[0].data["checkpoint_count"] == 1
        assert events[0].data.get("flat") is None
        assert state.best_trend == 3
        assert len(state.checkpoints) == 1
        assert state.checkpoints[0].objective is objective

    def test_flat_confirmed_creates_checkpoint(self):
        # Equal trend, but a CONFIRMED outcome still banks a checkpoint.
        state = _make_state(best_trend=3, checkpoints=[_cp(("c",), _oneshot_plc(), 3)])
        trial = _make_trial(3, Outcome.CONFIRMED)
        events = tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))

        assert [e.kind for e in events] == ["trend_checkpoint"]
        assert events[0].data["flat"] is True
        assert len(state.checkpoints) == 2
        assert state.best_trend == 3  # unchanged on a flat checkpoint

    def test_frontier_preserves_baseline(self):
        # A FRONTIER knowingly exposes deeper prerequisites (worse trend) — the
        # pre-frontier checkpoint and high-water mark must survive.
        state = _make_state(best_trend=3, checkpoints=[_cp(("c",), _oneshot_plc(), 3)])
        trial = _make_trial(8, Outcome.FRONTIER)
        events = tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))

        assert [e.kind for e in events] == ["trend_checkpoint"]
        assert events[0].data["frontier"] is True
        assert events[0].data["baseline_trend"] == 3
        assert state.best_trend == 3  # NOT advanced to the worse frontier trend
        assert len(state.checkpoints) == 1  # pre-frontier checkpoint preserved

    def test_confirmed_route_landing_keeps_its_source_checkpoint(self):
        state = _make_state(best_trend=2, checkpoints=[_cp(("idle",), _oneshot_plc(), 2)])
        objective = BearingObjective(
            TargetSpec("Completed", True),
            (("State", 17),),
        )
        trial = _make_trial(
            15,
            Outcome.CONFIRMED,
            bearing_objective=objective,
            zoom_channel_tag="State",
            zoom_target_value=3,
            fork_snap={"State": 3},
        )

        events = tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))

        assert [event.kind for event in events] == ["trend_checkpoint"]
        assert events[0].data["channel"] == "State"
        assert events[0].data["channel_value"] == 3
        assert events[0].data["baseline_trend"] == 2
        assert events[0].data["provisional"] is True
        assert state.best_trend == 15
        assert len(state.checkpoints) == 1  # preserve the pre-route rollback receipt

    def test_confirmed_route_edge_captures_its_immediate_source_world(self):
        state = _make_state(best_trend=2, checkpoints=[_cp(("aborted",), _oneshot_plc(), 9)])
        source_scan = state.work.state.scan_id
        frame = _frame()
        frame.key = ("idle",)
        frame.distance_before = 2
        frame.snap["State"] = 4
        frame.tree.children = ()
        frame.tree.satisfied = True
        objective = BearingObjective(
            TargetSpec("Completed", True),
            (("State", 17),),
        )
        trial = _make_trial(
            15,
            Outcome.CONFIRMED,
            bearing_objective=objective,
            zoom_channel_tag="State",
            zoom_target_value=3,
            fork_snap={"State": 3},
        )

        _anchor_bearing_receipt(trial, frame, state)

        assert len(state.checkpoints) == 2
        receipt = state.checkpoints[-1]
        assert receipt.key == ("idle",)
        assert receipt.trend == 2
        assert receipt.world.work.state.scan_id == source_scan
        assert receipt.objective is objective

    def test_channel_recovery_origin_owns_the_whole_tenure(self):
        entered = _oneshot_plc()
        entered._state = entered.state.with_tags({"A": True})
        entered.step()
        nested = entered.fork()
        nested.step()
        later = nested.fork()
        later.step()
        state = _make_state(
            best_trend=1,
            checkpoints=[
                _cp(("before",), _oneshot_plc(), 4),
                _cp(("entered",), entered, 3),
                _cp(("timer",), nested, 2),
                _cp(("step",), later, 1),
            ],
        )
        trial = _make_trial(1, Outcome.AMBIENT_DRIFT, scan_before=later.state.scan_id)
        frame = _frame()
        frame.snap = {"A": True, "FrameOnly": True}

        origin = _channel_recovery_origin(state, trial, frame, "A", True)

        assert origin.checkpoint_owner is state.checkpoints[1].owner
        assert origin.anchor_scan == entered.state.scan_id
        assert origin.before_snap["A"] is True
        assert "FrameOnly" not in origin.before_snap

    def test_current_tenure_receipt_uses_the_coast_frame(self):
        entered = _oneshot_plc()
        entered._state = entered.state.with_tags({"A": True})
        entered.step()
        state = _make_state(
            best_trend=1,
            checkpoints=[_cp(("entered",), entered, 1)],
        )
        trial = _make_trial(1, Outcome.AMBIENT_DRIFT, scan_before=entered.state.scan_id)
        frame = _frame()
        frame.snap = {"A": True, "FrameOnly": True}

        origin = _channel_recovery_origin(state, trial, frame, "A", True)

        assert origin.checkpoint_owner is state.checkpoints[0].owner
        assert origin.anchor_scan == trial.scan_before
        assert origin.before_snap == frame.snap


def test_improved_trace_distance_does_not_promote_pending_departure():
    """Fewer open trace leaves are a local checkpoint, not banked work."""
    state = _make_state(best_trend=5, checkpoints=[_cp(("src",), _oneshot_plc(), 5)])
    state.pending_departure = _pending_departure(
        state,
        progress_mark=(("Step", 101),),
    )
    trial = _make_trial(3, Outcome.CONFIRMED)
    ctx = SimpleNamespace(target_tag="State", target_value=17, target_predicate=None)

    events = tuple(_monitor_trend(trial, _frame(), state, ctx))

    assert [e.kind for e in events] == ["trend_checkpoint"]
    assert events[0].data["provisional"] is True
    assert state.pending_departure is not None
    assert len(state.checkpoints) == 2
    assert state.best_trend == 3


def test_pending_departure_marks_the_settled_landing_not_inflight_motion():
    source = _oneshot_plc()
    source._state = source.state.with_tags({"State": 6, "Step": 105})
    settled = source.fork()
    settled._state = settled.state.with_tags({"State": 11, "Step": 107})
    checkpoint = _cp(("source",), source, 5)
    gauge = Gauge((GaugeComponent("Step", "stepper", 1),))
    state = _make_state(
        best_trend=5,
        checkpoints=[checkpoint],
        work=source,
        gauge=gauge,
    )
    trial = _make_trial(
        5,
        Outcome.AMBIENT_DRIFT,
        before_snap={"State": 6, "Step": 105},
        fork_snap={"State": 10, "Step": 105},
        zoom_channel_tag="State",
        zoom_target_value=16,
    )
    verdict = DepartureVerdict(
        decision="continue",
        reason="clean continuation",
        settled_fork=settled,
        settled_value=11,
        settle_scans=1,
        reentry_value=6,
        route=(11, 12, 6),
        progress=gauge.receipt(trial.before_snap, settled.state.tags),
    )

    events = _open_pending_departure(
        verdict,
        trial,
        state,
        SimpleNamespace(max_scans=10_000),
        "State",
    )

    assert state.pending_departure is not None
    assert state.pending_departure.progress_mark == (("Step", 107),)
    assert events[0].data["gauge_at_source"] == (("Step", 107),)


def test_pending_expiry_without_saved_progress_rolls_back():
    """A pending departure that never earned anything — no gauge advance, no saved
    checkpoint — expires by rolling back to its boundary without a nogood."""
    checkpoint = _cp(("src",), _oneshot_plc(), 5)
    state = _make_state(best_trend=5, checkpoints=[checkpoint])
    state.pending_departure = _pending_departure(
        state,
        progress_mark=(("Step", 101),),
        expires_at=0,  # already past — the attempt is out of budget
    )
    trial = _make_trial(5, Outcome.CONFIRMED)
    ctx = SimpleNamespace(target_tag="State", target_value=17, target_predicate=None)

    events = tuple(_monitor_trend(trial, _frame(), state, ctx))

    assert [e.kind for e in events] == ["provisional_expired"]
    assert state.pending_departure is None
    assert len(state.checkpoints) == 1  # rolled back to the boundary
    assert state.best_trend == 5


def test_pending_expiry_restores_the_current_checkpoint_artifact():
    """Correction lifecycle may re-key a receipt without changing its owner."""
    checkpoint = _cp(("source",), _oneshot_plc(), 5)
    state = _make_state(best_trend=5, checkpoints=[checkpoint])
    state.pending_departure = _pending_departure(state, expires_at=0)
    corrected = replace(checkpoint, key=("corrected",), trend=4)
    state.checkpoints[0] = corrected
    trial = _make_trial(5, Outcome.CONFIRMED)
    ctx = SimpleNamespace(target_tag="State", target_value=17, target_predicate=None)

    events = tuple(_monitor_trend(trial, _frame(), state, ctx))

    assert [event.kind for event in events] == ["provisional_expired"]
    assert state.checkpoints == [corrected]
    assert state.best_trend == 4


def test_same_key_checkpoint_refresh_preserves_saved_progress_ownership():
    rollback = _cp(("source",), _oneshot_plc(), 5)
    saved = _cp(("saved",), _oneshot_plc(), 4)
    state = _make_state(best_trend=4, checkpoints=[rollback, saved])
    state.pending_departure = replace(
        _pending_departure(state, expires_at=0),
        rollback_owner=rollback.owner,
        saved_progress_owner=saved.owner,
    )
    frame = _frame()
    frame.key = saved.key
    frame.distance_before = 3

    _anchor_frame_receipt(frame, state, state.checkpoints[-1].objective)

    refreshed = state.checkpoints[-1]
    assert refreshed is not saved
    assert refreshed.owner is saved.owner
    events = tuple(
        _monitor_trend(
            _make_trial(4, Outcome.CONFIRMED),
            frame,
            state,
            SimpleNamespace(target_tag="State", target_value=17, target_predicate=None),
        )
    )
    assert [event.kind for event in events] == ["provisional_expired"]
    assert state.checkpoints[-1] is refreshed


def test_pending_regression_recovers_from_refreshed_saved_progress(monkeypatch):
    """A later incident cannot erase progress banked inside an outer corridor."""
    rollback = _cp(("source",), _oneshot_plc(), 5)
    saved = _cp(("saved",), _oneshot_plc(), 4)
    saved_work = saved.world.work.fork()
    saved_work.patch({"A": True})
    saved_work.step()
    refreshed = replace(
        saved,
        key=("saved-refreshed",),
        world=saved.world.set(work=saved_work, best_trend=3),
        trend=3,
    )
    current_work = saved_work.fork()
    current_work.step()
    current_step = _Step(
        inputs={"A": False},
        scan_before=saved_work.state.scan_id,
        scan_after=current_work.state.scan_id,
    )
    state = _make_state(
        best_trend=3,
        checkpoints=[rollback, refreshed],
        work=current_work,
        steps=[current_step],
    )
    state.pending_departure = replace(
        _pending_departure(state),
        rollback_owner=rollback.owner,
        saved_progress_owner=saved.owner,
    )
    trial = _make_trial(
        8,
        Outcome.CONFIRMED,
        fork=current_work,
        fork_snap=dict(current_work.state.tags),
        chase_regression_causes=False,
    )
    captured: dict[str, Any] = {}

    def _capture(*args, origin, **kwargs):
        captured["origin"] = origin
        captured["acts"] = tuple(state.committed_acts)
        return _investigate_and_revert(*args, origin=origin, **kwargs)

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress._investigate_and_revert",
        _capture,
    )

    events = _apply_departure_decision(
        DepartureDecision(DepartureAction.REGRESS, "behind"),
        trial,
        _frame(),
        state,
        SimpleNamespace(target_tag="State"),
    )

    assert events is not None
    assert [event.kind for event in events] == ["provisional_regressed", "trend_regression"]
    assert captured["origin"].checkpoint_owner is saved.owner
    assert captured["origin"].anchor_scan == saved_work.state.scan_id
    assert captured["origin"].before_snap == dict(saved_work.state.tags)
    assert captured["acts"]
    assert state.checkpoints == [rollback, refreshed]
    assert state.pending_departure is None
    assert state.best_trend == 3
    assert state.work.state.scan_id == saved_work.state.scan_id
    assert dict(state.work.state.tags) == dict(saved_work.state.tags)


def test_pending_regression_without_saved_progress_uses_rollback_owner(monkeypatch):
    rollback_work = _oneshot_plc()
    rollback_work.step()
    rollback = _cp(("source",), rollback_work, 5)
    current_work = rollback_work.fork()
    current_work.step()
    state = _make_state(
        best_trend=5,
        checkpoints=[rollback],
        work=current_work,
    )
    state.pending_departure = _pending_departure(state)
    trial = _make_trial(
        8,
        Outcome.CONFIRMED,
        fork=current_work,
        fork_snap=dict(current_work.state.tags),
        chase_regression_causes=False,
    )
    captured = []

    def _capture(*args, origin, **kwargs):
        captured.append(origin)
        return _investigate_and_revert(*args, origin=origin, **kwargs)

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress._investigate_and_revert",
        _capture,
    )

    events = _apply_departure_decision(
        DepartureDecision(DepartureAction.REGRESS, "behind"),
        trial,
        _frame(),
        state,
        SimpleNamespace(target_tag="State"),
    )

    assert events is not None
    assert captured[0].checkpoint_owner is rollback.owner
    assert captured[0].anchor_scan == rollback_work.state.scan_id
    assert state.checkpoints == [rollback]
    assert state.best_trend == 5
    assert state.work.state.scan_id == rollback_work.state.scan_id


def test_instruction_owned_dwell_does_not_expire_pending_search_budget():
    """Raw timer scans are waiting; only the shared search coordinate expires."""
    work = _oneshot_plc()
    work.run(cycles=100)
    checkpoint = _cp(("src",), _oneshot_plc(), 5)
    state = _make_state(
        best_trend=5,
        checkpoints=[checkpoint],
        work=work,
        dwell_scans=100,
    )
    state.pending_departure = _pending_departure(
        state,
        expires_at=50,
    )
    trial = _make_trial(5, Outcome.CONFIRMED, fork=work.fork())
    ctx = SimpleNamespace(target_tag="State", target_value=17, target_predicate=None)

    events = tuple(_monitor_trend(trial, _frame(), state, ctx))

    assert all(event.kind != "provisional_expired" for event in events)
    assert state.search_scan == 0
    assert state.pending_departure is not None


def test_preserved_departure_while_pending_is_investigated(monkeypatch):
    """Even expired pending policy cannot bypass a concrete departure receipt."""
    checkpoint = _cp(("source",), _oneshot_plc(), 2)
    trial = _make_trial(
        2,
        Outcome.AMBIENT_DRIFT,
        before_snap={"State": 2},
        fork_snap={"State": 4},
        zoom_channel_tag="State",
        zoom_target_value=17,
    )
    state = _make_state(best_trend=2, checkpoints=[checkpoint], work=trial.fork)
    state.pending_departure = _pending_departure(
        state,
        expires_at=0,
    )
    verdict = DepartureVerdict(
        decision="continue",
        reason="unique clean current",
        settled_fork=trial.fork,
        settled_value=4,
        settle_scans=0,
        progress=GaugeReceipt((), (), "preserved"),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.classify_departure",
        lambda *_args, **_kwargs: verdict,
    )
    investigated = []

    def _investigate(*_args, retain_if_unresolved=None, **_kwargs):
        investigated.append(retain_if_unresolved)
        return (PilotEvent("departure_investigated", 0, {"retained": True}),)

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress._investigate_and_revert",
        _investigate,
    )
    ctx = SimpleNamespace(
        target_tag="State",
        target_value=17,
        target_predicate=None,
    )

    events = tuple(_monitor_trend(trial, _frame(), state, ctx))

    assert [event.kind for event in events] == [
        "letrun_ejection",
        "departure_check_started",
        "investigation_started",
        "departure_investigated",
    ]
    assert investigated == [verdict]
    assert state.pending_departure is not None
    assert state.work is trial.fork
    assert len(state.checkpoints) == 1


# ---------------------------------------------------------------------------
# Trend monitoring — regression
# ---------------------------------------------------------------------------


def test_prescribed_departure_outranks_a_preserved_recipe_gauge(monkeypatch):
    """A tide-table edge is progress in its own channel.

    The selected recipe gauge may remain flat while a prescribed state/mode
    transition crosses an intermediate channel value. That is not an ambient
    ejection to diagnose, even when the landing has a clean continuation.
    """
    checkpoint = _cp(("source",), _oneshot_plc(), 2)
    trial = _make_trial(
        2,
        Outcome.AMBIENT_DRIFT,
        before_snap={"State": 9},
        fork_snap={"State": 2},
        zoom_channel_tag="State",
        zoom_target_value=1,
        route_prescribed=True,
        assessment=TrialAssessment(
            agency=Agency.PILOT,
            bearing=BearingEffect.DEPARTED,
            progress=ProgressEffect.PRESERVED,
            new_frontier=True,
            accepted=True,
        ),
    )
    state = _make_state(best_trend=2, checkpoints=[checkpoint], work=trial.fork)
    verdict = DepartureVerdict(
        decision="continue",
        reason="clean prescribed continuation",
        settled_fork=trial.fork,
        settled_value=2,
        settle_scans=0,
        progress=GaugeReceipt(
            source_mark=(("RecipeStep", 101),),
            landing_mark=(("RecipeStep", 101),),
            effect="preserved",
        ),
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.classify_departure",
        lambda *_args, **_kwargs: verdict,
    )

    def _unexpected_investigation(*_args, **_kwargs):
        raise AssertionError("a prescribed tide-table edge must not be investigated")

    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress._investigate_and_revert",
        _unexpected_investigation,
    )
    ctx = SimpleNamespace(
        target_tag="State",
        target_value=17,
        target_predicate=None,
        max_scans=100,
    )

    events = tuple(_monitor_trend(trial, _frame(), state, ctx))

    assert [event.kind for event in events] == [
        "letrun_ejection",
        "departure_check_started",
        "provisional_started",
    ]
    assert state.pending_departure is not None
    assert state.pending_departure.opening_progress.effect == "preserved"


def _seal_in_regression_inputs():
    """A regression with a real context, for the chase-causes investigation path.

    Drives the seal-in program (``Out`` latches only under ``Hold``) through a
    pulse where ``Out`` went True then reverted, leaving a departure for the
    investigation to chew on.  Returns the controlled (state, trial, frame, ctx).
    """
    Command = Bool("Command", external=True)
    Hold = Bool("Hold", external=True)
    Out = Bool("Out")
    with Program() as prog:
        with Rung(Or(rise(Command), And(Out, Hold))):
            out(Out)

    pdg = build_program_graph(prog)
    cp = PLC(prog, dt=0.010)
    cp.patch({"Command": False, "Hold": False})
    cp.step()
    cp_fork = cp.fork()
    anchor = cp_fork.state.scan_id

    work = cp.fork()
    work.patch({"Command": False})
    work.step()
    work.patch({"Command": True})
    work.step()
    for _ in range(4):
        work.step()
    end = work.state.scan_id
    fork_snap = dict(work.state.tags)

    steerable = frozenset(compute_steerable(pdg, work._known_tags_by_name, prog))
    ctx = SimpleNamespace(
        resting={"Command": False},
        edge_tags={"Command"},
        target_tag="Out",
        target_value=True,
        pdg=pdg,
        program=prog,
        steerable=steerable,
        opaque_loop=frozenset(),
        pipeline_internal_tags=frozenset(),
        route=None,
        pipeline_roles=(),
        compass=SimpleNamespace(action_tags=frozenset()),
    )
    frame = SimpleNamespace(
        snap={"Out": False},
        tree=SimpleNamespace(ordered_actions=lambda: []),
        key=("f",),
        distance_before=2,
    )
    state = _make_state(
        best_trend=2,
        checkpoints=[_cp(("cpk",), cp_fork, 2)],
        work=work,
        watch_tags=["Out"],
        steps=[_Step(inputs={"Command": True}, scan_before=anchor, scan_after=end)],
    )
    trial = _make_trial(
        6,
        Outcome.CONFIRMED,
        fork=work,
        scan_before=anchor,
        candidate={"Command": True},
        applied=(("Command", True),),
        before_snap={"Out": False},
        post_pulse_snap=fork_snap,
        fork_snap=fork_snap,
        chase_regression_causes=True,
    )
    return state, trial, frame, ctx


class TestRegression:
    """Trend regression triggers investigation and revert."""

    def test_regression_triggers_investigation(self):
        # chase_regression_causes=True runs the investigation pipeline and
        # attaches its payload to the regression event.
        state, trial, frame, ctx = _seal_in_regression_inputs()
        events = tuple(_monitor_trend(trial, frame, state, ctx))

        assert [e.kind for e in events] == ["investigation_started", "trend_regression"]
        investigation = events[1].data["investigation"]
        # The investigation ran (payload populated), unlike the chase=False path
        # which leaves it empty.
        assert "hypotheses" in investigation
        assert "confirmed" in investigation

    def test_recovery_consumes_the_executed_bearing_objective(self, monkeypatch):
        from pyrung.core.analysis.pilot.investigate import InvestigationResult

        state, trial, frame, ctx = _seal_in_regression_inputs()
        objective = BearingObjective(
            TargetSpec("Completed", True),
            (("State", 17), ("RotateFeedback", True)),
        )
        trial = replace(trial, bearing_objective=objective)
        assert state.checkpoints[-1].objective is not objective
        captured: list[tuple[tuple[str, Any], ...]] = []

        def _stub(_plc, _incident, _ctx, _replay, **kwargs):
            captured.append(tuple(kwargs["needed"]))
            return InvestigationResult()

        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.progress.investigate_deviation",
            _stub,
        )

        tuple(_monitor_trend(trial, frame, state, ctx))

        assert captured == [objective.frontier]

    def test_regression_reverts_to_checkpoint(self):
        cp_fork = _oneshot_plc()
        cp_fork.step()
        state = _make_state(best_trend=2, checkpoints=[_cp(("cpk",), cp_fork, 2)])
        work_before = state.work
        trial = _make_trial(6, Outcome.CONFIRMED, chase_regression_causes=False)
        events = tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))

        assert [e.kind for e in events] == ["trend_regression"]
        assert events[0].data["from_trend"] == 6
        assert events[0].data["to_trend"] == 2
        assert state.best_trend == 2  # reverted to the checkpoint's trend
        assert state.work is not work_before  # forked anew from the checkpoint

    def test_rungs_appended_after_checkpoint_vanish_on_revert(self):
        cp_fork = _oneshot_plc()
        cp_fork.step()
        state = _make_state(
            best_trend=2,
            checkpoints=[_cp(("cpk",), cp_fork, 2)],
        )
        from pyrung.core.analysis.pilot._ops import PilotRung

        state.rungs = (*state.rungs, PilotRung("A", True, ~state.work._known_tags_by_name["B"]))
        trial = _make_trial(6, Outcome.CONFIRMED, chase_regression_causes=False)
        tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))

        state.work.step()
        assert not state.rungs
        assert state.work.state.tags["A"] is False

    def test_regression_nogoods_recorded_at_action_source(self):
        from pyrung.core.analysis.pilot.compass import Compass

        cp_fork = _oneshot_plc()
        cp_fork.step()
        state = _make_state(best_trend=2, checkpoints=[_cp(("cpk",), cp_fork, 2)])
        trial = _make_trial(
            6,
            Outcome.CONFIRMED,
            chase_regression_causes=False,
            regression_nogoods=frozenset({("X", True)}),
        )
        ctx = SimpleNamespace(compass=Compass())
        events = tuple(_monitor_trend(trial, _frame(), state, ctx))

        assert ("X", True) in ctx.compass.knowledge.nogood_pairs(("f",))
        assert ("X", True) not in ctx.compass.knowledge.nogood_pairs(("cpk",))
        assert ("X", True) in events[0].data["regression_nogoods"]


# ---------------------------------------------------------------------------
# Trend monitoring — terminal let-run ejection
# ---------------------------------------------------------------------------


class TestLetrunEjection:
    """Terminal let-run ejection investigates over the coast-span window."""

    def test_ejection_anchors_at_coast_start(self):
        # A let-run that ejected lands on a misleadingly LOW trend (fewer open
        # leaves on the side branch).  The ejection branch must intercept it as a
        # regression rather than banking it as a checkpoint.
        state = _make_state(best_trend=5, checkpoints=[_cp(("cpk",), _oneshot_plc(), 5)])
        trial = _make_trial(
            2,  # lower than best_trend — would normally checkpoint
            Outcome.AMBIENT_DRIFT,
            observe_label="letrun",
            zoom_channel_tag="S",
            zoom_target_value=1,
            before_snap={"S": 0},
            fork_snap={"S": 2},
            chase_regression_causes=False,
        )
        events = tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))
        # The ejection is announced, then handed to investigation/revert.
        assert [e.kind for e in events] == [
            "letrun_ejection",
            "departure_check_started",
            "trend_regression",
        ]
        announce = events[0]
        assert announce.data["channel_tag"] == "S"
        assert announce.data["investigated"] is True
        assert announce.data["reason"] is None

    def test_ejection_and_check_are_streamed_before_slow_work(self, monkeypatch):
        state = _make_state(best_trend=5, checkpoints=[_cp(("cpk",), _oneshot_plc(), 5)])
        trial = _make_trial(
            2,
            Outcome.AMBIENT_DRIFT,
            zoom_channel_tag="State",
            zoom_target_value=1,
            before_snap={"State": 6},
            fork_snap={"State": 10},
        )
        classified = False
        investigated = False

        def _classify(*_args, **_kwargs):
            nonlocal classified
            classified = True
            return DepartureVerdict(
                decision="unknown",
                reason="no clean continuation",
                settled_fork=trial.fork,
                settled_value=10,
                settle_scans=0,
            )

        def _investigate(*_args, **_kwargs):
            nonlocal investigated
            investigated = True
            return ()

        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.progress.classify_departure",
            _classify,
        )
        monkeypatch.setattr(
            "pyrung.core.analysis.pilot.progress._investigate_and_revert",
            _investigate,
        )

        events = _monitor_trend(trial, _frame(), state, SimpleNamespace())
        assert next(events).kind == "letrun_ejection"
        assert classified is False
        assert next(events).kind == "departure_check_started"
        assert classified is False
        assert next(events).kind == "investigation_started"
        assert classified is True
        assert investigated is False

    def test_ejection_without_checkpoints_is_announced_but_not_investigated(self):
        # No checkpoint to revert to → the ejected state stands committed, but
        # the bail is surfaced as a letrun_ejection event rather than a silent
        # no-op so the reason is visible in the event stream.
        state = _make_state(best_trend=10, checkpoints=[])
        trial = _make_trial(
            3,
            Outcome.AMBIENT_DRIFT,
            observe_label="letrun",
            zoom_channel_tag="S",
            zoom_target_value=1,
            before_snap={"S": 0},
            fork_snap={"S": 2},
        )
        events = tuple(_monitor_trend(trial, _frame(), state, SimpleNamespace()))
        assert [e.kind for e in events] == ["letrun_ejection"]
        assert events[0].data["investigated"] is False
        assert events[0].data["reason"] == "no checkpoint to revert to"


# ---------------------------------------------------------------------------
# Integration — the checkpoint stream through pilot_events
# ---------------------------------------------------------------------------


def _three_step_program() -> tuple[Program, Bool]:
    """Three sealed-in stages: each latch is a prerequisite for the next, so
    PILOT banks a checkpoint as it closes each one toward the target."""
    a = Bool("a", external=True)
    b = Bool("b", external=True)
    c = Bool("c", external=True)
    s1 = Bool("s1")
    s2 = Bool("s2")
    s3 = Bool("s3")
    with Program() as prog:
        with Rung(a):
            latch(s1)
        with Rung(s1, b):
            latch(s2)
        with Rung(s2, c):
            latch(s3)
    return prog, s3


class TestCheckpointStream:
    """End-to-end: PILOT banks decreasing-trend checkpoints as it solves."""

    def test_checkpoints_emitted_with_decreasing_trend(self):
        prog, target = _three_step_program()
        plc = PLC(prog, dt=0.010)
        events = list(pilot_events(plc, target))

        assert events[-1].kind == "finished"
        assert events[-1].data["reached"] is True

        checkpoints = [e for e in events if e.kind == "trend_checkpoint"]
        assert len(checkpoints) >= 2

        trends = [e.data["trend"] for e in checkpoints]
        assert trends == sorted(trends, reverse=True)  # monotonically improving
        counts = [e.data["checkpoint_count"] for e in checkpoints]
        assert counts == sorted(counts)  # checkpoint_count grows


# ---------------------------------------------------------------------------
# Recording grounds — zoom landing + investigation rejection slugs
# ---------------------------------------------------------------------------


def test_zoom_accepted_payload_records_requested_and_landed():
    """An overshooting coast records both the requested bearing and where it
    actually landed, so a zoom that ejected past its target no longer reads as a
    clean advance."""
    from pyrung.core.analysis.pilot.recording import _zoom_accepted_payload

    trial = _make_trial(
        7,
        Outcome.AMBIENT_DRIFT,
        zoom_channel_tag="State",
        zoom_target_value=6,
        fork_snap={"State": 8},
    )
    payload = _zoom_accepted_payload(trial)

    assert payload["zoom_target_value"] == 6  # requested bearing
    assert payload["zoom_actual_value"] == 8  # where the world actually landed
    assert payload["ejected"] is True


def test_zoom_accepted_payload_records_owned_bearing_receipt():
    from pyrung.core.analysis.pilot.recording import _zoom_accepted_payload

    trial = _make_trial(
        7,
        Outcome.CONFIRMED,
        zoom_channel_tag="Acc",
        zoom_target_value=4,
        bearing_stop_reason="reached",
        fork_snap={"Acc": 5},
    )

    assert _zoom_accepted_payload(trial)["bearing_stop_reason"] == "reached"


def test_deviation_bearing_is_departed_source_not_unvisited_zoom_target():
    """A failed 6 -> 16 zoom that ejects to 8 departed Execute (6).

    The navigation destination remains 16 on the trial/replay contract, but it
    was never held and therefore cannot own a departure timestamp.
    """
    trial = _make_trial(
        7,
        Outcome.AMBIENT_DRIFT,
        zoom_channel_tag="State",
        zoom_target_value=16,
        before_snap={"State": 6},
        fork_snap={"State": 8},
    )
    frame = SimpleNamespace(snap={"State": 6})

    bearing = _deviation_bearing(trial, frame, ["State"], ())

    assert bearing == (("State", 6),)


def test_investigation_event_rejected_detail_carries_slug(monkeypatch):
    """The regression event's investigation payload surfaces the machine-readable
    ground slug beside the human detail for every rejected hypothesis."""
    from pyrung.core.analysis.pilot.investigate import (
        InvestigationHypothesis,
        InvestigationRejection,
        InvestigationResult,
    )

    reject_a = InvestigationHypothesis("a", (("GroundA", True),))
    reject_b = InvestigationHypothesis("b", (("GroundB", True),))

    def _stub(_plc, _incident, _ctx, _replay, **_kwargs):
        return InvestigationResult(
            hypotheses=(reject_a, reject_b),
            confirmed=(),
            rejected=(
                InvestigationRejection(
                    reject_a,
                    "exploratory-replay-failed",
                    "exploratory replay rejected: watchdog still fired",
                ),
                InvestigationRejection(
                    reject_b,
                    "guarded-replay-failed",
                    "guarded replay rejected: guard released",
                ),
            ),
            unresolved=("GroundA",),
        )

    monkeypatch.setattr("pyrung.core.analysis.pilot.progress.investigate_deviation", _stub)

    state, trial, frame, ctx = _seal_in_regression_inputs()
    events = tuple(_monitor_trend(trial, frame, state, ctx))

    assert [e.kind for e in events] == ["investigation_started", "trend_regression"]
    rejected_detail = events[1].data["investigation"]["rejected_detail"]
    assert [r["slug"] for r in rejected_detail] == [
        "exploratory-replay-failed",
        "guarded-replay-failed",
    ]
    # The human ground rides alongside the slug, unchanged.
    assert rejected_detail[0]["ground"] == "exploratory replay rejected: watchdog still fired"
