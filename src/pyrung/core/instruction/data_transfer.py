"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.copy_converters import CopyConverter
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
    a string literal.  Destination may be a `Tag` or `IndirectRef`.

    Pass ``convert=`` to enable text/numeric conversion (see
    :mod:`pyrung.core.copy_converters`).

    **Clamping semantics:** Out-of-range values are clamped to the
    destination type's min/max (e.g. copying 40 000 into an INT tag produces
    32 767). This differs from `CalcInstruction`, which wraps.

    **Pointer errors:** If the pointer resolves to an out-of-range address,
    the address-error fault flag is set and the copy is skipped.

    Args:
        source: Value to copy from.
        target: Tag or indirect reference to copy into.
        convert: Optional :class:`CopyConverter` for text/numeric conversion.
        oneshot: When True, execute only on the rung's rising edge (once per
            False→True transition). Default False.
    """

    _reads = ("source",)
    _writes = ("target",)
    _conditions = ()
    _structural_fields = ("convert",)

    def __init__(
        self,
        source: Tag | IndirectRef | IndirectExprRef | str | Any,
        target: Tag | IndirectRef | IndirectExprRef,
        *,
        convert: CopyConverter | None = None,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.target = target
        self.convert = convert

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

        if self.convert is not None:
            self._execute_converter_copy(ctx, resolved_target, self.convert)
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

        # String literal fan-out: "00026" → sequential CHAR tags
        from pyrung.core.tag import TagType

        if isinstance(value, str) and len(value) > 1 and resolved_target.type == TagType.CHAR:
            try:
                targets = _sequential_tags(resolved_target, len(value))
                updates = {
                    t.name: _as_single_ascii_char(ch) for t, ch in zip(targets, value, strict=True)
                }
            except (TypeError, ValueError):
                _set_fault_out_of_range(ctx)
                return
            ctx.set_tags(updates)
            return

        value = _store_copy_value_to_tag_type(value, resolved_target)
        ctx.set_tag(resolved_target.name, value)

    def _execute_converter_copy(
        self, ctx: ScanContext, resolved_target: Tag, converter: CopyConverter
    ) -> None:
        mode = converter.mode
        if mode in {"value", "ascii"}:
            self._copy_text_to_numeric(ctx, resolved_target, converter, mode=mode)
            return
        if mode == "text":
            self._copy_numeric_to_text(ctx, resolved_target, converter)
            return
        if mode == "binary":
            self._copy_binary_to_text(ctx, resolved_target, converter)
            return
        _set_fault_out_of_range(ctx)

    def _copy_text_to_numeric(
        self,
        ctx: ScanContext,
        resolved_target: Tag,
        converter: CopyConverter,
        *,
        mode: str,
    ) -> None:
        try:
            source_value = resolve_tag_or_value_ctx(self.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(self.source, (IndirectRef, IndirectExprRef)):
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
        self, ctx: ScanContext, resolved_target: Tag, converter: CopyConverter
    ) -> None:
        from pyrung.core.memory_block import IndirectExprRef, IndirectRef
        from pyrung.core.tag import TagType

        if resolved_target.type != TagType.CHAR:
            _set_fault_out_of_range(ctx)
            return

        source_tag: Tag | None = None
        try:
            if isinstance(self.source, Tag):
                source_tag = self.source
            elif isinstance(self.source, (IndirectRef, IndirectExprRef)):
                source_tag = resolve_tag_ctx(self.source, ctx)
            value = resolve_tag_or_value_ctx(self.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            if isinstance(self.source, (IndirectRef, IndirectExprRef)):
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
                suppress_zero=converter.suppress_zero,
                pad=None,
                exponential=converter.exponential,
            )
            rendered += _termination_char(converter.termination_code)
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
        self, ctx: ScanContext, resolved_target: Tag, converter: CopyConverter
    ) -> None:
        from pyrung.core.tag import TagType

        if resolved_target.type != TagType.CHAR:
            _set_fault_out_of_range(ctx)
            return

        try:
            value = int(resolve_tag_or_value_ctx(self.source, ctx))
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(self.source, (IndirectRef, IndirectExprRef)):
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

    Pass ``convert=to_value`` or ``convert=to_ascii`` for text→numeric block
    conversion (Click PLC Block Copy Option 4).  Only ``value`` and ``ascii``
    modes are supported for block copy.
    """

    _reads = ("source",)
    _writes = ("dest",)
    _conditions = ()
    _structural_fields = ("convert",)

    def __init__(
        self,
        source: Any,
        dest: Any,
        *,
        convert: CopyConverter | None = None,
        oneshot: bool = False,
    ):
        if convert is not None and convert.mode not in {"value", "ascii"}:
            raise ValueError(
                f"BlockCopy only supports 'value' and 'ascii' converters, got {convert.mode!r}"
            )
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.dest = dest
        self.convert = convert

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)

        if self.convert is not None:
            self._execute_converter_block_copy(ctx, self.convert, dst_tags)
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

    def _execute_converter_block_copy(
        self, ctx: ScanContext, converter: CopyConverter, dst_tags: list[Tag]
    ) -> None:
        src_tags = resolve_block_range_tags_ctx(self.source, ctx)
        if len(src_tags) != len(dst_tags):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_tags)} elements, "
                f"dest has {len(dst_tags)} elements"
            )

        try:
            updates = {}
            for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
                char = _as_single_ascii_char(ctx.get_tag(src_tag.name, src_tag.default))
                if char == "":
                    raise ValueError("empty CHAR cannot be converted to numeric")
                updates[dst_tag.name] = _store_numeric_text_digits(
                    char, [dst_tag], mode=converter.mode
                )[dst_tag.name]
            ctx.set_tags(updates)
        except (IndexError, TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)


class FillInstruction(OneShotMixin, Instruction):
    """Fill instruction.

    Writes a constant value to every element in a destination BlockRange.

    Value can be a literal, Tag, or Expression (resolved once, written to all).
    Dest can be BlockRange or IndirectBlockRange (resolved at scan time).
    """

    _reads = ("value",)
    _writes = ("dest",)
    _conditions = ()
    _structural_fields = ()

    def __init__(self, value: Any, dest: Any, *, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.value = value
        self.dest = dest

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)

        value = resolve_tag_or_value_ctx(self.value, ctx)

        updates = {}
        for dst_tag in dst_tags:
            if dst_tag.type.name == "CHAR":
                updates[dst_tag.name] = _as_single_ascii_char(value)
            else:
                updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag)
        ctx.set_tags(updates)
