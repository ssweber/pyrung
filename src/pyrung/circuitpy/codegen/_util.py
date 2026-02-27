"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pyrung.circuitpy.codegen._constants import _IDENT_RE, _TYPE_DEFAULTS
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import Tag, TagType

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


def _optional_range_type_name(range_value: Any) -> str | None:
    if isinstance(range_value, (BlockRange, IndirectBlockRange)):
        return range_value.block.type.name
    return None


def _value_type_name(value: Any) -> str:
    if isinstance(value, Tag):
        return value.type.name
    if isinstance(value, (IndirectRef, IndirectExprRef)):
        return value.block.type.name
    raise TypeError(f"Unsupported typed value target: {type(value).__name__}")


def _range_type_name(range_value: Any) -> str:
    if isinstance(range_value, (BlockRange, IndirectBlockRange)):
        return range_value.block.type.name
    raise TypeError(f"Expected BlockRange or IndirectBlockRange, got {type(range_value).__name__}")


def _range_reverse(range_value: Any) -> bool:
    if isinstance(range_value, (BlockRange, IndirectBlockRange)):
        return bool(range_value.reverse_order)
    return False


def _static_range_length(range_value: Any) -> int | None:
    if isinstance(range_value, BlockRange):
        return len(range_value.addresses)
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


def _coil_target_default(target: Tag | BlockRange | IndirectBlockRange, ctx: CodegenContext) -> str:
    if isinstance(target, Tag):
        return repr(target.default)
    binding = ctx.block_bindings[id(target.block)]
    return repr(_TYPE_DEFAULTS[binding.tag_type])


def _global_line(symbols: list[str], indent: int) -> str | None:
    if not symbols:
        return None
    return f"{' ' * indent}global {', '.join(symbols)}"


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
