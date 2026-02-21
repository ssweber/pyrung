"""Direct unit tests for TraceFormatter."""

from __future__ import annotations

from pyrung.core.trace_formatter import TraceFormatter


def test_condition_annotation_status_labels_and_fallback() -> None:
    assert (
        TraceFormatter.condition_annotation(status="skipped", expression="A == 1", summary="ignored")
        == "[SKIP] A == 1"
    )
    assert TraceFormatter.condition_annotation(status="false", expression="A", summary="A(False)") == "[F] A(False)"
    assert TraceFormatter.condition_annotation(status="true", expression="A", summary=" ") == "[T] A"


def test_condition_detail_map_ignores_invalid_entries() -> None:
    details = [
        {"name": "left", "value": "A"},
        {"name": "left_value", "value": 10},
        {"name": 123, "value": "ignored"},
        "not-a-dict",
    ]
    assert TraceFormatter.condition_detail_map(details) == {"left": "A", "left_value": 10}


def test_comparison_parts_parses_binary_expressions() -> None:
    assert TraceFormatter.comparison_parts(" A >= 5 ") == ("A", ">=", "5")
    assert TraceFormatter.comparison_parts("A+1") is None


def test_is_literal_operand_detects_numbers_booleans_and_quoted_values() -> None:
    assert TraceFormatter.is_literal_operand("42") is True
    assert TraceFormatter.is_literal_operand("-2.5") is True
    assert TraceFormatter.is_literal_operand("TRUE") is True
    assert TraceFormatter.is_literal_operand("'A'") is True
    assert TraceFormatter.is_literal_operand('"B"') is True
    assert TraceFormatter.is_literal_operand("Tag1") is False


def test_comparison_right_text_priority_rules() -> None:
    assert TraceFormatter.comparison_right_text("B", {"right": "Other", "right_value": 7}) == "Other(7)"
    assert TraceFormatter.comparison_right_text("5", {"right_value": 5}) == "5"
    assert TraceFormatter.comparison_right_text("B", {"right_value": 9}) == "B(9)"
    assert TraceFormatter.comparison_right_text("B", {"right": "Resolved"}) == "Resolved"
    assert TraceFormatter.comparison_right_text("B", {}) == "B"


def test_condition_term_text_with_comparison_and_resolved_right() -> None:
    details = [
        {"name": "left", "value": "A"},
        {"name": "left_value", "value": 10},
        {"name": "right", "value": "B"},
        {"name": "right_value", "value": 5},
    ]
    assert TraceFormatter.condition_term_text(expression="A > B", details=details) == "A(10) > B(5)"


def test_condition_term_text_with_literal_rhs_falls_back_to_expression_literal() -> None:
    details = [
        {"name": "left", "value": "A"},
        {"name": "left_value", "value": 3},
        {"name": "right_value", "value": 7},
    ]
    assert TraceFormatter.condition_term_text(expression="A == 7", details=details) == "A(3) == 7"


def test_condition_term_text_with_non_comparison_expression_uses_rhs_fallback() -> None:
    details = [
        {"name": "left", "value": "A"},
        {"name": "left_value", "value": 3},
        {"name": "right_value", "value": 7},
    ]
    assert TraceFormatter.condition_term_text(expression="A", details=details) == "A(3), rhs(7)"


def test_condition_term_text_supports_tag_value_and_edge_forms() -> None:
    assert TraceFormatter.condition_term_text(
        expression="ignored",
        details=[{"name": "tag", "value": "X"}, {"name": "value", "value": True}],
    ) == "X(True)"

    assert TraceFormatter.condition_term_text(
        expression="ignored",
        details=[
            {"name": "tag", "value": "Pulse"},
            {"name": "current", "value": False},
            {"name": "previous", "value": True},
        ],
    ) == "Pulse(False) prev(True)"


def test_condition_term_text_supports_terms_and_expression_fallback() -> None:
    assert TraceFormatter.condition_term_text(
        expression="ignored",
        details=[{"name": "terms", "value": "A(True) and B(False)"}],
    ) == "A(True) and B(False)"
    assert TraceFormatter.condition_term_text(expression="A", details=[]) == "A"
