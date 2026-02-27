from __future__ import annotations

import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONDITION_FILE = PROJECT_ROOT / "src" / "pyrung" / "core" / "condition.py"


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


def main() -> int:
    source = CONDITION_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CONDITION_FILE))
    visitor = CompareDefaultVisitor()
    visitor.visit(tree)

    if not visitor.violations:
        print("semantic-lint: ok")
        return 0

    print("semantic-lint: found comparison get_tag calls without defaults:")
    for line, class_name, message in visitor.violations:
        print(f"  - {CONDITION_FILE}:{line} ({class_name}): {message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
