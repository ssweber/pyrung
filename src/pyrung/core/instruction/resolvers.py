"""Automatically generated module split."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import ImmediateRef, Tag

from .conversions import (
    _as_single_ascii_char,
    _ascii_char_from_code,
)
from .operands import ConstArg, ExprArg, PointerArg, PointerRangeArg, RangeArg, TagArg

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import (
        BlockRange,
        IndirectExprRef,
        IndirectRef,
    )

_TAG_SUFFIX_RE = re.compile(r"^(.*?)(\d+)$")


def operand_object(value: Any) -> Any:
    """Return the object inside a parsed operand, and any other value unchanged.

    A reader that walks every field of an instruction calls this function on each value
    it meets.  The tag or the range comes back without its ``immediate()``.
    """
    match value:
        case TagArg(tag=tag):
            return tag
        case RangeArg(span=span) | PointerRangeArg(span=span):
            return span
        case PointerArg(ref=ref):
            return ref
        case ExprArg(expr=expr):
            return expr
        case ConstArg(value=constant):
            return constant
        case _:
            return value


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
    from pyrung.core.memory_block import IndirectExprRef, PastBankError
    from pyrung.core.memory_block import IndirectRef as IndirectRefType

    # Check for immediate wrapper first.
    if isinstance(source, ImmediateRef):
        return resolve_tag_or_value_ctx(source.value, ctx)
    # Check for Expression first (includes TagExpr)
    if isinstance(source, Expression):
        return source.evaluate(ctx)
    # Check for IndirectExprRef or IndirectRef
    if isinstance(source, (IndirectExprRef, IndirectRefType)):
        try:
            resolved_tag = source.resolve_ctx(ctx)
        except PastBankError:
            # pyrung has no memory past a bank, so the read returns the type's zero.
            return source.block._type_default()
        return ctx.get_tag(resolved_tag.name, resolved_tag.default)
    # Check for Tag
    if isinstance(source, Tag):
        return ctx.get_tag(source.name, source.default)
    # Literal value
    return source


def resolve_tag_ctx(
    target: Tag | IndirectRef | IndirectExprRef | ImmediateRef, ctx: ScanContext
) -> Tag:
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
    if isinstance(target, ImmediateRef):
        if not isinstance(target.value, Tag):
            raise TypeError(
                f"An immediate() output target must be a Tag, got {type(target.value).__name__}."
            )
        return target.value
    # Regular Tag
    return target


def reads_indexed_element(value: Any, seen: set[int] | None = None) -> bool:
    """Return True when *value*, a source or an expression, reads an indexed block element.

    The element is addressed at scan time, as in ``Tbl[P]`` or ``Tbl[Q + 1]``, so it can
    fall below the block.
    """
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import IndirectExprRef
    from pyrung.core.memory_block import IndirectRef as IndirectRefType

    if seen is None:
        seen = set()
    if id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, (IndirectRefType, IndirectExprRef)):
        return True
    if isinstance(value, Expression):
        return any(reads_indexed_element(child, seen) for child in vars(value).values())
    if isinstance(value, (list, tuple)):
        return any(reads_indexed_element(item, seen) for item in value)
    return False


def resolve_source_type_tag_ctx(source: IndirectRef | IndirectExprRef, ctx: ScanContext) -> Tag:
    """Return a Tag with the type of the element that *source* reads.

    Past its bank there is no element, and the read returns the type's zero.  The
    bank's first element then supplies the type.  Read the value through
    ``resolve_tag_or_value_ctx``, not from this Tag.
    """
    from pyrung.core.memory_block import PastBankError

    try:
        return source.resolve_ctx(ctx)
    except PastBankError:
        return source.block._get_tag(source.block.start)


def resolve_tag_name_ctx(
    target: Tag | IndirectRef | IndirectExprRef | ImmediateRef, ctx: ScanContext
) -> str:
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

    if isinstance(block_range, ImmediateRef):
        block_range = block_range.value

    if isinstance(block_range, IndirectBlockRange):
        block_range = block_range.resolve_ctx(ctx)

    if not isinstance(block_range, BlockRange):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(block_range).__name__}"
        )

    return block_range


def _raise_fault(ctx: ScanContext, name: str) -> None:
    ctx._set_tag_internal(name, True)


def _set_fault_out_of_range(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    _raise_fault(ctx, system.fault.out_of_range.name)


def _set_fault_address_error(ctx: ScanContext) -> None:
    from pyrung.core.system_points import system

    _raise_fault(ctx, system.fault.address_error.name)


def _sequential_tags(start_tag: Tag, count: int) -> list[Tag]:
    """Return *start_tag* and the tags after it, *count* in all, but none past its block's end.

    In a block, these are the tags that hold the addresses (``elements_after``).  A tag
    that is in no block counts on by name.
    """
    from pyrung.core.memory_block import elements_after, elements_that_fit

    count = elements_that_fit(start_tag, count)
    if count <= 0:
        return []
    if count == 1:
        return [start_tag]
    following = elements_after(start_tag, count)
    if following is not None:
        return [start_tag, *following]

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
