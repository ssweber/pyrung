"""Tests for pilot steer — Act instrument mechanics.

Coverage targets:
- _settle_watched_tags: dwell control, fixpoint detection
- _coast_to_bearing: channel-register coast, ejection guard, settle fallback
- _try_terminal_letrun: terminal stall evidence
- _compass_observations: transition, contradiction, and causal filtering
"""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import Bool, Int, Program, Rung, Timer, copy, on_delay, out
from pyrung.core.analysis.pilot.coast import TARGET, value_trigger
from pyrung.core.analysis.pilot.execution import MotionKind
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    BearingObjective,
    Coast,
    TargetSpec,
)
from pyrung.core.analysis.pilot.steer import (
    _action_caused_change,
    _coast_to_bearing,
    _compass_observations,
    _settle_watched_tags,
    _try_terminal_letrun,
)
from pyrung.core.analysis.pilot.trace_tree import TraceNode
from pyrung.core.analysis.pilot.types import _IterationFrame
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _StateKeyConfig
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Programs
# ---------------------------------------------------------------------------


def _follow_program() -> Program:
    """Out follows In with no internal state — settles to a fixpoint at once."""
    In = Bool("In", external=True)
    Out = Bool("Out")
    with Program() as prog:
        with Rung(In):
            out(Out)
    return prog


def _stage_program(target_val: int) -> Program:
    """A channel register Stage jumps to *target_val* when a timer fires."""
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Stage = Int("Stage", default=0)
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 30, "ms")
        with Rung(Tmr.Done):
            copy(target_val, Stage)
    return prog


def _timer_program() -> Program:
    Enable = Bool("Enable", external=True)
    Tmr = Timer.clone("Tmr")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Enable):
            on_delay(Tmr, 100, "ms")
        with Rung(Tmr.Done):
            out(Done)
    return prog


def _competing_coast_program() -> Program:
    """Global target and world departure both precede a slow heading."""
    Enable = Bool("CompetingEnable", external=True)
    GlobalTmr = Timer.clone("CompetingGlobalTmr")
    DepartureTmr = Timer.clone("CompetingDepartureTmr")
    HeadingTmr = Timer.clone("CompetingHeadingTmr")
    Goal = Bool("CompetingGoal")
    State = Int("CompetingState", default=3)
    with Program() as prog:
        with Rung(Enable):
            on_delay(GlobalTmr, 30, "ms")
            on_delay(DepartureTmr, 50, "ms")
            on_delay(HeadingTmr, 100, "ms")
        with Rung(GlobalTmr.Done):
            out(Goal)
        with Rung(DepartureTmr.Done):
            copy(6, State)
    return prog


# ---------------------------------------------------------------------------
# Cone settlement
# ---------------------------------------------------------------------------


class TestSettleCone:
    """_settle_watched_tags: coast until watched tags stop moving."""

    def test_fixpoint_within_ceiling(self):
        plc = PLC(_follow_program(), dt=0.010)
        plc.force("In", True)
        plc.step()
        snaps = _settle_watched_tags(plc, frozenset({"Out"}), floor=2, ceiling=16)
        # Out is steady, so settle stops at the floor — well under the ceiling.
        assert len(snaps) < 16
        assert snaps[-1]["Out"] == snaps[-2]["Out"]

    def test_floor_minimum_respected(self):
        plc = PLC(_follow_program(), dt=0.010)
        plc.force("In", True)
        plc.step()
        # Already at a fixpoint, but the floor forces a minimum dwell.
        snaps = _settle_watched_tags(plc, frozenset({"Out"}), floor=5, ceiling=16)
        assert len(snaps) == 5


# ---------------------------------------------------------------------------
# Bearing coast
# ---------------------------------------------------------------------------


class TestBearingCoast:
    """_coast_to_bearing: cross timer/step-counter plateaus."""

    def test_channel_tag_reaches_target(self):
        plc = PLC(_stage_program(5), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        snaps, receipt = _coast_to_bearing(plc, "Stage", 5, frozenset({"Stage"}))
        assert snaps[-1]["Stage"] == 5
        assert plc.state.tags["Stage"] == 5
        assert receipt is not None and receipt.stop_reason == "reached"

    def test_ejection_guard_stops_bearing_coast(self):
        # Target 9, but the program drives Stage to 5 — a third value that is
        # neither the start (0) nor the target (9), so the guard ejects.
        plc = PLC(_stage_program(5), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        assert plc.state.tags["Stage"] == 0  # bearing-coast start value
        snaps, receipt = _coast_to_bearing(plc, "Stage", 9, frozenset({"Stage"}))
        assert snaps[-1]["Stage"] != 9
        assert snaps[-1]["Stage"] == 5
        assert receipt is not None and receipt.stop_reason == "departed"

    def test_no_channel_tag_falls_back_to_settle(self):
        plc = PLC(_timer_program(), dt=0.010)
        plc.patch({"Enable": True})
        plc.step()
        snaps, receipt = _coast_to_bearing(plc, None, None, frozenset({"Done"}))
        assert receipt is None
        # Settle fallback returns the per-scan trajectory (>= floor), not the
        # single final snapshot a channel coast returns.
        assert len(snaps) >= 2

    def test_global_target_preempts_the_slower_heading(self):
        plc = PLC(_competing_coast_program(), dt=0.010)
        plc.patch({"CompetingEnable": True})
        plc.step()
        terminal = value_trigger(
            plc,
            "global-target",
            TARGET,
            "CompetingGoal",
            True,
        )

        _snaps, receipt = _coast_to_bearing(
            plc,
            "CompetingHeadingTmr.Done",
            True,
            frozenset(),
            terminal_target=terminal,
            departure_tags=("CompetingState",),
        )

        assert receipt.stop_reason == "reached"
        assert "global-target" in receipt.fired
        assert plc.state.tags["CompetingGoal"] is True
        assert receipt.logical_scans < 9

    def test_current_world_departure_preempts_the_slower_heading(self):
        plc = PLC(_competing_coast_program(), dt=0.010)
        plc.patch({"CompetingEnable": True})
        plc.step()

        _snaps, receipt = _coast_to_bearing(
            plc,
            "CompetingHeadingTmr.Done",
            True,
            frozenset(),
            departure_tags=("CompetingState",),
        )

        assert receipt.stop_reason == "departed"
        assert receipt.departure_transitions == (("CompetingState", 3, 6),)
        assert receipt.logical_scans < 9


class TestTerminalLetrun:
    """_try_terminal_letrun: generalized bottom-of-loop fallback."""

    def test_stall_is_dead_end(self):
        program = _follow_program()
        plc = PLC(program, dt=0.010)
        snap = dict(plc.state.tags)
        config = _StateKeyConfig(
            stateful_names=("Out",),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
        key = _pilot_world_key(snap, config, ())
        target = TargetSpec("Out", True)
        frame = _IterationFrame(
            snap=snap,
            tree=TraceNode("Out", True),
            key=key,
            distance_before=1,
            raw_trace_actions=(),
            raw_trace_action_details=(),
        )
        state = SimpleNamespace(
            work=plc,
            pilot_rungs=(),
            key_config=config,
            earned_work=None,
            watch_tags=[],
            remaining_search_scans=lambda *_args, **_kwargs: 2,
        )
        ctx = SimpleNamespace(
            target=target,
            program=program,
            pipeline_roles=(),
            avoid_pred=None,
            max_scans=2,
            steerable=frozenset({"In"}),
        )
        bearing = Bearing(
            key,
            Coast(
                "terminal",
                ActPolicy(
                    ActSource.TERMINAL,
                    motion=MotionKind.COAST_HOLDING_WORLD,
                ),
            ),
            BearingObjective(target),
        )

        result = _try_terminal_letrun(bearing, frame, state, ctx)

        assert result.trial is None
        assert result.gate_events[0].event == "dead-end"
        assert result.gate_events[0].detail == "terminal stall, no ejection"
        assert result.stall_receipt is not None
        assert result.stall_receipt.stop_reason == "timeout"
        assert result.stall_pending is False


class TestCompassObservations:
    """_compass_observations: transitions, no-change, ambient filtering."""

    def test_observes_transitions(self):
        action = ("CompassAction", True)
        world_key = ("world",)
        before = {"CompassState": 0}
        after = {"CompassState": 1}

        observations = _compass_observations(
            action,
            SimpleNamespace(tree=TraceNode("CompassState", 1)),
            before,
            after,
            SimpleNamespace(steerable=frozenset({action[0]})),
            contradict_no_change=False,
            world_key=world_key,
            applied=(action,),
        )

        assert len(observations) == 1
        observation = observations[0]
        assert observation.kind == "edge"
        assert observation.tag == "CompassState"
        assert observation.cause == action
        assert observation.from_val == 0
        assert observation.to_val == 1
        assert observation.world_key == world_key
        assert observation.context == (("CompassState", 0),)
        assert observation.applied == (action,)

    def test_no_change_contradicts_when_enabled(self):
        action = ("CompassNoChange", True)
        world_key = ("world", "unchanged")
        snap = {"CompassState": 0}

        observations = _compass_observations(
            action,
            SimpleNamespace(tree=TraceNode("CompassState", 1)),
            snap,
            snap,
            SimpleNamespace(steerable=frozenset({action[0]})),
            contradict_no_change=True,
            world_key=world_key,
            applied=(action,),
        )

        assert len(observations) == 1
        observation = observations[0]
        assert observation.kind == "contradict"
        assert observation.tag == "CompassState"
        assert observation.cause == action
        assert observation.from_val == 0
        assert observation.to_val is None
        assert observation.world_key == world_key
        assert observation.applied == (action,)

    def test_repeated_trace_tags_share_one_source_receipt(self):
        action = ("CompassRepeated", True)
        snap = {"RepeatedState": 0, "OtherState": 0}
        tree = TraceNode(
            "Goal",
            True,
            satisfied=True,
            children=[
                TraceNode("RepeatedState", 1),
                TraceNode("RepeatedState", 2),
                TraceNode("OtherState", 1),
            ],
        )

        observations = _compass_observations(
            action,
            SimpleNamespace(tree=tree),
            snap,
            snap,
            SimpleNamespace(steerable=frozenset({action[0]})),
            contradict_no_change=True,
            world_key=("shared-source",),
            applied=(action,),
        )

        assert [observation.tag for observation in observations] == [
            "RepeatedState",
            "OtherState",
        ]
        assert observations[0].context is observations[1].context

    def test_ambient_changes_filtered_with_fork(self):
        action = Bool("CompassControl", external=True)
        ambient_source = Bool("CompassAmbientSource", external=True)
        action_effect = Bool("CompassActionEffect")
        ambient_effect = Bool("CompassAmbientEffect")
        with Program() as program:
            with Rung(action):
                out(action_effect)
            with Rung(ambient_source):
                out(ambient_effect)

        plc = PLC(program, dt=0.010)
        before = dict(plc.state.tags)
        plc.patch({action.name: True, ambient_source.name: True})
        plc.step()
        after = dict(plc.state.tags)
        action_pair = (action.name, True)
        tree = TraceNode(
            "CompassGoal",
            True,
            children=[
                TraceNode(action_effect.name, True),
                TraceNode(ambient_effect.name, True),
            ],
        )

        assert _action_caused_change(
            plc,
            action.name,
            action_effect.name,
            frozenset({action.name, ambient_source.name}),
            scan=plc.state.scan_id,
        )
        assert not _action_caused_change(
            plc,
            action.name,
            ambient_effect.name,
            frozenset({action.name, ambient_source.name}),
            scan=plc.state.scan_id,
        )

        observations = _compass_observations(
            action_pair,
            SimpleNamespace(tree=tree),
            before,
            after,
            SimpleNamespace(
                steerable=frozenset({action.name, ambient_source.name}),
            ),
            contradict_no_change=False,
            world_key=("causal-world",),
            applied=(action_pair,),
            fork=plc,
            scan=plc.state.scan_id,
        )

        assert [
            (observation.tag, observation.from_val, observation.to_val)
            for observation in observations
        ] == [(action_effect.name, False, True)]

    def test_action_transition_uses_exact_bounded_scan(self, monkeypatch):
        action = Bool("CompassLocalControl", external=True)
        intermediate = Bool("CompassLocalIntermediate")
        effect = Bool("CompassLocalEffect")
        with Program() as program:
            with Rung(action):
                out(intermediate)
            with Rung(intermediate):
                out(effect)

        plc = PLC(program, dt=0.010)
        plc.patch({action.name: True})
        plc.step()

        original_cause = PLC.cause
        calls = []

        def bounded_cause(self, *args, **kwargs):
            calls.append(kwargs)
            assert kwargs["deep"] is False
            assert kwargs["since"] == plc.state.scan_id
            return original_cause(self, *args, **kwargs)

        monkeypatch.setattr(PLC, "cause", bounded_cause)
        assert _action_caused_change(
            plc,
            action.name,
            effect.name,
            frozenset({action.name}),
            scan=plc.state.scan_id,
            start_scan=plc.state.scan_id,
        )
        assert calls

    def test_action_relationship_can_cross_scans_inside_pulse_window(self):
        action = Bool("CompassWindowControl", external=True)
        intermediate = Bool("CompassWindowIntermediate")
        effect = Bool("CompassWindowEffect")
        with Program() as program:
            # Read the intermediate before its writer so the effect follows on
            # the next scan rather than the action's own scan.
            with Rung(intermediate):
                out(effect)
            with Rung(action):
                out(intermediate)

        plc = PLC(program, dt=0.010)
        plc.patch({action.name: True})
        plc.step()
        first_scan = plc.state.scan_id
        assert plc.state.tags[intermediate.name] is True
        assert plc.state.tags[effect.name] is False
        plc.step()
        assert plc.state.tags[effect.name] is True

        assert _action_caused_change(
            plc,
            action.name,
            effect.name,
            frozenset({action.name}),
            scan=plc.state.scan_id,
            start_scan=first_scan,
        )

    def test_change_before_pulse_window_does_not_call_cause(self, monkeypatch):
        action = Bool("CompassOldControl", external=True)
        effect = Bool("CompassOldEffect")
        with Program() as program:
            with Rung(action):
                out(effect)

        plc = PLC(program, dt=0.010)
        plc.patch({action.name: True})
        plc.step()
        plc.step()

        def no_cause(*_args, **_kwargs):
            raise AssertionError("pre-window motion is established context")

        monkeypatch.setattr(PLC, "cause", no_cause)
        assert not _action_caused_change(
            plc,
            action.name,
            effect.name,
            frozenset({action.name}),
            scan=plc.state.scan_id,
            start_scan=plc.state.scan_id,
        )
