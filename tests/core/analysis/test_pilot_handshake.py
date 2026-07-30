"""PILOT coverage for handshake and PackML chain patterns.

Consumed-same-scan handshakes: a tag produced and cleared within one scan
is never true at a scan boundary. PILOT must fire the whole chain mid-scan.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, call, copy, latch, out, reset, rise, subroutine
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.runner import PLC


def _replay(path) -> PLC:
    return path.replay()


# ---------------------------------------------------------------------------
# Handshake: rise(Req) + ModeSel → transient ReqBool → copy → Target
# ---------------------------------------------------------------------------


def _handshake_program():
    Req = Bool("Req", external=True)
    ModeSel = Int("ModeSel", external=True)
    ReqBool = Bool("ReqBool")
    Mode = Int("Mode")
    Target = Bool("Target")

    with Program() as prog:
        with Rung(rise(Req), ModeSel >= 1, ModeSel <= 3):
            latch(ReqBool)
        with Rung(ReqBool, Mode == 0):
            copy(ModeSel, Mode)
        with Rung(ReqBool):
            reset(ReqBool)
        with Rung(Mode == 2):
            out(Target)

    return prog, Target


def test_handshake_premise() -> None:
    prog, _target = _handshake_program()
    plc = PLC(prog, dt=0.010)
    plc.patch({"ModeSel": 2})
    plc.step()
    plc.patch({"Req": True})
    plc.step()
    assert plc.state.tags["Mode"] == 2
    assert plc.state.tags["Target"] is True


def test_handshake_solves() -> None:
    """PILOT solves the consumed-same-scan handshake."""
    prog, Target = _handshake_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    path = pilot_how(plc, Target)
    assert path.reachable

    replay = _replay(path)
    assert replay.state.tags["Target"] is True


# ---------------------------------------------------------------------------
# PackML chain: ChgReq + ProdMode → transient ReqBool → mode_sub → Target
# ---------------------------------------------------------------------------


def _packml_chain_program():
    ProdMode = Bool("ProdMode", external=True)
    ChgReq = Bool("ChgReq", external=True)
    UnitMode = Int("UnitMode", default=5)
    ReqBool = Int("ReqBool")
    ModeCur = Int("ModeCur")
    Target = Bool("Target")

    @subroutine("mode_sub")
    def mode_sub():
        with Rung(ProdMode):
            copy(1, UnitMode, oneshot=True)
        with Rung(UnitMode >= 1, UnitMode <= 3):
            copy(UnitMode, ModeCur)
        with Rung():
            copy(0, ReqBool)
        with Rung():
            copy(0, UnitMode)
        with Rung():
            reset(ChgReq)

    with Program() as prog:
        with Rung(ChgReq):
            copy(1, ReqBool, oneshot=True)
        with Rung(ReqBool == 1):
            call(mode_sub)
        with Rung(ModeCur == 1):
            out(Target)

    return prog, Target


def test_packml_chain_premise() -> None:
    prog, _target = _packml_chain_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    plc.patch({"ProdMode": True, "ChgReq": True})
    plc.step()
    assert plc.state.tags["ModeCur"] == 1
    assert plc.state.tags["Target"] is True


def test_packml_chain_solves() -> None:
    """PILOT solves the PackML mode-change chain."""
    prog, Target = _packml_chain_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    path = pilot_how(plc, Target)
    assert path.reachable

    replay = _replay(path)
    assert replay.state.tags["Target"] is True
