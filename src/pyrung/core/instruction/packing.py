"""Automatically generated module split."""

from __future__ import annotations

from typing import Any

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


class PackBitsInstruction(OneShotMixin, Instruction):
    """Pack BOOL tags from a BlockRange into a destination register."""

    def __init__(self, bit_block: Any, dest: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.bit_block = bit_block
        self.dest = dest

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not self.should_execute(enabled):
            return

        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        if dest_tag.type not in {TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL}:
            raise TypeError(
                f"pack_bits destination must be INT, WORD, DINT, or REAL; got {dest_tag.type.name}"
            )

        bit_tags = resolve_block_range_tags_ctx(self.bit_block, ctx)
        width = 16 if dest_tag.type in {TagType.INT, TagType.WORD} else 32
        if len(bit_tags) > width:
            raise ValueError(
                f"pack_bits destination width is {width} bits but block has {len(bit_tags)} tags"
            )

        packed = 0
        for bit_index, bit_tag in enumerate(bit_tags):
            if bit_tag.type != TagType.BOOL:
                raise TypeError(
                    f"pack_bits source tags must be BOOL; got {bit_tag.type.name} at {bit_tag.name}"
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

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not self.should_execute(enabled):
            return

        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        if dest_tag.type not in {TagType.DINT, TagType.REAL}:
            raise TypeError(
                f"pack_words destination must be DINT or REAL; got {dest_tag.type.name}"
            )

        word_tags = resolve_block_range_tags_ctx(self.word_block, ctx)
        if len(word_tags) != 2:
            raise ValueError(f"pack_words requires exactly 2 source tags; got {len(word_tags)}")
        if word_tags[0].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"pack_words source tags must be INT or WORD; got {word_tags[0].type.name} "
                f"at {word_tags[0].name}"
            )
        if word_tags[1].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"pack_words source tags must be INT or WORD; got {word_tags[1].type.name} "
                f"at {word_tags[1].name}"
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

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not self.should_execute(enabled):
            return

        from pyrung.core.tag import TagType

        dest_tag = resolve_tag_ctx(self.dest, ctx)
        if dest_tag.type not in {TagType.INT, TagType.DINT, TagType.WORD, TagType.REAL}:
            raise TypeError(
                f"pack_text destination must be INT, DINT, WORD, or REAL; got {dest_tag.type.name}"
            )

        src_tags = resolve_block_range_tags_ctx(self.source_range, ctx)
        for src in src_tags:
            if src.type != TagType.CHAR:
                raise TypeError(
                    f"pack_text source range must contain only CHAR tags; got {src.type.name} at {src.name}"
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

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not self.should_execute(enabled):
            return

        from pyrung.core.tag import TagType

        source_tag = resolve_tag_ctx(self.source, ctx)
        if source_tag.type not in {TagType.INT, TagType.WORD, TagType.DINT, TagType.REAL}:
            raise TypeError(
                "unpack_to_bits source must be INT, WORD, DINT, or REAL; "
                f"got {source_tag.type.name}"
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
            if bit_tag.type != TagType.BOOL:
                raise TypeError(
                    f"unpack_to_bits destination tags must be BOOL; got "
                    f"{bit_tag.type.name} at {bit_tag.name}"
                )
            updates[bit_tag.name] = bool((bits >> bit_index) & 1)
        ctx.set_tags(updates)


class UnpackToWordsInstruction(OneShotMixin, Instruction):
    """Unpack a 32-bit register value into two 16-bit destination tags."""

    def __init__(self, source: Any, word_block: Any, oneshot: bool = False):
        OneShotMixin.__init__(self, oneshot)
        self.source = source
        self.word_block = word_block

    def execute(self, ctx: ScanContext, enabled: bool) -> None:
        if not self.should_execute(enabled):
            return

        from pyrung.core.tag import TagType

        source_tag = resolve_tag_ctx(self.source, ctx)
        if source_tag.type not in {TagType.DINT, TagType.REAL}:
            raise TypeError(
                f"unpack_to_words source must be DINT or REAL; got {source_tag.type.name}"
            )

        word_tags = resolve_block_range_tags_ctx(self.word_block, ctx)
        if len(word_tags) != 2:
            raise ValueError(
                f"unpack_to_words requires exactly 2 destination tags; got {len(word_tags)}"
            )
        if word_tags[0].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"unpack_to_words destination tags must be INT or WORD; got "
                f"{word_tags[0].type.name} at {word_tags[0].name}"
            )
        if word_tags[1].type not in {TagType.INT, TagType.WORD}:
            raise TypeError(
                f"unpack_to_words destination tags must be INT or WORD; got "
                f"{word_tags[1].type.name} at {word_tags[1].name}"
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
