"""Tests for physical feedback integration in the corridor walker.

The walker installs a Harness on its work fork so that feedback timing
is respected during time-folding.  ``PLC.fork()`` propagates the
Harness, so every trial fork inherits it.  Three cases: bool on_delay,
bool off_delay, and profile-driven analog ramp.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Real, Rung, copy
from pyrung.core.harness import Harness, _profile_registry
from pyrung.core.physical import Physical
from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOTOR_FB = Physical("MotorFb", on_delay="200ms", off_delay="100ms")
VALVE_FB = Physical("ValveFb", on_delay="50ms", off_delay="50ms")
SENSOR = Physical("TempSensor", profile="walk_test_thermal")


# Register a simple linear profile for testing.
if "walk_test_thermal" not in _profile_registry:

    @staticmethod  # type: ignore[misc]
    def _thermal(cur: float, en: bool, dt: float) -> float:
        return cur + (1.0 if en else -0.5) * dt

    _profile_registry["walk_test_thermal"] = _thermal


# ---------------------------------------------------------------------------
# Bool on_delay: enable → feedback delayed → interlock clears
# ---------------------------------------------------------------------------


def _on_delay_program():
    Enable = Bool("Enable", external=True)
    Feedback = Bool("Feedback", physical=MOTOR_FB, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Feedback):
            copy(1, Stage)
    return prog, Stage, Enable, Feedback


class TestBoolOnDelay:
    def test_walk_reaches_goal_through_feedback_delay(self):
        prog, Stage, _Enable, _Feedback = _on_delay_program()
        plc = PLC(prog, dt=0.010)

        path = plc.how(Stage == 1)
        assert path is not None
        assert path.reachable

    def test_harness_propagates_through_fork(self):
        prog, _Stage, _Enable, _Feedback = _on_delay_program()
        plc = PLC(prog, dt=0.010)
        harness = Harness(plc)
        harness.install()

        fork = plc.fork()
        assert len(fork._pre_scan_callbacks) == 1

        fork2 = fork.fork()
        assert len(fork2._pre_scan_callbacks) == 1

    def test_replay_with_harness_matches(self):
        """The plan from the walker replays correctly with the real Harness."""
        prog, Stage, _Enable, _Feedback = _on_delay_program()
        plc = PLC(prog, dt=0.010)

        path = plc.how(Stage == 1)
        assert path is not None

        verify = plc.fork()
        harness = Harness(verify)
        harness.install()
        for step in path.steps:
            if step.action:
                verify.patch(step.action)
            for _ in range(step.scans):
                verify.step()
        assert verify.state.tags.get("Stage") == 1


# ---------------------------------------------------------------------------
# Bool off_delay: de-energize → feedback drops delayed → gate clears
# ---------------------------------------------------------------------------


def _off_delay_program():
    Enable = Bool("Enable", external=True, default=True)
    Feedback = Bool("Feedback", physical=MOTOR_FB, link="Enable", default=True)
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(~Enable, ~Feedback):
            copy(1, Stage)
    return prog, Stage, Enable, Feedback


class TestBoolOffDelay:
    def test_walk_reaches_goal_through_off_delay(self):
        prog, Stage, _Enable, _Feedback = _off_delay_program()
        plc = PLC(prog, dt=0.010)

        path = plc.how(Stage == 1)
        assert path is not None
        assert path.reachable

    def test_plan_scans_include_delay(self):
        """The plan must include enough scans for the off_delay (100ms = 10 scans)."""
        prog, Stage, _Enable, _Feedback = _off_delay_program()
        plc = PLC(prog, dt=0.010)

        path = plc.how(Stage == 1)
        assert path is not None
        assert path.total_scans >= 10


# ---------------------------------------------------------------------------
# Profile: analog ramp to comparison threshold
# ---------------------------------------------------------------------------


def _profile_program():
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=SENSOR, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)
    return prog, Stage, Enable, Temp


class TestProfileFeedback:
    def test_profile_fb_in_jump_context(self):
        """Profile feedback names should be in the JumpContext exclusion set."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.walk import _build_jump_context

        prog, _Stage, _Enable, _Temp = _profile_program()
        plc = PLC(prog, dt=0.010)
        harness = Harness(plc)
        harness.install()
        pdg = build_program_graph(prog)

        jctx = _build_jump_context(plc, pdg, prog)
        assert "Temp" in jctx.profile_fb_names

    def test_walk_reaches_goal_through_profile_ramp(self):
        prog, Stage, _Enable, _Temp = _profile_program()
        plc = PLC(prog, dt=0.010)

        path = plc.how(Stage == 1)
        assert path is not None
        assert path.reachable
        # +0.01/scan to reach 5.0 ≈ 500 scans
        assert 400 <= path.total_scans <= 600


# ---------------------------------------------------------------------------
# Fold interaction: verify the fold stops at feedback crossings
# ---------------------------------------------------------------------------


def _fold_interaction_program():
    Enable = Bool("Enable", external=True)
    Feedback = Bool("Feedback", physical=VALVE_FB, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Feedback):
            copy(1, Stage)
    return prog, Stage, Enable, Feedback


class TestFoldInteraction:
    def test_fold_stops_at_feedback(self):
        """The plan should not skip past the feedback landing."""
        prog, Stage, _Enable, _Feedback = _fold_interaction_program()
        plc = PLC(prog, dt=0.010)

        path = plc.how(Stage == 1)
        assert path is not None
        assert path.reachable
        assert path.total_scans >= 5

    def test_harness_heap_constrains_fold(self):
        """An installed Harness's pending patch constrains the fold distance."""
        from pyrung.core.analysis.prove.walk import _harness_nearest_scan

        prog, _Stage, _Enable, _Feedback = _fold_interaction_program()
        plc = PLC(prog, dt=0.010)
        harness = Harness(plc)
        harness.install()

        plc.patch({"Enable": True})
        plc.step()

        nearest = _harness_nearest_scan(plc)
        assert nearest is not None
        assert nearest == plc.state.scan_id + 5
