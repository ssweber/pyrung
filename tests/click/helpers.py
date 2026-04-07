"""Shared test helpers for the Click ladder export / codegen test audit.

These live next to the test files, not in production code.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import NamedTuple

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
    Tms,
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

# block_var string → imported Click block object
_CLICK_BLOCKS: dict[str, object] = {
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
    "Tms": Tms,
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


# ---------------------------------------------------------------------------
# 1. build_program
# ---------------------------------------------------------------------------


def build_program(source: str) -> tuple[Program, TagMap]:
    """Build a Program + TagMap from a pyrung snippet using raw Click addresses.

    Scans *source* for Click address patterns (C1, DS1, T1, …), auto-declares
    each as the type its prefix implies, creates a TagMap mapping each to its
    native Click block address, execs the source, and returns ``(logic, mapping)``.

    The source should contain a ``with Program() as p:`` (or ``as logic:``) block.
    """
    cleaned = textwrap.dedent(source).strip()

    # Find all address tokens (avoiding string literals)
    stripped = _strip_quoted_strings(cleaned)
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

    # Ensure strict=False so exec'd snippets don't warn about missing AST
    cleaned = cleaned.replace("Program()", "Program(strict=False)")

    # Exec in a fresh namespace with all imports pre-loaded
    ns = dict(_EXEC_NAMESPACE)
    exec("\n".join(decl_lines), ns)  # noqa: S102
    exec(cleaned, ns)  # noqa: S102

    # Extract the program
    logic = ns.get("p") or ns.get("logic")
    if logic is None:
        raise ValueError(
            "Source must assign to 'p' or 'logic' via `with Program() as p:` "
            "or `with Program() as logic:`"
        )

    # Build the TagMap
    mapping_dict = {}
    for operand, (_, block_var, index) in seen.items():
        tag_obj = ns[operand]
        block_obj = _CLICK_BLOCKS[block_var]
        mapping_dict[tag_obj] = block_obj[index]

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
