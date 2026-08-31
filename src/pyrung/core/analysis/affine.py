"""Stable single-source affine-expression analysis.

The helpers here intentionally live below PDG and prover modules so static
analysis consumers can share one definition without importing each other.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.reverse_edges import (
    literal_value_from_value,
    tag_name_from_value,
)
from pyrung.core.expression import BinaryExpr, UnaryExpr
from pyrung.core.instruction.calc import CalcInstruction
from pyrung.core.instruction.data_transfer import CopyInstruction

AffineForm = tuple[str, int, int | float]


def extract_affine_expression(expression: Any) -> AffineForm | None:
    """Return ``(source, scale, offset)`` for a simple affine expression.

    Accepted forms are unary ``+source`` / ``-source``, ``source +/- literal``,
    and ``literal +/- source``. The scale is therefore always ``1`` or ``-1``.
    """
    if isinstance(expression, UnaryExpr):
        source = tag_name_from_value(expression.operand)
        if source is None:
            return None
        if expression.symbol == "+":
            return source, 1, 0
        if expression.symbol == "-":
            return source, -1, 0
        return None

    if not isinstance(expression, BinaryExpr) or expression.symbol not in ("+", "-"):
        return None

    left_tag = tag_name_from_value(expression.left)
    left_lit = literal_value_from_value(expression.left)
    right_tag = tag_name_from_value(expression.right)
    right_lit = literal_value_from_value(expression.right)

    if left_tag is not None and isinstance(right_lit, (int, float)):
        offset = right_lit if expression.symbol == "+" else -right_lit
        return left_tag, 1, offset

    if right_tag is not None and isinstance(left_lit, (int, float)):
        scale = 1 if expression.symbol == "+" else -1
        return right_tag, scale, left_lit

    return None


def extract_forward_affine(instruction: Any) -> AffineForm | None:
    """Return ``dest = scale * source + offset`` for a simple write."""
    if isinstance(instruction, CopyInstruction):
        if instruction.convert is not None:
            return None
        source = tag_name_from_value(instruction.source)
        return (source, 1, 0) if source is not None else None
    if isinstance(instruction, CalcInstruction):
        return extract_affine_expression(instruction.expression)
    return None


def extract_forward_offset(instruction: Any) -> tuple[str, int | float] | None:
    """Return ``(source, offset)`` for the scale-1 affine subset."""
    affine = extract_forward_affine(instruction)
    if affine is None or affine[1] != 1:
        return None
    return affine[0], affine[2]


__all__ = [
    "AffineForm",
    "extract_affine_expression",
    "extract_forward_affine",
    "extract_forward_offset",
]
