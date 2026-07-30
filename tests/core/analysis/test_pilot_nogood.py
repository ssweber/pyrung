"""PILOT coverage for cross-guard mutual-clobber patterns.

Two latches feed Target with mutual cross-guards — arming one blocks the
other. Requires a multi-phase sequence: arm A, reset guard, arm B.
"""

from __future__ import annotations

from pyrung import And, Bool, Or, Program, Rung, Timer, on_delay, out, rise
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.runner import PLC


def _nogood_program() -> tuple[Program, Bool]:
    """Full cross-guard mutual clobber with timers on both sides."""
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Guard_A = Bool("Guard_A")
    Guard_B = Bool("Guard_B")
    TimerA = Timer.clone("TimerA")
    TimerB = Timer.clone("TimerB")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(Input_A):
            on_delay(TimerA, 100, "ms")
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")
        with Rung(Or(And(TimerA.Done, ~Guard_B), Latch_A)):
            out(Latch_A)
        with Rung(Or(And(TimerB.Done, ~Guard_A), Latch_B)):
            out(Latch_B)
        with Rung(Or(TimerA.Done, Guard_A), ~Reset_Cmd):
            out(Guard_A)
        with Rung(Or(TimerB.Done, Guard_B), ~Reset_Cmd):
            out(Guard_B)
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def _clobber_program() -> tuple[Program, Bool]:
    """Simpler one-sided clobber: arming B blocks A via a Blocker latch."""
    Input_A = Bool("Input_A", external=True)
    Input_B = Bool("Input_B", external=True)
    Reset_Cmd = Bool("Reset_Cmd", external=True)
    Latch_A = Bool("Latch_A")
    Latch_B = Bool("Latch_B")
    Blocker = Bool("Blocker")
    TimerB = Timer.clone("TimerB")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(Or(rise(Input_A), Latch_A), ~Blocker):
            out(Latch_A)
        with Rung(Input_B):
            on_delay(TimerB, 100, "ms")
        with Rung(Or(TimerB.Done, Latch_B)):
            out(Latch_B)
        with Rung(Or(rise(Input_B), Blocker), ~Reset_Cmd):
            out(Blocker)
        with Rung(Latch_A, Latch_B):
            out(Target)

    return prog, Target


def _replay(path) -> PLC:
    return path.replay()


def test_nogood_premise() -> None:
    """Manual sequence: hold A, reset, hold B -> Target."""
    prog, _Target = _nogood_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"Input_A": True})
    for _ in range(15):
        plc.step()
    plc.patch({"Input_A": False})
    plc.step()
    assert plc.state.tags["Latch_A"] is True
    assert plc.state.tags["Guard_A"] is True

    plc.patch({"Reset_Cmd": True})
    plc.step()
    plc.patch({"Reset_Cmd": False})
    plc.step()
    assert plc.state.tags["Guard_A"] is False
    assert plc.state.tags["Latch_A"] is True

    plc.patch({"Input_B": True})
    for _ in range(15):
        plc.step()
    assert plc.state.tags["Latch_B"] is True
    assert plc.state.tags["Target"] is True


def test_nogood_solves() -> None:
    """PILOT solves the cross-guard mutual clobber — simultaneous batch
    sidesteps the sequential clobber because both latches arm before
    either guard seals (rung execution order)."""
    prog, Target = _nogood_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Target, max_scans=200)
    assert path.reachable

    replay = _replay(path)
    assert replay.state.tags["Target"] is True


def test_clobber_premise() -> None:
    """Manual sequence for one-sided clobber: arm A, arm B+reset, Target."""
    prog, _Target = _clobber_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"Input_A": True})
    plc.step()
    plc.patch({"Input_A": False})
    plc.step()
    assert plc.state.tags["Latch_A"] is True

    plc.patch({"Input_B": True})
    for _ in range(15):
        plc.step()
    assert plc.state.tags["Latch_B"] is True
    assert plc.state.tags["Blocker"] is True
    assert plc.state.tags["Latch_A"] is not True

    plc2 = PLC(prog, dt=0.010)
    plc2.patch({"Input_B": True})
    for _ in range(15):
        plc2.step()
    plc2.patch({"Input_B": False, "Reset_Cmd": True})
    plc2.step()
    plc2.patch({"Reset_Cmd": False, "Input_A": True})
    plc2.step()
    plc2.patch({"Input_A": False})
    plc2.step()
    assert plc2.state.tags["Latch_A"] is True
    assert plc2.state.tags["Latch_B"] is True
    assert plc2.state.tags["Target"] is True


def test_clobber_solves() -> None:
    """PILOT solves the one-sided clobber via accept-with-damage: commit the
    regressive input, trace finds Reset_Cmd to clear Blocker, re-pulse Input_A."""
    prog, Target = _clobber_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Target, max_scans=200)
    assert path.reachable
