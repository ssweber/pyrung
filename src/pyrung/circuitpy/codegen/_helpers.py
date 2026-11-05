"""The helper functions generated code calls, written once for every renderer.

``code.py`` (helpers inline), ``pyrung_rt.py`` (every helper) and the in-process
kernel all emit these same definitions; each picks the ones it needs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _DINT_MIN,
    _HELPER_ORDER,
    _INT_MAX,
    _INT_MIN,
)
from pyrung.core.storage import REAL_MAX

_HELPER_DEFS: dict[str, list[str]] = {
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
        "    if code < 0 or code > 255:",
        '        raise ValueError("character code out of range")',
        "    return chr(code)",
        "",
    ],
    "_as_single_ascii_char": [
        "def _as_single_ascii_char(value):",
        "    if not isinstance(value, str):",
        '        raise ValueError("CHAR value must be a string")',
        '    if value == "":',
        '        return "\\x00"',
        "    if len(value) != 1 or ord(value) > 255:",
        '        raise ValueError("CHAR value must be blank or one 8-bit character")',
        "    return value",
        "",
    ],
    "_text_from_source_value": [
        "def _text_from_source_value(value):",
        "    if isinstance(value, str):",
        '        return value if value != "" else "\\x00"',
        '    raise ValueError("text conversion source must resolve to str")',
        "",
    ],
    "_store_numeric_text_digit": [
        "def _store_numeric_text_digit(char, mode):",
        "    _char = _as_single_ascii_char(char)",
        '    if mode == "value":',
        '        if _char == "\\x00":',
        "            return 0",
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
        '            return "nan" if numeric != numeric else ("inf" if numeric > 0 else "-inf")',
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
        '        _m = re.match(r"([+-]?\\d+)(?:\\.\\d*)?$", text)',
        "        if _m is None:",
        '            raise ValueError("integer parse failed")',
        "        return int(_m.group(1), 10)",
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
        '    if dest_type == "REAL":',
        "        return _store_real(value)",
        "    if isinstance(value, float) and not math.isfinite(value):",
        "        # A clamp pins +/-Inf at its bound; NaN, and anything that wraps, is 0.",
        '        if value == value and dest_type == "INT":',
        f"            return {_INT_MAX} if value > 0 else {_INT_MIN}",
        '        if value == value and dest_type == "DINT":',
        f"            return {_DINT_MAX} if value > 0 else {_DINT_MIN}",
        "        value = 0",
        '    if dest_type == "INT":',
        f"        return max({_INT_MIN}, min({_INT_MAX}, int(value)))",
        '    if dest_type == "DINT":',
        f"        return max({_DINT_MIN}, min({_DINT_MAX}, int(value)))",
        '    if dest_type == "WORD":',
        "        return int(value) & 0xFFFF",
        '    if dest_type == "BOOL":',
        "        return bool(value)",
        '    if dest_type == "CHAR":',
        "        if not isinstance(value, str):",
        "            return value",
        '        if value == "":',
        '            return "\\x00"',
        "        if len(value) != 1 or ord(value) > 255:",
        '            raise ValueError("CHAR value must be blank or one 8-bit character")',
        "        return value",
        "    return value",
        "",
    ],
    "_store_int": [
        "def _store_int(value):",
        "    if type(value) is int:",
        f"        return {_INT_MAX} if value > {_INT_MAX} else ({_INT_MIN} if value < {_INT_MIN} else value)",
        "    if type(value) is float and not math.isfinite(value):",
        "        if value != value:",
        "            return 0",
        f"        return {_INT_MAX} if value > 0 else {_INT_MIN}",
        f"    return max({_INT_MIN}, min({_INT_MAX}, int(value)))",
        "",
    ],
    "_store_dint": [
        "def _store_dint(value):",
        "    if type(value) is int:",
        f"        return {_DINT_MAX} if value > {_DINT_MAX} else ({_DINT_MIN} if value < {_DINT_MIN} else value)",
        "    if type(value) is float and not math.isfinite(value):",
        "        if value != value:",
        "            return 0",
        f"        return {_DINT_MAX} if value > 0 else {_DINT_MIN}",
        f"    return max({_DINT_MIN}, min({_DINT_MAX}, int(value)))",
        "",
    ],
    "_store_word": [
        "def _store_word(value):",
        "    if type(value) is int:",
        "        return value & 0xFFFF",
        "    if type(value) is float and not math.isfinite(value):",
        "        return 0",
        "    return int(value) & 0xFFFF",
        "",
    ],
    "_store_real": [
        "def _store_real(value):",
        "    if type(value) is not float:",
        "        try:",
        "            value = float(value)",
        "        except OverflowError:",
        '            return float("inf") if value > 0 else float("-inf")',
        f"    if value > {REAL_MAX!r}:",
        '        return float("inf")',
        f"    if value < {-REAL_MAX!r}:",
        '        return float("-inf")',
        "    return value",
        "",
    ],
    "_math_func": [
        "class _MathDomainError(ValueError):",
        "    pass",
        "",
        "",
        "def _math_func(func, value):",
        "    try:",
        "        return func(value)",
        "    except ValueError:",
        "        if func in (math.sqrt, math.log, math.log10, math.asin, math.acos):",
        "            raise _MathDomainError()",
        '        return float("nan")',
        "",
    ],
    "_bit_int": [
        "def _bit_int(value):",
        "    if value != value:",
        '        raise OverflowError("cannot convert float NaN to integer")',
        "    return int(value)",
        "",
    ],
    "_shift_count": [
        "def _shift_count(value):",
        "    value = _bit_int(value)",
        "    if value < 0:",
        '        raise OverflowError("negative shift count")',
        "    return value",
        "",
    ],
    # A randint() site whose bounds are read: equal ones (46, SC46) or Inf or NaN
    # (43, SC43) make the site 0, and in a calc append the flag to *flags*, which
    # the calc sets after the formula (RandIntExpr.evaluate).  Outside calc
    # *flags* is None.  The kernel reads the scan's draw; the board draws.
    "_rand_fail": [
        "def _rand_fail(flags, code):",
        "    if flags is not None:",
        "        flags.append(code)",
        "    return 0",
        "",
    ],
    "_rand_term": [
        "def _rand_term(flags, draw, lo, hi):",
        "    try:",
        "        lo = _bit_int(lo)",
        "        hi = _bit_int(hi)",
        "    except OverflowError:",
        "        return _rand_fail(flags, 43)",
        "    if lo == hi:",
        "        return _rand_fail(flags, 46)",
        "    return draw",
        "",
    ],
    "_rand_draw": [
        "def _rand_draw(flags, lo, hi):",
        "    try:",
        "        lo = _bit_int(lo)",
        "        hi = _bit_int(hi)",
        "    except OverflowError:",
        "        return _rand_fail(flags, 43)",
        "    if lo == hi:",
        "        return _rand_fail(flags, 46)",
        "    return random.randint(min(lo, hi), max(lo, hi))",
        "",
    ],
    # 0 ** 0, 0 ** -1 and a MOD by 0: a CLICK's Math gives 0 with no flag
    # (pyrung.core.expression.QuietMathError).
    "_QuietMathError": [
        "class _QuietMathError(ArithmeticError):",
        "    pass",
        "",
    ],
    "_mod": [
        "def _mod(a, b):",
        "    if b == 0:",
        "        raise _QuietMathError()",
        "    r = abs(a) % abs(b)",
        "    return -r if a < 0 else r",
        "",
    ],
    # A calc step past 2 ** 53 as the double a CLICK's Math holds (to_double).
    "_dbl": [
        "def _dbl(value):",
        f"    if type(value) is int and not -{1 << 53} <= value <= {1 << 53}:",
        "        try:",
        "            return float(value)",
        "        except OverflowError:",
        '            return float("inf") if value > 0 else float("-inf")',
        "    return value",
        "",
    ],
    # ABS of an Int or Dint tag at the bottom of its range has no value there:
    # OverflowError, so the calc stores 0 with SC43 (ABS_OF_TYPE).
    "_abs_int": [
        "def _abs_int(value, floor):",
        "    if value == floor:",
        '        raise OverflowError("ABS of the bottom of the range")',
        "    return abs(value)",
        "",
    ],
    "_permissive_math": [
        "def _permissive_math(func, value):",
        "    try:",
        "        return _math_func(func, value)",
        "    except (ValueError, OverflowError):",
        "        return 0",
        "",
    ],
    "_pow": [
        "def _pow(base, exp):",
        "    if base == 0 and exp <= 0:",
        "        raise _QuietMathError()",
        "    try:",
        "        result = base ** exp",
        "    except OverflowError:",
        "        if type(base) is not float and type(exp) is not float:",
        "            raise",
        "        if type(exp) is int:",
        "            odd = exp % 2 == 1",
        "        else:",
        "            odd = exp == int(exp) and int(exp) % 2 == 1",
        '        return float("-inf") if base < 0 and odd else float("inf")',
        "    except ValueError:",
        "        raise _MathDomainError()",
        "    # A negative base to a fractional power is complex on CPython (a",
        "    # board built without complex numbers may raise ValueError instead).",
        "    if type(result) is not float and type(result) is not int:",
        "        raise _MathDomainError()",
        "    return result",
        "",
    ],
    "_permissive_op": [
        "def _permissive_op(op, a, b):",
        "    try:",
        '        if op == "+":',
        "            return a + b",
        '        if op == "-":',
        "            return a - b",
        '        if op == "*":',
        "            return a * b",
        '        if op == "/":',
        "            return a / b",
        '        if op == "//":',
        "            return a // b",
        '        if op == "%":',
        "            return _mod(a, b)",
        '        if op == "**":',
        "            return _pow(a, b)",
        "        a = _bit_int(a)",
        "        b = _bit_int(b)",
        '        if op == "&":',
        "            return a & b",
        '        if op == "|":',
        "            return a | b",
        '        if op == "^":',
        "            return a ^ b",
        '        if op == "~":',
        "            return ~a",
        '        if op == "<<" or op == "lsh":',
        "            return a << b",
        '        if op == ">>" or op == "rsh":',
        "            return a >> b",
        '        if op == "lro":',
        "            return (((a & 0xFFFF) << (b % 16)) | ((a & 0xFFFF) >> (16 - (b % 16)))) & 0xFFFF",
        '        if op == "rro":',
        "            return (((a & 0xFFFF) >> (b % 16)) | ((a & 0xFFFF) << (16 - (b % 16)))) & 0xFFFF",
        "    except (ArithmeticError, ValueError):",
        "        return 0",
        "    raise ValueError(op)",
        "",
    ],
    # A read with no element there gets the type's zero: ``_cond_index_*`` (a
    # rung condition) and ``_read_index_*`` (an instruction's source past its
    # bank) give -1 for such an address.
    "_cond_elem": [
        "def _cond_elem(seq, i, default):",
        "    if i < 0:",
        "        return default",
        "    return seq[i]",
        "",
    ],
    "_cond_tag_elem": [
        "def _cond_tag_elem(tags, names, i, default):",
        "    if i < 0:",
        "        return default",
        "    return tags.get(names[i], default)",
        "",
    ],
    "_store_bool": [
        "def _store_bool(value):",
        "    if type(value) is float and not math.isfinite(value):",
        "        return False",
        "    return bool(value)",
        "",
    ],
}


def _with_helper_deps(used: Iterable[str]) -> set[str]:
    """*used* plus the helpers they call."""
    needed = set(used)
    if "_store_numeric_text_digit" in needed:
        needed.add("_as_single_ascii_char")
    if "_termination_char" in needed:
        needed.update({"_as_single_ascii_char", "_ascii_char_from_code"})
    if "_render_text_from_numeric" in needed:
        needed.add("_format_int_text")
    if "_store_copy_value_to_type" in needed:
        needed.add("_store_real")
    if "_permissive_op" in needed:
        needed.update({"_bit_int", "_math_func", "_pow", "_mod"})
    if needed & {"_rand_term", "_rand_draw"}:
        needed.update({"_bit_int", "_rand_fail"})
    if needed & {"_mod", "_pow"}:
        needed.add("_QuietMathError")
    if "_pow" in needed:
        needed.add("_math_func")
    return needed


def _render_index_helpers(
    ctx: Any, binding: Any, fault_symbol: str | None, fault_global: str | None
) -> list[str]:
    """The helpers that turn an address in *binding*'s block into an offset.

    ``_resolve_index_*`` (an instruction's destination): below the block or in a
    gap it sets the address-error flag and raises.  Past the block it raises with
    no flag, and the instruction's guard drops the write.

    ``_read_index_*`` (an instruction's source): as above, but -1 past the block,
    which ``_cond_elem`` reads as the type's zero.

    ``_window_addrs_*`` (the bounds of a dynamic window): the flag and a raise when
    a bound is outside the block, past it too; otherwise the window's addresses,
    without the gaps of a sparse block.

    ``_cond_index_*`` (a rung condition): -1 anywhere outside the block, no flag.
    """
    helper_name = ctx.index_helper_name(binding.block_id)
    message = (
        f'f"Address {{addr}} out of range for {binding.logical_name}'
        f' ({binding.start}-{binding.end})"'
    )
    lines = [f"def {helper_name}(addr):"]
    if fault_global is not None:
        lines.append(f"    global {fault_global}")
    fault_line = [f"        {fault_symbol} = True"] if fault_symbol is not None else []
    lines.append(f"    if addr < {binding.start}:")
    lines.extend(fault_line)
    lines.append(f"        raise IndexError({message})")
    lines.append(f"    if addr > {binding.end}:")
    lines.append(f"        raise IndexError({message})")
    if binding.valid_addresses is not None:
        lines.append(f"    if addr not in {binding.valid_addresses!r}:")
        lines.extend(fault_line)
        lines.append(f"        raise IndexError({message})")
    lines.append(f"    return int(addr) - {binding.start}")
    lines.append("")
    if binding.block_id in ctx.used_read_index_blocks:
        lines.append(f"def {ctx.read_index_helper_name(binding.block_id)}(addr):")
        lines.append(f"    if addr > {binding.end}:")
        lines.append("        return -1")
        lines.append(f"    return {helper_name}(addr)")
        lines.append("")
    if binding.block_id in ctx.used_window_addrs_blocks:
        lines.append(f"def {ctx.window_addrs_helper_name(binding.block_id)}(lo, hi):")
        if fault_global is not None:
            lines.append(f"    global {fault_global}")
        lines.append(f"    if lo < {binding.start} or hi > {binding.end}:")
        lines.extend(fault_line)
        lines.append(
            f'        raise IndexError(f"Window {{lo}}..{{hi}} out of range for '
            f'{binding.logical_name} ({binding.start}-{binding.end})")'
        )
        if binding.valid_addresses is None:
            lines.append("    return range(lo, hi + 1)")
        else:
            lines.append(f"    valid = {binding.valid_addresses!r}")
            lines.append("    return [addr for addr in range(lo, hi + 1) if addr in valid]")
        lines.append("")
    if binding.block_id in ctx.used_condition_index_blocks:
        lines.append(f"def {ctx.condition_index_helper_name(binding.block_id)}(addr):")
        lines.append(f"    if addr < {binding.start} or addr > {binding.end}:")
        lines.append("        return -1")
        if binding.valid_addresses is not None:
            lines.append(f"    if addr not in {binding.valid_addresses!r}:")
            lines.append("        return -1")
        lines.append(f"    return int(addr) - {binding.start}")
        lines.append("")
    return lines


def _render_helper_defs(needed: Iterable[str]) -> list[str]:
    """The definitions of the helpers in *needed*, in ``_HELPER_ORDER``."""
    needed = set(needed)
    lines: list[str] = []
    for name in _HELPER_ORDER:
        if name in needed:
            lines.extend(_HELPER_DEFS[name])
    return lines
