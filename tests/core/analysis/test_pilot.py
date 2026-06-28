"""Integration tests for PILOT loop (pilot_how / pilot_drive).

Tests are organized in three sections:
1. Core PILOT tests (direct pilot_how/pilot_drive calls)
2. engine="pilot" parity tests (same programs as test_walk_how_e2e)
3. Real-pattern parity tests (same programs as test_walk_real_patterns)
"""

from __future__ import annotations

from pyrung import (
    PLC,
    Block,
    Bool,
    Int,
    Or,
    Physical,
    Program,
    Rung,
    TagType,
    Timer,
    calc,
    call,
    copy,
    latch,
    on_delay,
    out,
    return_early,
    rise,
    rung,
    subroutine,
)
from pyrung.core.analysis.pilot import pilot_drive, pilot_events, pilot_how


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


def test_pilot_events_stream_candidate_decisions():
    x_Go = Bool("x_Go", external=True)
    y_Out = Bool("y_Out")

    with Program() as logic:
        with rung(x_Go):
            out(y_Out)

    plc = PLC(logic)
    events = list(pilot_events(plc, y_Out))
    kinds = [event.kind for event in events]

    assert "started" in kinds
    assert "iteration" in kinds
    assert "candidates_built" in kinds
    assert "candidate_try" in kinds
    assert "candidate_accepted" in kinds
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    accepted = next(event for event in events if event.kind == "candidate_accepted")
    assert accepted.data["changes"]["total"]
    assert accepted.data["accepted_because"]["target_reached"] is True


def test_bool_output_ambiguous_requires_choice():
    ProdCmd = Bool("ProdCmd", external=True)
    MaintCmd = Bool("MaintCmd", external=True)
    Mode = Int("Mode")
    ProdMode = Bool("ProdMode")
    MaintMode = Bool("MaintMode")
    Burner = Bool("Burner")

    with Program() as logic:
        with rung(ProdCmd):
            copy(1, Mode)
        with rung(MaintCmd):
            copy(2, Mode)
        with rung(Mode == 1):
            out(ProdMode)
        with rung(Mode == 2):
            out(MaintMode)
        with rung(Or(ProdMode, MaintMode)):
            out(Burner)

    plc = PLC(logic)
    ambiguous = pilot_how(plc, Burner)

    assert ambiguous.ambiguous
    assert not ambiguous.reachable
    assert len(ambiguous.choices) == 2
    assert "ProdMode" in str(ambiguous.choices[0])
    assert "MaintMode" in str(ambiguous.choices[1])

    chosen = pilot_how(plc, Burner, choice=1)
    assert chosen.reachable
    actions = [step.action for step in chosen.steps]
    assert any(action.get("ProdCmd") is True for action in actions)
    assert all(action.get("MaintCmd") is not True for action in actions)


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
    path = plc.how(Running)
    assert path.reachable
    assert path.total_changes > 0


def test_engine_two_step():
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Done)
    assert path.reachable
    assert path.total_changes > 0


def test_engine_three_step():
    prog, CmdA, CmdB, CmdC, A, B, C = _three_step_program()
    plc = PLC(prog, dt=0.010)
    path = plc.how(C)
    assert path.reachable


def test_engine_replay_validates():
    """Every returned path must replay correctly."""
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    path = plc.how(Done)
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["Done"] is True


def test_engine_already_satisfied():
    prog, Start, Running = _simple_latch_prog()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Start": True})
    plc.step()
    assert plc.state.tags["Running"] is True
    path = plc.how(Running)
    assert path.reachable
    assert path.total_changes == 0


def test_engine_from_stepped_state():
    prog, Start, Confirm, Ready, Done = _two_step_latch()
    plc = PLC(prog, dt=0.010)
    plc.patch({"Start": True})
    plc.step()
    assert plc.state.tags["Ready"] is True
    path = plc.how(Done)
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
    path = plc.how(C)
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
    path = plc.how(Output)
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
    path = plc.how(Output)
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
    path = plc.how(Output)
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
    path = plc.how(Output, max_scans=6000)
    assert path.reachable


# ===================================================================
# Section 4: Writer ranking and gate-movement acceptance
# ===================================================================


def test_writer_ranking_literal_preferred():
    """trace_back prefers Literal writers over generic Affine copies.

    StateCurrent has both a specific Literal writer (copy(6, StateCurrent))
    and a generic Affine writer (copy(StateRequested, StateCurrent)).
    When the Affine writer sorts first by rung index, writer ranking must
    still prefer the Literal — otherwise the trace dead-ends at an
    unreachable intermediate value.
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.trace import compute_steerable, trace_back

    CmdClear = Bool("CmdClear", external=True)
    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=9)
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    @subroutine("apply_state")
    def apply_state():
        with rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

    with Program() as prog:
        with rung(CmdClear, StateCurrent == 9):
            copy(1, StateRequested)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)
        with rung():
            call(apply_state)
        with rung(StateRequested == 1):
            copy(2, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 3):
            copy(6, StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    plc = PLC(prog)
    plc.step()
    snap = dict(plc.state.tags)
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    tree = trace_back("StateCurrent", 6, snap, pdg, prog, steerable)

    # The Literal writer (copy(6, StateCurrent) when StateRequested==3)
    # should be selected — NOT the Affine writer (copy(StateRequested, StateCurrent)).
    # With the wrong writer, the trace dead-ends at StateRequested=6 (no producer).
    # With the right writer, it traces to StateRequested==3 → CmdStart.
    actions = tree.ordered_actions()
    assert len(actions) > 0, "trace should find steerable actions, not dead-end"
    action_tags = {t for t, _v in actions}
    assert "CmdStart" in action_tags or "CmdClear" in action_tags


def test_packml_state_sequence():
    """PackML-like state machine: Aborted(9) -> Stopped(2) -> Idle(4) -> Execute(6).

    Each transition requires a command valid only from the current state.
    Transient states auto-complete to the next resting state.
    Tests sequential state discovery through writer ranking and
    gate-movement acceptance.
    """
    CmdClear = Bool("CmdClear", external=True)
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=9)
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    with Program() as prog:
        with rung(CmdClear, StateCurrent == 9):
            copy(1, StateRequested)
        with rung(CmdReset, StateCurrent == 2):
            copy(15, StateRequested)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)

        with rung(StateRequested == 1):
            copy(2, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 15):
            copy(4, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 3):
            copy(6, StateCurrent)
            copy(0, StateRequested)

        with rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

        with rung(StateCurrent == 6):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable
    assert path.total_changes >= 3


def test_gate_movement_acceptance():
    """PILOT accepts NEUTRAL actions that move watched gate tags.

    Five-step state machine: 0 -> 1 -> 2 -> 3 -> 4 -> 5.
    Each step requires a command valid only from the current state.
    Even if distance doesn't improve on a step, gate-tag movement
    (State changing) should be accepted so PILOT can re-trace from
    the new state and discover the next command.
    """
    Cmd0 = Bool("Cmd0", external=True)
    Cmd1 = Bool("Cmd1", external=True)
    Cmd2 = Bool("Cmd2", external=True)
    Cmd3 = Bool("Cmd3", external=True)
    Cmd4 = Bool("Cmd4", external=True)
    State = Int("State", default=0)
    Output = Bool("Output")

    with Program() as prog:
        with rung(State == 0, Cmd0):
            copy(1, State)
        with rung(State == 1, Cmd1):
            copy(2, State)
        with rung(State == 2, Cmd2):
            copy(3, State)
        with rung(State == 3, Cmd3):
            copy(4, State)
        with rung(State == 4, Cmd4):
            copy(5, State)
        with rung(State == 5):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable
    assert path.total_changes >= 5


# ===================================================================
# Section 5: Harness integration
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
        assert "x_MotorFB" not in step.action, "PILOT should not steer x_MotorFB — Harness owns it"


# ===================================================================
# Section 6: Influence mapping (Layer 6)
# ===================================================================


def test_compass_bfs_shortest_path():
    """BFS finds the shortest action sequence through a transition table."""
    from pyrung.core.analysis.pilot.compass import Compass

    inf = Compass()
    tag = "State"
    action_a = ("Cmd", 1)
    action_b = ("Cmd", 2)
    action_c = ("Cmd", 3)
    action_d = ("Recipe", 7)
    inf.record(tag, action_a, 0, 1)
    inf.record(tag, action_b, 1, 2)
    inf.record(tag, action_c, 2, 3)
    inf.record(tag, action_d, 0, 3)  # direct shortcut

    path = inf.find_path(tag, 0, 3)
    assert path == [action_d], f"BFS should find direct path, got {path}"

    path_long = inf.find_path(tag, 0, 2)
    assert path_long == [action_a, action_b], f"Should find 2-step path, got {path_long}"

    assert inf.find_path(tag, 0, 99) is None


def test_compass_paths_include_wait_transitions():
    """WAIT is a transition cause, but not a candidate action."""
    from pyrung.core.analysis.pilot.compass import WAIT, Compass

    inf = Compass()
    tag = "State"
    action_a = ("Cmd", "clear")
    action_b = ("Cmd", "start")
    action_bad = ("Cmd", "abort")
    inf.record(tag, action_a, 9, 1)
    inf.record(tag, WAIT, 1, 2)
    inf.record(tag, action_b, 2, 6)
    inf.record(tag, action_bad, 1, 9)

    assert inf.find_path(tag, 9, 6) == [action_a, WAIT, action_b]
    assert inf.find_path(tag, 1, 6) == [WAIT, action_b]
    assert inf.off_path_actions(tag, 1, 6) == {action_bad}


def test_candidate_generation_does_not_sweep_nd_domains():
    """ND value domains are not automatically candidate action domains."""
    from pyrung.core.analysis.pilot.candidates import upstream_candidates

    class _PDG:
        def upstream_slice(self, tag: str) -> set[str]:
            assert tag == "Output"
            return {"Analog", "Cmd"}

    snap = {"Analog": 0, "Cmd": False}
    candidates = upstream_candidates(
        {"Output"},
        frozenset({"Analog", "Cmd"}),
        set(),
        snap,
        _PDG(),
        nd_domains={"Analog": (0, 1, 2), "Cmd": (False, True)},
    )

    assert candidates == [("Cmd", True)]


def test_candidate_generation_uses_trace_needed_values():
    """Trace-derived values are explicit actions even when not Bool."""
    from pyrung.core.analysis.pilot.candidates import upstream_candidates

    class _PDG:
        def upstream_slice(self, tag: str) -> set[str]:
            assert tag == "Output"
            return {"Recipe"}

    candidates = upstream_candidates(
        {"Output"},
        frozenset({"Recipe"}),
        set(),
        {"Recipe": 0},
        _PDG(),
        nd_domains={"Recipe": (0, 7, 9)},
        needed_values={"Recipe": 7},
    )

    assert candidates == [("Recipe", 7)]


def test_detect_opaque_pipeline():
    """detect_opaque_pipelines finds indirect-copy targets and their steerable inputs."""
    from pyrung.click import ClickBlocks
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.compass import detect_opaque_pipelines
    from pyrung.core.analysis.pilot.trace import compute_steerable

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")
    Scratch = Int("Scratch")
    State = Int("State", default=0)
    Output = Bool("Output")

    ds.slot(10, name="val_A", default=10)
    ds.slot(11, name="val_B", default=20)
    ds.slot(12, name="val_C", default=30)

    @subroutine("ApplyState")
    def apply_state():
        with rung():
            calc(CmdReg + 10, Pointer)
        with rung():
            copy(ds[Pointer], Scratch)
        with rung(Scratch != 0):
            copy(Scratch, State)
            copy(0, CmdReg)

    with Program() as prog:
        with rung(CmdA):
            copy(0, CmdReg)
        with rung(CmdB):
            copy(1, CmdReg)
        with rung(CmdC):
            copy(2, CmdReg)
        with rung(CmdReg != 0):
            call(apply_state)
        with rung(State == 30):
            out(Output)

    pdg = build_program_graph(prog)
    plc = PLC(prog)
    plc.step()
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    slices = detect_opaque_pipelines(pdg, prog, steerable)
    assert len(slices) >= 1, "Should detect the indirect copy pipeline"

    all_action_tags = frozenset().union(*(s.action_tags for s in slices))
    assert {"CmdA", "CmdB", "CmdC"} <= all_action_tags, (
        f"Should find steerable action tags, got {all_action_tags}"
    )


def test_l6_probe_with_trace_context():
    """L6 probes include trace-known inputs as context for opaque pipelines.

    The mode pipeline uses a two-tag pointer calc (CmdReg + Base), so the
    backward trace cannot invert the IndirectRef — CmdProd is only
    discoverable through L6 convergent steer detection. The command rungs
    require both CmdProd AND rise(Enable). Enable is in the trace's
    ordered_actions (needed for the output rung), but probing CmdProd
    alone never triggers rise(Enable). The L6 probe must apply trace
    context to discover the Mode transition.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    Enable = Bool("Enable", external=True)
    CmdA = Bool("CmdA", external=True)
    CmdProd = Bool("CmdProd", external=True)
    Base = Int("Base", default=10)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")
    Scratch = Int("Scratch")
    Mode = Int("Mode", default=0)
    Output = Bool("Output")

    ds.slot(10, name="mode_0", default=0)
    ds.slot(11, name="mode_a", default=1)
    ds.slot(12, name="mode_prod", default=3)

    @subroutine("ApplyMode")
    def apply_mode():
        with rung():
            calc(CmdReg + Base, Pointer)
        with rung():
            copy(ds[Pointer], Scratch)
        with rung(Scratch != 0):
            copy(Scratch, Mode)
            copy(0, CmdReg)
            copy(0, Scratch)

    with Program() as prog:
        with rung(rise(Enable), CmdA):
            copy(1, CmdReg)
        with rung(rise(Enable), CmdProd):
            copy(2, CmdReg)
        with rung():
            call(apply_mode)
        with rung(Enable, Mode == 3):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable, (
        f"L6 should discover Mode transition with trace context: {getattr(path, 'reason', '')}"
    )


def test_influence_driven_opaque_state_machine():
    """PILOT reaches a target through an opaque pipeline via influence mapping.

    State is written through an indirect copy (ds[pointer] -> Scratch -> State).
    The backward trace dead-ends at Scratch (opaque writer). Influence mapping
    detects the pipeline upfront, probes command buttons systematically, and
    BFS finds the path.
    """
    from pyrung.click import ClickBlocks

    x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    CmdC = Bool("CmdC", external=True)
    CmdReg = Int("CmdReg")
    Pointer = Int("Pointer")
    Scratch = Int("Scratch")
    State = Int("State", default=0)
    Output = Bool("Output")

    # ds[10]=1, ds[11]=2, ds[12]=3
    ds.slot(10, name="jump_0", default=1)
    ds.slot(11, name="jump_1", default=2)
    ds.slot(12, name="jump_2", default=3)

    @subroutine("ApplyState")
    def apply_state():
        with rung():
            calc(CmdReg + 10, Pointer)
        with rung():
            copy(ds[Pointer], Scratch)
        with rung(Scratch != 0):
            copy(Scratch, State)
            copy(0, CmdReg)
            copy(0, Scratch)

    with Program() as prog:
        with rung(CmdA):
            copy(1, CmdReg)
        with rung(CmdB):
            copy(2, CmdReg)
        with rung(CmdC):
            copy(3, CmdReg)
        with rung():
            call(apply_state)
        with rung(State == 3):
            out(Output)

    plc = PLC(prog)
    path = pilot_how(plc, Output, max_scans=3000)
    assert path.reachable, (
        f"Should reach Output via influence mapping: {getattr(path, 'reason', '')}"
    )


# ===================================================================
# Section 7: Static route expansion (evidence module)
# ===================================================================


def _packml_program():
    """PackML-like program with Literal+Affine writers for StateCurrent."""
    CmdClear = Bool("CmdClear", external=True)
    CmdReset = Bool("CmdReset", external=True)
    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=9)
    StateRequested = Int("StateRequested")
    Output = Bool("Output")

    with Program() as prog:
        with rung(CmdClear, StateCurrent == 9):
            copy(1, StateRequested)
        with rung(CmdReset, StateCurrent == 2):
            copy(15, StateRequested)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)

        with rung(StateRequested == 1):
            copy(2, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 15):
            copy(4, StateCurrent)
            copy(0, StateRequested)
        with rung(StateRequested == 3):
            copy(6, StateCurrent)
            copy(0, StateRequested)

        with rung(StateRequested != 0):
            copy(StateRequested, StateCurrent)
            copy(0, StateRequested)

        with rung(StateCurrent == 6):
            out(Output)

    return prog, Output


def test_expand_routes_packml_state_machine():
    """Static route expansion finds all state transitions with correct destinations."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes
    from pyrung.core.analysis.pilot.trace import compute_steerable

    prog, _Output = _packml_program()
    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("StateCurrent", pdg, prog, steerable, frozenset())

    # Pipeline routes via StateRequested
    pipeline = [r for r in routes if r.request_tag == "StateRequested"]
    assert len(pipeline) >= 3, f"Expected >=3 pipeline routes, got {len(pipeline)}"

    # Build source→dest map from pipeline routes
    route_map: dict[int, int] = {}
    for r in pipeline:
        for tag, value in r.source_constraints:
            if tag == "StateCurrent":
                route_map[value] = r.destination_value

    assert route_map.get(9) == 2, f"Clear: 9→2, got {route_map}"
    assert route_map.get(2) == 4, f"Reset: 2→4, got {route_map}"
    assert route_map.get(4) == 6, f"Start: 4→6, got {route_map}"

    # Every pipeline route should have steerable action tags
    for r in pipeline:
        assert r.action_tags, f"Route should have action tags: {r.source_constraints}"


def test_expand_routes_indirect_jump_table_pipeline():
    """Indirect copy routes lift pointer scratch back to the request tag."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes, infer_pipeline_roles
    from pyrung.core.analysis.pilot.sandbox import expand_pipeline_need
    from pyrung.core.analysis.pilot.trace import compute_steerable

    CmdStart = Bool("CmdStart", external=True)
    StateComplete = Bool("StateComplete", external=True)
    StateCurrent = Int("StateCurrent", default=4)
    StateRequested = Int("StateRequested")
    StateEnabled = Bool("StateEnabled")
    StateStarting = Bool("StateStarting")
    JumpIdx = Int("JumpIdx")
    JumpTable = Block("JumpTable", TagType.INT, 100, 110)
    JumpTable.slot(103, default=6)
    Output = Bool("Output")

    with Program() as prog:
        with rung(StateCurrent == 4):
            out(StateStarting)
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)
        with rung(StateComplete, StateStarting):
            copy(3, StateRequested)
        with rung(StateRequested != 0):
            out(StateEnabled)
        with rung(StateEnabled):
            calc(StateRequested + 100, JumpIdx)
            copy(JumpTable[JumpIdx], StateCurrent)
            copy(0, StateRequested)
        with rung(StateCurrent == 6):
            out(Output)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("StateCurrent", pdg, prog, steerable, frozenset())

    pipeline = [r for r in routes if r.request_tag == "StateRequested"]
    assert pipeline
    start = next(
        r
        for r in pipeline
        if ("StateCurrent", 4) in r.source_constraints and ("CmdStart", True) in r.enablers
    )
    assert start.request_value == 3
    assert start.destination_value == 6
    assert start.action_tags == frozenset({"CmdStart"})

    complete = next(
        r
        for r in pipeline
        if ("StateCurrent", 4) in r.source_constraints and ("StateComplete", True) in r.enablers
    )
    assert ("StateStarting", True) not in complete.enablers
    assert complete.destination_value == 6

    roles = infer_pipeline_roles("StateCurrent", pdg, prog, steerable, frozenset())
    assert roles.request_tags == frozenset({"StateRequested"})
    assert roles.guard_internal_tags == frozenset({"StateEnabled"})
    assert "StateEnabled" in roles.trace_internal_tags
    assert "StateRequested" not in roles.trace_internal_tags

    expansions = expand_pipeline_need("StateRequested", 3, (roles,), tuple(routes))
    assert len(expansions) == 1
    expansion = expansions[0]
    assert expansion.role == roles
    assert any(
        ("StateCurrent", 4) in route.source_constraints
        and ("StateComplete", True) in route.enablers
        for route in expansion.routes
    )


def test_sandbox_scan_suppresses_non_participants():
    """Sandbox scans run full scans while pinning unrelated side effects."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles
    from pyrung.core.analysis.pilot.sandbox import (
        roles_for_needed_tag,
        run_sandbox_scan,
    )
    from pyrung.core.analysis.pilot.trace import compute_steerable

    CmdStart = Bool("CmdStart", external=True)
    StateCurrent = Int("StateCurrent", default=4)
    StateRequested = Int("StateRequested")
    StateEnabled = Bool("StateEnabled")
    SideEffect = Int("SideEffect")
    JumpIdx = Int("JumpIdx")
    JumpTable = Block("JumpTable", TagType.INT, 100, 110)
    JumpTable.slot(103, default=6)

    with Program() as prog:
        with rung(CmdStart, StateCurrent == 4):
            copy(3, StateRequested)
            copy(99, SideEffect)
        with rung(StateRequested != 0):
            out(StateEnabled)
        with rung(StateEnabled):
            calc(StateRequested + 100, JumpIdx)
            copy(JumpTable[JumpIdx], StateCurrent)
            copy(0, StateRequested)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)
    role = infer_pipeline_roles("StateCurrent", pdg, prog, steerable, frozenset())
    assert roles_for_needed_tag("StateRequested", (role,)) == (role,)

    result = run_sandbox_scan(
        plc,
        role,
        pdg,
        actions=(("CmdStart", True),),
        extra_tags=frozenset({"JumpIdx"}),
        scans=1,
    )

    assert result.after["StateCurrent"] == 6
    assert result.after["SideEffect"] == 0
    assert ("StateCurrent", 4, 6) in result.participating_changes
    assert not result.suppressed_changes


def test_seed_compass_from_static_routes():
    """Static routes pre-populate the compass with a complete BFS path."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.compass import Compass
    from pyrung.core.analysis.pilot.evidence import expand_routes
    from pyrung.core.analysis.pilot.trace import compute_steerable

    prog, _Output = _packml_program()
    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("StateCurrent", pdg, prog, steerable, frozenset())
    inf = Compass()
    seeded = inf.seed_routes("StateCurrent", routes)

    assert seeded >= 3, f"Expected >=3 seeded entries, got {seeded}"

    path = inf.find_path("StateCurrent", 9, 6)
    assert path is not None, "BFS should find path 9→6"
    assert len(path) == 3, f"Path should be 3 hops, got {len(path)}: {path}"


def test_expand_routes_direct_writer():
    """Direct Literal writers produce routes without a request tag."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes
    from pyrung.core.analysis.pilot.trace import compute_steerable

    CmdA = Bool("CmdA", external=True)
    CmdB = Bool("CmdB", external=True)
    State = Int("State", default=0)
    Output = Bool("Output")

    with Program() as prog:
        with rung(State == 0, CmdA):
            copy(1, State)
        with rung(State == 1, CmdB):
            copy(2, State)
        with rung(State == 2):
            out(Output)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("State", pdg, prog, steerable, frozenset())

    assert len(routes) >= 2
    # All direct — no request pipeline
    for r in routes:
        assert r.request_tag is None
        assert r.destination_value is not None

    route_map = {}
    for r in routes:
        for tag, value in r.source_constraints:
            if tag == "State":
                route_map[value] = r.destination_value
    assert route_map.get(0) == 1
    assert route_map.get(1) == 2


def test_expand_routes_subroutine_call_site_gates():
    """Routes through subroutine writers include call-site gate conditions."""
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot.evidence import expand_routes
    from pyrung.core.analysis.pilot.trace import compute_steerable

    Enable = Bool("Enable", external=True)
    Cmd = Bool("Cmd", external=True)
    State = Int("State", default=0)
    Output = Bool("Output")

    @subroutine("doWork")
    def do_work():
        with rung(State == 0, Cmd):
            copy(1, State)

    with Program() as prog:
        with rung(Enable):
            call(do_work)
        with rung(State == 1):
            out(Output)

    plc = PLC(prog)
    plc.step()
    pdg = build_program_graph(prog)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, prog)

    routes = expand_routes("State", pdg, prog, steerable, frozenset())

    assert len(routes) >= 1
    route = routes[0]
    assert route.writer_subroutine == "doWork"
    # Call site gate should include Enable
    gate_tags = {tag for tag, _val in route.call_site_gates}
    assert "Enable" in gate_tags, f"Expected Enable in call_site_gates, got {route.call_site_gates}"
