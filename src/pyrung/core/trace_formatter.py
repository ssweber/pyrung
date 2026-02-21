"""Trace formatting helpers for debugger condition output."""

from __future__ import annotations

import re
from typing import Any


class TraceFormatter:
    """Build stable, human-readable condition trace strings."""

    @staticmethod
    def condition_annotation(*, status: str, expression: str, summary: str) -> str:
        if status == "skipped":
            return f"[SKIP] {expression}"
        label = "F" if status == "false" else "T"
        text = summary.strip() if isinstance(summary, str) else ""
        if not text:
            text = expression
        return f"[{label}] {text}"

    @staticmethod
    def condition_detail_map(details: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for detail in details:
            if not isinstance(detail, dict):
                continue
            name = detail.get("name")
            if not isinstance(name, str):
                continue
            result[name] = detail.get("value")
        return result

    @staticmethod
    def comparison_parts(expression: str) -> tuple[str, str, str] | None:
        match = re.match(r"^(.+?)\s*(==|!=|<=|>=|<|>)\s*(.+)$", expression.strip())
        if match is None:
            return None
        left, operator, right = match.groups()
        return left.strip(), operator, right.strip()

    @staticmethod
    def is_literal_operand(text: str) -> bool:
        value = text.strip()
        if re.match(r"^[-+]?\d+(\.\d+)?$", value):
            return True
        if value.lower() in {"true", "false", "null", "none"}:
            return True
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            return True
        return False

    @classmethod
    def comparison_right_text(cls, right: str, details: dict[str, Any]) -> str:
        if "right" in details and "right_value" in details:
            return f"{details['right']}({details['right_value']})"
        if "right_value" in details:
            if cls.is_literal_operand(right):
                return right
            return f"{right}({details['right_value']})"
        if "right" in details:
            return str(details["right"])
        return right

    @classmethod
    def condition_term_text(cls, *, expression: str, details: list[dict[str, Any]]) -> str:
        detail_map = cls.condition_detail_map(details)

        if "left" in detail_map and "left_value" in detail_map:
            left_label = str(detail_map["left"])
            left_text = f"{left_label}({detail_map['left_value']})"
            comparison = cls.comparison_parts(expression)
            if comparison is not None:
                _left, operator, right = comparison
                right_text = cls.comparison_right_text(right, detail_map)
                return f"{left_text} {operator} {right_text}"
            if "right_value" in detail_map:
                return f"{left_text}, rhs({detail_map['right_value']})"
            return left_text

        if "tag" in detail_map and "value" in detail_map:
            return f"{detail_map['tag']}({detail_map['value']})"

        if "current" in detail_map or "previous" in detail_map:
            tag = str(detail_map.get("tag", "value"))
            current = detail_map.get("current", "?")
            previous = detail_map.get("previous", "?")
            return f"{tag}({current}) prev({previous})"

        if "terms" in detail_map:
            return str(detail_map["terms"])

        return expression
