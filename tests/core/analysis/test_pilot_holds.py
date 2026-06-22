"""Shared-gate hold pattern ported from test_walk_holds for PILOT.

Two stages sharing a Common gate; Target needs both. StageB seals on
a rising EnableB edge but its seal-in is gated by Common.
"""

from __future__ import annotations

from pyrung import Bool, Or, Program, Rung, out, rise
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.runner import PLC


def _shared_gate_program() -> tuple[Program, Bool]:
    EnableA = Bool("EnableA", external=True)
    EnableB = Bool("EnableB", external=True)
    Common = Bool("Common", external=True)
    StageA = Bool("StageA")
    StageB = Bool("StageB")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(EnableA, Common):
            out(StageA)
        with Rung(Or(rise(EnableB), StageB), Common):
            out(StageB)
        with Rung(StageA, StageB):
            out(Target)

    return prog, Target


def _replay(prog: Program, path) -> PLC:
    plc = PLC(prog, dt=0.010)
    for step in path.steps:
        plc.patch(step.action)
        for _ in range(step.scans):
            plc.step()
    return plc


def test_shared_gate_premise() -> None:
    """Hold EnableA+Common, then pulse EnableB -> Target."""
    prog, _Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"EnableA": True, "Common": True})
    plc.step()
    assert plc.state.tags["StageA"] is True

    plc.patch({"EnableB": True})
    plc.step()
    assert plc.state.tags["StageB"] is True
    assert plc.state.tags["Target"] is True


def test_shared_gate_solves() -> None:
    """PILOT solves the shared-gate hold pattern."""
    prog, Target = _shared_gate_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Target)
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["Target"] is True
