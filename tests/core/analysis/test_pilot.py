"""Integration tests for PILOT loop (pilot_how / pilot_drive)."""

from __future__ import annotations

from pyrung import Bool, Int, PLC, Program, Timer, calc, call, copy, on_delay, out, rung, subroutine
from pyrung.core.analysis.pilot import pilot_drive, pilot_how


# -- Test 1: Simple latch (one input) ---------------------------------------


def test_simple_latch():
    """Single input gates the output — one step."""
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


# -- Test 2: Two-step sequential (A then B) ---------------------------------


def test_two_step_sequential():
    """Two inputs needed in sequence: x_A enables y_Mid, then x_B + y_Mid enables y_Out."""
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


# -- Test 3: Already-satisfied target (zero steps) --------------------------


def test_already_satisfied():
    """Target already true — path has zero steps."""
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


# -- Test 4: State machine (copy-based transitions) -------------------------


def test_state_machine():
    """State machine: State 0 → 1 → 2, each transition gated by a different input."""
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


# -- Test 5: Subroutine call chain ------------------------------------------


def test_subroutine_call():
    """Output inside a subroutine gated by a call condition."""
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


# -- Test 6: Timer (time folding) -------------------------------------------


def test_timer_wait():
    """Timer: input enables timer, done bit gates output. PILOT must wait."""
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


# -- Test 7: pilot_drive modifies the live PLC --------------------------------


def test_pilot_drive_live():
    """pilot_drive operates on the live PLC — state should change."""
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


# -- Test 8: Path.to_commands produces valid DAP commands --------------------


def test_path_to_commands():
    """to_commands() output matches the expected DAP format."""
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
