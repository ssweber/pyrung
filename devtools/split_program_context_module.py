"""Split ``src/pyrung/core/program/context.py`` into a package.

The split is intentionally conservative:

- Slice named top-level definitions from the monolith with ``ast``.
- Keep the package root thin, re-exporting the existing public API.
- Preserve a couple of private compatibility helpers used elsewhere.
- Use ``ruff`` afterwards for import cleanup and formatting.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/pyrung/core/program/context.py"
PACKAGE = ROOT / "src/pyrung/core/program/context"
FUTURE_IMPORT = "from __future__ import annotations\n"

STATE_NAMES = [
    "_rung_stack",
    "_forloop_active",
    "_current_rung",
    "_require_rung_context",
    "_push_rung_context",
    "_pop_rung_context",
    "_new_capture_context",
]

PROGRAM_NAMES = [
    "_validate_subroutine_name",
    "Program",
]

RUNG_NAMES = [
    "_set_scope_end_line",
    "Rung",
    "_MAX_COMMENT_LENGTH",
    "comment",
    "RungContext",
]

CONTROL_FLOW_NAMES = [
    "Subroutine",
    "SubroutineFunc",
    "subroutine",
    "ForLoop",
    "forloop",
    "Branch",
    "branch",
]

PUBLIC_EXPORTS = [
    "Branch",
    "ForLoop",
    "Program",
    "Rung",
    "RungContext",
    "Subroutine",
    "SubroutineFunc",
    "branch",
    "comment",
    "forloop",
    "subroutine",
]


def _node_name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def _slice_node(source_lines: list[str], node: ast.stmt) -> str:
    decorator_lines = getattr(node, "decorator_list", [])
    start = node.lineno
    if decorator_lines:
        start = min([start, *[decorator.lineno for decorator in decorator_lines]])
    end = node.end_lineno
    if end is None:
        raise RuntimeError(f"Node {type(node).__name__} is missing end_lineno in {SOURCE}")
    return "\n".join(source_lines[start - 1 : end]).rstrip() + "\n"


def _join_blocks(*blocks: str) -> str:
    parts = [block.strip() for block in blocks if block.strip()]
    return "\n\n".join(parts) + "\n"


def _top_level_nodes(module: ast.Module) -> dict[str, ast.stmt]:
    nodes: dict[str, ast.stmt] = {}
    for node in module.body:
        name = _node_name(node)
        if name is not None:
            nodes[name] = node
    return nodes


def _require_block(source_lines: list[str], nodes: dict[str, ast.stmt], name: str) -> str:
    node = nodes.get(name)
    if node is None:
        raise RuntimeError(f"Missing expected top-level symbol in {SOURCE}: {name}")
    return _slice_node(source_lines, node)


def _require_blocks(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    names: list[str],
) -> list[str]:
    return [_require_block(source_lines, nodes, name) for name in names]


def _extract_docstring_block(source_lines: list[str], module: ast.Module) -> str:
    if not module.body:
        raise RuntimeError(f"No top-level statements found in {SOURCE}")
    doc_node = module.body[0]
    if not (
        isinstance(doc_node, ast.Expr)
        and isinstance(doc_node.value, ast.Constant)
        and isinstance(doc_node.value.value, str)
    ):
        return '"""Program and rung context helpers."""\n'
    return _slice_node(source_lines, doc_node)


def _patch_new_capture_context(block: str) -> str:
    marker = '    """Create a lightweight Rung wrapper for temporary capture scopes."""\n'
    replacement = (
        '    """Create a lightweight Rung wrapper for temporary capture scopes."""\n'
        "    from ._rung import Rung\n\n"
    )
    if marker not in block:
        raise RuntimeError("Could not patch _new_capture_context() local import")
    return block.replace(marker, replacement, 1)


def _patch_control_flow_block(block: str) -> str:
    block = block.replace("        global _forloop_active\n\n", "")
    block = block.replace("        if _forloop_active:\n", "        if _state._forloop_active:\n")
    block = block.replace(
        "        _forloop_active = True\n", "        _state._forloop_active = True\n"
    )
    block = block.replace(
        "        _forloop_active = False\n",
        "        _state._forloop_active = False\n",
    )
    return block


def _render_state_module(source_lines: list[str], nodes: dict[str, ast.stmt]) -> str:
    new_capture_context = _patch_new_capture_context(
        _require_block(source_lines, nodes, "_new_capture_context")
    )
    body = _join_blocks(
        *_require_blocks(source_lines, nodes, STATE_NAMES[:6]),
        new_capture_context,
    )
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            from typing import TYPE_CHECKING

            from pyrung.core.rung import Rung as RungLogic

            if TYPE_CHECKING:
                from ._rung import Rung
            """
        ),
        body,
    )


def _render_program_module(source_lines: list[str], nodes: dict[str, ast.stmt]) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, PROGRAM_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            import inspect
            from typing import TYPE_CHECKING, Any, ClassVar

            from pyrung.core.instruction import SubroutineReturnSignal
            from pyrung.core.rung import Rung as RungLogic

            from ..validation import _check_with_body_from_frame

            if TYPE_CHECKING:
                from pyrung.core.context import ScanContext
                from pyrung.core.state import SystemState

                from ..validation import DialectValidator
            """
        ),
        body,
    )


def _render_rung_module(source_lines: list[str], nodes: dict[str, ast.stmt]) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, RUNG_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            import textwrap
            from typing import Any

            from pyrung.core._source import (
                _capture_source,
                _capture_with_call_arg_lines,
                _capture_with_end_line,
            )
            from pyrung.core.condition import ConditionTerm
            from pyrung.core.rung import Rung as RungLogic

            from ._program import Program
            from ._state import _pop_rung_context, _push_rung_context
            """
        ),
        body,
    )


def _render_control_flow_module(source_lines: list[str], nodes: dict[str, ast.stmt]) -> str:
    blocks = [
        _patch_control_flow_block(_require_block(source_lines, nodes, name))
        for name in CONTROL_FLOW_NAMES
    ]
    body = _join_blocks(*blocks)
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            from collections.abc import Callable
            from typing import Any

            from pyrung.core._source import _capture_source, _capture_with_call_arg_lines
            from pyrung.core.condition import ConditionTerm
            from pyrung.core.instruction import ForLoopInstruction
            from pyrung.core.rung import Rung as RungLogic
            from pyrung.core.tag import Tag, TagType

            from . import _state
            from ._program import Program, _validate_subroutine_name
            from ._rung import Rung, _set_scope_end_line
            from ._state import (
                _current_rung,
                _new_capture_context,
                _pop_rung_context,
                _push_rung_context,
                _require_rung_context,
            )
            from ..validation import _check_function_body_strict
            """
        ),
        body,
    )


def _render_package_init(docstring: str) -> str:
    exports = ", ".join(f'"{name}"' for name in PUBLIC_EXPORTS)
    return _join_blocks(
        docstring,
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            f"""
            from ._control_flow import (
                Branch,
                ForLoop,
                Subroutine,
                SubroutineFunc,
                branch,
                forloop,
                subroutine,
            )
            from ._program import Program, _validate_subroutine_name as _validate_subroutine_name
            from ._rung import Rung, RungContext, comment
            from ._state import _require_rung_context as _require_rung_context

            __all__ = [{exports}]
            """
        ),
    )


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Expected source module at {SOURCE}")

    if PACKAGE.exists() and any(PACKAGE.iterdir()):
        raise SystemExit(f"Refusing to overwrite existing package at {PACKAGE}")

    source_text = SOURCE.read_text(encoding="utf-8")
    source_lines = source_text.splitlines()
    module = ast.parse(source_text)
    nodes = _top_level_nodes(module)
    docstring = _extract_docstring_block(source_lines, module)

    PACKAGE.mkdir(parents=True, exist_ok=True)
    _write(PACKAGE / "_state.py", _render_state_module(source_lines, nodes))
    _write(PACKAGE / "_program.py", _render_program_module(source_lines, nodes))
    _write(PACKAGE / "_rung.py", _render_rung_module(source_lines, nodes))
    _write(PACKAGE / "_control_flow.py", _render_control_flow_module(source_lines, nodes))
    _write(PACKAGE / "__init__.py", _render_package_init(docstring))
    SOURCE.unlink()

    print(f"Split {SOURCE} -> {PACKAGE}")


if __name__ == "__main__":
    main()
