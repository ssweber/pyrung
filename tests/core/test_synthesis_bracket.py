"""Runner bracket hook: a hand-built synthesis overlay reproduces the dwell.

Increment 2 of the synthesis-is-rungs consolidation.  The PLC scans
``_synthesis.holds`` before user logic and ``_synthesis.plant`` after, in the
same ctx/commit.  Here we hand-build a ``plant`` TON/TOF pair (the shape the
harness factory will emit) and assert it reproduces the bool dwell — *without*
the harness, proving the bracket itself carries the semantics.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, copy
from pyrung.core.condition import BitCondition
from pyrung.core.runner import PLC
from pyrung.core.synthesis import Synthesis, bool_feedback_rungs, copy_hold_rung


def _prog() -> Program:
    Enable = Bool("Enable", external=True)
    Fb = Bool("Fb")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Fb):
            copy(1, Stage)
    return prog


def _plant_overlay(plc: PLC, *, on_ms: int, off_ms: int) -> Synthesis:
    En = plc._known_tags_by_name["Enable"]
    Fb = plc._known_tags_by_name["Fb"]
    rungs = bool_feedback_rungs(
        enable=BitCondition(En),
        fb_tag=Fb,
        ton_done=Bool("__cpl_ond__Fb"),
        ton_acc=Int("__cpl_on__Fb"),
        tof_acc=Int("__cpl_off__Fb"),
        on_delay_ms=on_ms,
        off_delay_ms=off_ms,
    )
    return Synthesis(plant=rungs)


def _run(plc: PLC, en_seq: list[bool]) -> list[bool]:
    out = []
    for en in en_seq:
        plc.patch({"Enable": en})
        plc.step()
        out.append(plc.state.tags["Fb"])
    return out


def test_plant_ton_reproduces_dwell() -> None:
    plc = PLC(_prog(), dt=0.1)
    plc._synthesis = _plant_overlay(plc, on_ms=200, off_ms=100)
    # on_delay 200ms = 2 scans, off_delay 100ms = 1 scan; En held 4 then 3 off.
    # __plant__ reads the scan's settled En, so the feedback is program-visible
    # one scan after the command: rise at scan 2, three trues, fall at scan 5.
    assert _run(plc, [True] * 4 + [False] * 3) == [False, True, True, True, False, False, False]


def test_plant_glitch_is_suppressed() -> None:
    plc = PLC(_prog(), dt=0.1)
    plc._synthesis = _plant_overlay(plc, on_ms=300, off_ms=100)  # 3-scan on-delay
    # A 1-scan glitch (< on_delay) never sustains the TON → Fb stays False.
    assert _run(plc, [True] + [False] * 6) == [False] * 7


def test_empty_synthesis_is_inert() -> None:
    plc = PLC(_prog(), dt=0.1)
    plc._synthesis = Synthesis()  # no holds, no plant
    # Fb is never synthesized; the program never sets Stage.
    assert _run(plc, [True, True, True]) == [False, False, False]
    assert plc.state.tags["Stage"] == 0


def test_holds_bracket_steers_input_before_program_reads_it() -> None:
    # A steady hold copies True into Enable each scan *before* user logic; the
    # program sees the held input the same scan, so Fb's plant arms immediately.
    plc = PLC(_prog(), dt=0.1)
    syn = _plant_overlay(plc, on_ms=0, off_ms=0)
    En = plc._known_tags_by_name["Enable"]
    syn.holds.append(copy_hold_rung(value=True, dest=En))
    plc._synthesis = syn
    # Never patch Enable — the hold drives it.  on_delay 0 → Fb on the next scan.
    plc.step()
    assert plc.state.tags["Enable"] is True
    plc.step()
    assert plc.state.tags["Fb"] is True
