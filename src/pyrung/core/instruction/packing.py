"""Automatically generated module split."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, assert_never

from pyrung.core.memory_block import PastBankError
from pyrung.core.tag import Tag, TagType

from .base import Instruction, OneShotMixin
from .conversions import (
    _as_single_ascii_char,
    _float_to_int_bits,
    _int_to_float_bits,
    _parse_pack_text_value,
    _store_copy_value_to_tag_type,
    _truncate_to_tag_type,
)
from .data_transfer import _dest_tag, _read, _span_tags
from .operands import RANGE, TAG, Dest, FitsRegister, PointerArg, Span, TagArg, check_rules
from .resolvers import (
    _set_fault_address_error,
    _set_fault_out_of_range,
    resolve_source_type_tag_ctx,
)
from .utils import guard_oneshot_execution

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


# Pack and Unpack are Copy tabs on a CLICK, so their flags follow them as Copy's flags do.
_COPY_FAULTS = ("fault.address_error", "fault.out_of_range")

_REGISTER = TAG.of(TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL)
_WIDE_REGISTER = TAG.of(TagType.DINT, TagType.REAL)
_BITS = RANGE.of(TagType.BOOL, example="c.select(1, 16)")
_TWO_WORDS = RANGE.of(TagType.INT, TagType.WORD, count=2, example="ds.select(1, 2)")
_CHARS = RANGE.of(TagType.CHAR, example="txt.select(1, 4)")


def _skip_on_bad_pointer(func: Callable[..., None]) -> Callable[..., None]:
    """Wrap an instruction's execute method to handle a pointer outside its bank.

    If the pointer is below its bank, set the address-error flag and skip the instruction.
    If the pointer is past its bank, drop the write and set no flag.
    """

    @wraps(func)
    def wrapper(self: Any, ctx: ScanContext, enabled: bool) -> None:
        try:
            func(self, ctx, enabled)
        except PastBankError:
            pass
        except IndexError:
            _set_fault_address_error(ctx)

    return wrapper


def _unpack_source_tag(source: Dest, ctx: ScanContext) -> Tag:
    """Return a tag with the type of the register that *source* reads on this scan."""
    match source:
        case TagArg(tag=tag):
            return tag
        case PointerArg(ref=ref):
            return resolve_source_type_tag_ctx(ref, ctx)
        case _:
            assert_never(source)


def _dest_type_tag(dest: Dest) -> Tag:
    """Return a tag with the type of the register that *dest* writes, without reading a pointer."""
    match dest:
        case TagArg(tag=tag):
            return tag
        case PointerArg(ref=ref):
            return ref.block._get_tag(ref.block.start)
        case _:
            assert_never(dest)


class PackBitsInstruction(OneShotMixin, Instruction):
    """Pack BOOL tags from a BlockRange into a destination register.

    ``__init__`` parses both operands. ``bit_block`` is a ``Span`` and ``dest`` is a ``Dest``
    (:mod:`pyrung.core.instruction.operands`).
    """

    _reads = ("bit_block",)
    _writes = ("dest",)
    _call_operands = (("bit_block", "bit_block"), ("dest", "dest"))
    _fault_writes = _COPY_FAULTS
    _fault_follows = _COPY_FAULTS
    _conditions = ()
    _structural_fields = ()
    _operands = {"bit_block": _BITS, "dest": _REGISTER}
    _operand_rules = (FitsRegister(bits="bit_block", register="dest"),)

    def __init__(self, bit_block: Any, dest: Any, *, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.bit_block: Span = _BITS.parse("pack_bits()", "bit_block", bit_block)
        self.dest: Dest = _REGISTER.parse("pack_bits()", "dest", dest)
        check_rules(
            self._operands,
            self._operand_rules,
            "pack_bits()",
            {"bit_block": bit_block, "dest": dest},
        )

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing. ``__init__`` has already parsed every operand and checked the rules."""

    @guard_oneshot_execution
    @_skip_on_bad_pointer
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        dest_tag = _dest_tag(self.dest, ctx)
        bit_tags = _span_tags(self.bit_block, ctx)
        # ``__init__`` refuses a range with fixed ends that is too long.  The length of a
        # range with pointers is known only when the scan runs, so the scan checks it.
        width = 16 if dest_tag.type in {TagType.INT, TagType.WORD} else 32
        if len(bit_tags) > width:
            raise ValueError(
                f"pack_bits destination width is {width} bits but block has {len(bit_tags)} tags"
            )

        packed = 0
        for bit_index, bit_tag in enumerate(bit_tags):
            bit_value = ctx.get_tag(bit_tag.name, bit_tag.default)
            if bool(bit_value):
                packed |= 1 << bit_index

        if dest_tag.type == TagType.REAL:
            value = _int_to_float_bits(packed)
        else:
            value = _truncate_to_tag_type(packed, dest_tag)
        ctx.set_tag(dest_tag.name, value)


class PackWordsInstruction(OneShotMixin, Instruction):
    """Pack two 16-bit tags into a 32-bit destination register.

    ``__init__`` parses both operands. ``word_block`` is a ``Span`` and ``dest`` is a ``Dest``
    (:mod:`pyrung.core.instruction.operands`).
    """

    _reads = ("word_block",)
    _writes = ("dest",)
    _call_operands = (("word_block", "word_block"), ("dest", "dest"))
    _fault_writes = _COPY_FAULTS
    _fault_follows = _COPY_FAULTS
    _conditions = ()
    _structural_fields = ()
    _operands = {"word_block": _TWO_WORDS, "dest": _WIDE_REGISTER}

    def __init__(self, word_block: Any, dest: Any, *, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.word_block: Span = _TWO_WORDS.parse("pack_words()", "word_block", word_block)
        self.dest: Dest = _WIDE_REGISTER.parse("pack_words()", "dest", dest)

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing. ``__init__`` has already parsed every operand and checked the rules."""

    @guard_oneshot_execution
    @_skip_on_bad_pointer
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        dest_tag = _dest_tag(self.dest, ctx)
        word_tags = _span_tags(self.word_block, ctx)
        # ``__init__`` refuses a range with fixed ends of another length.  The length of a
        # range with pointers is known only when the scan runs, so the scan checks it.
        if len(word_tags) != 2:
            raise ValueError(f"pack_words requires exactly 2 source tags; got {len(word_tags)}")

        lo_value = ctx.get_tag(word_tags[0].name, word_tags[0].default)
        hi_value = ctx.get_tag(word_tags[1].name, word_tags[1].default)
        packed = (int(hi_value) << 16) | (int(lo_value) & 0xFFFF)

        if dest_tag.type == TagType.REAL:
            value = _int_to_float_bits(packed)
        else:
            value = _truncate_to_tag_type(packed, dest_tag)
        ctx.set_tag(dest_tag.name, value)


class PackTextInstruction(OneShotMixin, Instruction):
    """Pack Copy text mode: parse CHAR range into a numeric destination.

    ``__init__`` parses both operands. ``source_range`` is a ``Span`` and ``dest`` is a ``Dest``
    (:mod:`pyrung.core.instruction.operands`).
    """

    _reads = ("source_range",)
    _writes = ("dest",)
    _call_operands = (("source_range", "source_range"), ("dest", "dest"))
    _call_keywords = ("allow_whitespace", "oneshot")
    _fault_writes = _COPY_FAULTS
    _fault_follows = _COPY_FAULTS
    _conditions = ()
    _structural_fields = ("allow_whitespace",)
    _operands = {"source_range": _CHARS, "dest": _REGISTER}

    def __init__(
        self, source_range: Any, dest: Any, *, allow_whitespace: bool = False, oneshot: bool = False
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source_range: Span = _CHARS.parse("pack_text()", "source_range", source_range)
        self.dest: Dest = _REGISTER.parse("pack_text()", "dest", dest)
        self.allow_whitespace = bool(allow_whitespace)

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing. ``__init__`` has already parsed every operand and checked the rules."""

    @guard_oneshot_execution
    @_skip_on_bad_pointer
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        # Take only the type here.  A pointer in the destination counts once the text parses.
        dest_type = _dest_type_tag(self.dest)
        src_tags = _span_tags(self.source_range, ctx)

        try:
            text = "".join(
                _as_single_ascii_char(ctx.get_tag(src.name, src.default)) for src in src_tags
            )
            if not self.allow_whitespace and text != text.strip():
                _set_fault_out_of_range(ctx)
                return
            if self.allow_whitespace:
                text = text.strip()
            parsed = _parse_pack_text_value(text, dest_type)
        except (TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)
            return

        dest_tag = _dest_tag(self.dest, ctx)
        stored = _store_copy_value_to_tag_type(parsed, dest_tag)
        ctx.set_tag(dest_tag.name, stored)
        if dest_tag.type in {TagType.INT, TagType.DINT} and stored != parsed:
            # CLICK stores the clamped value and raises the out-of-range fault.
            _set_fault_out_of_range(ctx)


class UnpackToBitsInstruction(OneShotMixin, Instruction):
    """Unpack a register value into individual BOOL tags in a BlockRange.

    ``__init__`` parses both operands. ``source`` is a ``Dest`` and ``bit_block`` is a ``Span``
    (:mod:`pyrung.core.instruction.operands`).  ``dest`` returns ``bit_block``.
    """

    _reads = ("source",)
    _writes = ("bit_block",)
    _call_operands = (("source", "source"), ("bit_block", "bit_block"))
    _fault_writes = _COPY_FAULTS
    _fault_follows = _COPY_FAULTS
    _conditions = ()
    _structural_fields = ()
    _operands = {"source": _REGISTER, "bit_block": _BITS}
    _operand_rules = (FitsRegister(bits="bit_block", register="source"),)

    def __init__(self, source: Any, bit_block: Any, *, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source: Dest = _REGISTER.parse("unpack_to_bits()", "source", source)
        self.bit_block: Span = _BITS.parse("unpack_to_bits()", "bit_block", bit_block)
        check_rules(
            self._operands,
            self._operand_rules,
            "unpack_to_bits()",
            {"source": source, "bit_block": bit_block},
        )

    @property
    def dest(self) -> Span:
        return self.bit_block

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing. ``__init__`` has already parsed every operand and checked the rules."""

    @guard_oneshot_execution
    @_skip_on_bad_pointer
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        source_tag = _unpack_source_tag(self.source, ctx)
        bit_tags = _span_tags(self.bit_block, ctx)
        # ``__init__`` refuses a range with fixed ends that is too long.  The length of a
        # range with pointers is known only when the scan runs, so the scan checks it.
        width = 16 if source_tag.type in {TagType.INT, TagType.WORD} else 32
        if len(bit_tags) > width:
            raise ValueError(
                f"unpack_to_bits source width is {width} bits but block has {len(bit_tags)} tags"
            )

        source_value = _read(self.source, ctx)
        if source_tag.type == TagType.REAL:
            bits = _float_to_int_bits(source_value)
        elif source_tag.type in {TagType.INT, TagType.WORD}:
            bits = int(source_value) & 0xFFFF
        else:  # DINT
            bits = int(source_value) & 0xFFFFFFFF

        updates = {}
        for bit_index, bit_tag in enumerate(bit_tags):
            updates[bit_tag.name] = bool((bits >> bit_index) & 1)
        ctx.set_tags(updates)


class UnpackToWordsInstruction(OneShotMixin, Instruction):
    """Unpack a 32-bit register value into two 16-bit destination tags.

    ``__init__`` parses both operands. ``source`` is a ``Dest`` and ``word_block`` is a ``Span``
    (:mod:`pyrung.core.instruction.operands`).  ``dest`` returns ``word_block``.
    """

    _reads = ("source",)
    _writes = ("word_block",)
    _call_operands = (("source", "source"), ("word_block", "word_block"))
    _fault_writes = _COPY_FAULTS
    _fault_follows = _COPY_FAULTS
    _conditions = ()
    _structural_fields = ()
    _operands = {"source": _WIDE_REGISTER, "word_block": _TWO_WORDS}

    def __init__(self, source: Any, word_block: Any, *, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source: Dest = _WIDE_REGISTER.parse("unpack_to_words()", "source", source)
        self.word_block: Span = _TWO_WORDS.parse("unpack_to_words()", "word_block", word_block)

    @property
    def dest(self) -> Span:
        return self.word_block

    def check_operands(self, call: str | None = None) -> None:
        """Do nothing. ``__init__`` has already parsed every operand and checked the rules."""

    @guard_oneshot_execution
    @_skip_on_bad_pointer
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        source_tag = _unpack_source_tag(self.source, ctx)
        word_tags = _span_tags(self.word_block, ctx)
        # ``__init__`` refuses a range with fixed ends of another length.  The length of a
        # range with pointers is known only when the scan runs, so the scan checks it.
        if len(word_tags) != 2:
            raise ValueError(
                f"unpack_to_words requires exactly 2 destination tags; got {len(word_tags)}"
            )

        source_value = _read(self.source, ctx)
        bits = (
            _float_to_int_bits(source_value)
            if source_tag.type == TagType.REAL
            else (int(source_value) & 0xFFFFFFFF)
        )

        lo_word = bits & 0xFFFF
        hi_word = (bits >> 16) & 0xFFFF

        updates = {
            word_tags[0].name: _truncate_to_tag_type(lo_word, word_tags[0]),
            word_tags[1].name: _truncate_to_tag_type(hi_word, word_tags[1]),
        }
        ctx.set_tags(updates)
