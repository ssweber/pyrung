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
