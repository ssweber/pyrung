"""Delayed-tide reachability for a non-PackML, config-governed program.

A "tool changer" whose enablement is a bitwise allow-mask indexed by a governor
register — the same *math* as the PackML disabled-state mask, but with zero
state-machine shape:

    ToolSel   (governor)     selects the active tool set
    AllowWord = dh[200+ToolSel]      recomputed every scan (the "delayed tide")
    ToolBit   = dh[210+ToolReq]      the requested tool's bit
    EnableResult = AllowWord & ToolBit               non-affine gate
    ToolEnable gated by EnableResult != 0
    ToolActive := ToolReq  gated by ToolEnable        the target write

From config 1 (allows tools 1,2), ``how(ToolActive == 3)`` is a "wrong tide":
tool 3 is only allowed in config 2, so PILOT must change the governor first,
let the gauge re-read, then command the tool.

Two governor shapes, both valid user code, exercise two distinct generalizations:

* **retained** — ``copy(ToolSelCmd, ToolSel)`` gated by ``!= 0``.  The governor
  is genuine cross-scan state.  Exercises the finite-domain fix: the inverter
  must enumerate ``ToolSel`` over the command's ``choices`` even though the write
  is a plain copy, not a literal decode (``table_oracle._producible_int_domain``).

* **decoded** — ``rung(ToolSelCmd == 1): copy(1, ToolSel)`` …  The governor is a
  pure function of the command each scan, so the prover soundly *elides* it as
  scan-local.  Exercises the pre-elision state-key fix: the pilot observes the
  governor (it does not enumerate inputs like BFS does), so its macro-state key
  must keep the elided governor or the establish move reads as SPIN.
"""

from __future__ import annotations

import pytest

from pyrung import Int, Program, Word, calc, copy, rung
from pyrung.click import ClickBlocks


def _tool_changer(*, retained: bool):
    """Build a config-governed tool-changer program; return ``(logic, ToolActive)``."""
    _x, _y, _c, _t, _ct, _sc, _ds, _dd, dh, *_rest = ClickBlocks()

    cfg_choices = {0: "None", 1: "SetA", 2: "SetB"} if retained else {1: "SetA", 2: "SetB"}
    tool_choices = {0: "None", 1: "T1", 2: "T2", 3: "T3", 4: "T4"}

    ToolSelCmd = Int("ToolSelCmd", external=True, choices=cfg_choices)
    ToolSel = Int("ToolSel", choices=cfg_choices)
    ToolReqCmd = Int("ToolReqCmd", external=True, choices=tool_choices)
    ToolReq = Int("ToolReq", choices=tool_choices)
    ToolActive = Int("ToolActive", choices=tool_choices)
    AllowWord = Word("AllowWord")
    ToolBit = Word("ToolBit")
    EnableResult = Word("EnableResult")
    ToolEnable = Int("ToolEnable")
    CfgIdx = Int("CfgIdx")
    BitIdx = Int("BitIdx")
    InitDone = Int("InitDone")

    with Program(strict=False) as logic:
        with rung(InitDone == 0):
            copy(1, InitDone)
            copy(1, ToolSel)  # start in config 1
            # allow-masks: config 1 -> tools 1,2 (0b0011); config 2 -> 3,4 (0b1100)
            copy(0x0003, dh[201])
            copy(0x000C, dh[202])
            # per-tool bit: tool n -> 1 << (n - 1)
            copy(0x0001, dh[211])
            copy(0x0002, dh[212])
            copy(0x0004, dh[213])
            copy(0x0008, dh[214])

        if retained:
            # Governor retains its value when the command rests at 0 -> genuine
            # cross-scan state (mirrors packml UnitModeCmd -> UnitModeCurrent).
            with rung(ToolSelCmd != 0):
                copy(ToolSelCmd, ToolSel)
        else:
            # Governor decoded: always rewritten when the command is valid, so the
            # prover elides it as scan-local.
            with rung(ToolSelCmd == 1):
                copy(1, ToolSel)
            with rung(ToolSelCmd == 2):
                copy(2, ToolSel)
        with rung(ToolReqCmd != 0):
            copy(ToolReqCmd, ToolReq)

        with rung():
            calc(200 + ToolSel, CfgIdx)
        with rung():
            copy(dh[CfgIdx], AllowWord)
        with rung():
            calc(210 + ToolReq, BitIdx)
        with rung():
            copy(dh[BitIdx], ToolBit)
        with rung():
            calc(AllowWord & ToolBit, EnableResult)
        with rung(EnableResult != 0):
            copy(1, ToolEnable)
        with rung(EnableResult == 0):
            copy(0, ToolEnable)
        with rung(ToolEnable == 1):
            copy(ToolReq, ToolActive)

    return logic, ToolActive


def _reach(logic, ToolActive):
    from pyrung.core.runner import PLC

    plc = PLC(logic, dt=0.010)
    plc.step()  # init -> config 1
    assert plc.state.tags["ToolSel"] == 1
    return plc.how(ToolActive == 3, max_scans=400)


def test_delayed_tide_retained_governor():
    """Retained governor: the inverter enumerates ToolSel over the command's
    choices (plain copy, no literal decode), so the mode change is discovered."""
    logic, ToolActive = _tool_changer(retained=True)
    path = _reach(logic, ToolActive)
    assert path.reachable, f"unreachable: {path.reason}"
    assert path.replay().state.tags["ToolActive"] == 3


@pytest.mark.filterwarnings("ignore:BoundsViolation")
def test_delayed_tide_decoded_governor_survives_elision():
    """Decoded governor: the prover elides ToolSel as scan-local, but the pilot's
    pre-elision macro-state key keeps it, so the establish move is not SPIN."""
    logic, ToolActive = _tool_changer(retained=False)
    path = _reach(logic, ToolActive)
    assert path.reachable, f"unreachable: {path.reason}"
    assert path.replay().state.tags["ToolActive"] == 3
