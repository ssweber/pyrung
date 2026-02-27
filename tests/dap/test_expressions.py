"""Tests for DAP condition expression parsing and compilation."""

from __future__ import annotations

from pyrung.core.state import SystemState
from pyrung.dap.expressions import (
    And,
    Compare,
    Literal,
    Not,
    Or,
    TagRef,
    compile,
    parse,
    validate,
)


def _state(tags: dict[str, bool | int | float | str]) -> SystemState:
    return SystemState().with_tags(tags)


def test_parse_truthy_tag() -> None:
    expr = parse("Fault")
    assert expr == Compare(tag=TagRef(name="Fault"), op=None, right=None)


def test_parse_negated_tag() -> None:
    expr = parse("~Fault")
    assert expr == Not(child=TagRef(name="Fault"))


def test_parse_comparison() -> None:
    expr = parse("MotorTemp > 100")
    assert expr == Compare(tag=TagRef(name="MotorTemp"), op=">", right=Literal(value=100))


def test_parse_comma_implicit_and() -> None:
    expr = parse("Fault, Pump")
    assert isinstance(expr, And)
    assert len(expr.children) == 2


def test_parse_mixed_operators() -> None:
    expr = parse("Running | ~Estop, Mode == 1")
    assert isinstance(expr, And)
    assert isinstance(expr.children[0], Or)
    assert isinstance(expr.children[1], Compare)


def test_parse_allows_parenthesized_comparisons_with_boolean_operators() -> None:
    and_expr = parse("Fault & (MotorTemp > 100)")
    or_expr = parse("Running | (Mode == 1)")
    assert isinstance(and_expr, And)
    assert isinstance(or_expr, Or)


def test_parse_rejects_unparenthesized_comparisons_with_boolean_operators() -> None:
    and_errors = validate("Fault & MotorTemp > 100")
    or_errors = validate("Running | Mode == 1")
    assert len(and_errors) == 1
    assert "must be parenthesized" in and_errors[0]
    assert len(or_errors) == 1
    assert "must be parenthesized" in or_errors[0]


def test_parse_all_of_any_of() -> None:
    all_expr = parse("all_of(Fault, Pump, Valve)")
    any_expr = parse("any_of(Low, High, Emergency)")
    assert isinstance(all_expr, And)
    assert len(all_expr.children) == 3
    assert isinstance(any_expr, Or)
    assert len(any_expr.children) == 3


def test_validate_reports_parse_errors() -> None:
    errors = validate("~(A | B)")
    assert len(errors) == 1
    assert "~ only supports single tag negation" in errors[0]


def test_compile_evaluates_against_system_state() -> None:
    predicate = compile(parse("Run, ~Stop, Temp > 100"))
    assert predicate(_state({"Run": True, "Stop": False, "Temp": 125})) is True
    assert predicate(_state({"Run": True, "Stop": True, "Temp": 125})) is False
    assert predicate(_state({"Run": True, "Stop": False, "Temp": 90})) is False


def test_compile_handles_string_bool_and_numeric_literals() -> None:
    predicate = compile(parse("Mode == 'Auto', Enabled == true, Count >= 2"))
    assert predicate(_state({"Mode": "Auto", "Enabled": True, "Count": 2})) is True
    assert predicate(_state({"Mode": "Manual", "Enabled": True, "Count": 3})) is False
