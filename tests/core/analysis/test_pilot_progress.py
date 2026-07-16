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

from types import SimpleNamespace
from typing import Any

from pyrsistent import pvector

from pyrung import And, Bool, Or, Program, Rung, latch, out, rise
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.detour import DepartureVerdict, Provisional
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.progress import _anchor_bearing_receipt, _monitor_trend
from pyrung.core.analysis.pilot.types import (
    _Checkpoint,
    _PilotState,
    _Step,
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
            steps=pvector([]),
            step_contexts=pvector([]),
            best_trend=trend,
            rungs=pvector([]),
            dwell_scans=0,
        ),
        trend,
        frontier,
    )


def _make_state(best_trend: int, checkpoints: list, **over: Any) -> _PilotState:
    world = _World(
        work=over.pop("work", None) or _oneshot_plc(),
        steps=pvector(over.pop("steps", [])),
        step_contexts=pvector(over.pop("step_contexts", [])),
        best_trend=best_trend,
        rungs=pvector(over.pop("rungs", [])),
        dwell_scans=over.pop("dwell_scans", 0),
    )
    base: dict[str, Any] = {
        "world": world,
        "key_config": None,
        "seen_keys": set(),
        "nogoods": {},
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
        "new_key": ("k",),
        "trend": trend,
        "outcome": outcome,
    }
    base.update(over)
    return _TrialResult(**base)


def _frame() -> SimpleNamespace:
    return SimpleNamespace(
        snap={},
        tree=SimpleNamespace(ordered_actions=lambda: []),
        key=("f",),
        distance_before=5,
    )


def _noop_dbg(_msg: str) -> None:
    return None


# ---------------------------------------------------------------------------
# Trend monitoring — checkpoints
# ---------------------------------------------------------------------------


class TestCheckpoints:
    """Trend improvement creates checkpoints; flat CONFIRMED does too."""

    def test_trend_improvement_creates_checkpoint(self):
        state = _make_state(best_trend=5, checkpoints=[])
        trial = _make_trial(3, Outcome.CONFIRMED)
        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)

        assert [e.kind for e in events] == ["trend_checkpoint"]
        assert events[0].data["trend"] == 3
        assert events[0].data["checkpoint_count"] == 1
        assert events[0].data.get("flat") is None
        assert state.best_trend == 3
        assert len(state.checkpoints) == 1

    def test_flat_confirmed_creates_checkpoint(self):
        # Equal trend, but a CONFIRMED outcome still banks a checkpoint.
        state = _make_state(best_trend=3, checkpoints=[_cp(("c",), _oneshot_plc(), 3)])
        trial = _make_trial(3, Outcome.CONFIRMED)
        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)

        assert [e.kind for e in events] == ["trend_checkpoint"]
        assert events[0].data["flat"] is True
        assert len(state.checkpoints) == 2
        assert state.best_trend == 3  # unchanged on a flat checkpoint

    def test_frontier_preserves_baseline(self):
        # A FRONTIER knowingly enters a deeper corridor (worse trend) — the
        # pre-frontier checkpoint and high-water mark must survive.
        state = _make_state(best_trend=3, checkpoints=[_cp(("c",), _oneshot_plc(), 3)])
        trial = _make_trial(8, Outcome.FRONTIER)
        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)

        assert [e.kind for e in events] == ["trend_checkpoint"]
        assert events[0].data["frontier"] is True
        assert events[0].data["baseline_trend"] == 3
        assert state.best_trend == 3  # NOT advanced to the worse frontier trend
        assert len(state.checkpoints) == 1  # pre-frontier checkpoint preserved

    def test_confirmed_route_landing_starts_a_provisional_corridor(self):
        state = _make_state(best_trend=2, checkpoints=[_cp(("idle",), _oneshot_plc(), 2)])
        trial = _make_trial(
            15,
            Outcome.CONFIRMED,
            zoom_channel_tag="State",
            zoom_target_value=3,
            fork_snap={"State": 3},
        )

        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)

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
        trial = _make_trial(
            15,
            Outcome.CONFIRMED,
            zoom_channel_tag="State",
            zoom_target_value=3,
            fork_snap={"State": 3},
        )

        _anchor_bearing_receipt(trial, frame, state, _noop_dbg)

        assert len(state.checkpoints) == 2
        receipt = state.checkpoints[-1]
        assert receipt.key == ("idle",)
        assert receipt.trend == 2
        assert receipt.world.work.state.scan_id == source_scan


def test_banked_ordinary_checkpoint_promotes_the_provisional():
    """Improved-trend work banked inside a provisional discharges its doubt:
    the march is real, so a later expiry must never roll it back."""
    state = _make_state(best_trend=5, checkpoints=[_cp(("src",), _oneshot_plc(), 5)])
    state.provisional = Provisional(
        channel_tag="State",
        from_value=9,
        gauge_at_source=(("Step", 101),),
        checkpoint_depth=1,
        started_at=0,
        expires_at=2000,
        classification="provisional",
    )
    trial = _make_trial(3, Outcome.CONFIRMED)
    ctx = SimpleNamespace(target_tag="State", target_value=17, target_predicate=None)

    events = _monitor_trend(trial, _frame(), state, ctx, _noop_dbg)

    assert [e.kind for e in events] == ["trend_checkpoint", "provisional_promoted"]
    assert events[1].data["outcome"] == "banked ordinary progress"
    assert state.provisional is None
    assert len(state.checkpoints) == 2  # the march is kept, nothing collapsed


def test_provisional_expiry_without_banked_progress_rolls_back():
    """A provisional that never earned anything — no gauge advance, no banked
    checkpoint — expires by rolling back to its boundary without a nogood."""
    checkpoint = _cp(("src",), _oneshot_plc(), 5)
    state = _make_state(best_trend=5, checkpoints=[checkpoint])
    state.provisional = Provisional(
        channel_tag="State",
        from_value=9,
        gauge_at_source=(("Step", 101),),
        checkpoint_depth=1,
        started_at=0,
        expires_at=0,  # already past — the attempt is out of budget
        classification="provisional",
    )
    trial = _make_trial(5, Outcome.CONFIRMED)
    ctx = SimpleNamespace(target_tag="State", target_value=17, target_predicate=None)

    events = _monitor_trend(trial, _frame(), state, ctx, _noop_dbg)

    assert [e.kind for e in events] == ["provisional_expired"]
    assert state.provisional is None
    assert len(state.checkpoints) == 1  # rolled back to the boundary
    assert state.best_trend == 5


def test_clean_departure_inside_provisional_remains_ordinary_piloting(monkeypatch):
    """A second clean state move must not nest or roll back the attempt."""
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
    state.provisional = Provisional(
        channel_tag="State",
        from_value=9,
        gauge_at_source=(),
        checkpoint_depth=1,
        started_at=0,
        expires_at=1000,
        classification="provisional",
    )
    verdict = DepartureVerdict(
        verdict="provisional",
        reason="unique clean current",
        settled_fork=trial.fork,
        settled_value=4,
        settle_scans=0,
    )
    monkeypatch.setattr(
        "pyrung.core.analysis.pilot.progress.classify_departure",
        lambda *_args, **_kwargs: verdict,
    )
    ctx = SimpleNamespace(
        target_tag="State",
        target_value=17,
        target_predicate=None,
    )

    events = _monitor_trend(trial, _frame(), state, ctx, _noop_dbg)

    assert [event.kind for event in events] == ["letrun_ejection"]
    assert state.provisional is not None
    assert state.work is trial.fork
    assert len(state.checkpoints) == 1


# ---------------------------------------------------------------------------
# Trend monitoring — regression
# ---------------------------------------------------------------------------


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
        events = _monitor_trend(trial, frame, state, ctx, _noop_dbg)

        assert [e.kind for e in events] == ["trend_regression"]
        investigation = events[0].data["investigation"]
        # The investigation ran (payload populated), unlike the chase=False path
        # which leaves it empty.
        assert "hypotheses" in investigation
        assert "confirmed" in investigation

    def test_regression_reverts_to_checkpoint(self):
        cp_fork = _oneshot_plc()
        cp_fork.step()
        state = _make_state(best_trend=2, checkpoints=[_cp(("cpk",), cp_fork, 2)])
        work_before = state.work
        trial = _make_trial(6, Outcome.CONFIRMED, chase_regression_causes=False)
        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)

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
        _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)

        state.work.step()
        assert not state.rungs
        assert state.work.state.tags["A"] is False

    def test_regression_nogoods_recorded(self):
        cp_fork = _oneshot_plc()
        cp_fork.step()
        state = _make_state(best_trend=2, checkpoints=[_cp(("cpk",), cp_fork, 2)])
        trial = _make_trial(
            6,
            Outcome.CONFIRMED,
            chase_regression_causes=False,
            regression_nogoods=frozenset({("X", True)}),
        )
        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)

        assert ("X", True) in state.nogoods[("cpk",)]
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
        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)
        # The ejection is announced, then handed to investigation/revert.
        assert [e.kind for e in events] == ["letrun_ejection", "trend_regression"]
        announce = events[0]
        assert announce.data["channel_tag"] == "S"
        assert announce.data["investigated"] is True
        assert announce.data["reason"] is None

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
        events = _monitor_trend(trial, _frame(), state, SimpleNamespace(), _noop_dbg)
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
    from pyrung.core.analysis.pilot.pilot import _zoom_accepted_payload

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


def test_investigation_event_rejected_detail_carries_slug(monkeypatch):
    """The regression event's investigation payload surfaces the machine-readable
    ground slug beside the human detail for every rejected hypothesis."""
    from pyrung.core.analysis.pilot.investigate import (
        InvestigationHypothesis,
        InvestigationResult,
    )

    reject_a = InvestigationHypothesis("a", (("GroundA", True),))
    reject_b = InvestigationHypothesis("b", (("GroundB", True),))

    def _stub(_plc, _incident, _ctx, _replay, **_kwargs):
        return InvestigationResult(
            confirmed_holds=(),
            hypotheses=(reject_a, reject_b),
            confirmed=(),
            rejected=(
                (reject_a, "raw replay rejected: watchdog still fired"),
                (reject_b, "guarded replay rejected: guard released"),
            ),
            rejection_slugs=("exploratory-replay-failed", "guarded-replay-failed"),
            unresolved=("GroundA",),
        )

    monkeypatch.setattr("pyrung.core.analysis.pilot.progress.investigate_deviation", _stub)

    state, trial, frame, ctx = _seal_in_regression_inputs()
    events = _monitor_trend(trial, frame, state, ctx, _noop_dbg)

    assert [e.kind for e in events] == ["trend_regression"]
    rejected_detail = events[0].data["investigation"]["rejected_detail"]
    assert [r["slug"] for r in rejected_detail] == [
        "exploratory-replay-failed",
        "guarded-replay-failed",
    ]
    # The human ground rides alongside the slug, unchanged.
    assert rejected_detail[0]["ground"] == "raw replay rejected: watchdog still fired"
