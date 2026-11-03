"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import ast
import math
import re
from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pyrung.circuitpy.codegen._constants import _IDENT_RE, _STORE_TYPE_HELPERS
from pyrung.core.instruction.coils import reset_value_for_type
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import ImmediateRef, Tag, TagType

if TYPE_CHECKING:
    from pyrung.circuitpy.codegen.context import CodegenContext


def _indent_body(lines: list[str], spaces: int) -> list[str]:
    prefix = " " * spaces
    return [f"{prefix}{line}" if line else line for line in lines]


def _bool_literal(expr: str) -> bool | None:
    stripped = expr.strip()
    if stripped == "True":
        return True
    if stripped == "False":
        return False
    return None


def _optional_value_type_name(value: Any) -> str | None:
    if isinstance(value, Tag):
        return value.type.name
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return value.block.type.name
    return None


def _value_type_name(value: Any) -> str:
    if isinstance(value, Tag):
        return value.type.name
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return value.block.type.name
    raise TypeError(f"Unsupported typed value target: {type(value).__name__}")


def _store_coerce_expr(source_expr: str, dest_type: str, ctx: CodegenContext) -> str:
    helper = _STORE_TYPE_HELPERS.get(dest_type)
    if helper is not None:
        ctx.mark_helper(helper)
        return f"{helper}({source_expr})"
    ctx.mark_helper("_store_copy_value_to_type")
    return f'_store_copy_value_to_type({source_expr}, "{dest_type}")'


def _range_type_name(range_value: Any) -> str:
    if isinstance(range_value, (BlockRange, IndirectBlockRange)):
        return range_value.block.type.name
    raise TypeError(f"Expected BlockRange or IndirectBlockRange, got {type(range_value).__name__}")


def _static_range_length(range_value: Any) -> int | None:
    if isinstance(range_value, BlockRange):
        return len(range_value)
    return None


def _first_defined_name(source: str) -> str | None:
    match = re.search(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source, flags=re.MULTILINE)
    if match is not None:
        return match.group(1)
    match = re.search(
        r"^\s*async\s+def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", source, flags=re.MULTILINE
    )
    if match is not None:
        return match.group(1)
    return None


def _module_level_binds(tree: ast.Module) -> Counter[str]:
    """How many times each name is bound at the top of a module, and through ``global``."""
    binds: Counter[str] = Counter()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                binds[child.name] += 1
                continue
            if isinstance(child, ast.Import | ast.ImportFrom):
                binds.update(a.asname or a.name.split(".")[0] for a in child.names)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                binds[child.id] += 1
            elif isinstance(child, ast.ExceptHandler) and child.name:
                binds[child.name] += 1
            walk(child)

    walk(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            binds.update(node.names)
    return binds


def _refuse_a_def_over_a_generated_name(source: str, function_sources: Mapping[str, str]) -> None:
    """Refuse a generated module whose embedded ``def`` shares a name with its other code.

    A def lands at the top of the module, so it and a helper, an import, a tag or another
    function's alias of that name overwrite one another: the module calls something other
    than the function, or the function's symbol is something else.  Asked of the module as
    rendered rather than of a list of names, so it answers for what this program uses and
    for this target.  Two embedded defs may share a name: each alias binds after its def.
    """
    if not function_sources:
        return
    defs = Counter(_first_defined_name(fn_source) for fn_source in function_sources.values())
    binds = _module_level_binds(ast.parse(source))
    for name, count in defs.items():
        if name is not None and binds[name] > count:
            raise ValueError(
                f"Callable name would shadow a name the generated code uses: {name!r}. "
                "Rename the function."
            )


def _coil_reset_value(
    target: Tag | BlockRange | IndirectBlockRange | ImmediateRef,
    ctx: CodegenContext,
) -> str:
    if isinstance(target, ImmediateRef):
        return _coil_reset_value(target.value, ctx)
    # RESET writes the core engine's OFF/zero value; for CHAR that is NUL,
    # the same blank an unwritten TXT holds.
    if isinstance(target, Tag):
        return repr(reset_value_for_type(target.type))
    binding = ctx.block_bindings[id(target.block)]
    return repr(reset_value_for_type(binding.tag_type))


def _global_line(symbols: list[str], indent: int) -> str | None:
    if not symbols:
        return None
    return f"{' ' * indent}global {', '.join(symbols)}"


def _default_literal(value: Any) -> str | None:
    """Python source for a default, or None for one no literal can spell.

    ``repr`` is not source for every default a slot can carry: an ``IntEnum`` member
    spells ``<Color.BLUE: 2>``, and ``float("inf")`` spells a bare ``inf``.
    """
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, int):
        return int.__repr__(value)
    if isinstance(value, float):
        return float.__repr__(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return str.__repr__(value)
    return None


def _ret_defaults_literal(ctx: CodegenContext) -> dict[str, Any]:
    return {name: ctx.retentive_tags[name].default for name in sorted(ctx.retentive_tags)}


def _ret_types_literal(ctx: CodegenContext) -> dict[str, str]:
    return {name: ctx.retentive_tags[name].type.name for name in sorted(ctx.retentive_tags)}


def _source_location(obj: Any) -> str:
    src_file = getattr(obj, "source_file", None)
    src_line = getattr(obj, "source_line", None)
    if src_file is None or src_line is None:
        return "unknown"
    return f"{src_file}:{src_line}"


def _mangle_symbol(logical_name: str, prefix: str, used: set[str]) -> str:
    sanitized = _IDENT_RE.sub("_", logical_name)
    if not sanitized:
        sanitized = "_"
    if sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    candidate = f"{prefix}{sanitized}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    n = 2
    while True:
        next_candidate = f"{candidate}_{n}"
        if next_candidate not in used:
            used.add(next_candidate)
            return next_candidate
        n += 1


def _subroutine_symbol(name: str) -> str:
    base = _IDENT_RE.sub("_", name)
    if not base:
        base = "_"
    if base[0].isdigit():
        base = f"_{base}"
    return f"_sub_{base}"


def _io_kind(tag_type: TagType) -> str:
    if tag_type == TagType.BOOL:
        return "discrete"
    if tag_type == TagType.INT:
        return "analog"
    if tag_type == TagType.REAL:
        return "temperature"
    raise ValueError(f"Unsupported CircuitPython I/O tag type: {tag_type.name}")
