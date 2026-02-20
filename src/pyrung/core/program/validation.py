from __future__ import annotations

import ast
import inspect
import textwrap
import warnings
from collections.abc import Callable
from types import FrameType
from typing import Any


class ForbiddenControlFlowError(RuntimeError):
    """Raised when Python control flow is used inside strict DSL scope."""


_IF_HINT = "Use `Rung(condition)` to express conditional logic"


_BOOL_HINT = "Use `all_of()` / `any_of()` for compound conditions"


_NOT_HINT = "Use `nc()` for normally-closed contacts"


_LOOP_HINT = "Each rung is independent; express repeated patterns as separate rungs"


_ASSIGN_HINT = "DSL instructions write to tags directly; no intermediate Python variables needed"


_TRY_HINT = "Errors in DSL scope are programming mistakes; no recovery logic in ladder logic"


_COMPREHENSION_HINT = (
    "Build tag collections outside the Program scope, then reference them in rungs"
)


_SCOPE_HINT = "DSL scope should not mutate external Python state"


_RETURN_HINT = "Use `return_()` for early subroutine exit; no Python control flow in DSL scope"


_IMPORT_HINT = "Move imports outside the Program/subroutine scope"


_ASSERT_HINT = "Not valid in ladder logic; handle validation outside DSL scope"


_DEF_HINT = "Define functions and classes outside the Program/subroutine scope"


_GENERIC_STMT_HINT = "Only `with ...:`, bare function calls, and `pass` are allowed in DSL scope"


DialectValidator = Callable[..., Any]


_EXPR_STMT_HINT = "Only bare call expressions are allowed as statements"


_FORBIDDEN_NODE_RULES: tuple[tuple[tuple[type[ast.AST], ...], str | None, str], ...] = (
    ((ast.If, ast.IfExp), "if/elif/else", _IF_HINT),
    ((ast.BoolOp,), "and/or", _BOOL_HINT),
    ((ast.For, ast.AsyncFor, ast.While), "for/while", _LOOP_HINT),
    ((ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr), "assignment", _ASSIGN_HINT),
    ((ast.Try,), "try/except", _TRY_HINT),
    (
        (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension),
        "comprehension/generator",
        _COMPREHENSION_HINT,
    ),
    ((ast.Global, ast.Nonlocal), "global/nonlocal", _SCOPE_HINT),
    ((ast.Yield, ast.YieldFrom, ast.Await, ast.Return), "yield/await/return", _RETURN_HINT),
    ((ast.Import, ast.ImportFrom), "import", _IMPORT_HINT),
    ((ast.Assert, ast.Raise, ast.Delete), "assert/raise/del", _ASSERT_HINT),
    ((ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef), None, _DEF_HINT),
    ((ast.Expr,), "expression statement", _EXPR_STMT_HINT),
    ((ast.stmt,), None, _GENERIC_STMT_HINT),
)


def _warn_check_skipped(target: str, reason: Exception) -> None:
    """Warn and skip strict checking when source inspection/parsing is unavailable."""
    warnings.warn(
        f"Unable to perform strict DSL control-flow check for {target}: {reason}",
        RuntimeWarning,
        stacklevel=3,
    )


def _absolute_line(node: ast.AST, line_offset: int) -> int:
    lineno = getattr(node, "lineno", 1)
    return line_offset + lineno - 1


def _describe_forbidden_node(node: ast.AST) -> tuple[str, str]:
    """Return user-facing construct label and DSL hint for a forbidden node."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return "not", _NOT_HINT
    for node_types, label, hint in _FORBIDDEN_NODE_RULES:
        if isinstance(node, node_types):
            return (type(node).__name__ if label is None else label), hint
    return type(node).__name__, "This construct is not allowed in strict DSL scope"


def _raise_forbidden_node(
    node: ast.AST,
    *,
    filename: str,
    line_offset: int,
    opt_out_hint: str,
) -> None:
    construct, hint = _describe_forbidden_node(node)
    line = _absolute_line(node, line_offset)
    raise ForbiddenControlFlowError(
        f"{filename}:{line}: forbidden Python construct '{construct}' in strict DSL scope. "
        f"{hint}. Opt out with {opt_out_hint}."
    )


def _iter_expression_nodes(node: ast.AST) -> list[ast.AST]:
    """Iterate expression graph for a statement, excluding nested statements."""
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        nodes.append(current)
        children = list(ast.iter_child_nodes(current))
        for child in reversed(children):
            if isinstance(child, ast.stmt):
                continue
            stack.append(child)
    return nodes


def _check_expression_tree(
    node: ast.AST,
    *,
    filename: str,
    line_offset: int,
    opt_out_hint: str,
) -> None:
    for child in _iter_expression_nodes(node):
        if isinstance(child, ast.BoolOp):
            _raise_forbidden_node(
                child,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
        if isinstance(child, ast.UnaryOp) and isinstance(child.op, ast.Not):
            _raise_forbidden_node(
                child,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
        if isinstance(
            child,
            (
                ast.IfExp,
                ast.NamedExpr,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.comprehension,
            ),
        ):
            _raise_forbidden_node(
                child,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )


def _check_statement_list(
    statements: list[ast.stmt],
    *,
    filename: str,
    line_offset: int,
    opt_out_hint: str,
) -> None:
    for statement in statements:
        if isinstance(statement, ast.Pass):
            continue

        if isinstance(statement, ast.Expr):
            if not isinstance(statement.value, ast.Call):
                _raise_forbidden_node(
                    statement,
                    filename=filename,
                    line_offset=line_offset,
                    opt_out_hint=opt_out_hint,
                )
            _check_expression_tree(
                statement.value,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
            continue

        if isinstance(statement, ast.With):
            for item in statement.items:
                _check_expression_tree(
                    item.context_expr,
                    filename=filename,
                    line_offset=line_offset,
                    opt_out_hint=opt_out_hint,
                )
                if item.optional_vars is not None:
                    _check_expression_tree(
                        item.optional_vars,
                        filename=filename,
                        line_offset=line_offset,
                        opt_out_hint=opt_out_hint,
                    )
            _check_statement_list(
                statement.body,
                filename=filename,
                line_offset=line_offset,
                opt_out_hint=opt_out_hint,
            )
            continue

        _raise_forbidden_node(
            statement,
            filename=filename,
            line_offset=line_offset,
            opt_out_hint=opt_out_hint,
        )


def _check_function_body_strict(
    fn: Callable[[], None],
    *,
    opt_out_hint: str,
    source_label: str,
) -> None:
    try:
        source_lines, start_line = inspect.getsourcelines(fn)
        source = textwrap.dedent("".join(source_lines))
        module = ast.parse(source)
    except (OSError, TypeError, SyntaxError) as exc:
        _warn_check_skipped(source_label, exc)
        return

    function_nodes = [
        node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not function_nodes:
        _warn_check_skipped(source_label, RuntimeError("function body AST not found"))
        return

    filename = inspect.getsourcefile(fn) or inspect.getfile(fn)
    _check_statement_list(
        function_nodes[0].body,
        filename=filename,
        line_offset=start_line - 1,
        opt_out_hint=opt_out_hint,
    )


def _find_enclosing_with(module: ast.Module, line_number: int) -> ast.With | None:
    matches: list[ast.With] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.With):
            continue
        end_line = getattr(node, "end_lineno", node.lineno)
        if node.lineno <= line_number <= end_line:
            matches.append(node)

    if not matches:
        return None
    return max(matches, key=lambda node: node.lineno)


def _check_with_body_from_frame(frame: FrameType, *, opt_out_hint: str) -> None:
    code = frame.f_code
    source_label = f"{code.co_filename}:{frame.f_lineno}"
    try:
        source_lines, start_line = inspect.getsourcelines(code)
        source = textwrap.dedent("".join(source_lines))
        module = ast.parse(source)
    except (OSError, TypeError, SyntaxError) as exc:
        _warn_check_skipped(source_label, exc)
        return

    relative_line = frame.f_lineno - start_line + 1
    with_node = _find_enclosing_with(module, relative_line)
    if with_node is None:
        _warn_check_skipped(source_label, RuntimeError("enclosing with-statement AST not found"))
        return

    filename = inspect.getsourcefile(code) or code.co_filename
    _check_statement_list(
        with_node.body,
        filename=filename,
        line_offset=start_line - 1,
        opt_out_hint=opt_out_hint,
    )
