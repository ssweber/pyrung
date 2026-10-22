"""Tests for source-shaped validation and analysis rendering."""

from pyrung import Bool, Timer
from pyrung.core.condition import AllCondition
from pyrung.core.validation.render import operand_name, render_condition


def test_boolean_literal_comparisons_render_as_contacts() -> None:
    flag = Bool("RenderTest_Flag")

    assert render_condition(flag == False) == "~RenderTest_Flag"  # noqa: E712
    assert render_condition(flag != True) == "~RenderTest_Flag"  # noqa: E712
    assert render_condition(flag == True) == "RenderTest_Flag"  # noqa: E712
    assert render_condition(flag != False) == "RenderTest_Flag"  # noqa: E712


def test_structure_field_uses_its_source_access_path() -> None:
    timer = Timer.clone("RenderTest_Timer")
    sensor = Bool("RenderTest_Sensor")

    assert timer.Done.name == "RenderTest_Timer_Done"
    assert operand_name(timer.Done) == "RenderTest_Timer.Done"
    guard = AllCondition(timer.Done == False, sensor != True)  # noqa: E712
    assert render_condition(guard) == "And(~RenderTest_Timer.Done, ~RenderTest_Sensor)"


def test_render_expr_spells_functions_and_range_aggregates_as_source() -> None:
    from pyrung import Block, Int, TagType
    from pyrung.core.expression import lsh, sqrt
    from pyrung.core.validation.render import render_expr

    x = Int("RenderTest_X")
    ds = Block("DS", TagType.INT, 1, 10)

    assert render_expr(abs(x)) == "abs(RenderTest_X)"
    assert render_expr(sqrt(x)) == "sqrt(RenderTest_X)"
    assert render_expr(lsh(x, 2)) == "lsh(RenderTest_X, 2)"
    assert render_expr(ds.select(1, 5).sum()) == "DS.select(1, 5).sum()"
    assert render_expr(ds.select(1, 5).median()) == "DS.select(1, 5).median()"
    assert render_expr(ds.select(1, 5).max() + 1) == "(DS.select(1, 5).max() + 1)"
