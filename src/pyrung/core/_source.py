"""Source-location capture helpers for DSL metadata."""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class _WithEndCandidate:
    end_line: int
    context_names: tuple[str | None, ...]


@dataclass(frozen=True)
class _WithArgCandidate:
    context_name: str | None
    arg_lines: tuple[int, ...]


@dataclass(frozen=True)
class _CallEndCandidate:
    call_name: str | None
    end_line: int


@dataclass(frozen=True)
class _SourceAstIndex:
    with_end_by_line: dict[int, tuple[_WithEndCandidate, ...]]
    with_args_by_line: dict[int, tuple[_WithArgCandidate, ...]]
    call_end_by_line: dict[int, tuple[_CallEndCandidate, ...]]


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

    index = _build_source_ast_index(source_file)
    if index is None:
        return None

    candidates = index.with_end_by_line.get(start_line, ())
    if not candidates:
        return None

    best = min(
        candidates,
        key=lambda candidate: 0 if context_name in candidate.context_names else 1,
    )
    return best.end_line


def _capture_with_call_arg_lines(
    source_file: str | None,
    start_line: int | None,
    *,
    context_name: str | None = None,
) -> list[int]:
    """Best-effort lookup of `with` context-manager call argument line numbers."""
    if source_file is None or start_line is None:
        return []
    if source_file.startswith("<"):
        return []

    index = _build_source_ast_index(source_file)
    if index is None:
        return []

    candidates = index.with_args_by_line.get(start_line, ())
    if not candidates:
        return []

    best = min(
        candidates,
        key=lambda candidate: (
            0 if context_name is None or candidate.context_name == context_name else 1
        ),
    )
    return list(best.arg_lines)


def _capture_call_end_line(
    source_file: str | None,
    start_line: int | None,
    *,
    call_name: str | None = None,
) -> int | None:
    """Best-effort lookup of a call-expression end line from source."""
    if source_file is None or start_line is None:
        return None
    if source_file.startswith("<"):
        return None

    index = _build_source_ast_index(source_file)
    if index is None:
        return None

    candidates = index.call_end_by_line.get(start_line, ())
    if not candidates:
        return None

    best = min(
        candidates,
        key=lambda candidate: (
            0 if call_name is None or candidate.call_name == call_name else 1,
            -candidate.end_line,
        ),
    )
    return best.end_line


@lru_cache(maxsize=256)
def _build_source_ast_index(source_file: str) -> _SourceAstIndex | None:
    try:
        source = Path(source_file).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        tree = ast.parse(source, filename=source_file)
    except SyntaxError:
        return None

    with_end_by_line: dict[int, list[_WithEndCandidate]] = {}
    with_args_by_line: dict[int, list[_WithArgCandidate]] = {}
    call_end_by_line: dict[int, list[_CallEndCandidate]] = {}

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            line = getattr(node, "lineno", None)
            end_line = getattr(node, "end_lineno", None)
            if line is None:
                continue

            if end_line is not None:
                with_end_by_line.setdefault(int(line), []).append(
                    _WithEndCandidate(
                        end_line=int(end_line),
                        context_names=tuple(
                            _context_expr_name(item.context_expr) for item in node.items
                        ),
                    )
                )

            arg_candidates = with_args_by_line.setdefault(int(line), [])
            for item in node.items:
                context_expr = item.context_expr
                if not isinstance(context_expr, ast.Call):
                    continue
                arg_candidates.append(
                    _WithArgCandidate(
                        context_name=_context_expr_name(context_expr.func),
                        arg_lines=tuple(
                            int(line_no)
                            for arg in context_expr.args
                            if (line_no := getattr(arg, "lineno", None)) is not None
                        ),
                    )
                )
            continue

        if isinstance(node, ast.Call):
            line = getattr(node, "lineno", None)
            end_line = getattr(node, "end_lineno", None)
            if line is None or end_line is None:
                continue
            call_end_by_line.setdefault(int(line), []).append(
                _CallEndCandidate(
                    call_name=_context_expr_name(node.func),
                    end_line=int(end_line),
                )
            )

    return _SourceAstIndex(
        with_end_by_line={line: tuple(candidates) for line, candidates in with_end_by_line.items()},
        with_args_by_line={
            line: tuple(candidates) for line, candidates in with_args_by_line.items()
        },
        call_end_by_line={line: tuple(candidates) for line, candidates in call_end_by_line.items()},
    )


def _context_expr_name(expr: ast.AST) -> str | None:
    if isinstance(expr, ast.Call):
        return _context_expr_name(expr.func)
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None
