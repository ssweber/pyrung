"""Unit tests for ConditionTraceEngine."""

from __future__ import annotations

from typing import Any

from pyrung.core import Block, Bool, Int, TagType, all_of, any_of
from pyrung.core.condition import (
    BitCondition,
    Condition,
    FallingEdgeCondition,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.condition_trace import ConditionTraceEngine
from pyrung.core.context import ScanContext
from pyrung.core.state import SystemState
from pyrung.core.trace_formatter import TraceFormatter


def _ctx(
    tags: dict[str, bool | int | float | str] | None = None,
    memory: dict[str, Any] | None = None,
) -> ScanContext:
    state = SystemState()
    if tags:
        state = state.with_tags(tags)
    if memory:
        state = state.with_memory(memory)
    return ScanContext(state)


def _detail_map(details: list[dict[str, Any]]) -> dict[str, Any]:
    return {str(item["name"]): item["value"] for item in details}


def test_condition_trace_engine_evaluates_basic_contacts_and_edges() -> None:
    engine = ConditionTraceEngine(formatter=TraceFormatter())
    button = Bool("Button")
    step = Int("Step")

    bit_cond = BitCondition(button)
    bit_value, bit_details = engine.evaluate(bit_cond, _ctx({"Button": True}))
    assert bit_value is True
    assert _detail_map(bit_details) == {"tag": "Button", "value": True}
    assert engine.expression(bit_cond) == "Button"

    int_cond = IntTruthyCondition(step)
    int_value, int_details = engine.evaluate(int_cond, _ctx({"Step": 2}))
    assert int_value is True
    assert _detail_map(int_details) == {"tag": "Step", "value": 2}
    assert engine.expression(int_cond) == "Step != 0"

    nc_cond = NormallyClosedCondition(button)
    nc_value, nc_details = engine.evaluate(nc_cond, _ctx({"Button": False}))
    assert nc_value is True
    assert _detail_map(nc_details) == {"tag": "Button", "value": False}
    assert engine.expression(nc_cond) == "!Button"

    rise_cond = RisingEdgeCondition(button)
    rise_value, rise_details = engine.evaluate(
        rise_cond,
        _ctx({"Button": True}, {"_prev:Button": False}),
    )
    assert rise_value is True
    assert _detail_map(rise_details) == {"tag": "Button", "current": True, "previous": False}
    assert engine.expression(rise_cond) == "rise(Button)"

    fall_cond = FallingEdgeCondition(button)
    fall_value, fall_details = engine.evaluate(
        fall_cond,
        _ctx({"Button": False}, {"_prev:Button": True}),
    )
    assert fall_value is True
    assert _detail_map(fall_details) == {"tag": "Button", "current": False, "previous": True}
    assert engine.expression(fall_cond) == "fall(Button)"


def test_condition_trace_engine_evaluates_direct_indirect_and_expr_comparisons() -> None:
    engine = ConditionTraceEngine(formatter=TraceFormatter())
    step = Int("Step")
    debug_step = Int("DebugStep")
    cur_step = Int("CurStep")
    step_block = Block(
        "Step",
        TagType.INT,
        0,
        9,
        address_formatter=lambda name, addr: f"{name}[{addr}]",
    )

    direct_cond = step == 1
    direct_value, direct_details = engine.evaluate(direct_cond, _ctx({"Step": 0}))
    assert direct_value is False
    assert _detail_map(direct_details) == {"left": "Step", "left_value": 0, "right_value": 1}
    assert engine.expression(direct_cond) == "Step == 1"

    left_indirect_cond = step_block[cur_step] == debug_step
    left_indirect_value, left_indirect_details = engine.evaluate(
        left_indirect_cond,
        _ctx({"CurStep": 1, "Step[1]": 0, "DebugStep": 5}),
    )
    assert left_indirect_value is False
    assert _detail_map(left_indirect_details) == {
        "left": "Step[1]",
        "left_value": 0,
        "right": "DebugStep",
        "right_value": 5,
        "left_pointer_expr": "Step[CurStep]",
        "left_pointer": "CurStep",
        "left_pointer_value": 1,
    }
    assert engine.expression(left_indirect_cond) == "Step[CurStep] == DebugStep"

    right_indirect_cond = debug_step == step_block[cur_step]
    right_indirect_value, right_indirect_details = engine.evaluate(
        right_indirect_cond,
        _ctx({"CurStep": 1, "Step[1]": 0, "DebugStep": 0}),
    )
    assert right_indirect_value is True
    assert _detail_map(right_indirect_details) == {
        "left": "DebugStep",
        "left_value": 0,
        "right": "Step[1]",
        "right_value": 0,
        "right_pointer_expr": "Step[CurStep]",
        "right_pointer": "CurStep",
        "right_pointer_value": 1,
    }
    assert engine.expression(right_indirect_cond) == "DebugStep == Step[CurStep]"

    expr_cond = (step + 1) > 0
    expr_value, expr_details = engine.evaluate(expr_cond, _ctx({"Step": 0}))
    assert expr_value is True
    expr_detail_map = _detail_map(expr_details)
    assert expr_detail_map["left"] == repr(expr_cond.left)
    assert expr_detail_map["left_value"] == 1
    assert expr_detail_map["right"] == repr(expr_cond.right)
    assert expr_detail_map["right_value"] == 0
    assert engine.expression(expr_cond) == f"{expr_cond.left!r} > {expr_cond.right!r}"


def test_condition_trace_engine_short_circuit_terms_and_composite_expressions() -> None:
    engine = ConditionTraceEngine(formatter=TraceFormatter())
    step = Int("Step")
    auto_mode = Bool("AutoMode")

    all_cond = all_of(step == 1, auto_mode)
    all_value, all_details = engine.evaluate(all_cond, _ctx({"Step": 0, "AutoMode": True}))
    assert all_value is False
    all_terms = str(_detail_map(all_details)["terms"])
    assert "Step(0) == 1(false)" in all_terms
    assert "AutoMode(skipped)" in all_terms
    assert engine.expression(all_cond) == "(Step == 1 & AutoMode)"

    any_cond = any_of(step == 0, auto_mode)
    any_value, any_details = engine.evaluate(any_cond, _ctx({"Step": 0, "AutoMode": False}))
    assert any_value is True
    any_terms = str(_detail_map(any_details)["terms"])
    assert "Step(0) == 0(true)" in any_terms
    assert "AutoMode(skipped)" in any_terms
    assert engine.expression(any_cond) == "(Step == 0 | AutoMode)"


def test_condition_trace_engine_unknown_condition_uses_fallbacks() -> None:
    class AlwaysTrueCondition(Condition):
        def evaluate(self, ctx: ScanContext) -> bool:
            _ = ctx
            return True

    engine = ConditionTraceEngine(formatter=TraceFormatter())
    cond = AlwaysTrueCondition()
    value, details = engine.evaluate(cond, _ctx())
    assert value is True
    assert details == []
    assert engine.expression(cond) == "AlwaysTrueCondition"
    assert engine.summary(cond, details) == "AlwaysTrueCondition"
    assert (
        engine.annotation(
            status="true",
            expression="AlwaysTrueCondition",
            summary="AlwaysTrueCondition",
        )
        == "[T] AlwaysTrueCondition"
    )
