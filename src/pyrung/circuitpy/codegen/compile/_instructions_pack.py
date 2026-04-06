"""Automatically generated module split."""

from __future__ import annotations

from pyrung.circuitpy.codegen._util import (
    _indent_body,
    _range_type_name,
    _static_range_length,
    _value_type_name,
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

from ._primitives import (
    _compile_assignment_lines,
    _compile_guarded_instruction,
    _compile_range_setup,
    _compile_set_out_of_range_fault_body,
    _compile_value,
    _pack_store_expr,
)


def _compile_pack_bits_instruction(
    instr: PackBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"INT", "WORD", "DINT", "REAL"}:
        raise TypeError("pack_bits destination must be INT, WORD, DINT, or REAL")
    if _range_type_name(instr.bit_block) != "BOOL":
        raise TypeError("pack_bits source range must contain BOOL tags")
    width = 16 if dest_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("packbits")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.bit_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    static_len = _static_range_length(instr.bit_block)
    if static_len is not None and static_len > width:
        raise ValueError(
            f"pack_bits destination width is {width} bits but block has {static_len} tags"
        )
    enabled_body = [*src_setup]
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
            f"    if bool({src_symbol}[_src_idx]):",
            "        _packed |= (1 << _bit_index)",
            f"_packed_value = {_pack_store_expr('_packed', dest_type, ctx)}",
            *_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)

def _compile_pack_words_instruction(
    instr: PackWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"DINT", "REAL"}:
        raise TypeError("pack_words destination must be DINT or REAL")
    if _range_type_name(instr.word_block) not in {"INT", "WORD"}:
        raise TypeError("pack_words source range must contain INT/WORD tags")
    stem = ctx.next_name("packwords")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.word_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    static_len = _static_range_length(instr.word_block)
    if static_len is not None and static_len != 2:
        raise ValueError(f"pack_words requires exactly 2 source tags; got {static_len}")
    enabled_body = [*src_setup]
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
            *_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)

def _compile_pack_text_instruction(
    instr: PackTextInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _range_type_name(instr.source_range)
    if source_type != "CHAR":
        raise TypeError("pack_text source range must contain CHAR tags")
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"INT", "DINT", "WORD", "REAL"}:
        raise TypeError("pack_text destination must be INT, DINT, WORD, or REAL")
    ctx.mark_helper("_parse_pack_text_value")
    ctx.mark_helper("_store_copy_value_to_type")
    stem = ctx.next_name("packtext")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.source_range, ctx, stem=f"{stem}_src", include_addresses=False
    )
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    enabled_body = [
        *src_setup,
        f"_text = ''.join(str({src_symbol}[_idx]) for _idx in {src_indices})",
    ]
    if instr.allow_whitespace:
        enabled_body.append("_text = _text.strip()")
    else:
        enabled_body.extend(
            [
                "if _text != _text.strip():",
                *_indent_body(fault_body, 4),
                "else:",
                "    try:",
                f'        _parsed = _parse_pack_text_value(_text, "{dest_type}")',
                f'        _packed_value = _store_copy_value_to_type(_parsed, "{dest_type}")',
                *_indent_body(
                    _compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0), 8
                ),
                "    except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 8),
            ]
        )
        return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)
    enabled_body.extend(
        [
            "try:",
            f'    _parsed = _parse_pack_text_value(_text, "{dest_type}")',
            f'    _packed_value = _store_copy_value_to_type(_parsed, "{dest_type}")',
            *_indent_body(_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0), 4),
            "except (TypeError, ValueError, OverflowError):",
            *_indent_body(fault_body, 4),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)

def _compile_unpack_bits_instruction(
    instr: UnpackToBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _value_type_name(instr.source)
    if source_type not in {"INT", "WORD", "DINT", "REAL"}:
        raise TypeError("unpack_to_bits source must be INT, WORD, DINT, or REAL")
    if _range_type_name(instr.bit_block) != "BOOL":
        raise TypeError("unpack_to_bits destination range must contain BOOL tags")
    width = 16 if source_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("unpackbits")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.bit_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(instr.source, ctx)})"
    elif source_type in {"INT", "WORD"}:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFF)"
    else:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFFFFFF)"
    static_len = _static_range_length(instr.bit_block)
    if static_len is not None and static_len > width:
        raise ValueError(
            f"unpack_to_bits source width is {width} bits but block has {static_len} tags"
        )
    enabled_body = [*dst_setup]
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
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)

def _compile_unpack_words_instruction(
    instr: UnpackToWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _value_type_name(instr.source)
    if source_type not in {"DINT", "REAL"}:
        raise TypeError("unpack_to_words source must be DINT or REAL")
    dst_type = _range_type_name(instr.word_block)
    if dst_type not in {"INT", "WORD"}:
        raise TypeError("unpack_to_words destination range must contain INT/WORD tags")
    stem = ctx.next_name("unpackwords")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.word_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(instr.source, ctx)})"
    else:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFFFFFF)"
    lo_expr = "_lo_word"
    hi_expr = "_hi_word"
    if dst_type == "INT":
        ctx.mark_helper("_wrap_int")
        lo_store = f"_wrap_int({lo_expr}, 16, True)"
        hi_store = f"_wrap_int({hi_expr}, 16, True)"
    else:
        lo_store = f"({lo_expr} & 0xFFFF)"
        hi_store = f"({hi_expr} & 0xFFFF)"
    static_len = _static_range_length(instr.word_block)
    if static_len is not None and static_len != 2:
        raise ValueError(f"unpack_to_words requires exactly 2 destination tags; got {static_len}")
    enabled_body = [*dst_setup]
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
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)
