"""Kernel function renderer — compiles a Program into a fast step function.

Reuses the same compile_rung() pipeline as the CircuitPy renderer but
wraps the output in a plain-Python function that reads/writes kernel
dicts instead of module-level globals backed by hardware I/O.
"""

from __future__ import annotations

from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _DINT_MIN,
    _HELPER_ORDER,
    _INT_MAX,
    _INT_MIN,
    _TYPE_DEFAULTS,
)
from pyrung.circuitpy.codegen._util import (
    _first_defined_name,
    _global_line,
    _subroutine_symbol,
)
from pyrung.circuitpy.codegen.compile import compile_rung
from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.core.kernel import BlockSpec, CompiledKernel
from pyrung.core.program import Program


def compile_kernel(program: Program) -> CompiledKernel:
    """Compile a Program into a fast in-process replay kernel."""
    ctx = CodegenContext.for_kernel(program)
    source = _render_kernel_source(ctx)

    namespace: dict[str, Any] = {}
    exec(compile(source, "<kernel>", "exec"), namespace)  # noqa: S102
    step_fn = namespace["_kernel_step"]

    block_specs = _build_block_specs(ctx)

    return CompiledKernel(
        step_fn=step_fn,
        referenced_tags=dict(ctx.referenced_tags),
        block_specs=block_specs,
        edge_tags=set(ctx.edge_prev_tags),
        source=source,
    )


def _build_block_specs(ctx: CodegenContext) -> dict[str, BlockSpec]:
    specs: dict[str, BlockSpec] = {}
    for binding in sorted(
        ctx.block_bindings.values(),
        key=lambda b: (ctx.block_symbols[b.block_id], b.block_id),
    ):
        symbol = ctx.block_symbols[binding.block_id]
        size = binding.end - binding.start + 1
        tag_names: list[str] = []
        for addr in range(binding.start, binding.end + 1):
            tag = binding.block._get_tag(addr)
            tag_names.append(tag.name)
        specs[symbol] = BlockSpec(
            symbol=symbol,
            size=size,
            default=_TYPE_DEFAULTS[binding.tag_type],
            tag_type=binding.tag_type,
            tag_names=tuple(tag_names),
        )
    return specs


def _render_kernel_source(ctx: CodegenContext) -> str:
    sub_fn_lines = _compile_subroutines(ctx)
    main_body = _compile_main_body(ctx)

    lines: list[str] = []
    lines.extend(_render_imports(ctx))
    lines.extend(_render_helpers(ctx))
    lines.extend(_render_indirect_helpers(ctx))
    lines.extend(_render_embedded_functions(ctx))
    lines.extend(_render_declarations(ctx))
    lines.extend(sub_fn_lines)
    lines.extend(_render_step_function(ctx, main_body))
    return "\n".join(lines).rstrip() + "\n"


# -- Compilation (populates ctx side-effects) --------------------------------


def _compile_subroutines(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for sub_name in ctx.subroutine_names:
        fn_name = _subroutine_symbol(sub_name)
        ctx.function_globals[fn_name] = set()
        ctx.set_current_function(fn_name)
        body: list[str] = []
        for rung in ctx.program.subroutines[sub_name]:
            body.extend(compile_rung(rung, fn_name, ctx, indent=4))
        ctx.set_current_function(None)
        globals_line = _global_line(ctx.globals_for_function(fn_name), indent=4)
        lines.append(f"def {fn_name}():")
        if globals_line is not None:
            lines.append(globals_line)
        if body:
            lines.extend(body)
        else:
            lines.append("    pass")
        lines.append("")
    return lines


def _compile_main_body(ctx: CodegenContext) -> list[str]:
    fn_name = "_kernel_step"
    ctx.function_globals[fn_name] = set()
    ctx.set_current_function(fn_name)
    body: list[str] = []
    for rung in ctx.program.rungs:
        body.extend(compile_rung(rung, fn_name, ctx, indent=4))
    ctx.set_current_function(None)
    return body


# -- Source rendering ---------------------------------------------------------


def _render_imports(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    needs_math = (
        "_render_text_from_numeric" in ctx.used_helpers
        or "_parse_pack_text_value" in ctx.used_helpers
        or "_store_copy_value_to_type" in ctx.used_helpers
    )
    needs_struct = (
        "_int_to_float_bits" in ctx.used_helpers
        or "_float_to_int_bits" in ctx.used_helpers
        or "_parse_pack_text_value" in ctx.used_helpers
    )
    needs_re = "_parse_pack_text_value" in ctx.used_helpers
    if needs_math:
        lines.append("import math")
    if needs_re:
        lines.append("import re")
    if needs_struct:
        lines.append("import struct")
    if lines:
        lines.append("")
    return lines


def _render_helpers(ctx: CodegenContext) -> list[str]:
    helper_defs: dict[str, list[str]] = {
        "_clamp_int": [
            "def _clamp_int(value):",
            "    if value < -32768:",
            "        return -32768",
            "    if value > 32767:",
            "        return 32767",
            "    return int(value)",
            "",
        ],
        "_wrap_int": [
            "def _wrap_int(value, bits, signed):",
            "    mask = (1 << bits) - 1",
            "    v = int(value) & mask",
            "    if signed and v >= (1 << (bits - 1)):",
            "        v -= (1 << bits)",
            "    return v",
            "",
        ],
        "_rise": [
            "def _rise(curr, prev):",
            "    return bool(curr) and not bool(prev)",
            "",
        ],
        "_fall": [
            "def _fall(curr, prev):",
            "    return not bool(curr) and bool(prev)",
            "",
        ],
        "_int_to_float_bits": [
            "def _int_to_float_bits(n):",
            '    return struct.unpack("<f", struct.pack("<I", int(n) & 0xFFFFFFFF))[0]',
            "",
        ],
        "_float_to_int_bits": [
            "def _float_to_int_bits(f):",
            '    return struct.unpack("<I", struct.pack("<f", float(f)))[0]',
            "",
        ],
        "_ascii_char_from_code": [
            "def _ascii_char_from_code(code):",
            "    if code < 0 or code > 127:",
            '        raise ValueError("ASCII code out of range")',
            "    return chr(code)",
            "",
        ],
        "_as_single_ascii_char": [
            "def _as_single_ascii_char(value):",
            "    if not isinstance(value, str):",
            '        raise ValueError("CHAR value must be a string")',
            '    if value == "":',
            "        return value",
            "    if len(value) != 1 or ord(value) > 127:",
            '        raise ValueError("CHAR value must be blank or one ASCII character")',
            "    return value",
            "",
        ],
        "_text_from_source_value": [
            "def _text_from_source_value(value):",
            "    if isinstance(value, str):",
            "        return value",
            '    raise ValueError("text conversion source must resolve to str")',
            "",
        ],
        "_store_numeric_text_digit": [
            "def _store_numeric_text_digit(char, mode):",
            "    _char = _as_single_ascii_char(char)",
            '    if _char == "":',
            '        raise ValueError("empty CHAR cannot be converted to numeric")',
            '    if mode == "value":',
            '        if _char < "0" or _char > "9":',
            '            raise ValueError("Copy Character Value accepts only digits 0-9")',
            '        return ord(_char) - ord("0")',
            '    if mode == "ascii":',
            "        return ord(_char)",
            '    raise ValueError(f"Unsupported text->numeric mode: {mode}")',
            "",
        ],
        "_format_int_text": [
            "def _format_int_text(value, width, suppress_zero, signed=True):",
            "    if suppress_zero:",
            "        return str(value)",
            "    if not signed:",
            '        return f"{value:0{width}X}"',
            "    if value < 0:",
            '        return f"-{abs(value):0{width}d}"',
            '    return f"{value:0{width}d}"',
            "",
        ],
        "_render_text_from_numeric": [
            "def _render_text_from_numeric(",
            "    value,",
            "    *,",
            "    source_type=None,",
            "    suppress_zero=True,",
            "    pad=None,",
            "    exponential=False,",
            "):",
            '    if source_type == "REAL" or isinstance(value, float):',
            "        numeric = float(value)",
            "        if not math.isfinite(numeric):",
            '            raise ValueError("REAL source is not finite")',
            '        return f"{numeric:.7E}" if exponential else f"{numeric:.7f}"',
            "",
            "    number = int(value)",
            "    effective_suppress_zero = suppress_zero if pad is None else False",
            "    signed_width = max(pad - 1, 0) if pad is not None and number < 0 else pad",
            "",
            '    if source_type == "WORD":',
            "        width = 4 if pad is None else pad",
            "        return _format_int_text(number & 0xFFFF, width, effective_suppress_zero, False)",
            '    if source_type == "DINT":',
            "        width = 10 if signed_width is None else signed_width",
            "        return _format_int_text(number, width, effective_suppress_zero)",
            '    if source_type == "INT":',
            "        width = 5 if signed_width is None else signed_width",
            "        return _format_int_text(number, width, effective_suppress_zero)",
            "",
            "    if pad is None:",
            '        return str(number) if suppress_zero else f"{number:05d}"',
            "    width = 5 if signed_width is None else signed_width",
            "    return _format_int_text(number, width, False)",
            "",
        ],
        "_termination_char": [
            "def _termination_char(termination_code):",
            "    if termination_code is None:",
            '        return ""',
            "    if isinstance(termination_code, str):",
            "        if len(termination_code) != 1:",
            '            raise ValueError("termination_code must be one character or int ASCII code")',
            "        return _as_single_ascii_char(termination_code)",
            "    if not isinstance(termination_code, int):",
            '        raise TypeError("termination_code must be int, str, or None")',
            "    return _ascii_char_from_code(termination_code)",
            "",
        ],
        "_parse_pack_text_value": [
            "def _parse_pack_text_value(text, dest_type):",
            '    if text == "":',
            '        raise ValueError("empty text cannot be parsed")',
            '    if dest_type in {"INT", "DINT"}:',
            '        if not re.fullmatch(r"[+-]?\\\\d+", text):',
            '            raise ValueError("integer parse failed")',
            "        parsed = int(text, 10)",
            '        if dest_type == "INT" and (parsed < -32768 or parsed > 32767):',
            '            raise ValueError("integer out of INT range")',
            '        if dest_type == "DINT" and (parsed < -2147483648 or parsed > 2147483647):',
            '            raise ValueError("integer out of DINT range")',
            "        return parsed",
            '    if dest_type == "WORD":',
            '        if not re.fullmatch(r"[0-9A-Fa-f]+", text):',
            '            raise ValueError("hex parse failed")',
            "        parsed = int(text, 16)",
            "        if parsed < 0 or parsed > 0xFFFF:",
            '            raise ValueError("hex out of WORD range")',
            "        return parsed",
            '    if dest_type == "REAL":',
            "        parsed = float(text)",
            "        if not math.isfinite(parsed):",
            '            raise ValueError("REAL parse produced non-finite value")',
            '        struct.pack("<f", parsed)',
            "        return parsed",
            '    raise TypeError(f"Unsupported pack_text destination type: {dest_type}")',
            "",
        ],
        "_store_copy_value_to_type": [
            "def _store_copy_value_to_type(value, dest_type):",
            "    if isinstance(value, float) and not math.isfinite(value):",
            "        value = 0",
            '    if dest_type == "INT":',
            f"        return max({_INT_MIN}, min({_INT_MAX}, int(value)))",
            '    if dest_type == "DINT":',
            f"        return max({_DINT_MIN}, min({_DINT_MAX}, int(value)))",
            '    if dest_type == "WORD":',
            "        return int(value) & 0xFFFF",
            '    if dest_type == "REAL":',
            "        return float(value)",
            '    if dest_type == "BOOL":',
            "        return bool(value)",
            '    if dest_type == "CHAR":',
            "        if not isinstance(value, str):",
            '            raise ValueError("CHAR value must be a string")',
            '        if value == "":',
            "            return value",
            "        if len(value) != 1 or ord(value) > 127:",
            '            raise ValueError("CHAR value must be blank or one ASCII character")',
            "        return value",
            "    return value",
            "",
        ],
    }

    needed = set(ctx.used_helpers)
    if "_store_numeric_text_digit" in needed:
        needed.add("_as_single_ascii_char")
    if "_termination_char" in needed:
        needed.update({"_as_single_ascii_char", "_ascii_char_from_code"})
    if "_render_text_from_numeric" in needed:
        needed.add("_format_int_text")

    lines: list[str] = []
    for helper in _HELPER_ORDER:
        if helper in needed:
            lines.extend(helper_defs[helper])
    return lines


def _render_indirect_helpers(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for binding in sorted(
        (ctx.block_bindings[bid] for bid in ctx.used_indirect_blocks),
        key=lambda b: ctx.index_helper_name(b.block_id),
    ):
        helper_name = ctx.index_helper_name(binding.block_id)
        lines.append(f"def {helper_name}(addr):")
        lines.append(f"    if addr < {binding.start} or addr > {binding.end}:")
        lines.append(
            f'        raise IndexError(f"Address {{addr}} out of range'
            f" for {binding.logical_name} ({binding.start}-{binding.end})"
            '")'
        )
        if binding.valid_addresses is not None:
            lines.append(f"    if addr not in {binding.valid_addresses!r}:")
            lines.append(
                f'        raise IndexError(f"Address {{addr}} out of range'
                f" for {binding.logical_name} ({binding.start}-{binding.end})"
                '")'
            )
        lines.append(f"    return int(addr) - {binding.start}")
        lines.append("")
    return lines


def _render_embedded_functions(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for symbol in sorted(ctx.function_sources):
        src = ctx.function_sources[symbol].rstrip()
        lines.append(src)
        fn_name = _first_defined_name(src)
        if fn_name is not None and fn_name != symbol:
            lines.append(f"{symbol} = {fn_name}")
        lines.append("")
    return lines


def _render_declarations(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for tag_name in sorted(ctx.scalar_tags):
        tag = ctx.scalar_tags[tag_name]
        symbol = ctx.symbol_table[tag_name]
        lines.append(f"{symbol} = {tag.default!r}")

    for binding in sorted(
        ctx.block_bindings.values(),
        key=lambda b: (ctx.block_symbols[b.block_id], b.block_id),
    ):
        symbol = ctx.block_symbols[binding.block_id]
        size = binding.end - binding.start + 1
        default = _TYPE_DEFAULTS[binding.tag_type]
        lines.append(f"{symbol} = [{default!r}] * {size}")

    lines.extend(["_mem = {}", "_prev = {}", ""])
    return lines


def _render_step_function(ctx: CodegenContext, main_body: list[str]) -> list[str]:
    all_symbols: set[str] = set()
    scalar_symbols = sorted(ctx.symbol_table[n] for n in ctx.scalar_tags)
    block_symbols = sorted(ctx.block_symbols.values())
    all_symbols.update(scalar_symbols)
    all_symbols.update(block_symbols)
    all_symbols.update({"_mem", "_prev"})

    lines: list[str] = ["def _kernel_step(tags, blocks, memory, prev, dt):"]
    globals_line = _global_line(sorted(all_symbols), indent=4)
    if globals_line is not None:
        lines.append(globals_line)

    for tag_name in sorted(ctx.scalar_tags):
        symbol = ctx.symbol_table[tag_name]
        lines.append(f"    {symbol} = tags[{tag_name!r}]")

    for binding in sorted(
        ctx.block_bindings.values(),
        key=lambda b: (ctx.block_symbols[b.block_id], b.block_id),
    ):
        symbol = ctx.block_symbols[binding.block_id]
        lines.append(f"    {symbol} = blocks[{symbol!r}]")

    lines.extend(
        [
            "    _mem = memory",
            "    _prev = prev",
            '    memory["_dt"] = dt',
        ]
    )

    if main_body:
        lines.extend(main_body)
    else:
        lines.append("    pass")

    for tag_name in sorted(ctx.scalar_tags):
        symbol = ctx.symbol_table[tag_name]
        lines.append(f"    tags[{tag_name!r}] = {symbol}")

    lines.append("")
    return lines
