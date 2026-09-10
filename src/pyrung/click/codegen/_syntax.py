"""Validate CSV operand syntax before it becomes executable Python source.

This validates data accepted by the importer; it is not a sandbox for user
programs. Only the expression forms used by Click instructions are accepted.
"""

from __future__ import annotations

import ast
import json
import re


def _string_literal(value: str) -> str:
    """Render a Python string while retaining the generator's double quotes."""
    return json.dumps(value, ensure_ascii=False)


def _read_csv_string(source: str, start: int = 0) -> tuple[str, int]:
    """Decode Click's doubled quotes; backslashes are ordinary characters."""
    chars: list[str] = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if char != '"':
            chars.append(char)
        elif index + 1 < len(source) and source[index + 1] == '"':
            chars.append('"')
            index += 1
        else:
            return "".join(chars), index + 1
        index += 1
    raise ValueError(f"Malformed CSV string literal: {source!r}")


def _csv_string_value(source: str) -> str:
    value, end = _read_csv_string(source)
    if end != len(source):
        raise ValueError(f"Expected CSV string literal: {source!r}")
    return value


def _pythonize_csv_strings(source: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(source):
        if source[index] == '"':
            value, index = _read_csv_string(source, index)
            parts.append(_string_literal(value))
        elif source[index] in "'#":
            raise ValueError(f"Unsupported CSV operand expression: {source!r}")
        else:
            parts.append(source[index])
            index += 1
    return "".join(parts)


def _validate_expression(
    source: str,
    *,
    functions: set[str] | frozenset[str],
    constants: set[str] | frozenset[str],
    operand_pattern: re.Pattern[str],
    blocks: set[str] | frozenset[str],
) -> ast.expr:
    """Accept literals, addresses, arithmetic, comparisons and known calls.

    The caller normalizes Click operators and range syntax first. Validation
    deliberately precedes nickname substitution, so imported names cannot add
    Python expression capabilities.
    """

    def reject() -> None:
        raise ValueError(f"Unsupported CSV operand expression: {source!r}")

    try:
        root = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ValueError(f"Unsupported CSV operand expression: {source!r}") from None

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                reject()
        elif isinstance(node, ast.Name):
            if node.id not in constants and not operand_pattern.fullmatch(node.id):
                reject()
        elif isinstance(node, ast.BinOp):
            if not isinstance(
                node.op,
                (
                    ast.Add,
                    ast.Sub,
                    ast.Mult,
                    ast.Div,
                    ast.FloorDiv,
                    ast.Mod,
                    ast.Pow,
                    ast.BitAnd,
                    ast.BitOr,
                    ast.BitXor,
                    ast.LShift,
                    ast.RShift,
                ),
            ):
                reject()
            visit(node.left)
            visit(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert)):
                reject()
            visit(node.operand)
        elif isinstance(node, ast.Compare):
            if any(
                not isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                for op in node.ops
            ):
                reject()
            visit(node.left)
            for comparator in node.comparators:
                visit(comparator)
        elif isinstance(node, ast.List):
            for item in node.elts:
                visit(item)
        elif isinstance(node, ast.Subscript):
            if not isinstance(node.value, ast.Name) or node.value.id not in blocks:
                reject()
            visit(node.slice)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in functions:
                reject()
            for arg in node.args:
                visit(arg)
            for kwarg in node.keywords:
                if kwarg.arg is None:
                    reject()
                visit(kwarg.value)
        else:
            reject()

    try:
        visit(root.body)
    except RecursionError:
        reject()
    return root.body
