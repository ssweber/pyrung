"""Keep pyrung-authored runtime text safe for ordinary terminals."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).parents[1] / "src" / "pyrung"
_UTF8_GENERATORS = {
    Path("click/codegen/project_emitter.py"),
    Path("circuitpy/codegen/render_runtime.py"),
}


def _documentation_literals(tree: ast.AST) -> set[int]:
    """String expressions are docstrings, including attribute docstrings."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def test_pyrung_authored_runtime_literals_are_ascii() -> None:
    violations: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC)
        if relative in _UTF8_GENERATORS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        documentation = _documentation_literals(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in documentation or node.value.isascii():
                continue
            escaped = ascii(node.value[:120])
            violations.append(f"{relative}:{node.lineno}: {escaped}")

    assert not violations, "Non-ASCII runtime string literals:\n" + "\n".join(violations)
