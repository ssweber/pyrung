"""PILOT coverage for a copy-source chain.

PackML state register written only by copy(Requested, Current) — the
goal value never appears as a literal, so the engine must trace through
the copy source to find the chain.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, blockcopy, call, copy, out, reset, rise, subroutine
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.memory_block import Block
from pyrung.core.runner import PLC
from pyrung.core.tag import TagType


def _jump_state_program():
    ProdMode = Bool("ProdMode", external=True)
    ChgReq = Bool("ChgReq", external=True)
    Adv = Bool("Adv", external=True)
    UnitMode = Int("UnitMode", default=5)
    ReqBool = Int("ReqBool")
    Mode = Int("Mode", default=3)
    Req = Int("Req")
    Cur = Int("Cur", default=9)
    CompleteBool = Int("CompleteBool")
    Target = Bool("Target")

    @subroutine("mode_sub")
    def mode_sub():
        with Rung(ProdMode):
            copy(1, UnitMode, oneshot=True)
        with Rung(UnitMode >= 1, UnitMode <= 3):
            copy(UnitMode, Mode)
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
        with Rung(Adv, Cur == 9):
            copy(15, Req)
        with Rung(Mode == 1, Cur == 15):
            copy(1, CompleteBool)
        with Rung(CompleteBool == 1):
            copy(4, Req)
            copy(0, CompleteBool)
        with Rung(Req != 0):
            copy(Req, Cur)
            copy(0, Req)
        with Rung(Cur == 4):
            out(Target)

    return prog, Target


def _replay(path) -> PLC:
    return path.replay()


def test_jump_state_premise() -> None:
    """Ground truth: Adv then simultaneous ProdMode+ChgReq reaches Cur==4."""
    prog, _target = _jump_state_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    plc.patch({"Adv": True})
    plc.step()
    assert plc.state.tags["Cur"] == 15
    plc.patch({"ProdMode": True, "ChgReq": True})
    plc.step()
    assert plc.state.tags["Mode"] == 1
    assert plc.state.tags["Cur"] == 4
    assert plc.state.tags["Target"] is True


def test_jump_state_solves() -> None:
    """PILOT traces through the copy-source chain to reach Cur==4.
    Chain-width escalation widens the handshake batch after the first NEUTRAL."""
    prog, Target = _jump_state_program()
    plc = PLC(prog, dt=0.010)
    plc.step()
    path = pilot_how(plc, Target)
    assert path.reachable

    replay = _replay(path)
    assert replay.state.tags["Target"] is True


def test_blockcopy_source_receipt_keeps_subroutine_call_gate() -> None:
    """The selected subroutine writer reverses to its aligned source slot."""
    Enable = Bool("BlockCopySubEnable", external=True)
    Push = Bool("BlockCopySubPush", external=True)
    Value = Int("BlockCopySubValue", external=True)
    log = Block("BlockCopySubLog", TagType.INT, 1, 3)

    @subroutine("BlockCopyShift")
    def shift():
        with Rung(rise(Push)):
            blockcopy(log.select(1, 2), log.select(2, 3))
            copy(Value, log[1])

    with Program() as prog:
        with Rung(Enable):
            call(shift)

    path = pilot_how(PLC(prog, dt=0.010), log[2] == 7, max_scans=500)

    assert path.reachable
    assert {"BlockCopySubEnable", "BlockCopySubPush", "BlockCopySubValue"} <= path.changes.keys()
    replay = path.replay()
    assert replay.state.tags["BlockCopySubLog2"] == 7
