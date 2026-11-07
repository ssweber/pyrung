"""Unit tests for the synthesis rung factory (``core/synthesis.py``).

Structural only — these assert the factory builds the right rungs/instructions
from data.  Behavioural verification (the bracket actually reproduces the dwell)
lives with the runner bracket hook, which is what *runs* these rungs.
"""

from __future__ import annotations

from pyrung import Bool, Int, Real
from pyrung.core.condition import BitCondition, CompareEq
from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
from pyrung.core.instruction.control import FunctionCallInstruction
from pyrung.core.instruction.data_transfer import CopyInstruction
from pyrung.core.instruction.out_variants import DebounceInstruction, FlasherInstruction
from pyrung.core.synthesis import (
    Synthesis,
    bool_feedback_rungs,
    copy_hold_rung,
    function_rung,
    pulse_feedback_rungs,
)


def _timers(stem: str) -> dict[str, Bool | Int]:
    return {
        "on_done": Bool(f"__{stem}_ond__Fb"),
        "on_acc": Int(f"__{stem}_on__Fb"),
        "off_done": Bool(f"__{stem}_offd__Fb"),
        "off_acc": Int(f"__{stem}_off__Fb"),
    }


def test_bool_feedback_rungs_are_one_debounce_coil() -> None:
    En = Bool("Enable")
    Fb = Bool("Fb")
    timers = _timers("cpl")

    rungs = bool_feedback_rungs(
        enable=BitCondition(En), fb_tag=Fb, **timers, on_delay_ms=200, off_delay_ms=100
    )

    (rung,) = rungs
    (coil,) = rung._instructions
    assert isinstance(coil, DebounceInstruction)
    assert coil.target is Fb
    assert (coil.on_done, coil.on_acc, coil.off_done, coil.off_acc) == tuple(timers.values())
    assert (coil.on_preset, coil.off_preset, coil.unit.name) == (200, 100, "Tms")
    (power,) = rung._conditions
    assert isinstance(power, BitCondition)
    assert power._resolved_tag is En
    assert coil.enable_condition is power


def test_bool_feedback_rungs_accept_a_trigger_compare() -> None:
    Mode = Int("Mode")
    Fb = Bool("Fb")
    rungs = bool_feedback_rungs(
        enable=CompareEq(Mode, 2), fb_tag=Fb, **_timers("cpl"), on_delay_ms=0, off_delay_ms=0
    )
    (cond,) = rungs[0]._conditions
    assert isinstance(cond, CompareEq)


def test_pulse_feedback_rungs_are_one_flasher_coil() -> None:
    En = Bool("Enable")
    Fb = Bool("Fb")
    timers = _timers("pls")

    rungs = pulse_feedback_rungs(
        enable=BitCondition(En), fb_tag=Fb, **timers, on_dwell_ms=8, off_dwell_ms=40_000
    )

    (rung,) = rungs
    (coil,) = rung._instructions
    assert isinstance(coil, FlasherInstruction)
    assert coil.target is Fb
    assert (coil.on_done, coil.on_acc, coil.off_done, coil.off_acc) == tuple(timers.values())
    # One unit for both sides, as on a CLICK: 8 ms rounds up to a second.
    assert (coil.on_preset, coil.off_preset, coil.unit.name) == (1, 40, "Ts")


def test_copy_hold_rung_steady_vs_guarded() -> None:
    Cmd = Bool("Cmd")
    steady = copy_hold_rung(value=True, dest=Cmd)
    assert not steady._conditions  # unconditional steady hold
    (instr,) = steady._instructions
    assert isinstance(instr, LatchInstruction)
    assert instr.target is Cmd

    guarded = copy_hold_rung(value=False, dest=Cmd, guard=CompareEq(Cmd, 0))
    assert guarded._conditions  # gated by the guard
    (instr,) = guarded._instructions
    assert isinstance(instr, ResetInstruction)
    assert instr.target is Cmd


def test_copy_hold_rung_keeps_copy_for_non_bool_destinations() -> None:
    State = Int("State")
    rung = copy_hold_rung(value=1, dest=State)
    (instr,) = rung._instructions
    assert isinstance(instr, CopyInstruction)
    assert instr.source == 1
    assert instr.dest is State


def test_false_bool_hold_resets_default_true_target_off() -> None:
    Cmd = Bool("Cmd", default=True)
    rung = copy_hold_rung(value=False, dest=Cmd)
    (instr,) = rung._instructions
    assert isinstance(instr, ResetInstruction)


def test_function_rung_declares_dataflow() -> None:
    cur = Real("cur")
    out = Real("out")
    en = Bool("en")

    def _fn(cur: float) -> dict[str, float]:
        return {"result": cur + 1.0}

    rung = function_rung(_fn, ins={"cur": cur}, outs={"result": out}, guard=BitCondition(en))
    (instr,) = rung._instructions
    assert isinstance(instr, FunctionCallInstruction)
    assert instr._ins == {"cur": cur}
    assert instr._outs == {"result": out}
    assert rung._conditions  # armed by the guard


def test_synthesis_container() -> None:
    syn = Synthesis()
    assert syn.is_empty()
    assert list(syn.all_rungs()) == []

    Fb = Bool("Fb")
    syn.plant.extend(
        bool_feedback_rungs(
            enable=BitCondition(Bool("En")),
            fb_tag=Fb,
            **_timers("cpl"),
            on_delay_ms=0,
            off_delay_ms=0,
        )
    )
    syn.holds.append(copy_hold_rung(value=True, dest=Bool("Cmd")))
    assert not syn.is_empty()
    assert len(list(syn.all_rungs())) == 2  # 1 hold + 1 plant
