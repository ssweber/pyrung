"""Automatically generated module split."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import Instruction, OneShotMixin
from .conversions import (
    _as_single_ascii_char,
    _float_to_int_bits,
    _int_to_float_bits,
    _parse_pack_text_value,
    _store_copy_value_to_tag_type,
    _truncate_to_tag_type,
)
from .resolvers import (
    _set_fault_out_of_range,
    resolve_block_range_tags_ctx,
    resolve_tag_ctx,
)
from .utils import (
    assert_tag_type,
    guard_oneshot_execution,
)

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


class PackBitsInstruction(OneShotMixin, Instruction):
    """Pack BOOL tags from a BlockRange into a destination register."""

    def __init__(self, bit_block: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.bit_block = bit_block
        self.dest = dest

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        assert_tag_type(
            dest_tag,
            (TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL),
            label="pack_bits destination",
        )

        bit_tags = resolve_block_range_tags_ctx(self.bit_block, ctx)
        width = 16 if dest_tag.type in {TagType.INT, TagType.WORD} else 32
        if len(bit_tags) > width:
            raise ValueError(
                f"pack_bits destination width is {width} bits but block has {len(bit_tags)} tags"
            )

        packed = 0
        for bit_index, bit_tag in enumerate(bit_tags):
            assert_tag_type(
                bit_tag,
                (TagType.BOOL,),
                label="pack_bits source tags",
                include_tag_name=True,
            )
            bit_value = ctx.get_tag(bit_tag.name, bit_tag.default)
            if bool(bit_value):
                packed |= 1 << bit_index

        if dest_tag.type == TagType.REAL:
            value = _int_to_float_bits(packed)
        else:
            value = _truncate_to_tag_type(packed, dest_tag)
        ctx.set_tag(dest_tag.name, value)


class PackWordsInstruction(OneShotMixin, Instruction):
    """Pack two 16-bit tags into a 32-bit destination register."""

    def __init__(self, word_block: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.word_block = word_block
        self.dest = dest

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        assert_tag_type(dest_tag, (TagType.DINT, TagType.REAL), label="pack_words destination")

        word_tags = resolve_block_range_tags_ctx(self.word_block, ctx)
        if len(word_tags) != 2:
            raise ValueError(f"pack_words requires exactly 2 source tags; got {len(word_tags)}")
        assert_tag_type(
            word_tags[0],
            (TagType.INT, TagType.WORD),
            label="pack_words source tags",
            include_tag_name=True,
        )
        assert_tag_type(
            word_tags[1],
            (TagType.INT, TagType.WORD),
            label="pack_words source tags",
            include_tag_name=True,
        )

        lo_value = ctx.get_tag(word_tags[0].name, word_tags[0].default)
        hi_value = ctx.get_tag(word_tags[1].name, word_tags[1].default)
        packed = (int(hi_value) << 16) | (int(lo_value) & 0xFFFF)

        if dest_tag.type == TagType.REAL:
            value = _int_to_float_bits(packed)
        else:
            value = _truncate_to_tag_type(packed, dest_tag)
        ctx.set_tag(dest_tag.name, value)


class PackTextInstruction(OneShotMixin, Instruction):
    """Pack Copy text mode: parse CHAR range into a numeric destination."""

    def __init__(
        self, source_range: Any, dest: Any, *, allow_whitespace: bool = False, oneshot: bool = False
    ):
        OneShotMixin.__init__(self, oneshot)
        self.source_range = source_range
        self.dest = dest
        self.allow_whitespace = bool(allow_whitespace)

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        assert_tag_type(
            dest_tag,
            (TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL),
            label="pack_text destination",
        )

        src_tags = resolve_block_range_tags_ctx(self.source_range, ctx)
        for src in src_tags:
            assert_tag_type(
                src,
                (TagType.CHAR,),
                label="pack_text source range must contain only CHAR tags",
                include_tag_name=True,
            )

        try:
            text = "".join(
                _as_single_ascii_char(ctx.get_tag(src.name, src.default)) for src in src_tags
            )
            if not self.allow_whitespace and text != text.strip():
                _set_fault_out_of_range(ctx)
                return
            if self.allow_whitespace:
                text = text.strip()
            parsed = _parse_pack_text_value(text, dest_tag)
        except (TypeError, ValueError, OverflowError):
            _set_fault_out_of_range(ctx)
            return

        ctx.set_tag(dest_tag.name, _store_copy_value_to_tag_type(parsed, dest_tag))


class UnpackToBitsInstruction(OneShotMixin, Instruction):
    """Unpack a register value into individual BOOL tags in a BlockRange."""

    def __init__(self, source: Any, bit_block: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.bit_block = bit_block

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        from pyrung.core.tag import TagType

        source_tag = resolve_tag_ctx(self.source, ctx)
        assert_tag_type(
            source_tag,
            (TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL),
            label="unpack_to_bits source",
        )

        bit_tags = resolve_block_range_tags_ctx(self.bit_block, ctx)
        width = 16 if source_tag.type in {TagType.INT, TagType.WORD} else 32
        if len(bit_tags) > width:
            raise ValueError(
                f"unpack_to_bits source width is {width} bits but block has {len(bit_tags)} tags"
            )

        source_value = ctx.get_tag(source_tag.name, source_tag.default)
        if source_tag.type == TagType.REAL:
            bits = _float_to_int_bits(source_value)
        elif source_tag.type in {TagType.INT, TagType.WORD}:
            bits = int(source_value) & 0xFFFF
        else:  # DINT
            bits = int(source_value) & 0xFFFFFFFF

        updates = {}
        for bit_index, bit_tag in enumerate(bit_tags):
            assert_tag_type(
                bit_tag,
                (TagType.BOOL,),
                label="unpack_to_bits destination tags",
                include_tag_name=True,
            )
            updates[bit_tag.name] = bool((bits >> bit_index) & 1)
        ctx.set_tags(updates)


class UnpackToWordsInstruction(OneShotMixin, Instruction):
    """Unpack a 32-bit register value into two 16-bit destination tags."""

    def __init__(self, source: Any, word_block: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.word_block = word_block

    @guard_oneshot_execution
    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        from pyrung.core.tag import TagType

        source_tag = resolve_tag_ctx(self.source, ctx)
        assert_tag_type(
            source_tag,
            (TagType.DINT, TagType.REAL),
            label="unpack_to_words source",
        )

        word_tags = resolve_block_range_tags_ctx(self.word_block, ctx)
        if len(word_tags) != 2:
            raise ValueError(
                f"unpack_to_words requires exactly 2 destination tags; got {len(word_tags)}"
            )
        assert_tag_type(
            word_tags[0],
            (TagType.INT, TagType.WORD),
            label="unpack_to_words destination tags",
            include_tag_name=True,
        )
        assert_tag_type(
            word_tags[1],
            (TagType.INT, TagType.WORD),
            label="unpack_to_words destination tags",
            include_tag_name=True,
        )

        source_value = ctx.get_tag(source_tag.name, source_tag.default)
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
