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
from pyrung.core.instruction.timers import OffDelayInstruction, OnDelayInstruction
from pyrung.core.synthesis import (
    Synthesis,
    bool_feedback_rungs,
    copy_hold_rung,
    function_rung,
)


def test_bool_feedback_rungs_are_a_ton_tof_pair() -> None:
    En = Bool("Enable")
    Fb = Bool("Fb")
    ton_done = Bool("__cpl_ond__Fb")
    ton_acc = Int("__cpl_on__Fb")
    tof_acc = Int("__cpl_off__Fb")

    rungs = bool_feedback_rungs(
        enable=BitCondition(En),
        fb_tag=Fb,
        ton_done=ton_done,
        ton_acc=ton_acc,
        tof_acc=tof_acc,
        on_delay_ms=200,
        off_delay_ms=100,
    )

    assert len(rungs) == 2
    ton_rung, tof_rung = rungs

    # Rung 1: gated by the enable, runs the on-delay onto ton_done/ton_acc.
    (ton,) = ton_rung._instructions
    assert isinstance(ton, OnDelayInstruction)
    assert ton.done_bit is ton_done
    assert ton.accumulator is ton_acc
    assert ton.preset == 200
    assert ton_rung._conditions  # gated by `enable`, not unconditional

    # Rung 2: gated by ton_done, off-delay writes the feedback tag itself.
    (tof,) = tof_rung._instructions
    assert isinstance(tof, OffDelayInstruction)
    assert tof.done_bit is Fb
    assert tof.accumulator is tof_acc
    assert tof.preset == 100
    # The off-delay's power comes from the on-delay's done bit.
    (power,) = tof_rung._conditions
    assert isinstance(power, BitCondition)
    assert power._resolved_tag is ton_done


def test_bool_feedback_rungs_accept_a_trigger_compare() -> None:
    Mode = Int("Mode")
    Fb = Bool("Fb")
    rungs = bool_feedback_rungs(
        enable=CompareEq(Mode, 2),
        fb_tag=Fb,
        ton_done=Bool("d"),
        ton_acc=Int("a"),
        tof_acc=Int("b"),
        on_delay_ms=0,
        off_delay_ms=0,
    )
    (cond,) = rungs[0]._conditions
    assert isinstance(cond, CompareEq)


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
            ton_done=Bool("d"),
            ton_acc=Int("a"),
            tof_acc=Int("b"),
            on_delay_ms=0,
            off_delay_ms=0,
        )
    )
    syn.holds.append(copy_hold_rung(value=True, dest=Bool("Cmd")))
    assert not syn.is_empty()
    assert len(list(syn.all_rungs())) == 3  # 1 hold + 2 plant
