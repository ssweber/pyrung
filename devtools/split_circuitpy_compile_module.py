"""Split ``src/pyrung/circuitpy/codegen/compile.py`` into a package.

The split is intentionally conservative:

- Slice named top-level definitions from the monolith with ``ast``.
- Keep expression logic in ``_primitives.py`` so shared helpers stay one-way.
- Use ``ruff`` afterwards for import cleanup and formatting.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/pyrung/circuitpy/codegen/compile.py"
PACKAGE = ROOT / "src/pyrung/circuitpy/codegen/compile"
FUTURE_IMPORT = "from __future__ import annotations\n"

CORE_NODE_NAMES = [
    "_contact_tag_name",
    "compile_condition",
    "compile_rung",
    "_compile_rung_items",
    "_compile_condition_group",
    "_compile_calc_instruction",
    "_compile_function_call_instruction",
    "_compile_enabled_function_call_instruction",
    "_compile_for_loop_instruction",
    "_compile_instruction_list",
]

BASIC_NODE_NAMES = [
    "_compile_out_instruction",
    "_compile_call_instruction",
    "_compile_return_instruction",
    "_compile_on_delay_instruction",
    "_compile_off_delay_instruction",
    "_compile_count_up_instruction",
    "_compile_count_down_instruction",
    "_compile_copy_instruction",
    "_compile_copy_converter_instruction",
]

BLOCK_NODE_NAMES = [
    "_compile_blockcopy_instruction",
    "_compile_blockcopy_converter_instruction",
    "_compile_fill_instruction",
    "_compile_search_instruction",
    "_compile_shift_instruction",
    "_compile_step_selector_lines",
    "_compile_event_drum_instruction",
    "_compile_time_drum_instruction",
]

PACK_NODE_NAMES = [
    "_compile_pack_bits_instruction",
    "_compile_pack_words_instruction",
    "_compile_pack_text_instruction",
    "_compile_unpack_bits_instruction",
    "_compile_unpack_words_instruction",
]

MODBUS_NODE_NAMES = [
    "_modbus_client_symbol_spec",
    "_modbus_client_operand_tags",
    "_modbus_client_spec_for_instruction",
    "_compile_modbus_send_instruction",
    "_compile_modbus_receive_instruction",
]

PRIMITIVE_NODE_NAMES = [
    "_BITWISE_SYMBOLS",
    "_ALLOWED_MATH_FUNCS",
    "_copy_converter_target_info",
    "_copy_converter_write_lines",
    "_compile_guarded_instruction",
    "_compile_assignment_lines",
    "_compile_lvalue",
    "_compile_range_setup",
    "_sequence_expr",
    "_search_compare_expr",
    "_pack_store_expr",
    "_calc_store_expr",
    "_timer_dt_to_units_expr",
    "_load_cast_expr",
    "_compile_set_out_of_range_fault_body",
    "_compile_target_write_lines",
    "_compile_address_expr",
    "_compile_indirect_value",
    "_compile_value",
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
        raise RuntimeError(f"Expected a module docstring at the top of {SOURCE}")
    return _slice_node(source_lines, doc_node)


def _extract_import_block(source_lines: list[str], module: ast.Module) -> str:
    blocks: list[str] = []
    after_future = False
    for node in module.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            after_future = True
            continue
        if after_future and isinstance(node, (ast.Import, ast.ImportFrom)):
            blocks.append(_slice_node(source_lines, node))
            continue
        if blocks:
            break
    if not blocks:
        raise RuntimeError(f"Could not find import block in {SOURCE}")
    return "".join(blocks).rstrip() + "\n"


def _rename_function(block: str, old_name: str, new_name: str) -> str:
    return block.replace(f"def {old_name}(", f"def {new_name}(", 1)


def _patch_compile_instruction(block: str) -> str:
    block = re.sub(
        r"(?ms)^    if isinstance\(instr, LatchInstruction\):\n.*?(?=^    if isinstance\(instr, ResetInstruction\):)",
        "    if isinstance(instr, LatchInstruction):\n"
        "        return _compile_latch_instruction(instr, enabled_expr, ctx, indent)\n",
        block,
    )
    block = re.sub(
        r"(?ms)^    if isinstance\(instr, ResetInstruction\):\n.*?(?=^    if isinstance\(instr, OnDelayInstruction\):)",
        "    if isinstance(instr, ResetInstruction):\n"
        "        return _compile_reset_instruction(instr, enabled_expr, ctx, indent)\n",
        block,
    )
    return block


def _render_core_module(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    import_block: str,
) -> str:
    compile_instruction_block = _patch_compile_instruction(
        _require_block(source_lines, nodes, "compile_instruction")
    )
    prelude = _join_blocks(
        _require_block(source_lines, nodes, "_contact_tag_name"),
        _require_block(source_lines, nodes, "compile_condition"),
        textwrap.dedent(
            """
            def compile_expression(expr: Expression, ctx: CodegenContext) -> str:
                \"\"\"Return a Python expression string with explicit parentheses.\"\"\"
                return _compile_expression_impl(expr, ctx)
            """
        ),
    )
    remainder = _join_blocks(
        compile_instruction_block,
        *_require_blocks(source_lines, nodes, CORE_NODE_NAMES[2:]),
    )
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        import_block,
        textwrap.dedent(
            """
            from ._primitives import (
                _calc_store_expr,
                _compile_assignment_lines,
                _compile_expression_impl,
                _compile_guarded_instruction,
                _compile_indirect_value,
                _compile_value,
            )
            """
        ),
        prelude,
        textwrap.dedent(
            """
            from ._instructions_basic import (
                _compile_call_instruction,
                _compile_copy_instruction,
                _compile_count_down_instruction,
                _compile_count_up_instruction,
                _compile_latch_instruction,
                _compile_off_delay_instruction,
                _compile_on_delay_instruction,
                _compile_out_instruction,
                _compile_reset_instruction,
                _compile_return_instruction,
            )
            from ._instructions_block import (
                _compile_blockcopy_instruction,
                _compile_event_drum_instruction,
                _compile_fill_instruction,
                _compile_search_instruction,
                _compile_shift_instruction,
                _compile_time_drum_instruction,
            )
            from ._instructions_pack import (
                _compile_pack_bits_instruction,
                _compile_pack_text_instruction,
                _compile_pack_words_instruction,
                _compile_unpack_bits_instruction,
                _compile_unpack_words_instruction,
            )
            from ._modbus import (
                _compile_modbus_receive_instruction,
                _compile_modbus_send_instruction,
            )
            """
        ),
        remainder,
    )


def _render_basic_module(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    import_block: str,
) -> str:
    body = _join_blocks(
        textwrap.dedent(
            """
            def _compile_latch_instruction(
                instr: LatchInstruction,
                enabled_expr: str,
                ctx: CodegenContext,
                indent: int,
            ) -> list[str]:
                lines = [f"{' ' * indent}if {enabled_expr}:"]
                lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
                return lines


            def _compile_reset_instruction(
                instr: ResetInstruction,
                enabled_expr: str,
                ctx: CodegenContext,
                indent: int,
            ) -> list[str]:
                default_expr = _coil_target_default(instr.target, ctx)
                lines = [f"{' ' * indent}if {enabled_expr}:"]
                lines.extend(
                    _compile_target_write_lines(instr.target, default_expr, ctx, indent + 4)
                )
                return lines
            """
        ),
        *_require_blocks(source_lines, nodes, BASIC_NODE_NAMES),
    )
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        import_block,
        "from ._core import compile_condition",
        textwrap.dedent(
            """
            from ._primitives import (
                _compile_assignment_lines,
                _compile_guarded_instruction,
                _compile_set_out_of_range_fault_body,
                _compile_target_write_lines,
                _compile_value,
                _copy_converter_target_info,
                _copy_converter_write_lines,
                _timer_dt_to_units_expr,
            )
            """
        ),
        body,
    )


def _render_block_module(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    import_block: str,
) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, BLOCK_NODE_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        import_block,
        "from ._core import compile_condition",
        textwrap.dedent(
            """
            from ._primitives import (
                _compile_guarded_instruction,
                _compile_range_setup,
                _compile_set_out_of_range_fault_body,
                _compile_value,
                _search_compare_expr,
                _timer_dt_to_units_expr,
            )
            """
        ),
        body,
    )


def _render_pack_module(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    import_block: str,
) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, PACK_NODE_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        import_block,
        textwrap.dedent(
            """
            from ._primitives import (
                _compile_assignment_lines,
                _compile_guarded_instruction,
                _compile_range_setup,
                _compile_set_out_of_range_fault_body,
                _compile_value,
                _pack_store_expr,
            )
            """
        ),
        body,
    )


def _render_modbus_module(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    import_block: str,
) -> str:
    body = _join_blocks(*_require_blocks(source_lines, nodes, MODBUS_NODE_NAMES))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        import_block,
        body,
    )


def _render_primitives_module(
    source_lines: list[str],
    nodes: dict[str, ast.stmt],
    import_block: str,
) -> str:
    expression_block = _rename_function(
        _require_block(source_lines, nodes, "compile_expression"),
        "compile_expression",
        "_compile_expression_impl",
    )
    body = _join_blocks(
        expression_block,
        *_require_blocks(source_lines, nodes, PRIMITIVE_NODE_NAMES),
    )
    body = body.replace("compile_expression(", "_compile_expression_impl(")
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        import_block,
        body,
    )


def _render_package_init(docstring: str) -> str:
    return _join_blocks(
        docstring,
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            from ._core import (
                compile_condition,
                compile_expression,
                compile_instruction,
                compile_rung,
            )

            __all__ = [
                "compile_condition",
                "compile_expression",
                "compile_instruction",
                "compile_rung",
            ]
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
    import_block = _extract_import_block(source_lines, module)

    PACKAGE.mkdir(parents=True, exist_ok=True)
    _write(PACKAGE / "_core.py", _render_core_module(source_lines, nodes, import_block))
    _write(
        PACKAGE / "_instructions_basic.py",
        _render_basic_module(source_lines, nodes, import_block),
    )
    _write(
        PACKAGE / "_instructions_block.py",
        _render_block_module(source_lines, nodes, import_block),
    )
    _write(
        PACKAGE / "_instructions_pack.py",
        _render_pack_module(source_lines, nodes, import_block),
    )
    _write(PACKAGE / "_modbus.py", _render_modbus_module(source_lines, nodes, import_block))
    _write(
        PACKAGE / "_primitives.py",
        _render_primitives_module(source_lines, nodes, import_block),
    )
    _write(PACKAGE / "__init__.py", _render_package_init(docstring))
    SOURCE.unlink()

    print(f"Split {SOURCE} -> {PACKAGE}")


if __name__ == "__main__":
    main()
