"""Crossings — coil family (OUT / SET / RST) condition-level attribution."""

from __future__ import annotations

from pyrung import Block, Bool, Int, OutputBlock, TagType, immediate
from pyrung.core.analysis.crossings.boolean import LatchCrossing, OutCrossing, ResetCrossing
from pyrung.core.crossing import CondAttr, CrossingContext, Eq, Prior, eq_target
from pyrung.core.instruction.coils import LatchInstruction, OutInstruction, ResetInstruction


def _ctx() -> CrossingContext:
    return CrossingContext()


# --- OUT (biconditional) ------------------------------------------------------


def test_out_true_attributes_condition_true() -> None:
    r = OutCrossing().reverse(OutInstruction(Bool("Y")), None, eq_target("Y", True), _ctx())
    assert r.branches == ((CondAttr(expected=True),),)
    assert r.exact is True


def test_out_false_attributes_condition_false() -> None:
    r = OutCrossing().reverse(OutInstruction(Bool("Y")), None, eq_target("Y", False), _ctx())
    assert r.branches == ((CondAttr(expected=False),),)
    assert r.exact is True


def test_out_oneshot_is_necessary_not_sufficient() -> None:
    instr = OutInstruction(Bool("Y"), oneshot=True)
    r = OutCrossing().reverse(instr, None, eq_target("Y", True), _ctx())
    assert r.branches == ((CondAttr(expected=True),),)
    assert r.exact is False  # edge-triggered: condition-true is necessary, not sufficient


def test_out_non_bool_target_is_unsatisfiable() -> None:
    r = OutCrossing().reverse(OutInstruction(Bool("Y")), None, eq_target("Y", 5), _ctx())
    assert r.branches == ((Eq("Y", frozenset()),),)


# --- SET (fired-or-held True; held-only False) --------------------------------


def test_latch_true_is_fired_or_held() -> None:
    r = LatchCrossing().reverse(LatchInstruction(Bool("M")), None, eq_target("M", True), _ctx())
    assert r.branches == ((CondAttr(expected=True),), (Prior("M", "M", 1, 0),))
    assert r.exact is True


def test_latch_false_can_only_be_held() -> None:
    r = LatchCrossing().reverse(LatchInstruction(Bool("M")), None, eq_target("M", False), _ctx())
    assert r.branches == (
        (Prior("M", "M", 1, 0), CondAttr(expected=False)),
    )  # SET never drives False
    assert r.exact is True


# --- RST (type OFF/zero) -----------------------------------------------------


def test_reset_value_is_fired_or_held() -> None:
    r = ResetCrossing().reverse(ResetInstruction(Bool("M")), None, eq_target("M", False), _ctx())
    assert r.branches == ((CondAttr(expected=True),), (Prior("M", "M", 1, 0),))


def test_reset_non_reset_value_can_only_be_held() -> None:
    r = ResetCrossing().reverse(ResetInstruction(Bool("M")), None, eq_target("M", True), _ctx())
    assert r.branches == (
        (Prior("M", "M", 1, 0), CondAttr(expected=False)),
    )  # RST never drives a non-reset value


def test_reset_default_true_bool_still_drives_false() -> None:
    instr = ResetInstruction(Bool("M", default=True))
    assert ResetCrossing().forward(instr, "M", _ctx()).value is False
    fired_or_held = ResetCrossing().reverse(instr, None, eq_target("M", False), _ctx())
    assert fired_or_held.branches == (
        (CondAttr(expected=True),),
        (Prior("M", "M", 1, 0),),
    )
    held = ResetCrossing().reverse(instr, None, eq_target("M", True), _ctx())
    assert held.branches == ((Prior("M", "M", 1, 0), CondAttr(expected=False)),)


def test_reset_int_target_zero() -> None:
    # RST of an INT clears to 0 even when its initialization default is nonzero.
    instr = ResetInstruction(Int("Counter", default=7))
    assert ResetCrossing().forward(instr, "Counter", _ctx()).value == 0
    fired_or_held = ResetCrossing().reverse(instr, None, eq_target("Counter", 0), _ctx())
    assert fired_or_held.branches == (
        (CondAttr(expected=True),),
        (Prior("Counter", "Counter", 1, 0),),
    )
    held = ResetCrossing().reverse(instr, None, eq_target("Counter", 5), _ctx())
    assert held.branches == ((Prior("Counter", "Counter", 1, 0), CondAttr(expected=False)),)


def _assert_reset_polarity(
    instr: ResetInstruction,
    target_name: str,
    *,
    reset_value: object,
    non_reset_value: object,
) -> None:
    crossing = ResetCrossing()
    assert crossing.forward(instr, target_name, _ctx()).value == reset_value

    fired_or_held = crossing.reverse(instr, None, eq_target(target_name, reset_value), _ctx())
    assert fired_or_held.branches == (
        (CondAttr(expected=True),),
        (Prior(target_name, target_name, 1, 0),),
    )
    assert fired_or_held.exact is True

    held = crossing.reverse(instr, None, eq_target(target_name, non_reset_value), _ctx())
    assert held.branches == ((Prior(target_name, target_name, 1, 0), CondAttr(expected=False)),)
    assert held.exact is True


def test_reset_static_block_range_uses_element_type() -> None:
    flags = Block("Flags", TagType.BOOL, 1, 3)
    _assert_reset_polarity(
        ResetInstruction(flags.select(1, 3)),
        "Flags2",
        reset_value=False,
        non_reset_value=True,
    )


def test_reset_indirect_block_range_uses_static_element_type() -> None:
    registers = Block("Registers", TagType.INT, 1, 8)
    start = Int("RangeStart")
    end = Int("RangeEnd")
    _assert_reset_polarity(
        ResetInstruction(registers.select(start, end)),
        "Registers4",
        reset_value=0,
        non_reset_value=9,
    )


def test_reset_immediate_scalar_unwraps_destination_type() -> None:
    outputs = OutputBlock("Outputs", TagType.BOOL, 1, 2)
    _assert_reset_polarity(
        ResetInstruction(immediate(outputs[1])),
        "Outputs1",
        reset_value=False,
        non_reset_value=True,
    )


def test_reset_immediate_block_range_unwraps_destination_type() -> None:
    outputs = OutputBlock("WordOutputs", TagType.WORD, 1, 4)
    _assert_reset_polarity(
        ResetInstruction(immediate(outputs.select(2, 4))),
        "WordOutputs3",
        reset_value=0,
        non_reset_value=5,
    )
