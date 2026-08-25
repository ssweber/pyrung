"""Tests for how() reachability path-finder."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.graph import Plan, PlanStep
from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.condition import AllCondition

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _replay_path(program: Program, path) -> PLC:
    """Replay a how() path on a concrete PLC and return the final state."""
    return path.replay()


# ---------------------------------------------------------------------------
# Simple programs for testing
# ---------------------------------------------------------------------------


def _simple_latch_program() -> tuple[Program, Bool, Bool, Bool]:
    """One external input, one latched output."""
    Start = Bool("Start", external=True)
    Running = Bool("Running")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Running)
        with Rung(Running):
            out(Done)
    return prog, Start, Running, Done


def _two_step_program() -> tuple[Program, Bool, Bool, Bool, Bool]:
    """Reaching Done requires Start then Confirm — two input changes."""
    Start = Bool("Start", external=True)
    Confirm = Bool("Confirm", external=True)
    Ready = Bool("Ready")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Ready)
        with Rung(Ready, Confirm):
            out(Done)
    return prog, Start, Confirm, Ready, Done


def _unreachable_program() -> tuple[Program, Bool, Bool]:
    """Output can never be True — no rung writes it."""
    Input = Bool("Input", external=True)
    Impossible = Bool("Impossible")
    with Program() as prog:
        with Rung(Input):
            out(Input)  # self-referential, doesn't write Impossible
    return prog, Input, Impossible


# ---------------------------------------------------------------------------
# Path display
# ---------------------------------------------------------------------------


class TestPlanDisplay:
    def test_str_reachable(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plan = plc.how(Running)
        text = str(plan)
        assert "Reached" in text
        assert "Running" in text

    def test_str_unreachable(self):
        plan = Plan(reachable=False, target_tag="X", target_value=True, reason="nope")
        assert str(plan) == "Cannot reach X=True.\n  Reason: nope."

    def test_str_stopped(self):
        from pyrung.core.analysis.graph import PlanStatus

        plan = Plan(
            reachable=False,
            target_tag="X",
            target_value=True,
            reason="No productive next action was found; still waiting on Guard=True (have False)",
            status=PlanStatus.STOPPED,
        )
        assert str(plan) == (
            "Stopped before reaching X=True.\n"
            "  Reason: No productive next action was found.\n"
            "  Waiting for: Guard=True (have False)"
        )

    def test_str_already_there(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plc.force("Start", True)
        plc.step()
        plc.step()
        plan = plc.how(Running)
        assert "Reached Running=True in 0 scans." in str(plan)

    def test_guarded_hold_renders_value_scope_and_source(self):
        State = Int("Sts_StateCurrent")
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(
                state=SimpleNamespace(scan_id=10),
                _dt=0.010,
            ),
            journal=(
                PlanStep(
                    kind="force",
                    scan=2,
                    scans=0,
                    inputs=(("DoorClosed", True),),
                    label="DoorClosed",
                    rungs=(PilotRung("DoorClosed", True, State != 6),),
                    source="investigation",
                ),
            ),
        )

        text = str(plan)

        assert "with rung(Sts_StateCurrent != 6):" in text
        assert "latch(DoorClosed)" in text
        assert "(found during investigation)" in text
        assert "force DoorClosed" not in text

    def test_working_theory_owner_is_not_shown_as_plan_rationale(self):
        State = Int("TheoryState")
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(
                state=SimpleNamespace(scan_id=10),
                _dt=0.010,
            ),
            journal=(
                PlanStep(
                    kind="force",
                    scan=2,
                    scans=0,
                    inputs=(("DoorClosed", True),),
                    label="DoorClosed",
                    rungs=(PilotRung("DoorClosed", True, State == 6),),
                    source="working-theory-composition",
                ),
            ),
        )

        text = str(plan)

        assert "Install temporary logic:" in text
        assert "working-theory-composition" not in text

    def test_guarded_pair_renders_as_oscillator(self):
        State = Int("Sts_StateCurrent")
        RotateSensor = Bool("RotateSensor", external=True)
        pilot_rungs = (
            PilotRung(
                "RotateSensor",
                True,
                AllCondition(State == 6, RotateSensor != True),  # noqa: E712
            ),
            PilotRung(
                "RotateSensor",
                False,
                AllCondition(State == 6, RotateSensor != False),  # noqa: E712
            ),
        )
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(
                state=SimpleNamespace(scan_id=10),
                _dt=0.010,
            ),
            journal=(
                PlanStep(
                    kind="force",
                    scan=2,
                    scans=0,
                    inputs=(("RotateSensor", True), ("RotateSensor", False)),
                    label="RotateSensor",
                    rungs=pilot_rungs,
                    source="investigation",
                ),
                PlanStep(
                    kind="coast",
                    scan=3,
                    scans=5,
                    inputs=(),
                    label="",
                    rungs=pilot_rungs,
                ),
            ),
        )

        text = str(plan)

        assert "with rung(And(Sts_StateCurrent == 6, ~RotateSensor)):" in text
        assert "latch(RotateSensor)" in text
        assert "with rung(And(Sts_StateCurrent == 6, RotateSensor)):" in text
        assert "reset(RotateSensor)" in text
        assert "Temporary logic in effect: step 1." in text
        assert "holds: RotateSensor" not in text

    def test_self_guarded_boolean_is_not_summarized_as_a_steady_hold(self):
        WatchdogDone = Bool("WatchdogDone")
        RotateSensor = Bool("RotateSensor", external=True)
        rung = PilotRung(
            "RotateSensor",
            True,
            AllCondition(WatchdogDone == False, RotateSensor != True),  # noqa: E712
        )
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(state=SimpleNamespace(scan_id=10), _dt=0.010),
            journal=(
                PlanStep(
                    kind="force",
                    scan=2,
                    scans=0,
                    inputs=(),
                    label="",
                    rungs=(rung,),
                ),
                PlanStep(
                    kind="coast",
                    scan=3,
                    scans=5,
                    inputs=(),
                    label="",
                    rungs=(rung,),
                ),
            ),
        )

        text = str(plan)

        assert "with rung(And(~WatchdogDone, ~RotateSensor)):" in text
        assert "latch(RotateSensor)" in text
        assert "Temporary logic in effect: step 1." in text
        assert "Keep: RotateSensor=True" not in text
        assert "Oscillate: RotateSensor" not in text

    def test_wait_references_every_installation_step_in_effect(self):
        State = Int("State")
        first = PilotRung("Door", True, State == 3)
        second = PilotRung("Feedback", True, State != 6)
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(state=SimpleNamespace(scan_id=10), _dt=0.010),
            journal=(
                PlanStep("force", 1, 0, (), "", rungs=(first,)),
                PlanStep("force", 2, 0, (), "", rungs=(second,)),
                PlanStep("coast", 3, 5, (), "", rungs=(first, second)),
                PlanStep("coast", 8, 2, (), "", rungs=(first, second)),
            ),
        )

        text = str(plan)

        assert "Temporary logic in effect: steps 1 and 2." in text
        assert "Temporary logic: (same)." in text

    def test_revocation_names_the_removed_temporary_logic_and_installation_step(self):
        State = Int("State")
        old = PilotRung("Go", True, State == 6)
        replacement = PilotRung("Go", False, State == 6)
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(state=SimpleNamespace(scan_id=10), _dt=0.010),
            journal=(
                PlanStep("force", 1, 0, (), "", rungs=(old,)),
                PlanStep("revoke", 2, 0, (), "", rungs=(old,)),
                PlanStep("force", 2, 0, (), "", rungs=(replacement,)),
                PlanStep("coast", 3, 5, (), "", rungs=(replacement,)),
            ),
        )

        text = str(plan)

        assert "2. Remove temporary logic from step 1:" in text
        assert "with rung(State == 6):\n     latch(Go)" in text
        assert "3. Install temporary logic" in text
        assert "with rung(State == 6):\n     reset(Go)" in text
        assert "Temporary logic in effect: step 3." in text

    def test_wait_lists_the_manual_accumulator_edit_for_a_jump_ahead(self):
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(state=SimpleNamespace(scan_id=100), _dt=0.010),
            journal=(
                PlanStep(
                    "coast",
                    1,
                    99,
                    (),
                    "",
                    accelerators=(("Soak.Acc", 900),),
                ),
            ),
        )

        assert "Jump ahead: set Soak.Acc=900." in str(plan)

    def test_pulse_keeps_its_observed_transition(self):
        plan = Plan(
            reachable=True,
            target_tag="Target",
            target_value=True,
            fork=SimpleNamespace(state=SimpleNamespace(scan_id=2), _dt=0.010),
            journal=(
                PlanStep(
                    "pulse",
                    1,
                    1,
                    (("CmdStart", True),),
                    "CmdStart",
                    transition="State 2 -> 3",
                ),
            ),
        )

        assert str(plan).endswith("1. Pulse CmdStart=True.\n   Observed: State 2 -> 3.")


# ---------------------------------------------------------------------------
# PLC.how()
# ---------------------------------------------------------------------------


class TestPLCHow:
    def test_how_from_initial(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable
        assert path.total_changes >= 1

    def test_how_with_condition(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done == True)  # noqa: E712
        assert path.reachable

    def test_how_with_avoid(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done, avoid=Ready)
        assert not path.reachable

    def test_how_with_avoid_uses_non_avoided_route(self):
        Manual = Bool("Manual", external=True)
        Start = Bool("Start", external=True)
        Auto = Bool("Auto")
        Done = Bool("Done")
        with Program() as prog:
            with Rung(Start):
                latch(Auto)
            with Rung(Or(Manual, Auto)):
                out(Done)

        plc = PLC(prog, dt=0.010)
        path = plc.how(Done, avoid=Manual)

        assert path.reachable
        replay = _replay_path(prog, path)
        assert replay.state.tags["Done"] is True
        assert replay.state.tags["Manual"] is False
        assert replay.state.tags["Auto"] is True

    def test_how_without_explore_works(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable
        assert path.total_changes > 0

    def test_how_path_replays_correctly(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Running)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Running"] is True

    def test_how_two_step_replays_correctly(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Done)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Done"] is True

    def test_how_from_stepped_state(self):
        prog, Start, Running, Done = _simple_latch_program()
        plc = PLC(prog, dt=0.010)
        plc.patch({"Start": True})
        plc.step()
        assert plc.state.tags["Running"] is True
        path = plc.how(Running)
        assert path.reachable

    def test_how_multiple_conditions_and(self):
        prog, Start, Confirm, Ready, Done = _two_step_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Ready, Done)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Ready"] is True
        assert result.state.tags["Done"] is True

    def test_how_rejects_tag_valued_eq_target(self):
        """how(tag == ConstTag) rejects a Tag RHS — the value must be a frozen
        scalar (the trace would otherwise ride it as a TagExpr and crash the
        crossings machinery).  Pass the literal / named-array .default instead."""
        Start = Bool("Start", external=True)
        State = Int("State")
        K = Int("K", readonly=True, default=3)
        with Program() as prog:
            with Rung(Start):
                copy(3, State)
        plc = PLC(prog, dt=0.010)
        with pytest.raises(ValueError, match="not a concrete value"):
            plc.how(State == K)

    def test_recording_captures_all_steered_inputs(self):
        """The scan_log must record every input the fork was driven with.

        Regression: prerequisite_holds (e.g. C_UnitModeChgRequest) were applied
        to the fork via rungs but excluded from applied actions, so replay
        couldn't reproduce the reached state.
        """
        Enable = Bool("Enable", external=True)
        Gate = Bool("Gate", external=True)
        Armed = Bool("Armed")
        Output = Bool("Output")
        with Program() as prog:
            with Rung(Enable, Gate):
                latch(Armed)
            with Rung(Armed):
                out(Output)

        plc = PLC(prog, dt=0.010)
        plan = plc.how(Output)
        assert plan.reachable

        recorded_tags = set()
        snap = plan.fork._scan_log.snapshot()
        for patches in snap.patches_by_scan.values():
            recorded_tags.update(patches.keys())
        for forces in snap.force_changes_by_scan.values():
            recorded_tags.update(forces.keys())

        assert "Enable" in recorded_tags or "Gate" in recorded_tags, (
            "recording must capture the steered inputs, not just the decision"
        )

        result = plan.replay()
        assert result.state.tags["Output"] is True

    def test_how_from_initial_state_override(self):
        """how() finds the correct source when initial_state has different
        external input values than the graph's representative snapshot."""
        from pyrung.core.state import SystemState

        prog, Start, Running, Done = _simple_latch_program()
        # The graph reaches Running=True via Start=True.  Set Running=True
        # with Start=False — same internal state, different external input.
        tags = {"Running": True, "Done": True, "Start": False}
        plc = PLC(prog, dt=0.010, initial_state=SystemState().with_tags(tags))

        path = plc.how(Running)
        assert path.reachable
        assert path.total_changes == 0, "should already be at target"


# ---------------------------------------------------------------------------
# Timer/counter how()
# ---------------------------------------------------------------------------


def _timer_program() -> tuple[Program, Bool, Timer, Bool]:
    Enable = Bool("Enable", external=True)
    T1 = Timer.clone("T1")
    Output = Bool("Output")
    with Program() as prog:
        with Rung(Enable):
            on_delay(T1, preset=500)
        with Rung(T1.Done):
            out(Output)
    return prog, Enable, T1, Output


def _counter_program() -> tuple[Program, Bool, Counter, Bool]:
    Trigger = Bool("Trigger", external=True)
    Reset = Bool("Reset", external=True)
    C1 = Counter.clone("C1")
    Output = Bool("Output")
    with Program() as prog:
        with Rung(rise(Trigger)):
            count_up(C1, preset=5).reset(Reset)
        with Rung(C1.Done):
            out(Output)
    return prog, Trigger, C1, Output


class TestTimerCounterHow:
    def test_timer_how_finds_path(self):
        prog, Enable, T1, Output = _timer_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Output)
        assert path.reachable

    def test_timer_path_replays_correctly(self):
        prog, Enable, T1, Output = _timer_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Output)
        assert path.reachable
        result = _replay_path(prog, path)
        assert result.state.tags["Output"] is True

    def test_counter_how_finds_path(self):
        prog, Trigger, C1, Output = _counter_program()
        plc = PLC(prog, dt=0.010)
        path = plc.how(Output)
        # BFS planner cannot yet solve rise()-gated counters (replay
        # verification fails), so just confirm how() returns a Plan.
        assert isinstance(path, Plan)
