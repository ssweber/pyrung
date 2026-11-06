"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.copy_converters import CopyConverter
from pyrung.core.memory_block import PastBankError
from pyrung.core.storage import check_real_literal
from pyrung.core.tag import Tag

from .base import Instruction, OneShotMixin
from .conversions import (
    _NUMBER_TYPES,
    _as_single_ascii_char,
    _ascii_char_from_code,
    _copy_clamps,
    _render_text_from_numeric,
    _store_copy_value_to_tag_type,
    _store_numeric_text_digits,
    _text_from_source_value,
    copy_source_kind,
)
from .resolvers import (
    _sequential_tags,
    _set_fault_address_error,
    _set_fault_out_of_range,
    _termination_char,
    resolve_block_range_tags_ctx,
    resolve_source_type_tag_ctx,
    resolve_tag_ctx,
    resolve_tag_or_value_ctx,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef


def _plain_text_converter(source: Any, dest: Any) -> CopyConverter | None:
    """The conversion a Copy with no ``convert=`` makes across text, as CLICK's does.

    A Real into a TXT writes its text, as ``to_text()`` does; a TXT into a number reads
    its digit, as ``to_value`` does.  ``None`` for any other Copy.
    """
    from pyrung.core.copy_converters import to_text, to_value
    from pyrung.core.tag import TagType

    source_type = copy_source_kind(source)
    dest_type = copy_source_kind(dest)
    if source_type is TagType.REAL and dest_type is TagType.CHAR:
        return to_text()
    if source_type is TagType.CHAR and dest_type in _NUMBER_TYPES:
        return to_value
    return None


def _source_past_end_keeps(source: Any, ctx: ScanContext) -> bool:
    """True for a pointer *source* past the end of a bank whose Copy then writes nothing.

    CLICK leaves the destination of a Copy from past DS, DH, DF or TXT, with no flag
    (``Block._past_end_keeps``); past any other bank the read is the type's zero.
    """
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef

    if not isinstance(source, (IndirectRef, IndirectExprRef)) or not source.block._past_end_keeps:
        return False
    try:
        source.resolve_ctx(ctx)
    except PastBankError:
        return True
    except (IndexError, TypeError):
        return False
    return False


class CopyInstruction(OneShotMixin, Instruction):
    """Copy a single value from source to destination (CPY/MOV).

    Source may be a literal value, `Tag`, `IndirectRef`, `IndirectExprRef`, or
    a string literal.  Destination may be a `Tag` or `IndirectRef`.

    Pass ``convert=`` to enable text/numeric conversion (see
    :mod:`pyrung.core.copy_converters`).  With none, a REAL into a CHAR writes its
    text (``to_text()``) and a CHAR into a number reads its digit (``to_value``), as
    CLICK's plain Copy does; ``convert`` holds that converter and ``authored_convert``
    what was written.

    **Clamping semantics:** Out-of-range values are clamped to the
    destination type's min/max (e.g. copying 40 000 into an INT tag produces
    32 767) and the out-of-range flag (``system.fault.out_of_range``, CLICK's
    SC43) is set; +/-Inf into an INT or DINT clamps the same way, and NaN stores
    0 with no flag.  A REAL register clamps into a DINT with no flag. This differs from `CalcInstruction`, which wraps.  A WORD
    into an INT keeps its bits (0xFFFF is -1); into a WORD a constant keeps its
    low 16 bits, and a DINT or REAL clamps past 65535 and below 0 keeps its low
    16 bits, with no flag (see ``copy_store_transform``).  Into a BOOL, a number
    stores its low bit.

    **Pointer errors:** A pointer below its bank sets the address-error fault
    flag and skips the copy. A pointer past its bank raises no flag: a
    destination there drops the write, and a source there reads as 0, or, past a
    bank marked ``_past_end_keeps`` (CLICK's DS, DH, DF and TXT), leaves the
    destination as it was.

    **Fault flags:** A copy clears the out-of-range and address-error flags as it
    starts, before it reads its source, and sets each it raises.  A one-shot copy
    on an enabled rung clears them on the scans it does not fire too.

    Args:
        source: Value to copy from.
        dest: Tag or indirect reference to copy into.
        convert: Optional :class:`CopyConverter` for text/numeric conversion.
        oneshot: When True, execute only on the rung's rising edge (once per
            False→True transition). Default False.
    """

    _reads = ("source",)
    _writes = ("dest",)
    _fault_writes = ("fault.address_error", "fault.out_of_range")
    _fault_follows = _fault_writes
    _conditions = ()
    _structural_fields = ("convert",)

    def __init__(
        self,
        source: Tag | IndirectRef | IndirectExprRef | str | Any,
        dest: Tag | IndirectRef | IndirectExprRef,
        *,
        convert: CopyConverter | None = None,
        oneshot: bool = False,
    ):
        OneShotMixin.__init__(self, oneshot)
        check_real_literal(source, dest, "copy() source")
        self.source = source
        self.dest = dest
        self.authored_convert = convert
        self.convert = convert if convert is not None else _plain_text_converter(source, dest)

    @property
    def target(self):
        return self.dest

    @target.setter
    def target(self, value):
        self.dest = value

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        resolved_target: Tag | None
        try:
            resolved_target = resolve_tag_ctx(self.dest, ctx)
        except PastBankError:
            # The write is dropped, with no flag. The source is still read.
            resolved_target = None
        except IndexError:
            _set_fault_address_error(ctx)
            return
        except TypeError:
            from pyrung.core.memory_block import IndirectExprRef, IndirectRef

            if isinstance(self.dest, (IndirectRef, IndirectExprRef)):
                _set_fault_address_error(ctx)
                return
            raise

        convert = self.convert
        if convert is not None:
            if resolved_target is not None and not _source_past_end_keeps(self.source, ctx):
                self._execute_converter_copy(ctx, resolved_target, convert)
            return

        if _source_past_end_keeps(self.source, ctx):
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

        if resolved_target is None:
            return

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

        source = copy_source_kind(self.source)
        stored = _store_copy_value_to_tag_type(value, resolved_target, source)
        ctx.set_tag(resolved_target.name, stored)
        if _copy_clamps(value, resolved_target, source):
            _set_fault_out_of_range(ctx)

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
                source_tag = resolve_source_type_tag_ctx(self.source, ctx)
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
        except (ValueError, OverflowError):
            # int() of NaN raises the first, of +/-Inf the second.
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
    _fault_writes = ("fault.address_error", "fault.out_of_range")
    _fault_follows = _fault_writes
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
        try:
            dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return

        if self.convert is not None:
            self._execute_converter_block_copy(ctx, self.convert, dst_tags)
            return

        try:
            src_tags = resolve_block_range_tags_ctx(self.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return

        if len(src_tags) != len(dst_tags):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_tags)} elements, "
                f"dest has {len(dst_tags)} elements"
            )

        updates = {}
        for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
            value = ctx.get_tag(src_tag.name, src_tag.default)
            updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag, src_tag.type)
            if _copy_clamps(value, dst_tag, src_tag.type):
                _set_fault_out_of_range(ctx)
        ctx.set_tags(updates)

    def _execute_converter_block_copy(
        self, ctx: ScanContext, converter: CopyConverter, dst_tags: list[Tag]
    ) -> None:
        try:
            src_tags = resolve_block_range_tags_ctx(self.source, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return
        if len(src_tags) != len(dst_tags):
            raise ValueError(
                f"BlockCopy length mismatch: source has {len(src_tags)} elements, "
                f"dest has {len(dst_tags)} elements"
            )

        # A character that does not convert leaves its own element and faults; the
        # others are written (CLICK: "1A2" over 99s is 1, 99, 2).
        updates = {}
        for src_tag, dst_tag in zip(src_tags, dst_tags, strict=True):
            try:
                char = _as_single_ascii_char(ctx.get_tag(src_tag.name, src_tag.default))
                updates[dst_tag.name] = _store_numeric_text_digits(
                    char, [dst_tag], mode=converter.mode
                )[dst_tag.name]
            except (IndexError, TypeError, ValueError, OverflowError):
                _set_fault_out_of_range(ctx)
        ctx.set_tags(updates)


class FillInstruction(OneShotMixin, Instruction):
    """Fill instruction.

    Writes a constant value to every element in a destination BlockRange.

    Value can be a literal, Tag, or Expression (resolved once, written to all).
    Dest can be BlockRange or IndirectBlockRange (resolved at scan time).
    """

    _reads = ("value",)
    _writes = ("dest",)
    _fault_writes = ("fault.address_error", "fault.out_of_range")
    _fault_follows = _fault_writes
    _conditions = ()
    _structural_fields = ()

    def __init__(self, value: Any, dest: Any, *, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        check_real_literal(value, dest, "fill() value")
        self.value = value
        self.dest = dest

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        try:
            dst_tags = resolve_block_range_tags_ctx(self.dest, ctx)
            value = resolve_tag_or_value_ctx(self.value, ctx)
        except IndexError:
            _set_fault_address_error(ctx)
            return

        source = copy_source_kind(self.value)
        updates = {}
        for dst_tag in dst_tags:
            if dst_tag.type.name == "CHAR" and isinstance(value, str):
                updates[dst_tag.name] = _as_single_ascii_char(value)
            else:
                updates[dst_tag.name] = _store_copy_value_to_tag_type(value, dst_tag, source)
        ctx.set_tags(updates)
        if dst_tags and _copy_clamps(value, dst_tags[0], source):
            _set_fault_out_of_range(ctx)
