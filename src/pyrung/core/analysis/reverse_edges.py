"""Reverse-edge computation for backward value propagation.

Given a program with instructions like ``copy(X, Y)`` or ``calc(X + 5, Y)``,
build a map from source tags to target tags with inverse transforms.  This
lets a snapshot value for the target be back-propagated to constrain the
source: if ``Y = X + 5`` and ``Y = 42``, then ``X = 37``.

Used by both the prover (to seed BFS domains) and diagnosis (to narrow
snapshot explanations).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.program import Program

InvertFn = Callable[[Any], Any]
IDENTITY: InvertFn = lambda v: v


def tag_name_from_value(value: Any) -> str | None:
    """Extract a source tag name from a raw instruction operand/expression node."""
    from pyrung.core.expression import TagExpr
    from pyrung.core.tag import ImmediateRef, Tag

    raw = value.value if isinstance(value, ImmediateRef) else value
    if isinstance(raw, Tag):
        return raw.name
    if isinstance(raw, TagExpr):
        return raw.tag.name
    return None


def literal_value_from_value(value: Any) -> Any | None:
    """Extract a plain literal value from a raw instruction operand/expression node."""
    from pyrung.core.expression import LiteralExpr
    from pyrung.core.tag import ImmediateRef

    raw = value.value if isinstance(value, ImmediateRef) else value
    if isinstance(raw, LiteralExpr):
        return raw.value
    if isinstance(raw, (bool, int, float)):
        return raw
    return None


def compose_invert(outer: InvertFn, inner: InvertFn) -> InvertFn:
    """Chain two inverse transforms: ``compose_invert(f, g)(v) == f(g(v))``."""
    if inner is IDENTITY:
        return outer
    if outer is IDENTITY:
        return inner

    def _composed(v: Any) -> Any:
        mid = inner(v)
        return outer(mid) if mid is not None else None

    return _composed


def calc_reverse_edge(
    expression: Any,
) -> tuple[str, InvertFn] | None:
    """Extract ``(source_tag_name, invert_fn)`` from a calc expression.

    Returns the inverse transform for single-source-tag expressions of the
    form ``source ± literal``, ``literal ± source``, ``source * literal``,
    ``literal * source``, ``+source``, or ``-source``.  The invert function
    maps a target comparison value back to the source value that produces it,
    returning ``None`` when the preimage is not exact (e.g. non-integer
    division for ``*``).
    """
    from pyrung.core.expression import BinaryExpr, UnaryExpr

    if isinstance(expression, UnaryExpr):
        name = tag_name_from_value(expression.operand)
        if name is None:
            return None
        if expression.symbol == "+":
            return name, IDENTITY
        if expression.symbol == "-":
            return name, lambda v: -v
        return None

    if not isinstance(expression, BinaryExpr):
        return None
    if expression.symbol not in ("+", "-", "*"):
        return None

    left_tag = tag_name_from_value(expression.left)
    left_lit = literal_value_from_value(expression.left)
    right_tag = tag_name_from_value(expression.right)
    right_lit = literal_value_from_value(expression.right)

    if left_tag is not None and right_lit is not None and isinstance(right_lit, (int, float)):
        if expression.symbol == "+":
            return left_tag, lambda v, k=right_lit: v - k
        if expression.symbol == "-":
            return left_tag, lambda v, k=right_lit: v + k
        if expression.symbol == "*":
            if right_lit == 0:
                return None
            return (
                left_tag,
                lambda v, k=right_lit: (
                    v // k if isinstance(v, int) and isinstance(k, int) and v % k == 0 else None
                ),
            )

    if right_tag is not None and left_lit is not None and isinstance(left_lit, (int, float)):
        if expression.symbol == "+":
            return right_tag, lambda v, k=left_lit: v - k
        if expression.symbol == "-":
            return right_tag, lambda v, k=left_lit: k - v
        if expression.symbol == "*":
            if left_lit == 0:
                return None
            return (
                right_tag,
                lambda v, k=left_lit: (
                    v // k if isinstance(v, int) and isinstance(k, int) and v % k == 0 else None
                ),
            )

    return None


def build_reverse_edge_map(
    program: Program,
    *,
    expand_indirect: Callable[[Any], list[str]] | None = None,
) -> dict[str, list[tuple[str, InvertFn]]]:
    """Build a map from source tags to ``(target_tag, invert_fn)`` pairs.

    Walks all Copy, Fill, BlockCopy, and Calc instructions.  For each,
    extracts the source→target relationship and the inverse transform that
    maps a target value back to its source value.

    Args:
        program: The compiled program to analyze.
        expand_indirect: Optional callback to resolve indirect memory
            references to concrete tag names.  When ``None``, indirect
            refs that ``_resolve_tag_names`` can't handle are skipped.
    """
    from pyrung.core.instruction.calc import CalcInstruction
    from pyrung.core.instruction.data_transfer import (
        BlockCopyInstruction,
        CopyInstruction,
        FillInstruction,
    )
    from pyrung.core.validation._common import _resolve_tag_names, walk_instructions

    _expand = expand_indirect or (lambda dest: [])

    reverse_edges: dict[str, list[tuple[str, InvertFn]]] = {}

    for instr in walk_instructions(program):
        if isinstance(instr, CopyInstruction):
            if instr.convert is not None:
                continue
            source_name = tag_name_from_value(instr.source)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand(instr.dest)
            for target_name in target_names:
                reverse_edges.setdefault(source_name, []).append((target_name, IDENTITY))

        elif isinstance(instr, FillInstruction):
            source_name = tag_name_from_value(instr.value)
            if source_name is None:
                continue
            target_names = _resolve_tag_names(instr.dest)
            if not target_names:
                target_names = _expand(instr.dest)
            for target_name in target_names:
                reverse_edges.setdefault(source_name, []).append((target_name, IDENTITY))

        elif isinstance(instr, BlockCopyInstruction):
            if instr.convert is not None:
                continue
            source_names = _resolve_tag_names(instr.source)
            dest_names = _resolve_tag_names(instr.dest)
            if not dest_names:
                dest_names = _expand(instr.dest)
            if source_names and dest_names:
                if len(source_names) == len(dest_names):
                    for src, dst in zip(source_names, dest_names, strict=True):
                        reverse_edges.setdefault(src, []).append((dst, IDENTITY))
                else:
                    for src in source_names:
                        for dst in dest_names:
                            reverse_edges.setdefault(src, []).append((dst, IDENTITY))

        elif isinstance(instr, CalcInstruction):
            target_name = tag_name_from_value(instr.dest)
            if target_name is None:
                continue
            edge = calc_reverse_edge(instr.expression)
            if edge is not None:
                source_name, invert = edge
                reverse_edges.setdefault(source_name, []).append((target_name, invert))

    return reverse_edges
