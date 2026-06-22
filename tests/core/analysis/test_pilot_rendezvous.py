"""Rendezvous pattern ported from test_walk_real_patterns for PILOT engine.

Two independent SFCs must both complete (timer-gated) before Output.
The walker must hold both enables simultaneously.
"""

from __future__ import annotations

from pyrung import Bool, Program, Rung, Timer, call, on_delay, out, reset, rung, subroutine
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.runner import PLC


def _rendezvous_program() -> tuple[Program, Bool]:
    EnableA = Bool("EnableA", external=True)
    EnableB = Bool("EnableB", external=True)
    InitA = Bool("InitA")
    InitB = Bool("InitB")
    TmrA = Timer.clone("TmrA")
    TmrB = Timer.clone("TmrB")
    Output = Bool("Output")

    @subroutine("RendezvousSfcA")
    def sfc_a():
        with rung():
            on_delay(TmrA, 200, "ms")
        with rung(TmrA.Done):
            out(InitA)

    @subroutine("RendezvousSfcB")
    def sfc_b():
        with rung():
            on_delay(TmrB, 300, "ms")
        with rung(TmrB.Done):
            out(InitB)

    with Program() as prog:
        with Rung(EnableA):
            call(sfc_a)
        with Rung(~EnableA):
            reset(InitA)
        with Rung(EnableB):
            call(sfc_b)
        with Rung(~EnableB):
            reset(InitB)
        with Rung(InitA, InitB):
            out(Output)

    return prog, Output


def _replay(prog: Program, path) -> PLC:
    plc = PLC(prog, dt=0.010)
    for step in path.steps:
        plc.patch(step.action)
        for _ in range(step.scans):
            plc.step()
    return plc


def test_rendezvous_premise() -> None:
    """Both enables held simultaneously reaches Output."""
    prog, _Output = _rendezvous_program()
    plc = PLC(prog, dt=0.010)

    plc.patch({"EnableA": True, "EnableB": True})
    for _ in range(35):
        plc.step()
    assert plc.state.tags["InitA"] is True
    assert plc.state.tags["InitB"] is True
    assert plc.state.tags["Output"] is True


def test_rendezvous_solves() -> None:
    """PILOT holds both enables and reaches Output."""
    prog, Output = _rendezvous_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Output, max_scans=5000)
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["Output"] is True
