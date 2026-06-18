"""Crossings — shift / drum / search / modbus-receive (Step 3d)."""

from __future__ import annotations

from pyrung import Bool, Int
from pyrung.core.analysis.crossings.drums import DrumCrossing
from pyrung.core.analysis.crossings.external import ModbusReceiveCrossing
from pyrung.core.analysis.crossings.search import SearchCrossing
from pyrung.core.analysis.crossings.shift import ShiftCrossing
from pyrung.core.crossing import (
    CondAttr,
    CrossingContext,
    Eq,
    External,
    Prior,
    Quant,
    eq_target,
)
from pyrung.core.instruction.advanced import SearchInstruction, ShiftInstruction
from pyrung.core.instruction.drums import EventDrumInstruction
from pyrung.core.memory_block import Block
from pyrung.core.tag import TagType


def _ctx() -> CrossingContext:
    return CrossingContext()


# --- shift register -----------------------------------------------------------


def _shift():
    bits = Block("C", TagType.BOOL, 1, 8)
    return ShiftInstruction(bits.select(1, 8), Bool("D"), Bool("Clk"), Bool("Rst"))


def test_shift_interior_true_came_from_neighbour_or_held() -> None:
    r = ShiftCrossing().reverse(_shift(), None, eq_target("C3", True), _ctx())
    assert r.branches == ((Prior("C3", "C2", 1, 0),), (Prior("C3", "C3", 1, 0),))
    assert r.exact is True


def test_shift_head_true_is_data_condition_or_held() -> None:
    r = ShiftCrossing().reverse(_shift(), None, eq_target("C1", True), _ctx())
    assert r.branches == ((CondAttr(expected=True),), (Prior("C1", "C1", 1, 0),))


def test_shift_false_falls_through() -> None:
    assert ShiftCrossing().reverse(_shift(), None, eq_target("C3", False), _ctx()).fallthrough


# --- drum ---------------------------------------------------------------------


def _drum():
    outputs = [Bool("Y1"), Bool("Y2")]
    events = [Bool("E1"), Bool("E2")]
    pattern = [[1, 0], [0, 1]]  # step 1 -> Y1, step 2 -> Y2
    return EventDrumInstruction(
        outputs, events, pattern, Int("Step"), Bool("Cmpl"), Bool("Auto"), Bool("Rst")
    )


def test_drum_output_true_pins_step_set_or_held() -> None:
    r = DrumCrossing().reverse(_drum(), None, eq_target("Y2", True), _ctx())
    assert r.branches == ((Eq("Step", frozenset({2})),), (Prior("Y2", "Y2", 1, 0),))
    assert r.exact is True


def test_drum_output_false_pins_complementary_steps() -> None:
    r = DrumCrossing().reverse(_drum(), None, eq_target("Y1", False), _ctx())
    assert r.branches == ((Eq("Step", frozenset({2})),), (Prior("Y1", "Y1", 1, 0),))


def test_drum_step_target_falls_through() -> None:
    assert DrumCrossing().reverse(_drum(), None, eq_target("Step", 1), _ctx()).fallthrough


# --- search -------------------------------------------------------------------


def test_search_found_inverts_to_existential() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    instr = SearchInstruction(blk.select(1, 3) >= 100, result=Int("R"), found=Bool("F"))
    r = SearchCrossing().reverse(instr, None, eq_target("F", True), _ctx())
    assert r.branches == (
        (
            Quant(
                kind="exists", block=("DS1", "DS2", "DS3"), op=">=", value=100, value_is_tag=False
            ),
        ),
    )
    assert r.exact is False


def test_search_result_address_falls_through() -> None:
    blk = Block("DS", TagType.INT, 1, 5)
    instr = SearchInstruction(blk.select(1, 3) >= 100, result=Int("R"), found=Bool("F"))
    assert SearchCrossing().reverse(instr, None, eq_target("R", 2), _ctx()).fallthrough


# --- modbus receive -----------------------------------------------------------


def test_modbus_receive_target_is_external() -> None:
    r = ModbusReceiveCrossing().reverse(None, None, eq_target("RemoteTemp", 72), _ctx())
    assert r.branches == ((External("RemoteTemp"),),)
    assert r.exact is True
