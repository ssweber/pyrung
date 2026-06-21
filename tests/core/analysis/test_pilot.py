"""Integration tests for PILOT loop (pilot_how / pilot_drive).

Tests are organized in three sections:
1. Core PILOT tests (direct pilot_how/pilot_drive calls)
2. engine="pilot" parity tests (same programs as test_walk_how_e2e)
3. Real-pattern parity tests (same programs as test_walk_real_patterns)
"""

from __future__ import annotations

from pyrung import (
    Bool,
    Int,
    Physical,
    PLC,
    Program,
    Rung,
    Timer,
    calc,
    call,
    copy,
    latch,
    on_delay,
    out,
    reset,
    return_early,
    rung,
    subroutine,
)
from pyrung.core.analysis.pilot import pilot_drive, pilot_how


def _replay(prog: Program, path) -> PLC:
    """Replay a path on a fresh PLC and return it."""
    plc = PLC(prog, dt=0.010)
    for step in path.steps:
        plc.patch(step.action)
        for _ in range(step.scans):
            plc.step()
    return plc


# ===================================================================
# Section 1: Core PILOT tests
# ===================================================================


def test_simple_latch():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    path = pilot_how(plc, y_Out)

    assert path.reachable
    assert path.total_changes >= 1
    cmds = path.to_commands()
    assert any("x_Go" in c for c in cmds)


def test_two_step_sequential():
    x_A = Bool("x_A", external=True)
    x_B = Bool("x_B", external=True)
    y_Mid = Bool("y_Mid")
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_A):
            out(y_Mid)
        with rung(y_Mid, x_B):
            out(y_Out)

    plc = PLC(logic)
    path = pilot_how(plc, y_Out)

    assert path.reachable
    assert path.total_changes >= 2


def test_already_satisfied():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    plc.force("x_Go", True)
    plc.step()

    path = pilot_how(plc, y_Out)
    assert path.reachable
    assert len(path.steps) == 0


def test_state_machine():
    x_Reset = Bool("x_Reset", external=True)
    x_Start = Bool("x_Start", external=True)
    State = Int("State")
    y_Running = Bool("y_Running")

    with Program() as logic:
        with rung(State == 0, x_Reset):
            copy(1, State)
        with rung(State == 1, x_Start):
            copy(2, State)
        with rung(State == 2):
            out(y_Running)

    plc = PLC(logic)
    path = pilot_how(plc, y_Running)

    assert path.reachable
    assert path.total_changes >= 2


def test_subroutine_call():
    x_Enable = Bool("x_Enable", external=True)
    x_Action = Bool("x_Action", external=True)
    y_Done = Bool("y_Done")

    with Program() as logic:
        with subroutine("work"):
            with rung(x_Action):
                out(y_Done)
        with rung(x_Enable):
            call("work")

    plc = PLC(logic)
    path = pilot_how(plc, y_Done)

    assert path.reachable
    cmds = path.to_commands()
    assert any("x_Enable" in c for c in cmds)
    assert any("x_Action" in c for c in cmds)


def test_timer_wait():
    x_Start = Bool("x_Start", external=True)
    tmr = Timer.clone("T1")
    y_Complete = Bool("y_Complete")

    with Program() as logic:
        with rung(x_Start):
            on_delay(tmr, preset=10)
        with rung(tmr.Done):
            out(y_Complete)

    plc = PLC(logic)
    path = pilot_how(plc, y_Complete, max_scans=3000)
    assert path.reachable


def test_pilot_drive_live():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    assert plc.state.tags.get("y_Out") is not True

    path = pilot_drive(plc, y_Out)
    assert path.reachable
    assert plc.state.tags.get("y_Out") is True


def test_path_to_commands():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    path = pilot_how(plc, y_Out)

    cmds = path.to_commands()
    assert len(cmds) > 0
    assert cmds[-1] == "clear_forces"
    assert any(c.startswith("force ") for c in cmds)
    assert any(c.startswith("step ") for c in cmds)


# ===================================================================
# Section 2: engine="pilot" parity (from test_walk_how_e2e programs)
# ===================================================================


def _simple_latch_prog():
    Start = Bool("Start", external=True)
    Running = Bool("Running")
    with Program() as prog:
        with Rung(Start):
            latch(Running)
    return prog, Start, Running


def _two_step_latch():
    Start = Bool("Start", external=True)
    Confirm = Bool("Confirm", external=True)
    Ready = Bool("Ready")
    Done = Bool("Done")
    with Program() as prog:
        with Rung(Start):
            latch(Ready)
        with Rung(Ready, Confirm):
            latch(Done)
    return prog, Start, Confirm, Ready, Done


def _three_step_program():
    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    with Program() as prog:
        with Rung(CmdA):
            latch(A)
        with Rung(A, CmdB):
            latch(B)
        with Rung(B, CmdC):
            latch(C)
    return prog, CmdA, CmdB, CmdC, A, B, C


def test_engine_simple_latch():
    prog, Start, Running = _simple_latch_prog()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Running, engine="pilot")
    assert path.reachable
    assert path.total_changes > 0


def test_engine_two_step():
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Done, engine="pilot")
    assert path.reachable
    assert path.total_changes > 0


def test_engine_three_step():
    prog, CmdA, CmdB, CmdC, A, B, C = _three_step_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(C, engine="pilot")
    assert path.reachable


def test_engine_replay_validates():
    """Every returned path must replay correctly."""
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Done, engine="pilot")
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["Done"] is True


def test_engine_already_satisfied():
    prog, Start, Running = _simple_latch_prog()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Start": True})
    plc.step()
    assert plc.state.tags["Running"] is True
    path = plc.how(Running, engine="pilot")
    assert path.reachable
    assert path.total_changes == 0


def test_engine_from_stepped_state():
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Start": True})
    plc.step()
    assert plc.state.tags["Ready"] is True
    path = plc.how(Done, engine="pilot")
    assert path.reachable
    assert path.total_changes > 0


def test_engine_independent_and_dependent():
    """C depends on A (through cone), B is independent."""
    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    with Program() as prog:
        with Rung(CmdA):
            latch(A)
        with Rung(CmdB):
            latch(B)
        with Rung(A, CmdC):
            latch(C)
    plc = PLC(prog, dt=0.010)
    path = plc.how(C, engine="pilot")
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["C"] is True


# ===================================================================
# Section 3: Real-pattern parity (from test_walk_real_patterns)
# ===================================================================


def _cmd_protocol_program():
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)

    CtrlCmd = Int("CtrlCmd")
    CmdRequest = Int("CmdRequest")
    StateCurrent = Int("StateCurrent")
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    with Program() as prog:
        with Rung(CmdReset):
            copy(1, CtrlCmd)
            copy(1, CmdRequest)
        with Rung(CmdStart):
            copy(2, CtrlCmd)
            copy(1, CmdRequest)

        with Rung(CmdRequest == 1, CtrlCmd == 1, StateCurrent == 0):
            copy(1, StateRequested)
        with Rung(CmdRequest == 1, CtrlCmd == 2, StateCurrent == 1):
            copy(2, StateRequested)

        with Rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)
            copy(0, CmdRequest)
            copy(0, CtrlCmd)

        with Rung(StateCurrent == 2):
            out(Output)

    return prog, Output


def test_cmd_protocol():
    """Int command-value protocol: two-step Reset+Start sequence."""
    prog, Output = _cmd_protocol_program()
    plc = PLC(prog)
    path = plc.how(Output, engine="pilot")
    assert path.reachable


def _return_early_program():
    Enable = Bool("Enable", external=True)
    Output = Bool("Output")

    @subroutine("ReturnEarlySFC")
    def my_sfc():
        with rung(~Enable):
            return_early()
        with rung():
            out(Output)

    with Program() as prog:
        with Rung():
            call(my_sfc)

    return prog, Output


def test_return_early():
    """return_early() flow gating: Enable must be True."""
    prog, Output = _return_early_program()
    plc = PLC(prog)
    path = plc.how(Output, engine="pilot")
    assert path.reachable


def _step_sequencer_program():
    Advance = Bool("Advance", external=True)
    CurStep = Int("CurStep", default=1)
    Trans = Int("Trans")
    ValIsOdd = Int("ValIsOdd")
    Output = Bool("Output")

    with Program() as prog:
        with Rung(CurStep == 1, Advance):
            copy(1, Trans)
        with Rung(CurStep == 3):
            out(Output)
        with Rung():
            calc(CurStep % 2, ValIsOdd)
        with Rung(ValIsOdd != 1):
            calc(CurStep + 1, CurStep)
        with Rung(Trans == 1):
            calc(CurStep + 1, CurStep)
            copy(0, Trans)

    return prog, Output


def test_step_sequencer():
    """Odd/even step sequencer with auto-advance."""
    prog, Output = _step_sequencer_program()
    plc = PLC(prog)
    path = plc.how(Output, engine="pilot")
    assert path.reachable


def _deep_call_program():
    CmdProd = Bool("CmdProd", external=True)
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)
    Confirm = Bool("Confirm", external=True)

    Mode = Int("Mode")
    StateCurrent = Int("StateCurrent")
    StateRequested = Int("StateRequested")

    SfcCall = Int("SfcCall")
    CurStep = Int("CurStep", default=1)
    Trans = Int("Trans")
    ValIsOdd = Int("ValIsOdd")
    Tmr = Timer.clone("Tmr")

    Output = Bool("Output")

    @subroutine("DeepProduction")
    def production():
        with rung(StateCurrent == 2):
            copy(1, SfcCall)
        with rung(StateCurrent != 2):
            copy(0, SfcCall)

    @subroutine("DeepSFC")
    def my_sfc():
        with rung(SfcCall == 0):
            return_early()
        with rung(CurStep == 1):
            on_delay(Tmr, 100, "ms")
        with rung(CurStep == 1, Tmr.Done):
            copy(1, Trans)
        with rung(CurStep == 3, Confirm):
            out(Output)
        with rung():
            calc(CurStep % 2, ValIsOdd)
        with rung(ValIsOdd != 1):
            calc(CurStep + 1, CurStep)
        with rung(Trans == 1):
            calc(CurStep + 1, CurStep)
            copy(0, Trans)

    with Program() as prog:
        with Rung(CmdProd):
            copy(1, Mode)

        with Rung(CmdReset, StateCurrent == 0):
            copy(1, StateRequested)
        with Rung(CmdStart, StateCurrent == 1):
            copy(2, StateRequested)
        with Rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

        with Rung(Mode == 1):
            call(production)

        with Rung(SfcCall == 1):
            call(my_sfc)

    return prog, Output


def test_deep_call():
    """Six-level prerequisite chain across three subroutine scopes."""
    prog, Output = _deep_call_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Output, engine="pilot", max_steps=30)
    assert path.reachable


# ===================================================================
# Section 4: Harness integration
# ===================================================================


def test_harness_feedback_excluded_from_steerable():
    """Feedback tags with Physical+link are driven by the Harness, not PILOT.

    Program: x_Go enables o_Motor, x_MotorFB (linked to o_Motor) gates y_Done.
    PILOT must NOT steer x_MotorFB directly — the Harness synthesizes it
    after o_Motor goes True.
    """
    x_Go = Bool("x_Go", external=True)
    o_Motor = Bool("o_Motor")
    x_MotorFB = Bool(
        "x_MotorFB",
        external=True,
        physical=Physical("MotorFb", on_delay="0ms", off_delay="0ms"),
        link="o_Motor",
    )
    y_Done = Bool("y_Done")

    with Program() as logic:
        with rung(x_Go):
            out(o_Motor)
        with rung(o_Motor, x_MotorFB):
            out(y_Done)

    plc = PLC(logic, dt=0.010)
    path = pilot_how(plc, y_Done)

    assert path.reachable
    for step in path.steps:
        assert "x_MotorFB" not in step.action, (
            "PILOT should not steer x_MotorFB — Harness owns it"
        )
