"""Split ``send_receive/__init__.py`` into ``send_receive/_core.py``.

The split is intentionally conservative:

- Copy the current implementation-heavy ``__init__.py`` verbatim to ``_core.py``.
- Rewrite ``__init__.py`` as a thin re-export layer.
- Preserve the current package surface, while allowing tests to patch
  ``pyrung.core.instruction.send_receive._core`` directly for internals.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src/pyrung/core/instruction/send_receive"
SOURCE = PACKAGE / "__init__.py"
CORE = PACKAGE / "_core.py"
FUTURE_IMPORT = "from __future__ import annotations\n"

PUBLIC_CORE_EXPORTS = [
    "ModbusReceiveInstruction",
    "ModbusSendInstruction",
    "receive",
    "send",
]

PUBLIC_TYPE_EXPORTS = [
    "ModbusAddress",
    "ModbusRtuTarget",
    "ModbusTcpTarget",
    "RegisterType",
    "VALID_COM_PORTS",
    "WordOrder",
]


def _slice_node(source_lines: list[str], node: ast.stmt) -> str:
    decorator_lines = getattr(node, "decorator_list", [])
    start = node.lineno
    if decorator_lines:
        start = min([start, *[decorator.lineno for decorator in decorator_lines]])
    end = node.end_lineno
    if end is None:
        raise RuntimeError(f"Node {type(node).__name__} is missing end_lineno in {SOURCE}")
    return "\n".join(source_lines[start - 1 : end]).rstrip() + "\n"


def _extract_docstring_block(source_lines: list[str], module: ast.Module) -> str:
    if not module.body:
        raise RuntimeError(f"No top-level statements found in {SOURCE}")
    doc_node = module.body[0]
    if not (
        isinstance(doc_node, ast.Expr)
        and isinstance(doc_node.value, ast.Constant)
        and isinstance(doc_node.value.value, str)
    ):
        raise RuntimeError(f"Expected a module docstring at the top of {SOURCE}")
    return _slice_node(source_lines, doc_node)


def _extract_all_block(source_lines: list[str], module: ast.Module) -> str:
    for node in module.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "__all__":
                return _slice_node(source_lines, node)
    raise RuntimeError(f"Could not find __all__ assignment in {SOURCE}")


def _join_blocks(*blocks: str) -> str:
    parts = [block.strip() for block in blocks if block.strip()]
    return "\n\n".join(parts) + "\n"


def _render_import_block(module_path: str, names: list[str]) -> str:
    rendered_names = "\n".join(f"    {name}," for name in names)
    return f"from {module_path} import (\n{rendered_names}\n)\n"


def _render_package_init(docstring_block: str, all_block: str) -> str:
    return _join_blocks(
        docstring_block,
        FUTURE_IMPORT.strip(),
        _render_import_block("._core", PUBLIC_CORE_EXPORTS),
        _render_import_block(".types", PUBLIC_TYPE_EXPORTS),
        all_block,
    )


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Expected package root module at {SOURCE}")

    source_text = SOURCE.read_text(encoding="utf-8")
    implementation_text = source_text
    if (
        "class ModbusSendInstruction" not in implementation_text
        or "class ModbusReceiveInstruction" not in implementation_text
    ):
        if not CORE.is_file():
            raise SystemExit(f"{SOURCE} does not look like the pre-split implementation module")
        implementation_text = CORE.read_text(encoding="utf-8")
        if (
            "class ModbusSendInstruction" not in implementation_text
            or "class ModbusReceiveInstruction" not in implementation_text
        ):
            raise SystemExit(
                f"Could not find send/receive implementation in either {SOURCE} or {CORE}"
            )

    if FUTURE_IMPORT not in source_text:
        raise SystemExit(f"Expected {FUTURE_IMPORT!r} in {SOURCE}")

    source_lines = source_text.splitlines()
    module = ast.parse(source_text)
    docstring_block = _extract_docstring_block(source_lines, module)
    all_block = _extract_all_block(source_lines, module)

    _write(CORE, implementation_text)
    _write(SOURCE, _render_package_init(docstring_block, all_block))

    print(f"Split {SOURCE} -> {CORE}")


if __name__ == "__main__":
    main()
