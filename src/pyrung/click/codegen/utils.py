from __future__ import annotations

import ast
import keyword
import re
import warnings
from typing import TYPE_CHECKING

from pyrung.click.codegen._syntax import (
    _csv_string_value,
    _pythonize_csv_strings,
    _string_literal,
    _validate_expression,
)
from pyrung.click.codegen.constants import (
    _COPY_CONVERTERS,
    _FUNC_RE,
    _OPERAND_PREFIXES,
    _OPERAND_RE,
    _RANGE_RE,
    _STRING_KWARGS,
    _sub_operands,
)
from pyrung.click.codegen.formula import (
    _CLICK_FUNC_TO_PYTHON,
    _RANGE_AGG_TO_METHOD,
    _TIER_OF,
    _WORD_OPS,
    Address,
    BinaryOp,
    Call,
    Formula,
    Negate,
    Number,
    Pi,
    Pointer,
    RangeCall,
    parse_with_unconfirmed_pairs,
)
from pyrung.click.codegen.models import _OperandCollection
from pyrung.click.system_mappings import SYSTEM_OPERAND_PATHS

if TYPE_CHECKING:
    from pyrung.click.codegen.models import _SubroutineInfo
    from pyrung.click.tag_map import TagMap

# ---------------------------------------------------------------------------
# Click-native expression → Python operator conversion
# ---------------------------------------------------------------------------

# Python builtins in _CLICK_FUNC_TO_PYTHON.  They need no import.
_BUILTIN_EXPR_FUNCS = frozenset({"abs"})

# Regex matching Click function calls like SQRT(, LSH ( , etc.
_CLICK_FUNC_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _CLICK_FUNC_TO_PYTHON) + r")\s*\("
)

# Regex matching standalone PI (not followed by digits, which would be a prefix match)
_CLICK_PI_RE = re.compile(r"\bPI\b")

# Click hex literal: hex digits followed by lowercase 'h' (e.g. 0000h, FFFFh).
# Safe on raw Click text because operand prefixes use uppercase H (DH, TH).
_CLICK_HEX_RE = re.compile(r"\b([0-9A-Fa-f]+)h\b")
_MODBUS_ADDRESS_RE = re.compile(r"ModbusAddress\(([^()]*)\)")

# CLICK's operators that Python spells differently.  Python would read ^ as XOR, so the
# importer refuses these operators in any operand that is not a formula.
_CLICK_ONLY_OPERATOR_RE = re.compile(r"\^|\b(?:" + "|".join(sorted(_WORD_OPS)) + r")\b")


_MODBUS_984_RE = re.compile(r"\d+")
_MODBUS_HEX_RE = re.compile(r"[0-9A-Fa-f]+[hH]")
_MODBUS_984_COIL_MAX = 65536
_TO_TEXT_RE = re.compile(r"to_text\(([^()]*)\)")
_TO_TEXT_FLAGS = {"0": "False", "1": "True"}
_TERMINATION_CODE_RE = re.compile(r"\$[0-9A-Fa-f]{1,2}")
# CLICK's ASCII-code text constant: ``$0D`` is one CR, ``$00`` the TXT blank.
_TEXT_CODE_RE = re.compile(r"\$([0-9A-Fa-f]{1,2})")


def _text_code_value(text: str) -> str | None:
    """Return the character that a CLICK ``$hh`` text constant stands for, or None."""
    match = _TEXT_CODE_RE.fullmatch(text.strip())
    if match is None:
        return None
    return chr(int(match.group(1), 16))


_REGISTER_TYPE_MEMBERS = {
    "coil": "COIL",
    "discrete_input": "DISCRETE_INPUT",
    "holding": "HOLDING",
    "input": "INPUT",
}

# Regex matching a range aggregate with colon-range: SUM ( DS1 : DS10 ), MAX(DS1:DS10), ...
# Groups: 1 = function, 2/3 = first operand prefix/number, 4/5 = last operand prefix/number.
_RANGE_AGG_RE = re.compile(
    r"\b(" + "|".join(_RANGE_AGG_TO_METHOD) + r")\s*\(\s*([A-Z]+)(\d+)\s*:\s*([A-Z]+)(\d+)\s*\)"
)

# Pointer/indirect addressing: DH[DS134] means "DH at index stored in DS134"
_POINTER_RE = re.compile(r"(CTD|CT|TD|TXT|SC|SD|DS|DD|DH|DF|X|Y|C|T)\[(.+?)\]")

# Map Click memory-type prefix → pyrung block variable for pointer addressing
_PREFIX_TO_BLOCK: dict[str, str] = {p: bv for p, _, bv in _OPERAND_PREFIXES}

# The expression function names that generated code imports from pyrung
_EXPR_FUNC_IMPORT_NAMES = (frozenset(_CLICK_FUNC_TO_PYTHON.values()) - _BUILTIN_EXPR_FUNCS) | {
    "PI",
    "LiteralExpr",
}


# ---------------------------------------------------------------------------
# Token Parsing Helpers
# ---------------------------------------------------------------------------


def _parse_operand_prefix(operand: str) -> tuple[str, str, str, int] | None:
    """Parse an operand like X001 → (prefix, tag_type, block_var, index)."""
    for prefix, tag_type, block_var in _OPERAND_PREFIXES:
        if operand.startswith(prefix):
            num_str = operand[len(prefix) :]
            if num_str.isdigit():
                return prefix, tag_type, block_var, int(num_str)
    return None


def _strip_quoted_strings(text: str) -> str:
    """Remove quoted strings from text to avoid false operand matches."""
    return re.sub(r'"[^"]*"', "", text)


def _out_variant_timer_pairs(kwargs: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Return ``(side, done, acc)`` for each phase Timer of a debounce/flasher/timed token.

    The token names only the phase's TD, as CLICK's coil does.  Its Done is the T
    of the same number.
    """
    values = dict(kwargs)
    pairs: list[tuple[str, str, str]] = []
    for side in ("on", "off"):
        acc = values.get(f"{side}_acc")
        if acc is not None and acc.startswith("TD") and acc[2:].isdigit():
            pairs.append((side, f"T{acc[2:]}", acc))
    return pairs


def _parse_af_args(args_str: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse AF token arguments into positional args and keyword args.

    Handles nested parens and brackets for things like:
        out(Y001)
        math(DS1+DS2,DS3,mode=int)
        event_drum(outputs=[C1,C2],events=[X001,X002],pattern=[[1,0],[0,1]],...)
    """
    args: list[str] = []
    kwargs: list[tuple[str, str]] = []

    depth = 0
    current = ""
    quote = ""

    for ch in args_str:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
            continue
        if ch in ('"', "'"):
            quote = ch
            current += ch
            continue
        if ch in ("(", "["):
            depth += 1
            current += ch
        elif ch in (")", "]"):
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            _classify_arg(current.strip(), args, kwargs)
            current = ""
        else:
            current += ch

    if current.strip():
        _classify_arg(current.strip(), args, kwargs)

    return args, kwargs


def _classify_arg(
    token: str,
    args: list[str],
    kwargs: list[tuple[str, str]],
) -> None:
    """Classify a token as positional or keyword arg."""
    # Check for key=value (but not == which is a comparison)
    eq_idx = token.find("=")
    if (
        eq_idx > 0
        and token[eq_idx - 1] not in ("!", "<", ">")
        and (eq_idx + 1 >= len(token) or token[eq_idx + 1] != "=")
    ):
        key = token[:eq_idx]
        value = token[eq_idx + 1 :]
        # Verify key looks like an identifier
        if key.isidentifier():
            kwargs.append((key, value))
            return
    args.append(token)


def _render_to_text(args_str: str) -> str:
    """Render CSV ``to_text(suppress_zero=1,exponential=0,termination_code=$0D)`` as Python.

    CLICK writes ``termination_code=none`` when the option is off.
    """
    args, kwargs = _parse_af_args(args_str)
    fields = dict(kwargs)
    rendered: list[str] = []
    valid = not args and set(fields) <= {"suppress_zero", "exponential", "termination_code"}
    for key in ("suppress_zero", "exponential"):
        if key in fields:
            flag = _TO_TEXT_FLAGS.get(fields[key])
            valid = valid and flag is not None
            rendered.append(f"{key}={flag}")
    code = fields.get("termination_code", "none")
    if code != "none":
        valid = valid and _TERMINATION_CODE_RE.fullmatch(code) is not None
        rendered.append(f'termination_code="{code}"')
    if not valid:
        raise ValueError(f"Unsupported CSV operand expression: 'to_text({args_str})'")
    return f"to_text({', '.join(rendered)})"


def _check_to_text_source(source: str, convert: str) -> None:
    """Refuse ``to_text(exponential=1)`` from an integer source, as export does.

    CLICK offers Exponential Numbering only for a Real source.  A DF pointer and a Real
    constant count as a Real source.
    """
    to_text = _TO_TEXT_RE.fullmatch(convert)
    if to_text is None or dict(_parse_af_args(to_text.group(1))[1]).get("exponential") != "1":
        return
    if source.startswith("DF"):
        return
    try:
        float(source)
    except ValueError:
        pass
    else:
        if not source.lstrip("+-").isdigit():
            return
    raise ValueError(
        "to_text(exponential=True) needs a Real source, such as a DF or a Real constant. "
        f"The source here is {source}. Remove exponential=True, or use suppress_zero for an integer source."
    )


def _sub_operand_kwarg(
    key: str,
    value: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Substitute a kwarg value, quoting string enum values."""
    if key in _STRING_KWARGS:
        return _string_literal(value)
    # oneshot=1 → oneshot=True
    if key == "oneshot" and value == "1":
        return "True"
    # word_swap=1 → word_swap=True, word_swap=0 → word_swap=False
    if key == "word_swap":
        return "True" if value == "1" else "False"
    # Conversion syntax is source too; validate it before passing it through.
    if key == "convert":
        to_text = _TO_TEXT_RE.fullmatch(value)
        if to_text:
            return _render_to_text(to_text.group(1))
        _validate_operand(value)
        return _pythonize_csv_strings(value)
    return _sub_operand(value, collection, nicknames, structured_map)


def _parse_modbus_address(args_str: str) -> tuple[str, str | None]:
    """Parse CSV ``ModbusAddress(...)`` args into (Python address, ``RegisterType`` or None).

    CLICK shows a remote address either as MODBUS 984 or as hex with its register type.
    MODBUS 984 looks like ``400001``, and coils are ``1``..``65536``.  The hex form looks
    like ``address=50Eh,register_type=holding``.
    """
    args, kwargs = _parse_af_args(args_str)
    fields = dict(kwargs)
    address = fields.get("address", "")
    register_type = fields.get("register_type")
    if not args and set(fields) <= {"address", "register_type"}:
        if _MODBUS_HEX_RE.fullmatch(address):
            member = _REGISTER_TYPE_MEMBERS.get(register_type or "holding")
            if member is not None:
                return f'"{address}"', member
        elif _MODBUS_984_RE.fullmatch(address) and register_type is None:
            number = int(address)
            if 1 <= number <= _MODBUS_984_COIL_MAX:
                return str(number - 1), "COIL"
            return str(number), None
    raise ValueError(f"Unsupported CSV operand expression: 'ModbusAddress({args_str})'")


def _render_modbus_address(args_str: str) -> str:
    address, member = _parse_modbus_address(args_str)
    if member is None:
        return f"ModbusAddress(address={address})"
    return f"ModbusAddress(address={address}, register_type=RegisterType.{member})"


def _validate_operand(text: str) -> ast.expr:
    """Validate Click syntax before substituting trusted generated references."""
    if _CLICK_ONLY_OPERATOR_RE.search(_strip_quoted_strings(text)):
        raise ValueError(f"Unsupported CSV operand expression: {text!r}")
    normalized = _pythonize_csv_strings(text)
    normalized = _CLICK_HEX_RE.sub(lambda m: f"0x{m.group(1)}", normalized)
    normalized = _RANGE_RE.sub(
        lambda m: f"_range({m.group(1)}{m.group(2)}, {m.group(3)}{m.group(4)})",
        normalized,
    )
    return _validate_expression(
        normalized,
        functions=_COPY_CONVERTERS
        | {
            "all",
            "any",
            "ModbusTcpTarget",
            "ModbusRtuTarget",
            "_range",
            "immediate",
            "rise",
            "fall",
        },
        constants={"none"} | _COPY_CONVERTERS,
        operand_pattern=_OPERAND_RE,
        blocks=set(_PREFIX_TO_BLOCK),
    )


def _sub_operand(
    text: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Substitute operand names with variable names in a text fragment.

    Handles plain operands, ranges, expressions, and nested constructs.
    """
    if not text:
        return text

    code_char = _text_code_value(text)
    if code_char is not None:
        # pyrung reads "" as the blank (NUL); keep the source free of \u0000.
        return _string_literal("" if code_char == "\x00" else code_char)
    modbus_address = _MODBUS_ADDRESS_RE.fullmatch(text)
    if modbus_address:
        return _render_modbus_address(modbus_address.group(1))
    try:
        formula, pairs = parse_with_unconfirmed_pairs(text)
    except ValueError:
        pass
    else:
        if isinstance(formula, (BinaryOp, Negate, Call, RangeCall, Pi)):
            return _render_parsed_formula(
                text, formula, pairs, collection, nicknames, structured_map
            )
    expression = _validate_operand(text)

    # System operands (SC/SD → system.* path)
    if text in SYSTEM_OPERAND_PATHS:
        return SYSTEM_OPERAND_PATHS[text]

    render = collection.semantic_operands.get(text)
    if render is not None:
        return render.expr

    # Check if entire text is a known operand
    if text in collection.tags:
        return collection.tags[text].var_name

    render = collection.semantic_ranges.get(text)
    if render is not None:
        return render.expr
    if text in collection.ranges:
        r = collection.ranges[text]
        return _render_inline_range(r.prefix, r.start, r.end)

    # Decode the CSV string grammar before rendering a Python literal.
    if text.startswith('"'):
        return _string_literal(_csv_string_value(text))

    # Check for numeric literal
    try:
        float(text)
        return text
    except ValueError:
        pass

    # Check for Click hex literal (e.g. 0000h, FFFFh → 0x0000, 0xFFFF)
    hex_match = _CLICK_HEX_RE.fullmatch(text)
    if hex_match:
        return f"0x{hex_match.group(1).upper()}"

    # Check for none
    if text == "none":
        return "None"

    # Check for function-call operands
    match = _FUNC_RE.match(text)
    if match and isinstance(expression, ast.Call):
        func_name = match.group(2)
        inner_args_str = match.group(3) or ""
        if func_name == "ModbusTcpTarget":
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            for k, v in kwargs:
                rendered.append(f"{k}={_sub_operand(v, collection, nicknames, structured_map)}")
            return f"ModbusTcpTarget({', '.join(rendered)})"
        if func_name == "ModbusRtuTarget":
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            for k, v in kwargs:
                rendered.append(f"{k}={_sub_operand(v, collection, nicknames, structured_map)}")
            return f"ModbusRtuTarget({', '.join(rendered)})"
        if func_name in {"all", "any"}:
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            return f"{func_name}({', '.join(rendered)})"

    # Check for list/array: [C1,C2,C3]
    if text.startswith("[") and text.endswith("]") and isinstance(expression, ast.List):
        inner = text[1:-1]
        if not inner:
            return "[]"
        items, _ = _parse_af_args(inner)
        rendered = [_sub_operand(item, collection, nicknames, structured_map) for item in items]
        return f"[{', '.join(rendered)}]"

    # Pointer/indirect addressing: DH[DS134] → dh[tag_var_name]
    ptr_match = _POINTER_RE.fullmatch(text)
    if ptr_match and isinstance(expression, ast.Subscript):
        prefix = ptr_match.group(1)
        block_var = _PREFIX_TO_BLOCK[prefix]
        collection.used_blocks.add(block_var)
        inner = _sub_operand(ptr_match.group(2), collection, nicknames, structured_map)
        return f"{block_var}[{inner}]"

    # Check for ranges like DS100..DS102
    range_match = _RANGE_RE.fullmatch(text)
    if range_match:
        prefix = range_match.group(1)
        start_num = int(range_match.group(2))
        end_num = int(range_match.group(4))
        return _render_range_expr(prefix, start_num, end_num, collection)

    # For text that is not a CLICK formula, such as an expression that names a range, replace
    # each operand in it.
    result = _CLICK_HEX_RE.sub(lambda m: f"0x{m.group(1).upper()}", _pythonize_csv_strings(text))
    result = _RANGE_RE.sub(lambda m: _sub_range(m, collection, nicknames), result)

    # Pointer/indirect addressing within expressions: DH[DS134] → dh[tag_var]
    def _sub_pointer(m: re.Match[str]) -> str:
        prefix = m.group(1)
        block_var = _PREFIX_TO_BLOCK[prefix]
        collection.used_blocks.add(block_var)
        inner = _sub_operand(m.group(2), collection, nicknames, structured_map)
        return f"{block_var}[{inner}]"

    result = _POINTER_RE.sub(_sub_pointer, result)

    def _sub_operand_token(op: str) -> str:
        if op in SYSTEM_OPERAND_PATHS:
            return SYSTEM_OPERAND_PATHS[op]
        render = collection.semantic_operands.get(op)
        if render is not None:
            return render.expr
        if op in collection.tags:
            return collection.tags[op].var_name
        return op

    return _sub_operands(result, _sub_operand_token)


# ---------------------------------------------------------------------------
# CLICK Math formulas
# ---------------------------------------------------------------------------

# CLICK's binary operators in Python, with the strength of each Python operator (a higher
# strength binds tighter).  A unary minus has strength 7, and a name or a call has 9.
_PYTHON_BINARY_OPS: dict[str, tuple[str, int]] = {
    "^": ("**", 8),
    "*": ("*", 6),
    "/": ("/", 6),
    "MOD": ("%", 6),
    "+": ("+", 5),
    "-": ("-", 5),
    "AND": ("&", 3),
    "XOR": ("^", 2),
    "OR": ("|", 1),
}
_UNARY_STRENGTH = 7
_ATOM_STRENGTH = 9


def _render_math_formula(
    text: str,
    *,
    hex_mode: bool,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render the CLICK Math formula *text* as Python that computes what a CLICK computes."""
    try:
        formula, pairs = parse_with_unconfirmed_pairs(text, hex_mode=hex_mode)
    except ValueError as error:
        raise ValueError(f"Unsupported CSV operand expression: {text!r}") from error
    return _render_parsed_formula(text, formula, pairs, collection, nicknames, structured_map)


def _render_parsed_formula(
    text: str,
    formula: Formula,
    pairs: tuple[tuple[str, str], ...],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None,
) -> str:
    """Render the parsed *formula* of *text*, and warn once for each unconfirmed pair in it."""
    for first, second in pairs:
        if first == second:
            uses = f"{_operator_words(first)} twice"
        else:
            uses = f"{_operator_words(first)} and {_operator_words(second)}"
        warnings.warn(
            f"The Math formula {text!r} uses {uses} with no brackets between the two. pyrung "
            f"applies {_guessed_order(first, second)}, but no one has checked that order on a "
            "CLICK yet. Add brackets to the formula in CLICK to set the order.",
            stacklevel=2,
        )

    def render(node: Formula) -> tuple[str, int]:
        source, strength = render_bare(node)
        if node.bracketed and strength < _ATOM_STRENGTH:
            return f"({source})", _ATOM_STRENGTH
        return source, strength

    def render_bare(node: Formula) -> tuple[str, int]:
        if isinstance(node, Number):
            return _render_number(node.value, node.hex, node.text)
        if isinstance(node, Address):
            return _sub_operand(node.text, collection, nicknames, structured_map), _ATOM_STRENGTH
        if isinstance(node, Pointer):
            block_var = _PREFIX_TO_BLOCK[node.bank]
            collection.used_blocks.add(block_var)
            index = _sub_operand(node.index.text, collection, nicknames, structured_map)
            return f"{block_var}[{index}]", _ATOM_STRENGTH
        if isinstance(node, Pi):
            collection.used_expr_funcs.add("PI")
            return "PI", _ATOM_STRENGTH
        if isinstance(node, Call):
            name = _CLICK_FUNC_TO_PYTHON[node.name]
            if name in _EXPR_FUNC_IMPORT_NAMES:
                collection.used_expr_funcs.add(name)
            args = ", ".join(render(arg)[0] for arg in node.args)
            return f"{name}({args})", _ATOM_STRENGTH
        if isinstance(node, RangeCall):
            block = _render_range_expr(
                node.first.bank, node.first.index, node.last.index, collection
            )
            return f"{block}.{_RANGE_AGG_TO_METHOD[node.name]}()", _ATOM_STRENGTH
        if isinstance(node, Negate):
            operand, strength = render(node.operand)
            if _is_python_number(node.operand):
                # Python would fold the minus sign into the constant when it loads the file. A
                # hex Math reads a negative constant as its 16 bits, so - 5h / DH1 would divide
                # FFFBh. A LiteralExpr keeps the minus sign a step of the scan.
                operand, strength = f"LiteralExpr({operand})", _ATOM_STRENGTH
                collection.used_expr_funcs.add("LiteralExpr")
            if strength < _UNARY_STRENGTH:
                operand = f"({operand})"
            return f"-{operand}", _UNARY_STRENGTH
        symbol, strength = _PYTHON_BINARY_OPS[node.op]
        left, left_strength = render(node.left)
        right, right_strength = render(node.right)
        if _is_python_number(node.left) and _is_python_number(node.right):
            # Python would compute an operation between two numbers when it loads the
            # file, in its own arithmetic.  A LiteralExpr keeps the operation for the scan,
            # so ``5 / 0`` stores 0 and sets the division-error flag, as math() does.
            left, left_strength = f"LiteralExpr({left})", _ATOM_STRENGTH
            collection.used_expr_funcs.add("LiteralExpr")
        # Python applies ** from the right and every other operator from the left.
        if left_strength < strength or (left_strength == strength and symbol == "**"):
            left = f"({left})"
        if right_strength < strength or (right_strength == strength and symbol != "**"):
            right = f"({right})"
        if symbol == "**":
            return f"{left}**{right}", strength
        return f"{left} {symbol} {right}", strength

    return render(formula)[0]


def _render_number(value: int | float, is_hex: bool, text: str) -> tuple[str, int]:
    """Render a constant, with its hex digits as CLICK wrote them (``00FFh`` is ``0x00FF``)."""
    sign = "-" if value < 0 else ""
    if is_hex:
        source = f"{sign}0x{text.lstrip('-')[:-1].upper()}"
    elif isinstance(value, int):
        source = str(value)
    else:
        source = text
    return source, _UNARY_STRENGTH if sign else _ATOM_STRENGTH


def _is_python_number(node: Formula) -> bool:
    """Return whether Python computes *node* to a plain number when it loads the file."""
    if isinstance(node, Call) and _CLICK_FUNC_TO_PYTHON[node.name] == "abs":
        # ABS is Python's builtin abs, which gives a plain number for a plain number.
        return len(node.args) == 1 and _is_python_number(node.args[0])
    return isinstance(node, Number)


def _has_constant_operation(node: Formula) -> bool:
    """Return whether the rendered *node* has an operation between two plain numbers, or a
    minus sign before one.
    """
    if isinstance(node, BinaryOp):
        if _is_python_number(node.left) and _is_python_number(node.right):
            return True
        return _has_constant_operation(node.left) or _has_constant_operation(node.right)
    if isinstance(node, Negate):
        return _is_python_number(node.operand) or _has_constant_operation(node.operand)
    if isinstance(node, Call):
        return any(_has_constant_operation(arg) for arg in node.args)
    return False


def _token_needs_literal_expr(token: str) -> bool:
    """Return whether the importer writes ``LiteralExpr`` for the CLICK instruction *token*,
    so its file must import it.
    """
    open_paren = token.find("(")
    if open_paren <= 0 or not token.endswith(")"):
        return False
    try:
        args, kwargs = _parse_af_args(token[open_paren + 1 : -1])
    except ValueError:
        return False
    hex_mode = token.startswith("math(") and ("mode", "hex") in kwargs
    for arg in args:
        try:
            formula, _ = parse_with_unconfirmed_pairs(arg, hex_mode=hex_mode)
        except ValueError:
            continue
        if _has_constant_operation(formula):
            return True
    return False


def _operator_words(operator: str) -> str:
    """Return *operator* as a warning names it. A binary operator goes in quotes."""
    return f"'{operator}'" if operator in _TIER_OF else operator


def _guessed_order(first: str, second: str) -> str:
    """Return the words that say how pyrung orders two operators that no reading has ordered."""
    if first not in _TIER_OF:
        return "the minus sign first"
    if _TIER_OF[first] == _TIER_OF[second]:
        return "the two from the left"
    return f"'{first if _TIER_OF[first] < _TIER_OF[second] else second}' first"


def _sub_range(
    match: re.Match[str],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Substitute a range match."""
    return _render_range_expr(match.group(1), int(match.group(2)), int(match.group(4)), collection)


def _render_range_expr(
    prefix: str,
    start_num: int,
    end_num: int,
    collection: _OperandCollection,
) -> str:
    """Render a range using semantic ownership when available.

    An end below the start (``C1018..C1011``, CLICK's downward shift
    register) renders as the ascending window ``.reverse()``.
    """
    if start_num > end_num:
        return f"{_render_range_expr(prefix, end_num, start_num, collection)}.reverse()"
    range_str = f"{prefix}{start_num}..{prefix}{end_num}"
    render = collection.semantic_ranges.get(range_str)
    if render is not None:
        return render.expr
    return _render_inline_range(prefix, start_num, end_num)


def _render_inline_range(prefix: str, start_num: int, end_num: int) -> str:
    """Render a raw hardware-bank range inline."""
    parsed = _parse_operand_prefix(f"{prefix}{start_num}")
    if parsed:
        _, _, block_var, _ = parsed
        return f"{block_var}.select({start_num}, {end_num})"
    return f"{prefix}{start_num}..{prefix}{end_num}"


# ---------------------------------------------------------------------------
# Subroutine Parsing
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a subroutine name to a snake_case filename slug."""
    # Insert underscores at CamelCase boundaries (e.g. AlarmHandler → Alarm_Handler)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return slug if slug else "subroutine"


# Names imported from pyrung that a subroutine slug must not shadow.
_RESERVED_IMPORT_NAMES: frozenset[str] = frozenset(
    {
        # DSL keywords / context managers
        "Program",
        "rung",
        "call",
        "subroutine",
        "branch",
        "forloop",
        # Combinators
        "Or",
        "And",
        # Instructions (Python import names, not Click AF names)
        "out",
        "latch",
        "reset",
        "copy",
        "blockcopy",
        "fill",
        "math",
        "on_delay",
        "off_delay",
        "count_up",
        "count_down",
        "shift",
        "search",
        "pack_bits",
        "pack_words",
        "pack_text",
        "unpack_to_bits",
        "unpack_to_words",
        "event_drum",
        "time_drum",
        "time_event_drum",
        "toggle",
        "debounce",
        "flasher",
        "timed",
        "return_early",
        "send",
        "receive",
        # Tag types
        "Bool",
        "Int",
        "Dint",
        "Real",
        "Word",
        "Char",
        "Block",
        "TagType",
        "named_array",
        "udt",
        "Field",
    }
)

_CODEGEN_RESERVED_IDENTIFIERS: frozenset[str] = frozenset(
    set(_RESERVED_IMPORT_NAMES)
    | set(_EXPR_FUNC_IMPORT_NAMES)
    | set(_BUILTIN_EXPR_FUNCS)
    | {block_var for _, _, block_var in _OPERAND_PREFIXES}
    | {
        "TagMap",
        "logic",
        "mapping",
        "system",
        "PLC",
    }
)


def _make_safe_identifier(
    name: str,
    *,
    used_names: set[str] | None = None,
    fallback: str = "tag",
) -> str:
    """Convert a logical tag/block name into a safe Python identifier."""
    safe = re.sub(r"\W+", "_", name)
    if not safe:
        safe = fallback
    if safe[0].isdigit():
        safe = f"_{safe}"

    is_softkeyword = getattr(keyword, "issoftkeyword", lambda _: False)
    if keyword.iskeyword(safe) or is_softkeyword(safe) or safe in _CODEGEN_RESERVED_IDENTIFIERS:
        safe = f"_{safe}"

    if used_names is None:
        return safe

    candidate = safe
    suffix = 2
    while candidate in used_names:
        candidate = f"{safe}_{suffix}"
        suffix += 1
    return candidate


def _build_sub_name_map(
    subroutines: list[_SubroutineInfo],
    *,
    reserved_names: set[str] | frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Map subroutine display names to Python function identifiers.

    Returns ``{"Alarm Handler": "_sub_alarm_handler", "startup": "_sub_startup", ...}``.

    Generated subroutine functions always live in a private ``_sub_`` namespace
    so legal Click nicknames cannot shadow them. Any remaining collision with a
    generated Python symbol, or between normalized subroutine names, receives a
    numeric suffix.
    """
    result: dict[str, str] = {}
    used_names = set(_CODEGEN_RESERVED_IDENTIFIERS) | set(reserved_names)
    for sub in subroutines:
        slug = f"_sub_{_slugify(sub.name)}"
        candidate = slug
        suffix = 2
        while candidate in used_names:
            candidate = f"{slug}_{suffix}"
            suffix += 1
        result[sub.name] = candidate
        used_names.add(candidate)
    return result
