"""Fill-station shape ported from test_walk_why_regression for PILOT engine."""

from __future__ import annotations

from pyrung import PLC, Bool, Int, Program, Rung, calc, copy, out
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.tag import Tag


def _fill_program() -> tuple[Program, Tag]:
    """Synthetic replica of the live fill station shape.

    Rung 0: (unconditional) calc(SetPoint - Band, Lower)
    Rung 1: TareBtn → copy(PV, SetPoint)
    Rung 2: PV >= Lower → out(Target)

    PV is an ND analog input with a pipeline domain.  SetPoint starts at 10
    (a prior tare), Band rests at 0, so Lower=10.  The walker must either
    steer PV to a satisfying value (≥10) or tare to drop Lower.
    """
    PV = Int("PV", external=True, default=0)
    SetPoint = Int("SetPoint", default=10)
    Band = Int("Band", default=0)
    Lower = Int("Lower")
    TareBtn = Bool("TareBtn", external=True)
    Target = Bool("Target")

    with Program() as prog:
        with Rung():
            calc(SetPoint - Band, Lower)
        with Rung(TareBtn):
            copy(PV, SetPoint)
        with Rung(PV >= Lower):
            out(Target)

    return prog, Target


def _replay(prog: Program, path) -> PLC:
    return path.replay()


def test_fill_premise() -> None:
    """Ground truth: tare-then-check makes PV >= Lower."""
    prog, _ = _fill_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    assert plc.state.tags["Lower"] == 10
    assert plc.state.tags["Target"] is False

    plc2 = PLC(prog, dt=0.010)
    plc2.patch({"PV": 10})
    plc2.step()
    assert plc2.state.tags["Target"] is True

    plc3 = PLC(prog, dt=0.010)
    plc3.patch({"TareBtn": True})
    plc3.step()
    plc3.step()
    assert plc3.state.tags["Target"] is True


def test_fill_shape_solves() -> None:
    """PILOT solves the fill shape — PV chase or tare path."""
    prog, target = _fill_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, target)
    assert path.reachable

    replay = _replay(prog, path)
    assert replay.state.tags["Target"] is True
