"""The helper functions generated code calls, written once for every renderer.

``code.py`` (helpers inline), ``pyrung_rt.py`` (every helper) and the in-process
kernel all emit these same definitions; each picks the ones it needs.
"""

from __future__ import annotations

from collections.abc import Iterable

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
        '        return "\\x00"',
        "    if len(value) != 1 or ord(value) > 127:",
        '        raise ValueError("CHAR value must be blank or one ASCII character")',
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
        '        if not re.fullmatch(r"[+-]?\\d+", text):',
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
        "        return _store_real(value)",
        '    if dest_type == "BOOL":',
        "        return bool(value)",
        '    if dest_type == "CHAR":',
        "        if not isinstance(value, str):",
        '            raise ValueError("CHAR value must be a string")',
        '        if value == "":',
        '            return "\\x00"',
        "        if len(value) != 1 or ord(value) > 127:",
        '            raise ValueError("CHAR value must be blank or one ASCII character")',
        "        return value",
        "    return value",
        "",
    ],
    "_store_int": [
        "def _store_int(value):",
        "    if type(value) is int:",
        f"        return {_INT_MAX} if value > {_INT_MAX} else ({_INT_MIN} if value < {_INT_MIN} else value)",
        "    if type(value) is float and not math.isfinite(value):",
        "        return 0",
        f"    return max({_INT_MIN}, min({_INT_MAX}, int(value)))",
        "",
    ],
    "_store_dint": [
        "def _store_dint(value):",
        "    if type(value) is int:",
        f"        return {_DINT_MAX} if value > {_DINT_MAX} else ({_DINT_MIN} if value < {_DINT_MIN} else value)",
        "    if type(value) is float and not math.isfinite(value):",
        "        return 0",
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
        "    if type(value) is float:",
        "        if not math.isfinite(value):",
        "            return 0.0",
        "    else:",
        "        try:",
        "            value = float(value)",
        "        except OverflowError:",
        f"            return {REAL_MAX!r} if value > 0 else {-REAL_MAX!r}",
        f"    if value > {REAL_MAX!r}:",
        f"        return {REAL_MAX!r}",
        f"    if value < {-REAL_MAX!r}:",
        f"        return {-REAL_MAX!r}",
        "    return value",
        "",
    ],
    "_math_func": [
        "class _MathDomainError(ValueError):",
        "    pass",
        "",
        "",
        "def _math_func(func, value):",
        "    if type(value) is float and not math.isfinite(value):",
        "        return value",
        "    try:",
        "        return func(value)",
        "    except ValueError:",
        "        raise _MathDomainError()",
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
    return needed


def _render_helper_defs(needed: Iterable[str]) -> list[str]:
    """The definitions of the helpers in *needed*, in ``_HELPER_ORDER``."""
    needed = set(needed)
    lines: list[str] = []
    for name in _HELPER_ORDER:
        if name in needed:
            lines.extend(_HELPER_DEFS[name])
    return lines
