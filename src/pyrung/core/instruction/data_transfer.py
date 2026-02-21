"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.copy_modifiers import CopyModifier
from pyrung.core.tag import Tag

from .base import Instruction, OneShotMixin
from .conversions import (
    _as_single_ascii_char,
    _ascii_char_from_code,
    _render_text_from_numeric,
    _store_copy_value_to_tag_type,
    _store_numeric_text_digits,
    _text_from_source_value,
)
from .resolvers import (
    _sequential_tags,
    _set_fault_address_error,
    _set_fault_out_of_range,
    _termination_char,
    resolve_block_range_tags_ctx,
    resolve_tag_ctx,
    resolve_tag_or_value_ctx,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef


class CopyInstruction(OneShotMixin, Instruction):
    """Copy a single value from source to destination (CPY/MOV).

    Source may be a literal value, `Tag`, `IndirectRef`, `IndirectExprRef`, or
    a copy modifier (`.as_text()`, `.as_value()`, etc.).  Destination may be a
    `Tag` or `IndirectRef`.

    **Clamping semantics:** Out-of-range values are clamped to the
    destination type's min/max (e.g. copying 40 000 into an INT tag produces
    32 767). This differs from `MathInstruction`, which wraps.

    **Pointer errors:** If the pointer resolves to an out-of-range address,
    the address-error fault flag is set and the copy is skipped.

    Args:
        source: Value to copy from.
        target: Tag or indirect reference to copy into.
        oneshot: When True, execute only on the rung's rising edge (once per
            False→True transition). Default False.
    """

    def __init__(
        self,
        source: Tag | IndirectRef | IndirectExprRef | Any,
        target: Tag | IndirectRef | IndirectExprRef,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.target = target

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        try:
            resolved_target = resolve_tag_ctx(self.target, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(self.target, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
                return
            raise

        if isinstance(self.source, CopyModifier):
            self._execute_modifier_copy(ctx, resolved_target, self.source)
            return

        try:
            value = resolve_tag_or_value_ctx(self.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(self.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
                return
            raise

        value = _store_copy_value_to_tag_type(value, resolved_target)
        ctx.set_tag(resolved_target.name, value)

    def _execute_modifier_copy(
        self, ctx: ScanContext, resolved_target: Tag, modifier: CopyModifier
    ) -> None:
        mode = modifier.mode
        if mode in {"value", "ascii"}:
            self._copy_text_to_numeric(ctx, resolved_target, modifier, mode=mode)
            return
        if mode == "text":
            self._copy_numeric_to_text(ctx, resolved_target, modifier)
            return
        if mode == "binary":
            self._copy_binary_to_text(ctx, resolved_target, modifier)
            return
        _set_fault_out_of_range(ctx)

    def _copy_text_to_numeric(
        self,
        ctx: ScanContext,
        resolved_target: Tag,
        modifier: CopyModifier,
        *,
        mode: str,
    ) -> None:
        try:
            source_value = resolve_tag_or_value_ctx(modifier.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
            else:
                _set_fault_out_of_range(ctx)
            return
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        try:
            text = _text_from_source_value(source_value)
            targets = _sequential_tags(resolved_target, len(text))
            updates = _store_numeric_text_digits(text, targets, mode=mode)
        except (TypeError, ValueError):
            _set_fault_out_of_range(ctx)
            return
        ctx.set_tags(updates)

    def _copy_numeric_to_text(
        self, ctx: ScanContext, resolved_target: Tag, modifier: CopyModifier
    ) -> None:
        from pyrung.core.memory_block import IndirectExprRef, IndirectRef
        from pyrung.core.tag import TagType

        if resolved_target.type != TagType.CHAR:
            _set_fault_out_of_range(ctx)
            return

        source_tag: Tag | None = None
        try:
            if isinstance(modifier.source, Tag):
                source_tag = modifier.source
            elif isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                source_tag = resolve_tag_ctx(modifier.source, ctx)
            value = resolve_tag_or_value_ctx(modifier.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            if isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
            else:
                _set_fault_out_of_range(ctx)
            return
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        try:
            rendered = _render_text_from_numeric(
                value,
                source_tag=source_tag,
                suppress_zero=modifier.suppress_zero,
                exponential=modifier.exponential,
            )
            rendered += _termination_char(modifier.termination_code)
            targets = _sequential_tags(resolved_target, len(rendered))
            updates = {
                target.name: _as_single_ascii_char(char)
                for target, char in zip(targets, rendered, strict=True)
            }
        except (TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)
            return

        ctx.set_tags(updates)

    def _copy_binary_to_text(
        self, ctx: ScanContext, resolved_target: Tag, modifier: CopyModifier
    ) -> None:
        from pyrung.core.tag import TagType

        if resolved_target.type != TagType.CHAR:
            _set_fault_out_of_range(ctx)
            return

        try:
            value = int(resolve_tag_or_value_ctx(modifier.source, ctx))
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
            else:
                _set_fault_out_of_range(ctx)
            return
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        try:
            char = _ascii_char_from_code(value & 0xFF)
        except ValueError:
            _set_fault_out_of_range(ctx)
            return

        ctx.set_tag(resolved_target.name, char)


class BlockCopyInstruction(OneShotMixin, Instruction):
    """Block copy instruction.

    Copies values from a source BlockRange to a destination BlockRange.
    Both ranges must have the same length.

    Source and dest can be BlockRange or IndirectBlockRange (resolved at scan time).
    """

    def __init__(self, source: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.dest = dest

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)

        if isinstance(self.source, CopyModifier):
            self._execute_modifier_block_copy(ctx, self.source, dst_tags)
            return

        src_tags = resolve_block_range_tags_ctx(self.source, ctx)

        if len(src_tags) != len(dst_tags):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_tags)} elements, "
                f"dest has {len(dst_tags)} elements"
            )

        updates = {}
        for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
            value = ctx.get_tag(src_tag.name, src_tag.default)
            updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag)
        ctx.set_tags(updates)

    def _execute_modifier_block_copy(
        self, ctx: ScanContext, modifier: CopyModifier, dst_tags: list[Tag]
    ) -> None:
        src_tags = resolve_block_range_tags_ctx(modifier.source, ctx)
        if len(src_tags) != len(dst_tags):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_tags)} elements, "
                f"dest has {len(dst_tags)} elements"
            )

        try:
            if modifier.mode in {"value", "ascii"}:
                updates = {}
                for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
                    char = _as_single_ascii_char(ctx.get_tag(src_tag.name, src_tag.default))
                    if char == "":
                        raise ValueError("empty CHAR cannot be converted to numeric")
                    updates[dst_tag.name] = _store_numeric_text_digits(
                        char, [dst_tag], mode=modifier.mode
                    )[dst_tag.name]
                ctx.set_tags(updates)
                return

            if modifier.mode == "text":
                rendered = "".join(
                    _render_text_from_numeric(
                        ctx.get_tag(src_tag.name, src_tag.default),
                        source_tag=src_tag,
                        suppress_zero=modifier.suppress_zero,
                        exponential=modifier.exponential,
                    )
                    for src_tag in src_tags
                )
                rendered += _termination_char(modifier.termination_code)
                if len(rendered) != len(dst_tags):
                    raise ValueError("formatted text length does not match destination range")
                updates = {
                    dst.name: _as_single_ascii_char(char)
                    for dst, char in zip(dst_tags, rendered, strict=True)
                }
                ctx.set_tags(updates)
                return

            if modifier.mode == "binary":
                updates = {}
                for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
                    updates[dst_tag.name] = _ascii_char_from_code(
                        int(ctx.get_tag(src_tag.name, src_tag.default)) & 0xFF
                    )
                ctx.set_tags(updates)
                return
        except (IndexError, TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)
            return

        _set_fault_out_of_range(ctx)


class FillInstruction(OneShotMixin, Instruction):
    """Fill instruction.

    Writes a constant value to every element in a destination BlockRange.

    Value can be a literal, Tag, or Expression (resolved once, written to all).
    Dest can be BlockRange or IndirectBlockRange (resolved at scan time).
    """

    def __init__(self, value: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.value = value
        self.dest = dest

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)
        if isinstance(self.value, CopyModifier):
            self._execute_modifier_fill(ctx, self.value, dst_tags)
            return

        value = resolve_tag_or_value_ctx(self.value, ctx)

        updates = {}
        for dst_tag in dst_tags:
            if dst_tag.type.name == "CHAR":
                updates[dst_tag.name] = _as_single_ascii_char(value)
            else:
                updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag)
        ctx.set_tags(updates)

    def _execute_modifier_fill(
        self, ctx: ScanContext, modifier: CopyModifier, dst_tags: list[Tag]
    ) -> None:
        from pyrung.core.memory_block import IndirectExprRef, IndirectRef
        from pyrung.core.tag import TagType

        if not dst_tags:
            return

        if modifier.mode in {"value", "ascii"}:
            text = _text_from_source_value(resolve_tag_or_value_ctx(modifier.source, ctx))
            if len(text) != 1:
                raise ValueError("fill text->numeric conversion requires a single source character")
            numeric = _store_numeric_text_digits(text, [dst_tags[0]], mode=modifier.mode)[
                dst_tags[0].name
            ]
            updates = {tag.name: _store_copy_value_to_tag_type(numeric, tag) for tag in dst_tags}
            ctx.set_tags(updates)
            return

        if modifier.mode == "text":
            if any(tag.type != TagType.CHAR for tag in dst_tags):
                raise TypeError("fill(as_text(...)) requires CHAR destination range")

            source_tag: Tag | None = None
            if isinstance(modifier.source, Tag):
                source_tag = modifier.source
            elif isinstance(modifier.source, (IndirectRef, IndirectExprRef)):
                source_tag = resolve_tag_ctx(modifier.source, ctx)

            rendered = _render_text_from_numeric(
                resolve_tag_or_value_ctx(modifier.source, ctx),
                source_tag=source_tag,
                suppress_zero=modifier.suppress_zero,
                exponential=modifier.exponential,
            )
            rendered += _termination_char(modifier.termination_code)
            if len(rendered) > len(dst_tags):
                raise ValueError("formatted fill text exceeds destination range")

            updates: dict[str, Any] = {}
            for idx, dst in enumerate(dst_tags):
                updates[dst.name] = (
                    _as_single_ascii_char(rendered[idx]) if idx < len(rendered) else ""
                )
            ctx.set_tags(updates)
            return

        if modifier.mode == "binary":
            code = int(resolve_tag_or_value_ctx(modifier.source, ctx)) & 0xFF
            char = _ascii_char_from_code(code)
            updates = {tag.name: char for tag in dst_tags}
            ctx.set_tags(updates)
            return

        raise ValueError(f"Unsupported fill modifier mode: {modifier.mode}")
