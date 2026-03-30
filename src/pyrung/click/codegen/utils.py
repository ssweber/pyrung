from __future__ import annotations

import keyword
import re
from typing import TYPE_CHECKING

from pyrung.click.codegen.constants import (
    _FUNC_RE,
    _OPERAND_PREFIXES,
    _OPERAND_RE,
    _RANGE_RE,
    _STRING_KWARGS,
    _TIME_UNITS,
)
from pyrung.click.codegen.models import _OperandCollection
from pyrung.click.system_mappings import SYSTEM_OPERAND_PATHS

if TYPE_CHECKING:
    from pyrung.click.codegen.models import _SubroutineInfo
    from pyrung.click.tag_map import TagMap

# ---------------------------------------------------------------------------
# Click-native expression → Python operator conversion
# ---------------------------------------------------------------------------

# Click function names → Python equivalents (must NOT be treated as operand addresses)
_CLICK_FUNC_TO_PYTHON: dict[str, str] = {
    "SQRT": "sqrt",
    "SIN": "sin",
    "COS": "cos",
    "TAN": "tan",
    "ASIN": "asin",
    "ACOS": "acos",
    "ATAN": "atan",
    "RAD": "radians",
    "DEG": "degrees",
    "LOG": "log10",
    "LN": "log",
    "LSH": "lsh",
    "RSH": "rsh",
    "LRO": "lro",
    "RRO": "rro",
}

# Regex matching Click function calls like SQRT(, LSH(, etc.
_CLICK_FUNC_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in _CLICK_FUNC_TO_PYTHON) + r")\(")

# Regex matching standalone PI (not followed by digits, which would be a prefix match)
_CLICK_PI_RE = re.compile(r"\bPI\b")

# Regex matching SUM with colon-range: SUM ( DS1 : DS10 ) or SUM(DS1:DS10)
_SUM_RE = re.compile(r"SUM\s*\(\s*([A-Z]+)(\d+)\s*:\s*([A-Z]+)(\d+)\s*\)")

# Expression function names that require import from pyrung.core.expression
_EXPR_FUNC_IMPORT_NAMES = frozenset(_CLICK_FUNC_TO_PYTHON.values()) | {"PI"}

# Regex matching Python expression-function calls (lowercase) in generated code
_PYTHON_EXPR_FUNC_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in sorted(_EXPR_FUNC_IMPORT_NAMES) if n != "PI") + r")\("
)


def _click_expr_to_python(expr: str) -> str:
    """Convert a Click-native expression string to Python operator syntax.

    Handles infix operators (^ → **, MOD → %, AND → &, OR → |, XOR → ^)
    and function names (SQRT → sqrt, LSH → lsh, etc.).
    """
    result = expr

    # Infix operators — order matters: ^ before XOR to avoid double-conversion
    result = result.replace(" ^ ", "**")
    result = result.replace(" MOD ", " % ")
    result = result.replace(" AND ", " & ")
    result = result.replace(" OR ", " | ")
    result = result.replace(" XOR ", " ^ ")

    # Function names: SQRT( → sqrt(, etc.
    result = _CLICK_FUNC_RE.sub(lambda m: _CLICK_FUNC_TO_PYTHON[m.group(1)] + "(", result)

    # PI constant
    result = _CLICK_PI_RE.sub("PI", result)

    return result


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

    for ch in args_str:
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


def _sub_operand_kwarg(
    key: str,
    value: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Substitute a kwarg value, quoting string enum values."""
    if key in _STRING_KWARGS:
        return f'"{value}"'
    # oneshot=1 → oneshot=True
    if key == "oneshot" and value == "1":
        return "True"
    # word_swap=1 → word_swap=True, word_swap=0 → word_swap=False
    if key == "word_swap":
        return "True" if value == "1" else "False"
    # convert=to_value or convert=to_text(suppress_zero=0,...) — pass through
    if key == "convert":
        return value
    return _sub_operand(value, collection, nicknames, structured_map)


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

    # System operands (SC/SD → system.* path)
    if text in SYSTEM_OPERAND_PATHS:
        return SYSTEM_OPERAND_PATHS[text]

    # Check structured ownership first
    if structured_map is not None and text in collection.structure_owned_operands:
        owner = structured_map.owner_of(text)
        if owner is not None and owner.structure_type in ("named_array", "udt"):
            if owner.instance is not None:
                return f"{owner.structure_name}[{owner.instance}].{owner.field}"
            else:
                return f"{owner.structure_name}.{owner.field}"

    # Check if entire text is a known operand
    if text in collection.tags:
        return collection.tags[text].var_name

    # Check if entire text is a known range → use logical block's .select()
    if text in collection.structure_owned_ranges:
        return collection.structure_owned_ranges[text]
    if text in collection.ranges:
        r = collection.ranges[text]
        return f"{r.var_name}.select({r.start}, {r.end})"

    # Check for time units
    if text in _TIME_UNITS:
        return text

    # Check for quoted strings — pass through
    if text.startswith('"') and text.endswith('"'):
        return text

    # Check for numeric literal
    try:
        float(text)
        return text
    except ValueError:
        pass

    # Check for none
    if text == "none":
        return "None"

    # Check for function-call operands
    match = _FUNC_RE.match(text)
    if match:
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
        if func_name == "ModbusAddress":
            args, kwargs = _parse_af_args(inner_args_str)
            rendered: list[str] = []
            for k, v in kwargs:
                rendered.append(f"{k}={v}")
            return f"ModbusAddress({', '.join(rendered)})"
        if func_name in {"all", "any"}:
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            return f"{func_name}({', '.join(rendered)})"
        # SUM with colon-range: SUM ( DS1 : DS10 ) → ds.select(1, 10).sum()
        if func_name == "SUM":
            sum_match = _SUM_RE.match(text)
            if sum_match:
                prefix = sum_match.group(1)
                start_num = int(sum_match.group(2))
                end_num = int(sum_match.group(4))
                parsed = _parse_operand_prefix(f"{prefix}{start_num}")
                if parsed:
                    _, _, block_var, _ = parsed
                    return f"{block_var}.select({start_num}, {end_num}).sum()"
        # Click expression functions (SQRT, LSH, SIN, etc.)
        py_name = _CLICK_FUNC_TO_PYTHON.get(func_name)
        if py_name is not None:
            if py_name in _EXPR_FUNC_IMPORT_NAMES:
                collection.used_expr_funcs.add(py_name)
            args, _ = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            return f"{py_name}({', '.join(rendered)})"

    # Check for list/array: [C1,C2,C3]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1]
        if not inner:
            return "[]"
        items, _ = _parse_af_args(inner)
        rendered = [_sub_operand(item, collection, nicknames, structured_map) for item in items]
        return f"[{', '.join(rendered)}]"

    # Check for ranges like DS100..DS102
    range_match = _RANGE_RE.match(text)
    if range_match:
        range_str = range_match.group(0)
        if range_str in collection.structure_owned_ranges:
            return collection.structure_owned_ranges[range_str]
        if range_str in collection.ranges:
            return collection.ranges[range_str].var_name
        # Inline range: block.select(start, end)
        prefix = range_match.group(1)
        start_num = int(range_match.group(2))
        end_num = int(range_match.group(4))
        parsed = _parse_operand_prefix(f"{prefix}{start_num}")
        if parsed:
            _, _, block_var, _ = parsed
            return f"{block_var}.select({start_num}, {end_num})"

    # Expression with operators: convert Click-native operators to Python,
    # then substitute operand tokens within.

    # Convert SUM colon-ranges first (before general expression conversion)
    def _sub_sum(m: re.Match[str]) -> str:
        prefix = m.group(1)
        start_num = int(m.group(2))
        end_num = int(m.group(4))
        parsed = _parse_operand_prefix(f"{prefix}{start_num}")
        if parsed:
            _, _, block_var, _ = parsed
            return f"{block_var}.select({start_num}, {end_num}).sum()"
        return m.group(0)

    result = _SUM_RE.sub(_sub_sum, text)
    result = _click_expr_to_python(result)
    result = _RANGE_RE.sub(lambda m: _sub_range(m, collection, nicknames), result)

    def _sub_operand_token(m: re.Match[str]) -> str:
        op = m.group(0)
        if op in SYSTEM_OPERAND_PATHS:
            return SYSTEM_OPERAND_PATHS[op]
        if structured_map is not None and op in collection.structure_owned_operands:
            owner = structured_map.owner_of(op)
            if owner is not None and owner.structure_type in ("named_array", "udt"):
                if owner.instance is not None:
                    return f"{owner.structure_name}[{owner.instance}].{owner.field}"
                else:
                    return f"{owner.structure_name}.{owner.field}"
        if op in collection.tags:
            return collection.tags[op].var_name
        return op

    result = _OPERAND_RE.sub(_sub_operand_token, result)

    # Track expression function names for imports
    for m in _PYTHON_EXPR_FUNC_RE.finditer(result):
        collection.used_expr_funcs.add(m.group(1))
    if _CLICK_PI_RE.search(result):
        collection.used_expr_funcs.add("PI")

    return result


def _sub_range(
    match: re.Match[str],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Substitute a range match."""
    range_str = match.group(0)
    if range_str in collection.structure_owned_ranges:
        return collection.structure_owned_ranges[range_str]
    if range_str in collection.ranges:
        r = collection.ranges[range_str]
        return f"{r.var_name}.select({r.start}, {r.end})"
    prefix = match.group(1)
    start_num = int(match.group(2))
    end_num = int(match.group(4))
    parsed = _parse_operand_prefix(f"{prefix}{start_num}")
    if parsed:
        _, _, block_var, _ = parsed
        return f"{block_var}.select({start_num}, {end_num})"
    return range_str


# ---------------------------------------------------------------------------
# Subroutine Parsing
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a subroutine name to a filename slug (matching ladder.py)."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug if slug else "subroutine"


# Names imported from pyrung that a subroutine slug must not shadow.
_RESERVED_IMPORT_NAMES: frozenset[str] = frozenset(
    {
        # DSL keywords / context managers
        "Program",
        "Rung",
        "call",
        "subroutine",
        "branch",
        "forloop",
        # Combinators
        "any_of",
        "all_of",
        # Instructions (Python import names, not Click AF names)
        "out",
        "latch",
        "reset",
        "copy",
        "blockcopy",
        "fill",
        "calc",
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
    | {block_var for _, _, block_var in _OPERAND_PREFIXES}
    | {
        "TagMap",
        "logic",
        "mapping",
        "system",
        "PLCRunner",
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
    if (
        keyword.iskeyword(safe)
        or is_softkeyword(safe)
        or safe in _CODEGEN_RESERVED_IDENTIFIERS
    ):
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
) -> dict[str, str]:
    """Map subroutine display names to Python function identifiers.

    Returns ``{"Alarm Handler": "alarm_handler", "startup": "startup", ...}``.

    If a slug collides with a reserved pyrung import name (e.g. a
    subroutine named "calc" would shadow the ``calc`` instruction),
    the identifier is prefixed with ``sub_``.
    """
    result: dict[str, str] = {}
    for sub in subroutines:
        slug = _slugify(sub.name)
        if slug in _RESERVED_IMPORT_NAMES:
            slug = f"sub_{slug}"
        result[sub.name] = slug
    return result
