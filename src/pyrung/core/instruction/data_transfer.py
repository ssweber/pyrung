"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.copy_converters import CopyConverter
from pyrung.core.expression import Expression
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    PastBankError,
    _element_name,
    _store_block,
)
from pyrung.core.storage import check_real_literal, computed_real, store_value
from pyrung.core.tag import Tag, TagType

from .base import Instruction, OneShotMixin
from .conversions import (
    _NUMBER_TYPES,
    LITERAL,
    _as_single_ascii_char,
    _ascii_char_from_code,
    _copy_clamps,
    _render_text_from_numeric,
    _store_copy_value_to_tag_type,
    _store_numeric_text_digits,
    _text_from_source_value,
    copy_source_kind,
    copy_store_transform,
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
    """Return the conversion that a Copy with no ``convert=`` makes across text, as CLICK does.

    A number register or a number constant into a TXT writes its text, as ``to_text()``
    does.  CLICK's dialog has no Copy there without a conversion, and it starts on Copy
    Value with Suppress zero.  CLICK stores the digits of a constant too.  A TXT into a
    number reads its digit, as ``to_value`` does.  Return ``None`` for any other Copy.
    """
    from pyrung.core.copy_converters import to_text, to_value
    from pyrung.core.tag import TagType

    source_type = copy_source_kind(source)
    dest_type = copy_source_kind(dest)
    if dest_type is TagType.CHAR and (source_type in _NUMBER_TYPES or _is_number_constant(source)):
        return to_text()
    if source_type is TagType.CHAR and dest_type in _NUMBER_TYPES:
        return to_value
    return None


def _is_number_constant(value: Any) -> bool:
    """Return True for an int or float constant.  A bool is not a number to a Copy."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _unwrapped(value: Any) -> Any:
    from pyrung.core.tag import ImmediateRef

    return value.value if isinstance(value, ImmediateRef) else value


def _is_range(value: Any) -> bool:
    return isinstance(_unwrapped(value), (BlockRange, IndirectBlockRange))


def _range_type(block_range: Any) -> Any:
    """Return the tag type of a range from ``.select()``, or ``None`` for anything else."""
    block_range = _unwrapped(block_range)
    if isinstance(block_range, (BlockRange, IndirectBlockRange)):
        return block_range.block.type
    return None


def _refuse_text_that_stores_no_number(what: str, value: Any, dest_type: Any) -> None:
    """Raise ``ValueError`` for a text constant that a store into *dest_type* cannot read.

    A store of a text constant into a number register reads the text as a number, so
    ``"12"`` stores 12.  Text that is not a number would raise on every scan.
    """
    if not isinstance(value, str) or dest_type not in _NUMBER_TYPES:
        return
    storage = copy_store_transform(dest_type, LITERAL)
    if storage is None:
        return
    try:
        store_value(value, storage)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            f"{what} of the text {value!r} into a number register ({dest_type.name}) cannot "
            "store a number. Write the number without quotes, or copy a TXT with "
            "convert=to_value."
        ) from None


def _copy_dest(dest: Any) -> Any:
    """Return the register that a Copy into *dest* writes first.

    A Copy has one destination.  For a TXT range from ``.select()``, the Copy starts at
    the range's first TXT and writes on through the block, as the CLICK export's Copy
    into that TXT does.  Any other range is refused.
    """
    target = _unwrapped(dest)
    if isinstance(target, BlockRange) and target.block.type is TagType.CHAR:
        tags = target.tags()
        if not tags:
            raise ValueError("copy() into an empty range writes nothing. Copy into one TXT.")
        return tags[0]
    if isinstance(target, IndirectBlockRange):
        raise TypeError(
            "copy() writes one register, and its destination here is a range with a pointer. "
            "Copy into one element with a pointer, as in DS[Ptr]."
        )
    if isinstance(target, BlockRange):
        raise TypeError(
            "copy() writes one register, and its destination here is a range. Use fill() to "
            "write one value into a range, or blockcopy() to copy a range."
        )
    return dest


def _block_copy_dest(source: Any, dest: Any) -> Any:
    """Return the range that a Block Copy from *source* into *dest* writes.

    CLICK's Block Copy takes only the first destination address and writes one element
    for each source element.  So a single tag as *dest* becomes that range of its block.
    """
    if _is_range(dest):
        return dest
    target = _unwrapped(dest)
    if not isinstance(target, Tag):
        raise TypeError(
            "blockcopy() writes a range. Pass the destination as a range from .select()."
        )
    if not isinstance(source, BlockRange):
        raise TypeError(
            f"blockcopy() into {target.name} reads a range with a pointer, so pyrung cannot "
            "tell where the destination ends. Pass the destination as a range from .select()."
        )
    found = _store_block(target)
    if found is None:
        raise TypeError(
            f"blockcopy() into {target.name} needs the block that holds {target.name}. Pass "
            "the destination as a range from .select()."
        )
    block, addr = found
    count = len(source.tags())
    end = addr + count - 1
    if end > block.end:
        raise ValueError(
            f"blockcopy() into {target.name} copies {count} elements, which run past "
            f"{_element_name(block, block.end)} at the end of its block. Start it at "
            f"{_element_name(block, block.end - count + 1)} or lower."
        )
    return block.select(addr, end)


def _source_past_end_keeps(source: Any, ctx: ScanContext) -> bool:
    """Return True for a pointer *source* past the end of a bank whose Copy then writes nothing.

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
    :mod:`pyrung.core.copy_converters`).  With none, a number register or a number
    constant into a CHAR writes its text (``to_text()``).  A CHAR into a number reads its
    digit (``to_value``).  CLICK's plain Copy does the same.  ``convert`` holds the
    converter that runs, and ``authored_convert`` holds the one that was written.

    **Clamping semantics:** Out-of-range values are clamped to the
    destination type's min/max (e.g. copying 40 000 into an INT tag produces
    32 767) and the out-of-range flag (``system.fault.out_of_range``, CLICK's
    SC43) is set.  +/-Inf into an INT clamps the same way, and NaN stores 0 with
    no flag.  A REAL into a DINT clamps with no flag, and a NaN stores
    -2147483648.  A CLICK was read doing both for a Copy of a DF into a DD.
    This differs from `CalcInstruction`, which wraps.  A WORD
    into an INT keeps its bits (0xFFFF is -1); into a WORD a constant keeps its
    low 16 bits, and a DINT or REAL clamps past 65535 and below 0 keeps its low
    16 bits, with no flag (see ``copy_store_transform``).

    **Pointer errors:** A pointer below its bank sets the address-error fault
    flag and skips the copy. A pointer past its bank raises no flag: a
    destination there drops the write, and a source there reads as 0, or, past a
    bank marked ``_past_end_keeps`` (CLICK's DS, DH, DF and TXT), leaves the
    destination as it was.  A text or its digits written over several elements
    writes the ones the destination's block holds and drops the rest, with no flag.

    **Fault flags:** A copy clears the out-of-range and address-error flags as it
    starts, before it reads its source, and sets each it raises.  A one-shot copy
    that does not fire leaves them.

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
        if source is None:
            raise TypeError("copy() needs a source: a tag, a pointer or a constant.")
        if _is_range(source):
            raise TypeError("copy() reads one value. Use blockcopy() to copy a range.")
        dest = _copy_dest(dest)
        check_real_literal(source, dest, "copy() source")
        if convert is None:
            _refuse_text_that_stores_no_number("copy()", source, copy_source_kind(dest))
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
            # The write is dropped with no flag.  The source is still read.
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
        if resolved_target.type.name == "REAL" and isinstance(self.source, Expression):
            value = computed_real(value)

        # String literal fan-out: "00026" → sequential CHAR tags, as many as the block holds
        from pyrung.core.tag import TagType

        if isinstance(value, str) and len(value) > 1 and resolved_target.type == TagType.CHAR:
            try:
                chars = [_as_single_ascii_char(ch) for ch in value]
                targets = _sequential_tags(resolved_target, len(chars))
                updates = {t.name: ch for t, ch in zip(targets, chars, strict=False)}
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
            chars = [_as_single_ascii_char(char) for char in rendered]
            targets = _sequential_tags(resolved_target, len(chars))
            updates = {target.name: char for target, char in zip(targets, chars, strict=False)}
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
            # int() raises ValueError for NaN and OverflowError for +/-Inf.
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
    modes are supported for block copy.  With none, a TXT range into a number range
    runs ``to_value``, as a plain ``copy()`` does.  ``convert`` holds the converter
    that runs, and ``authored_convert`` holds the one that was written.

    A single tag as *dest* is the first address of the destination.  The Block Copy
    writes one element of its block for each source element, as CLICK's does.
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
                "blockcopy() accepts only the to_value and to_ascii converters, "
                f"got to_{convert.mode}."
            )
        OneShotMixin.__init__(self, oneshot)
        if not _is_range(source):
            raise TypeError(
                "blockcopy() reads a range from .select(). Use copy() to copy one value, "
                "or fill() to write one value into a range."
            )
        dest = _block_copy_dest(_unwrapped(source), dest)
        fixed_source, fixed_dest = _unwrapped(source), _unwrapped(dest)
        if isinstance(fixed_source, BlockRange) and isinstance(fixed_dest, BlockRange):
            source_count, dest_count = len(fixed_source.tags()), len(fixed_dest.tags())
            if source_count != dest_count:
                raise ValueError(
                    "blockcopy() needs ranges of the same length. "
                    f"The source has {source_count} tags, and the destination has {dest_count}."
                )
        self.source = source
        self.dest = dest
        self.authored_convert = convert
        if convert is None and _range_type(source) is TagType.CHAR:
            if _range_type(dest) in _NUMBER_TYPES:
                from pyrung.core.copy_converters import to_value

                convert = to_value
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
                "blockcopy() needs ranges of the same length. "
                f"The source has {len(src_tags)} tags, and the destination has {len(dst_tags)}."
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
                "blockcopy() needs ranges of the same length. "
                f"The source has {len(src_tags)} tags, and the destination has {len(dst_tags)}."
            )

        # A character that does not convert leaves its own element unchanged and faults.
        # The other characters are written (CLICK: "1A2" over 99s is 1, 99, 2).
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
        if not _is_range(dest):
            raise TypeError(
                "fill() writes a range. Pass the destination as a range from .select(), "
                "or use copy() to write one register."
            )
        if value is None:
            raise TypeError("fill() needs a value: a tag, a pointer or a constant.")
        if _is_range(value):
            raise TypeError("fill() writes one value. Use blockcopy() to copy a range.")
        dest_type = _range_type(dest)
        if copy_source_kind(value) is TagType.CHAR and dest_type in _NUMBER_TYPES:
            raise TypeError(
                "fill() cannot write a TXT into a number register. Copy the TXT into a "
                "number register with copy(..., convert=to_value), then fill() from that "
                "register."
            )
        _refuse_text_that_stores_no_number("fill()", value, dest_type)
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
        if dst_tags and dst_tags[0].type.name == "REAL" and isinstance(self.value, Expression):
            value = computed_real(value)

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
