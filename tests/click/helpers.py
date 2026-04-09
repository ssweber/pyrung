"""Shared test helpers for the Click ladder export / codegen test audit.

These live next to the test files, not in production code.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from pyrung.click import (
    ModbusAddress,
    ModbusRtuTarget,
    ModbusTcpTarget,
    RegisterType,
    TagMap,
    c,
    ct,
    ctd,
    dd,
    df,
    dh,
    ds,
    sc,
    sd,
    t,
    td,
    txt,
    x,
    y,
)
from pyrung.click.codegen.constants import _OPERAND_RE
from pyrung.click.codegen.utils import _parse_operand_prefix, _strip_quoted_strings
from pyrung.core import (
    Block,
    Bool,
    Char,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Word,
    all_of,
    any_of,
    fall,
    immediate,
    rise,
)
from pyrung.core.program import (
    blockcopy,
    branch,
    calc,
    call,
    comment,
    copy,
    count_down,
    count_up,
    event_drum,
    fill,
    forloop,
    latch,
    off_delay,
    on_delay,
    out,
    pack_bits,
    pack_text,
    pack_words,
    reset,
    return_early,
    search,
    shift,
    subroutine,
    time_drum,
    unpack_to_bits,
    unpack_to_words,
)

# ---------------------------------------------------------------------------
# Type name → Tag constructor
# ---------------------------------------------------------------------------

_TAG_TYPES: dict[str, type] = {
    "Bool": Bool,
    "Int": Int,
    "Dint": Dint,
    "Word": Word,
    "Real": Real,
    "Char": Char,
}

_TYPE_NAME_TO_TAG_TYPE: dict[str, TagType] = {
    "Bool": TagType.BOOL,
    "Int": TagType.INT,
    "Dint": TagType.DINT,
    "Word": TagType.WORD,
    "Real": TagType.REAL,
    "Char": TagType.CHAR,
}

_RAW_RANGE_RE = re.compile(r"\b([A-Z]+)(\d+)\.\.([A-Z]+)(\d+)\b")


class _AddressableBlock(Protocol):
    def __getitem__(self, key: int) -> Any: ...

    def select(self, start: int, end: int) -> Any: ...


# block_var string → imported Click block object
_CLICK_BLOCKS: dict[str, _AddressableBlock] = {
    "x": x,
    "y": y,
    "c": c,
    "ds": ds,
    "dd": dd,
    "dh": dh,
    "df": df,
    "t": t,
    "td": td,
    "ct": ct,
    "ctd": ctd,
    "sc": sc,
    "sd": sd,
    "txt": txt,
}

# Everything a test snippet might reference — pre-loaded into the exec namespace
_EXEC_NAMESPACE: dict[str, object] = {
    # Core types
    "Program": Program,
    "Rung": Rung,
    "Bool": Bool,
    "Int": Int,
    "Dint": Dint,
    "Word": Word,
    "Real": Real,
    "Char": Char,
    "Block": Block,
    "TagType": TagType,
    # Condition combinators
    "any_of": any_of,
    "all_of": all_of,
    "rise": rise,
    "fall": fall,
    "immediate": immediate,
    # Instructions
    "out": out,
    "latch": latch,
    "reset": reset,
    "copy": copy,
    "calc": calc,
    "on_delay": on_delay,
    "off_delay": off_delay,
    "count_up": count_up,
    "count_down": count_down,
    "branch": branch,
    "forloop": forloop,
    "shift": shift,
    "search": search,
    "fill": fill,
    "blockcopy": blockcopy,
    "pack_bits": pack_bits,
    "pack_words": pack_words,
    "pack_text": pack_text,
    "unpack_to_bits": unpack_to_bits,
    "unpack_to_words": unpack_to_words,
    "event_drum": event_drum,
    "time_drum": time_drum,
    "call": call,
    "subroutine": subroutine,
    "return_early": return_early,
    "comment": comment,
    # Click-specific
    "send": __import__("pyrung.click", fromlist=["send"]).send,
    "receive": __import__("pyrung.click", fromlist=["receive"]).receive,
    "nop": __import__("pyrung.click", fromlist=["nop"]).nop,
    "ModbusTcpTarget": ModbusTcpTarget,
    "ModbusRtuTarget": ModbusRtuTarget,
    "ModbusAddress": ModbusAddress,
    "RegisterType": RegisterType,
    # Click blocks (for indirect refs like ds[Pointer])
    **_CLICK_BLOCKS,
}


class _RangeSpec(NamedTuple):
    name: str
    prefix: str
    type_name: str
    block_var: str
    start: int
    end: int


# ---------------------------------------------------------------------------
# 1. build_program
# ---------------------------------------------------------------------------


def _ensure_program_wrapper(source: str) -> str:
    """Wrap a bare test body in ``with Program() as p:`` if needed."""
    cleaned = textwrap.dedent(source).strip()
    if "with Program" in cleaned:
        return cleaned

    lines = cleaned.splitlines()
    prelude: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("import ") or stripped.startswith("from "):
            prelude.append(line)
            body_start = i + 1
            continue
        break

    body = "\n".join(lines[body_start:])
    wrapped = "with Program() as p:\n" + textwrap.indent(body or "pass", "    ")
    if not prelude:
        return wrapped
    return "\n".join([*prelude, wrapped])


def _range_var_name(prefix: str, start: int, end: int) -> str:
    return f"_{prefix}_{start}_{end}"


def _replace_ranges_in_chunk(chunk: str, specs: dict[str, _RangeSpec]) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix, start_str, end_prefix, end_str = match.groups()
        if prefix != end_prefix:
            raise ValueError(f"Raw range must stay within one operand family: {match.group()}")

        start = int(start_str)
        end = int(end_str)
        if start > end:
            raise ValueError(f"Raw range start must be <= end: {match.group()}")

        parsed = _parse_operand_prefix(f"{prefix}{start}")
        if parsed is None:
            raise ValueError(f"Unsupported raw range prefix: {match.group()}")

        _, type_name, block_var, _ = parsed
        name = _range_var_name(prefix, start, end)
        specs.setdefault(name, _RangeSpec(name, prefix, type_name, block_var, start, end))
        return f"{name}.select({start}, {end})"

    return _RAW_RANGE_RE.sub(repl, chunk)


def _rewrite_raw_ranges(source: str) -> tuple[str, dict[str, _RangeSpec]]:
    """Rewrite raw Click ranges like ``DS1..DS3`` to synthetic Block selects.

    Rewrites only outside quoted strings so literals like ``"DS1"`` stay untouched.
    Returns the rewritten source plus the synthetic block declarations/mappings needed.
    """

    specs: dict[str, _RangeSpec] = {}
    parts: list[str] = []
    start = 0
    i = 0

    while i < len(source):
        triple = None
        if source.startswith("'''", i):
            triple = "'''"
        elif source.startswith('"""', i):
            triple = '"""'

        if triple is not None:
            parts.append(_replace_ranges_in_chunk(source[start:i], specs))
            end = source.find(triple, i + 3)
            if end == -1:
                parts.append(source[i:])
                return "".join(parts), specs
            end += 3
            parts.append(source[i:end])
            i = end
            start = i
            continue

        if source[i] in {"'", '"'}:
            quote = source[i]
            parts.append(_replace_ranges_in_chunk(source[start:i], specs))
            end = i + 1
            while end < len(source):
                if source[end] == "\\":
                    end += 2
                    continue
                if source[end] == quote:
                    end += 1
                    break
                end += 1
            parts.append(source[i:end])
            i = end
            start = i
            continue

        i += 1

    parts.append(_replace_ranges_in_chunk(source[start:], specs))
    return "".join(parts), specs


def build_program(source: str) -> tuple[Program, TagMap]:
    """Build a Program + TagMap from a pyrung snippet using raw Click addresses.

    Scans *source* for Click address patterns (C1, DS1, T1, …), auto-declares
    each as the type its prefix implies, rewrites raw ranges like ``DS1..DS3``
    to synthetic ``Block.select(...)`` expressions, creates a TagMap mapping each
    tag/block to its native Click address, execs the source, and returns
    ``(logic, mapping)``. Bare rung bodies are auto-wrapped in ``with Program() as p:``.
    """
    cleaned = _ensure_program_wrapper(source)
    cleaned, range_specs = _rewrite_raw_ranges(cleaned)

    # Find all address tokens (avoiding string literals)
    stripped = _strip_quoted_strings(cleaned)
    stripped = _RAW_RANGE_RE.sub("", stripped)
    seen: dict[str, tuple[str, str, int]] = {}  # operand → (type_name, block_var, index)
    for match in _OPERAND_RE.finditer(stripped):
        operand = match.group()
        if operand in seen:
            continue
        parsed = _parse_operand_prefix(operand)
        if parsed is not None:
            _, type_name, block_var, index = parsed
            seen[operand] = (type_name, block_var, index)

    # Build tag declarations
    decl_lines = []
    for operand, (type_name, _, _) in sorted(seen.items()):
        decl_lines.append(f'{operand} = {type_name}("{operand}")')
    for spec in sorted(range_specs.values(), key=lambda spec: (spec.prefix, spec.start, spec.end)):
        tag_type = _TYPE_NAME_TO_TAG_TYPE[spec.type_name].name
        decl_lines.append(
            f'{spec.name} = Block("{spec.prefix}{spec.start}_to_{spec.prefix}{spec.end}", '
            f"TagType.{tag_type}, {spec.start}, {spec.end})"
        )

    # Ensure strict=False so exec'd snippets don't warn about missing AST
    cleaned = cleaned.replace("Program()", "Program(strict=False)")

    # Exec in a fresh namespace with all imports pre-loaded
    ns = dict(_EXEC_NAMESPACE)
    exec("\n".join(decl_lines), ns)  # noqa: S102
    exec(cleaned, ns)  # noqa: S102

    # Extract the program
    logic = ns.get("p") or ns.get("logic")
    if not isinstance(logic, Program):
        raise ValueError(
            "Source must either contain `with Program() as p:` / `as logic:` "
            "or provide a bare body that can be auto-wrapped."
        )

    # Build the TagMap
    mapping_dict: dict[Any, Any] = {}
    for operand, (_, block_var, index) in seen.items():
        tag_obj = ns[operand]
        block_obj = _CLICK_BLOCKS[block_var]
        mapping_dict[tag_obj] = block_obj[index]
    for spec in range_specs.values():
        block_obj = _CLICK_BLOCKS[spec.block_var]
        mapping_dict[ns[spec.name]] = block_obj.select(spec.start, spec.end)

    return logic, TagMap(mapping_dict, include_system=False)


# ---------------------------------------------------------------------------
# 2. normalize_csv
# ---------------------------------------------------------------------------


def normalize_csv(rows: tuple[tuple[str, ...], ...]) -> list[tuple[str, ...]]:
    """Strip header, end() rung, and trailing empty cells from exported CSV rows.

    Accepts ``bundle.main_rows`` and returns a trimmed list suitable for
    direct equality comparison in tests.
    """
    result = []
    for row in rows:
        # Skip header row
        if row and row[0] == "marker":
            continue
        # Skip end() rung
        if row and row[-1] == "end()":
            continue
        # Strip trailing empty cells
        cells = list(row)
        while cells and cells[-1] == "":
            cells.pop()
        result.append(tuple(cells))
    return result


# ---------------------------------------------------------------------------
# 3. normalize_pyrung
# ---------------------------------------------------------------------------


def normalize_pyrung(code: str) -> str:
    """Normalize pyrung source for comparison.

    Strips trailing whitespace per line and leading/trailing blank lines.
    Indentation differences remain visible.
    """
    lines = [line.rstrip() for line in code.splitlines()]
    # Strip leading blank lines
    while lines and not lines[0]:
        lines.pop(0)
    # Strip trailing blank lines
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. strip_pyrung_boilerplate
# ---------------------------------------------------------------------------


def strip_pyrung_boilerplate(code: str) -> str:
    """Extract the rung body from generated pyrung code.

    Strips everything outside the ``with Program`` block (imports, docstrings,
    tag declarations, TagMap), then strips the ``with Program`` line itself and
    dedents one level.  Also removes any leading ``comment()`` calls (which
    appear when the CSV has rung comments).

    Returns only the rung-level statements (``with Rung(...)``, etc.),
    normalized.

    Raises ``ValueError`` if no ``with Program`` block is found.
    """
    lines = code.splitlines()

    # Find the "with Program" line
    prog_start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("with Program"):
            prog_start = i
            break

    if prog_start is None:
        raise ValueError(f"Expected 'with Program' in generated code, got:\n{code[:200]}")

    # Collect the body inside the with-block (indented deeper than `with Program`).
    base_indent = len(lines[prog_start]) - len(lines[prog_start].lstrip())
    body_lines: list[str] = []
    for line in lines[prog_start + 1 :]:
        stripped = line.strip()
        if not stripped:
            body_lines.append("")
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        # Dedent one level (remove one indentation step)
        body_lines.append(line[base_indent + 4 :])

    # Strip leading comment() calls generated from CSV rung comments.
    result_lines: list[str] = []
    in_comment = False
    for line in body_lines:
        stripped = line.strip()
        if in_comment:
            if stripped.endswith('""")') or stripped.endswith("''')"):
                in_comment = False
            continue
        if stripped.startswith("comment("):
            if stripped.endswith(")"):
                # Single-line comment() — skip
                continue
            # Multi-line comment("""...""") — skip until closing
            in_comment = True
            continue
        result_lines.append(line)

    return normalize_pyrung("\n".join(result_lines))


# ---------------------------------------------------------------------------
# 5. load_fixtures
# ---------------------------------------------------------------------------


class Fixture(NamedTuple):
    """A CSV-with-comment test fixture."""

    name: str  # file stem, used as pytest test ID
    csv_path: Path  # path to the CSV file
    expected: str  # expected pyrung source from the rung comment


def load_fixtures(directory: Path | str) -> list[Fixture]:
    """Load CSV-with-comment fixtures from a directory.

    Each CSV file should have the expected pyrung source written into the
    first rung's comment.  Returns a sorted list of ``Fixture`` tuples
    suitable for ``@pytest.mark.parametrize``.
    """
    from pyrung.click.codegen.parser import _parse_csv

    directory = Path(directory)
    if not directory.is_dir():
        return []

    fixtures = []
    for csv_path in sorted(directory.glob("*.csv")):
        raw_rungs = _parse_csv(csv_path)
        if not raw_rungs:
            continue
        comment_text = "\n".join(raw_rungs[0].comment_lines)
        if not comment_text.strip():
            continue
        fixtures.append(
            Fixture(
                name=csv_path.stem,
                csv_path=csv_path,
                expected=comment_text,
            )
        )

    return fixtures
