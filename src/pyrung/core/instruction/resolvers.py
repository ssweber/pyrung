"""Automatically generated module split."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag

from .conversions import (
    _as_single_ascii_char,
    _ascii_char_from_code,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import (
        BlockRange,
        IndirectBlockRange,
        IndirectExprRef,
        IndirectRef,
    )

_TAG_SUFFIX_RE = re.compile(r"^(.*?)(\d+)$")


def resolve_tag_or_value_ctx(
    source: Tag | IndirectRef | IndirectExprRef | Any, ctx: ScanContext
) -> Any:
    """Resolve tag (direct or indirect), expression, or return literal value using ScanContext.

    Args:
        source: Tag, IndirectRef, IndirectExprRef, Expression, or literal value.
        ctx: ScanContext for resolving values with read-after-write visibility.

    Returns:
        Resolved value from context or the literal value.
    """
    # Import here to avoid circular imports
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import IndirectExprRef
    from pyrung.core.memory_block import IndirectRef as IndirectRefType

    # Check for Expression first (includes TagExpr)
    if isinstance(source, Expression):
        return source.evaluate(ctx)
    # Check for IndirectExprRef
    if isinstance(source, IndirectExprRef):
        resolved_tag = source.resolve_ctx(ctx)
        return ctx.get_tag(resolved_tag.name, resolved_tag.default)
    # Check for IndirectRef
    if isinstance(source, IndirectRefType):
        resolved_tag = source.resolve_ctx(ctx)
        return ctx.get_tag(resolved_tag.name, resolved_tag.default)
    # Check for Tag
    if isinstance(source, Tag):
        return ctx.get_tag(source.name, source.default)
    # Literal value
    return source


def resolve_tag_ctx(target: Tag | IndirectRef | IndirectExprRef, ctx: ScanContext) -> Tag:
    """Resolve target to a concrete Tag (handling indirect) using ScanContext.

    Args:
        target: Tag, IndirectRef, or IndirectExprRef to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        The resolved Tag (with type info preserved).
    """
    # Import here to avoid circular imports
    from pyrung.core.memory_block import IndirectExprRef
    from pyrung.core.memory_block import IndirectRef as IndirectRefType

    # Check for IndirectExprRef first
    if isinstance(target, IndirectExprRef):
        return target.resolve_ctx(ctx)
    # Check for IndirectRef
    if isinstance(target, IndirectRefType):
        return target.resolve_ctx(ctx)
    # Regular Tag
    return target


def resolve_tag_name_ctx(target: Tag | IndirectRef | IndirectExprRef, ctx: ScanContext) -> str:
    """Resolve tag to its name (handling indirect) using ScanContext.

    Args:
        target: Tag, IndirectRef, or IndirectExprRef to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        The tag name string.
    """
    return resolve_tag_ctx(target, ctx).name


def resolve_block_range_tags_ctx(block_range: Any, ctx: ScanContext) -> list[Tag]:
    """Resolve a BlockRange or IndirectBlockRange to a list of Tags.

    Args:
        block_range: BlockRange or IndirectBlockRange to resolve.
        ctx: ScanContext for resolving indirect references.

    Returns:
        List of resolved Tag objects (with type info preserved).
    """
    return resolve_block_range_ctx(block_range, ctx).tags()


def resolve_block_range_ctx(block_range: Any, ctx: ScanContext) -> BlockRange:
    """Resolve a BlockRange or IndirectBlockRange to a concrete BlockRange."""
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if isinstance(block_range, IndirectBlockRange):
        block_range = block_range.resolve_ctx(ctx)

    if not isinstance(block_range, BlockRange):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(block_range).__name__}"
        )

    return block_range


def resolve_coil_targets_ctx(
    target: Tag | BlockRange | IndirectBlockRange, ctx: ScanContext
) -> list[Tag]:
    """Resolve a coil target to one or more concrete Tags.

    Coil targets support:
    - Single Tag
    - BlockRange from `.select(start, end)`
    - IndirectBlockRange from dynamic `.select(...)`
    """
    from pyrung.core.memory_block import BlockRange, IndirectBlockRange

    if isinstance(target, Tag):
        return [target]
    if isinstance(target, (BlockRange, IndirectBlockRange)):
        return resolve_block_range_tags_ctx(target, ctx)
    raise TypeError(f"Expected Tag, BlockRange, or IndirectBlockRange, got {type(target).__name__}")


def _set_fault_out_of_range(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    ctx._set_tag_internal(system.fault.out_of_range.name, True)


def _set_fault_division_error(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    ctx._set_tag_internal(system.fault.division_error.name, True)


def _set_fault_address_error(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    ctx._set_tag_internal(system.fault.address_error.name, True)


def _sequential_tags(start_tag: Tag, count: int) -> list[Tag]:
    if count <= 0:
        return []
    if count == 1:
        return [start_tag]

    match = _TAG_SUFFIX_RE.match(start_tag.name)
    if match is None:
        raise ValueError(f"Cannot expand sequential destination from {start_tag.name!r}")

    prefix, suffix = match.groups()
    width = len(suffix)
    base = int(suffix)
    tags = [start_tag]
    for offset in range(1, count):
        addr = base + offset
        name = f"{prefix}{addr:0{width}d}"
        tags.append(
            Tag(
                name=name,
                type=start_tag.type,
                retentive=start_tag.retentive,
                default=start_tag.default,
            )
        )
    return tags


def _termination_char(termination_code: int | str | None) -> str:
    if termination_code is None:
        return ""
    if isinstance(termination_code, str):
        if len(termination_code) != 1:
            raise ValueError("termination_code must be one character or int ASCII code")
        return _as_single_ascii_char(termination_code)
    if not isinstance(termination_code, int):
        raise TypeError("termination_code must be int, str, or None")
    return _ascii_char_from_code(termination_code)


def _fn_name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__name__", type(fn).__name__)
