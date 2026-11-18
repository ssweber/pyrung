from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONDITION_FILE = PROJECT_ROOT / "src" / "pyrung" / "core" / "condition.py"
TESTS_DIR = PROJECT_ROOT / "tests"


def _is_comparison_class(name: str) -> bool:
    return name.startswith("Compare") or name.startswith("IndirectCompare")


def _is_ctx_get_tag_without_default(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "ctx"
        and call.func.attr == "get_tag"
        and len(call.args) == 1
        and len(call.keywords) == 0
    )


class CompareDefaultVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self._class_stack: list[str] = []
        self.violations: list[tuple[int, str, str]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        class_name = self._class_stack[-1] if self._class_stack else ""
        if _is_comparison_class(class_name) and _is_ctx_get_tag_without_default(node):
            self.violations.append((node.lineno, class_name, "ctx.get_tag(...) missing default"))
        self.generic_visit(node)


def _calls_rung_class(expr: ast.expr) -> bool:
    # Walk down the call chain so that `Rung(A).continued()` counts as a call to Rung.
    while isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name):
            return func.id == "Rung"
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr == "Rung":
            return True
        expr = func.value
    return False


def find_with_rung_class(paths: list[Path]) -> list[tuple[Path, int]]:
    """Return the file and line of each `with Rung(...)` in *paths*.

    The tests call the DSL as `rung`. This check catches a `with Rung(` that a new test or
    an older branch brings back. It does not read code inside strings.
    """
    found = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        # Parsing every test file costs seconds, and most of them never write `Rung(`.
        if "Rung(" not in source:
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                found.extend(
                    (path, item.context_expr.lineno)
                    for item in node.items
                    if _calls_rung_class(item.context_expr)
                )
    return found


def main() -> int:
    source = CONDITION_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CONDITION_FILE))
    visitor = CompareDefaultVisitor()
    visitor.visit(tree)
    with_rung = find_with_rung_class(sorted(TESTS_DIR.rglob("*.py")))

    if not visitor.violations and not with_rung:
        print("semantic-lint: ok")
        return 0

    if visitor.violations:
        print("semantic-lint: found comparison get_tag calls without defaults:")
        for line, class_name, message in visitor.violations:
            print(f"  - {CONDITION_FILE}:{line} ({class_name}): {message}")
    if with_rung:
        print("semantic-lint: found `with Rung(` in tests. Use `with rung(` instead:")
        for path, line in with_rung:
            print(f"  - {path}:{line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
