"""Automatically generated module split."""

from __future__ import annotations

from typing import assert_never

from pyrung.circuitpy.codegen._util import (
    _indent_body,
    _store_coerce_expr,
)
from pyrung.circuitpy.codegen.context import (
    CodegenContext,
)
from pyrung.core.instruction import (
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.instruction.operands import (
    Dest,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    Span,
    TagArg,
)
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import Tag

from ._emit import emit_truth
from ._primitives import (
    _compile_assignment_lines,
    _compile_guarded_instruction,
    _compile_range_flush,
    _compile_range_setup,
    _compile_set_out_of_range_fault_body,
    _compile_value,
    _pack_store_expr,
    _pointer_check_lines,
)


def _register(dest: Dest) -> tuple[Tag | IndirectRef | IndirectExprRef, str]:
    """Return the tag or the pointer that *dest* holds, and the name of its type.

    The generated code reads and writes an ``immediate()`` tag as it does the plain tag.
    """
    match dest:
        case TagArg(tag=tag):
            return tag, tag.type.name
        case PointerArg(ref=ref):
            return ref, ref.block.type.name
        case _:
            assert_never(dest)


def _span(span: Span) -> tuple[BlockRange | IndirectBlockRange, int | None]:
    """Return the range that *span* holds, and its length if its ends are fixed.

    The generated code reads and writes an ``immediate()`` range as it does the plain range.
    ``__init__`` refuses a range with fixed ends of the wrong length.  The generated code
    checks only the length of a range with pointers.
    """
    match span:
        case RangeArg(span=fixed):
            return fixed, len(fixed)
        case PointerRangeArg(span=pointed):
            return pointed, None
        case _:
            assert_never(span)


def _compile_pack_bits_instruction(
    instr: PackBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest, dest_type = _register(instr.dest)
    bit_block, static_len = _span(instr.bit_block)
    width = 16 if dest_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("packbits")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        bit_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    enabled_body = [*_pointer_check_lines(dest, ctx, past_end_drops=True), *src_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({src_indices}) > {width}:",
                f'    raise ValueError(f"pack_bits destination width is {width} bits but block has {{len({src_indices})}} tags")',
            ]
        )
    enabled_body.extend(
        [
            "_packed = 0",
            f"for _bit_index, _src_idx in enumerate({src_indices}):",
            f"    if {emit_truth(f'{src_symbol}[_src_idx]')}:",
            "        _packed |= (1 << _bit_index)",
            f"_packed_value = {_pack_store_expr('_packed', dest_type, ctx)}",
            *_compile_assignment_lines(dest, "_packed_value", ctx, indent=0),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_pack_words_instruction(
    instr: PackWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest, dest_type = _register(instr.dest)
    word_block, static_len = _span(instr.word_block)
    stem = ctx.next_name("packwords")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        word_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    enabled_body = [*_pointer_check_lines(dest, ctx, past_end_drops=True), *src_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({src_indices}) != 2:",
                f'    raise ValueError(f"pack_words requires exactly 2 source tags; got {{len({src_indices})}}")',
            ]
        )
    enabled_body.extend(
        [
            f"_lo_value = int({src_symbol}[{src_indices}[0]])",
            f"_hi_value = int({src_symbol}[{src_indices}[1]])",
            "_packed = ((_hi_value << 16) | (_lo_value & 0xFFFF))",
            f"_packed_value = {_pack_store_expr('_packed', dest_type, ctx)}",
            *_compile_assignment_lines(dest, "_packed_value", ctx, indent=0),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _clamp_fault(dest_type: str, fault_body: list[str], spaces: int) -> list[str]:
    """Return the lines that raise the out-of-range fault when an INT or DINT pack clamped."""
    if dest_type not in {"INT", "DINT"}:
        return []
    return [
        " " * spaces + "if _packed_value != _parsed:",
        *_indent_body(fault_body, spaces + 4),
    ]


def _compile_pack_text_instruction(
    instr: PackTextInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_range, _ = _span(instr.source_range)
    dest, dest_type = _register(instr.dest)
    ctx.mark_helper("_parse_pack_text_value")
    ctx.mark_helper("_store_real")
    stem = ctx.next_name("packtext")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        source_range, ctx, stem=f"{stem}_src", include_addresses=False
    )
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    parse_body = [
        "try:",
        f'    _parsed = _parse_pack_text_value(_text, "{dest_type}")',
        f"    _packed_value = {_store_coerce_expr('_parsed', dest_type, ctx)}",
        *_indent_body(_compile_assignment_lines(dest, "_packed_value", ctx, indent=0), 4),
        *_clamp_fault(dest_type, fault_body, 4),
        "except (TypeError, ValueError, OverflowError):",
        *_indent_body(fault_body, 4),
    ]
    if instr.allow_whitespace:
        parse_body = ["_text = _text.strip()", *parse_body]
    else:
        parse_body = [
            "if _text != _text.strip():",
            *_indent_body(fault_body, 4),
            "else:",
            *_indent_body(parse_body, 4),
        ]
    enabled_body = [
        *src_setup,
        f"_text = ''.join(str({src_symbol}[_idx]) for _idx in {src_indices})",
        # A TXT holds one byte.  A character above FFh is refused before Python's int()
        # or float() can read it as a digit, such as U+0661.
        "if any(ord(_ch) > 255 for _ch in _text):",
        *_indent_body(fault_body, 4),
        "else:",
        *_indent_body(parse_body, 4),
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_unpack_bits_instruction(
    instr: UnpackToBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source, source_type = _register(instr.source)
    bit_block, static_len = _span(instr.bit_block)
    width = 16 if source_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("unpackbits")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        bit_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(source, ctx)})"
    elif source_type in {"INT", "WORD"}:
        bits_expr = f"(int({_compile_value(source, ctx)}) & 0xFFFF)"
    else:
        bits_expr = f"(int({_compile_value(source, ctx)}) & 0xFFFFFFFF)"
    enabled_body = [*_pointer_check_lines(source, ctx, past_end_drops=False), *dst_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({dst_indices}) > {width}:",
                f'    raise ValueError(f"unpack_to_bits source width is {width} bits but block has {{len({dst_indices})}} tags")',
            ]
        )
    enabled_body.extend(
        [
            f"_bits = {bits_expr}",
            f"for _bit_index, _dst_idx in enumerate({dst_indices}):",
            f"    {dst_symbol}[_dst_idx] = bool((_bits >> _bit_index) & 1)",
            *_compile_range_flush(bit_block, dst_symbol, ctx),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_unpack_words_instruction(
    instr: UnpackToWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source, source_type = _register(instr.source)
    word_block, static_len = _span(instr.word_block)
    dst_type = word_block.block.type.name
    stem = ctx.next_name("unpackwords")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        word_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(source, ctx)})"
    else:
        bits_expr = f"(int({_compile_value(source, ctx)}) & 0xFFFFFFFF)"
    lo_expr = "_lo_word"
    hi_expr = "_hi_word"
    if dst_type == "INT":
        ctx.mark_helper("_wrap_int")
        lo_store = f"_wrap_int({lo_expr}, 16, True)"
        hi_store = f"_wrap_int({hi_expr}, 16, True)"
    else:
        lo_store = f"({lo_expr} & 0xFFFF)"
        hi_store = f"({hi_expr} & 0xFFFF)"
    enabled_body = [*_pointer_check_lines(source, ctx, past_end_drops=False), *dst_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({dst_indices}) != 2:",
                f'    raise ValueError(f"unpack_to_words requires exactly 2 destination tags; got {{len({dst_indices})}}")',
            ]
        )
    enabled_body.extend(
        [
            f"_bits = {bits_expr}",
            "_lo_word = (_bits & 0xFFFF)",
            "_hi_word = ((_bits >> 16) & 0xFFFF)",
            f"{dst_symbol}[{dst_indices}[0]] = {lo_store}",
            f"{dst_symbol}[{dst_indices}[1]] = {hi_store}",
            *_compile_range_flush(word_block, dst_symbol, ctx),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)
