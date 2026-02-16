"""Source-location capture helpers for DSL metadata."""

from __future__ import annotations

import ast
import inspect
from functools import lru_cache
from pathlib import Path


def _capture_source(depth: int = 2) -> tuple[str | None, int | None]:
    """Capture (filename, line number) from the caller stack."""
    frame = inspect.currentframe()
    try:
        for _ in range(depth):
            if frame is None:
                return (None, None)
            frame = frame.f_back
        if frame is None:
            return (None, None)
        return (frame.f_code.co_filename, frame.f_lineno)
    finally:
        del frame


def _capture_with_end_line(
    source_file: str | None,
    start_line: int | None,
    *,
    context_name: str | None = None,
) -> int | None:
    """Best-effort lookup of a with-statement end line from source."""
    if source_file is None or start_line is None:
        return None
    if source_file.startswith("<"):
        return None

    tree = _parse_file_ast(source_file)
    if tree is None:
        return None

    candidates: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if getattr(node, "lineno", None) != start_line:
            continue

        end_line = getattr(node, "end_lineno", None)
        if end_line is None:
            continue

        if context_name is None:
            candidates.append((0, end_line))
            continue

        score = 1
        for item in node.items:
            name = _context_expr_name(item.context_expr)
            if name == context_name:
                score = 0
                break
        candidates.append((score, end_line))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]


@lru_cache(maxsize=256)
def _parse_file_ast(source_file: str) -> ast.AST | None:
    try:
        source = Path(source_file).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return ast.parse(source, filename=source_file)
    except SyntaxError:
        return None


def _context_expr_name(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Call):
        return _context_expr_name(expr.func)
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None
