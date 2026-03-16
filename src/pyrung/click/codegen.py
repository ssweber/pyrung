"""CSV v2 → pyrung source code generator.

Reads a Click ladder CSV (v2 format) and emits executable pyrung Python code.
When the generated code is run, ``TagMap.to_ladder()`` reproduces the same CSV,
completing the round-trip::

    golden.bin → decode_to_csv() → CSV → codegen → .py → exec → to_ladder() → CSV₂

No laddercodec dependency — CSV is parsed with stdlib :mod:`csv`.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONDITION_COLS = 31
_HEADER_WIDTH = 33  # marker + 31 condition cols + AF

# Operand prefix → (tag type constructor, block variable name)
# Order matters: longer prefixes first so CTD matches before CT, etc.
_OPERAND_PREFIXES: list[tuple[str, str, str]] = [
    ("CTD", "Dint", "ctd"),
    ("CT", "Bool", "ct"),
    ("TD", "Int", "td"),
    ("TXT", "Char", "txt"),
    ("SC", "Bool", "sc"),
    ("SD", "Int", "sd"),
    ("DS", "Int", "ds"),
    ("DD", "Dint", "dd"),
    ("DH", "Word", "dh"),
    ("DF", "Real", "df"),
    ("X", "Bool", "x"),
    ("Y", "Bool", "y"),
    ("C", "Bool", "c"),
    ("T", "Bool", "t"),
]

_OPERAND_RE = re.compile(
    r"(?:CTD|CT|TD|TXT|SC|SD|DS|DD|DH|DF|X|Y|C|T)\d+",
)

# Matches a range like DS100..DS102
_RANGE_RE = re.compile(
    r"([A-Z]+)(\d+)\.\.([A-Z]+)(\d+)",
)

# Matches a function-call token like out(Y001) or on_delay(T1,TD1,preset=100,unit=Tms)
_FUNC_RE = re.compile(r"^(\~?)(\w+)\((.*)?\)$")

# Matches a comparison condition like DS1==5 or DS1!=DS2
_COMPARE_RE = re.compile(r"^(.+?)(==|!=|<=|>=|<|>)(.+)$")

# Matches a pin row like .reset() or .jump(5)
_PIN_RE = re.compile(r"^\.(\w+)\((.*)\)$")

# Time unit names that should be imported from pyrung
_TIME_UNITS = {"Tms", "Ts", "Tm", "Th", "Td"}

# Condition wrappers
_CONDITION_WRAPPERS = {"rise", "fall", "immediate"}

# AF instructions that are pyrung DSL calls
_INSTRUCTION_NAMES = {
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
    "call",
    "return",
    "for",
    "next",
    "send",
    "receive",
}

# Instructions that support pin rows
_PIN_INSTRUCTIONS = {
    "on_delay",
    "off_delay",
    "count_up",
    "count_down",
    "shift",
    "event_drum",
    "time_drum",
}

# Copy modifier functions
_COPY_MODIFIERS = {"as_value", "as_ascii", "as_text", "as_binary"}


# ---------------------------------------------------------------------------
# Phase 1: Parse CSV → Raw Rungs
# ---------------------------------------------------------------------------


@dataclass
class _RawRung:
    """One rung from the CSV: optional comment lines + data rows."""

    comment_lines: list[str]
    rows: list[list[str]]  # each row is 33 cells: [marker, A..AE, AF]


@dataclass
class _SubroutineInfo:
    """A subroutine parsed from a sub_*.csv file."""

    name: str  # original subroutine name (from call() match or slug)
    analyzed: list[_AnalyzedRung]


def _parse_csv(csv_path: Path) -> list[_RawRung]:
    """Read a CSV v2 file and segment into raw rungs."""
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        all_rows = list(reader)

    if not all_rows:
        return []

    # Validate header
    header = all_rows[0]
    if len(header) != _HEADER_WIDTH:
        raise ValueError(f"Expected {_HEADER_WIDTH}-column header, got {len(header)} columns.")

    rungs: list[_RawRung] = []
    pending_comments: list[str] = []
    current_rung: _RawRung | None = None

    for row in all_rows[1:]:
        # Pad short rows
        while len(row) < _HEADER_WIDTH:
            row.append("")

        marker = row[0]

        if marker == "#":
            # Comment row — collect for the next rung
            pending_comments.append(row[1] if len(row) > 1 else "")
            continue

        if marker == "R":
            # Start of a new rung
            if current_rung is not None:
                rungs.append(current_rung)
            current_rung = _RawRung(
                comment_lines=pending_comments,
                rows=[row],
            )
            pending_comments = []
            continue

        # Continuation row (marker == "" or anything else)
        if current_rung is not None:
            current_rung.rows.append(row)

    if current_rung is not None:
        rungs.append(current_rung)

    return rungs


# ---------------------------------------------------------------------------
# Phase 2: Analyze Topology → Logical Structure
# ---------------------------------------------------------------------------


@dataclass
class _PinInfo:
    """A pin row (e.g. .reset(), .down(), .clock(), .jump(N))."""

    name: str  # "reset", "down", "clock", "jump", "jog"
    arg: str  # "" or the argument inside parens
    conditions: list[str]  # condition tokens on this row


@dataclass
class _InstructionInfo:
    """One instruction (AF token) with optional branch conditions and pins."""

    af_token: str
    branch_conditions: list[str]  # conditions after split column (branch-local)
    pins: list[_PinInfo]


@dataclass
class _OrGroup:
    """One OR alternative: a list of condition tokens."""

    conditions: list[str]


@dataclass
class _AnalyzedRung:
    """Fully analyzed rung topology."""

    comment: str | None
    shared_conditions: list[str]  # conditions before split (shared across all outputs)
    or_groups: list[_OrGroup] | None  # None if no OR expansion; list if any_of()
    instructions: list[_InstructionInfo]
    is_forloop_start: bool = False
    is_forloop_body: bool = False
    is_forloop_next: bool = False


def _extract_conditions(row: list[str], start: int, end: int) -> list[str]:
    """Extract non-wire condition tokens from columns [start, end)."""
    tokens: list[str] = []
    for col in range(start, end):
        cell = row[col + 1]  # +1 because row[0] is marker
        if cell and cell not in {"-", "T", "|"}:
            tokens.append(cell)
    return tokens


def _find_split_column(rows: list[list[str]]) -> int | None:
    """Find the first column with T or | on the first data row.

    Returns the column index (0-based, relative to condition columns) or None.
    """
    first_row = rows[0]
    for col in range(_CONDITION_COLS):
        cell = first_row[col + 1]  # +1 for marker
        if cell == "T":
            return col
    return None


def _is_pin_row(row: list[str]) -> bool:
    """Check if a row is a pin row (AF starts with '.')."""
    af = row[-1]
    return bool(af and af.startswith("."))


def _is_or_continuation(row: list[str], split_col: int) -> bool:
    """Check if a continuation row is an OR alternative.

    OR rows have: no AF (or blank AF), and condition content before split_col,
    and the split column cell is T or -.
    """
    af = row[-1]
    if af and not af.startswith("."):
        return False
    # Must have no AF
    if af:
        return False
    # Check for the T/- marker at the split column
    split_cell = row[split_col + 1]
    if split_cell not in {"T", "-"}:
        return False
    return True


def _analyze_rungs(raw_rungs: list[_RawRung]) -> list[_AnalyzedRung]:
    """Analyze topology of each rung."""
    analyzed: list[_AnalyzedRung] = []

    # First pass: detect for/next grouping
    i = 0
    while i < len(raw_rungs):
        rung = raw_rungs[i]
        af0 = rung.rows[0][-1] if rung.rows else ""

        if af0.startswith("for("):
            # for/next block
            analyzed.append(_analyze_single_rung(rung, is_forloop_start=True))
            i += 1
            # Collect body rungs until next()
            while i < len(raw_rungs):
                body_rung = raw_rungs[i]
                body_af = body_rung.rows[0][-1] if body_rung.rows else ""
                if body_af == "next()":
                    analyzed.append(_analyze_single_rung(body_rung, is_forloop_next=True))
                    i += 1
                    break
                analyzed.append(_analyze_single_rung(body_rung, is_forloop_body=True))
                i += 1
        else:
            analyzed.append(_analyze_single_rung(rung))
            i += 1

    return analyzed


def _analyze_single_rung(
    rung: _RawRung,
    *,
    is_forloop_start: bool = False,
    is_forloop_body: bool = False,
    is_forloop_next: bool = False,
) -> _AnalyzedRung:
    """Analyze a single rung's topology."""
    comment = "\n".join(rung.comment_lines) if rung.comment_lines else None
    rows = rung.rows

    if not rows:
        return _AnalyzedRung(
            comment=comment,
            shared_conditions=[],
            or_groups=None,
            instructions=[],
        )

    first_row = rows[0]
    af0 = first_row[-1]

    # Simple case: single row, no split
    if len(rows) == 1:
        conditions = _extract_conditions(first_row, 0, _CONDITION_COLS)
        return _AnalyzedRung(
            comment=comment,
            shared_conditions=conditions,
            or_groups=None,
            instructions=[_InstructionInfo(af_token=af0, branch_conditions=[], pins=[])]
            if af0
            else [],
            is_forloop_start=is_forloop_start,
            is_forloop_body=is_forloop_body,
            is_forloop_next=is_forloop_next,
        )

    # Multi-row: find split column
    split_col = _find_split_column(rows)

    if split_col is None:
        # Just a single instruction with pin rows
        conditions = _extract_conditions(first_row, 0, _CONDITION_COLS)
        pins = _collect_pins(rows[1:])
        return _AnalyzedRung(
            comment=comment,
            shared_conditions=conditions,
            or_groups=None,
            instructions=[_InstructionInfo(af_token=af0, branch_conditions=[], pins=pins)]
            if af0
            else [],
            is_forloop_start=is_forloop_start,
            is_forloop_body=is_forloop_body,
            is_forloop_next=is_forloop_next,
        )

    # Has split — classify continuation rows
    shared_conditions = _extract_conditions(first_row, 0, split_col)

    # Check: is this an OR pattern or a branch/multi-output pattern?
    # OR pattern: continuation rows have conditions before split, blank AF
    # Branch/multi-output: continuation rows have AF tokens

    # First, check if ALL continuation rows (non-pin) are OR continuations
    non_pin_continuations = [r for r in rows[1:] if not _is_pin_row(r)]
    all_or = (
        all(_is_or_continuation(r, split_col) for r in non_pin_continuations)
        if non_pin_continuations
        else False
    )

    if all_or and non_pin_continuations:
        return _analyze_or_rung_with_split(rung, comment, split_col, shared_conditions)

    # Branch / multi-output pattern
    return _analyze_branch_rung(rung, comment, split_col, shared_conditions)


def _analyze_or_rung_with_split(
    rung: _RawRung,
    comment: str | None,
    split_col: int,
    shared_before_split: list[str],
) -> _AnalyzedRung:
    """Analyze a rung where T at split_col is an OR merge point."""
    rows = rung.rows
    first_row = rows[0]
    af = first_row[-1]

    # First OR group: conditions before split on first row
    first_group_conds = _extract_conditions(first_row, 0, split_col)
    or_groups = [_OrGroup(conditions=first_group_conds)]

    # Post-split conditions (shared trailing AND)
    post_split = _extract_conditions(first_row, split_col + 1, _CONDITION_COLS)

    # Continuation rows contribute OR alternatives
    pin_rows: list[list[str]] = []
    for row in rows[1:]:
        if _is_pin_row(row):
            pin_rows.append(row)
            continue
        alt_conditions = _extract_conditions(row, 0, split_col)
        if alt_conditions:
            or_groups.append(_OrGroup(conditions=alt_conditions))

    pins = _collect_pins(pin_rows)

    return _AnalyzedRung(
        comment=comment,
        shared_conditions=post_split,
        or_groups=or_groups if len(or_groups) > 1 else None,
        instructions=[_InstructionInfo(af_token=af, branch_conditions=[], pins=pins)] if af else [],
    )


def _analyze_branch_rung(
    rung: _RawRung,
    comment: str | None,
    split_col: int,
    shared_conditions: list[str],
) -> _AnalyzedRung:
    """Analyze a rung with branches or multiple outputs."""
    rows = rung.rows
    first_row = rows[0]
    af0 = first_row[-1]

    instructions: list[_InstructionInfo] = []

    # First instruction: from the first row
    first_branch_conds = _extract_conditions(first_row, split_col + 1, _CONDITION_COLS)

    # Collect pin rows that belong to the first instruction
    first_pins: list[list[str]] = []
    rest_start = 1
    for idx in range(1, len(rows)):
        if _is_pin_row(rows[idx]):
            first_pins.append(rows[idx])
            rest_start = idx + 1
        else:
            break

    if af0:
        instructions.append(
            _InstructionInfo(
                af_token=af0,
                branch_conditions=first_branch_conds,
                pins=_collect_pins(first_pins),
            )
        )

    # Process remaining continuation rows
    idx = rest_start
    while idx < len(rows):
        row = rows[idx]
        af = row[-1]

        if _is_pin_row(row):
            # Attach to most recent instruction
            if instructions:
                pin_match = _PIN_RE.match(af)
                if pin_match:
                    pin_conds = _extract_conditions(row, 0, _CONDITION_COLS)
                    instructions[-1].pins.append(
                        _PinInfo(
                            name=pin_match.group(1),
                            arg=pin_match.group(2),
                            conditions=pin_conds,
                        )
                    )
            idx += 1
            continue

        if af:
            # New instruction/branch row
            branch_conds = _extract_conditions(row, split_col + 1, _CONDITION_COLS)
            pin_rows_for_this: list[list[str]] = []
            idx += 1
            while idx < len(rows) and _is_pin_row(rows[idx]):
                pin_rows_for_this.append(rows[idx])
                idx += 1
            instructions.append(
                _InstructionInfo(
                    af_token=af,
                    branch_conditions=branch_conds,
                    pins=_collect_pins(pin_rows_for_this),
                )
            )
            continue

        idx += 1

    return _AnalyzedRung(
        comment=comment,
        shared_conditions=shared_conditions,
        or_groups=None,
        instructions=instructions,
    )


def _collect_pins(pin_rows: list[list[str]]) -> list[_PinInfo]:
    """Extract pin info from pin rows."""
    pins: list[_PinInfo] = []
    for row in pin_rows:
        af = row[-1]
        match = _PIN_RE.match(af)
        if match:
            conditions = _extract_conditions(row, 0, _CONDITION_COLS)
            pins.append(
                _PinInfo(
                    name=match.group(1),
                    arg=match.group(2),
                    conditions=conditions,
                )
            )
    return pins


# ---------------------------------------------------------------------------
# Phase 3: Collect Operands → Tag Declarations
# ---------------------------------------------------------------------------


@dataclass
class _TagDecl:
    """A tag declaration to emit."""

    var_name: str  # Python variable name (nickname or raw operand)
    tag_type: str  # "Bool", "Int", "Dint", "Real", "Word", "Char"
    tag_name: str  # tag name string passed to constructor
    operand: str  # original operand (e.g. "X001")
    block_var: str  # block variable for TagMap (e.g. "x")
    block_index: int  # address index (e.g. 1 for X001)
    comment: str  # inline comment (e.g. "# X001" when using nicknames)


@dataclass
class _RangeDecl:
    """A block range declaration — generates a logical Block mapped to hardware."""

    var_name: str  # Python variable name for the Block
    block_var: str  # hardware block (e.g. "ds")
    tag_type: str  # IEC tag type name (e.g. "INT")
    prefix: str  # operand prefix (e.g. "DS")
    start: int
    end: int
    operand_str: str  # e.g. "DS100..DS102"


@dataclass
class _OperandCollection:
    """All operands found in the program."""

    tags: dict[str, _TagDecl] = field(default_factory=dict)  # keyed by operand
    ranges: dict[str, _RangeDecl] = field(default_factory=dict)  # keyed by range string
    used_types: set[str] = field(default_factory=set)  # tag types used
    used_blocks: set[str] = field(default_factory=set)  # block vars used
    used_instructions: set[str] = field(default_factory=set)  # instruction names
    used_conditions: set[str] = field(default_factory=set)  # rise, fall, immediate
    used_time_units: set[str] = field(default_factory=set)  # Tms, Ts, etc.
    used_copy_modifiers: set[str] = field(default_factory=set)  # as_value, as_text, etc.
    has_any_of: bool = False
    has_branch: bool = False
    has_subroutine: bool = False
    has_forloop: bool = False
    has_modbus_target: bool = False


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


def _collect_operands(
    rungs: list[_AnalyzedRung],
    nicknames: dict[str, str] | None,
) -> _OperandCollection:
    """Scan all rungs and collect operand declarations."""
    collection = _OperandCollection()

    for rung in rungs:
        if rung.or_groups:
            collection.has_any_of = True

        if rung.is_forloop_start:
            collection.has_forloop = True

        # Scan conditions
        all_conditions: list[str] = list(rung.shared_conditions)
        if rung.or_groups:
            for group in rung.or_groups:
                all_conditions.extend(group.conditions)

        for cond in all_conditions:
            _scan_token_for_operands(cond, collection, nicknames)

        # Scan instructions
        for instr in rung.instructions:
            _scan_af_token(instr.af_token, collection, nicknames)
            for cond in instr.branch_conditions:
                _scan_token_for_operands(cond, collection, nicknames)
            if instr.branch_conditions:
                collection.has_branch = True
            for pin in instr.pins:
                for cond in pin.conditions:
                    _scan_token_for_operands(cond, collection, nicknames)
                if pin.arg:
                    _scan_token_for_operands(pin.arg, collection, nicknames)

    return collection


def _scan_token_for_operands(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> None:
    """Scan a single token string for operand references."""
    # Check for condition wrappers
    match = _FUNC_RE.match(token)
    if match:
        func_name = match.group(2)
        args_str = match.group(3) or ""
        if func_name in _CONDITION_WRAPPERS:
            collection.used_conditions.add(func_name)
            # Scan the inner argument
            _register_operands_from_text(args_str, collection, nicknames)
            return

    # Check for comparison
    cmp_match = _COMPARE_RE.match(token)
    if cmp_match:
        _register_operands_from_text(cmp_match.group(1), collection, nicknames)
        _register_operands_from_text(cmp_match.group(3), collection, nicknames)
        return

    # Check for negation prefix
    if token.startswith("~"):
        _register_operands_from_text(token[1:], collection, nicknames)
        return

    # Plain operand
    _register_operands_from_text(token, collection, nicknames)


def _scan_af_token(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> None:
    """Scan an AF token for instruction name and operands."""
    if not token:
        return

    match = _FUNC_RE.match(token)
    if not match:
        return

    func_name = match.group(2)
    args_str = match.group(3) or ""

    if func_name in _INSTRUCTION_NAMES:
        collection.used_instructions.add(func_name)

    if func_name in {"send", "receive"}:
        # Check for ModbusTarget
        if "ModbusTarget(" in args_str:
            collection.has_modbus_target = True

    if func_name == "call":
        collection.has_subroutine = True

    # Strip quoted strings before scanning for operands
    clean_args = _strip_quoted_strings(args_str)

    # Check for condition wrappers inside AF args (e.g. out(immediate(Y001)))
    for cw in _CONDITION_WRAPPERS:
        if cw + "(" in clean_args:
            collection.used_conditions.add(cw)

    # Check for time units
    for tu in _TIME_UNITS:
        if tu in clean_args:
            collection.used_time_units.add(tu)

    # Check for copy modifiers
    for cm in _COPY_MODIFIERS:
        if cm + "(" in clean_args:
            collection.used_copy_modifiers.add(cm)

    # Scan for operands
    _register_operands_from_text(clean_args, collection, nicknames)


def _register_operands_from_text(
    text: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> None:
    """Find and register all operands in a text fragment."""
    # Check for ranges first — collect range spans to suppress individual tags
    range_spans: set[str] = set()
    for range_match in _RANGE_RE.finditer(text):
        prefix1 = range_match.group(1)
        num1 = int(range_match.group(2))
        prefix2 = range_match.group(3)
        num2 = int(range_match.group(4))
        if prefix1 == prefix2:
            range_str = range_match.group(0)
            if range_str not in collection.ranges:
                parsed = _parse_operand_prefix(f"{prefix1}{num1}")
                if parsed:
                    _, tag_type, block_var, _ = parsed
                    # IEC type constants for Block declaration
                    iec_type = tag_type.upper()
                    collection.ranges[range_str] = _RangeDecl(
                        var_name=range_str.replace("..", "_to_"),
                        block_var=block_var,
                        tag_type=iec_type,
                        prefix=prefix1,
                        start=num1,
                        end=num2,
                        operand_str=range_str,
                    )
                    collection.used_blocks.add(block_var)
            # Mark all addresses in this range to suppress individual tags
            for i in range(num1, num2 + 1):
                range_spans.add(f"{prefix1}{i}")

    # Find individual operands (skip those covered by a range)
    for op_match in _OPERAND_RE.finditer(text):
        operand = op_match.group(0)
        if operand in collection.tags:
            continue
        if operand in range_spans:
            continue

        parsed = _parse_operand_prefix(operand)
        if parsed is None:
            continue

        prefix, tag_type, block_var, index = parsed
        collection.used_types.add(tag_type)
        collection.used_blocks.add(block_var)

        nick = nicknames.get(operand) if nicknames else None
        var_name = nick if nick else operand
        tag_name = nick if nick else operand
        comment_str = f"  # {operand}" if nick else ""

        collection.tags[operand] = _TagDecl(
            var_name=var_name,
            tag_type=tag_type,
            tag_name=tag_name,
            operand=operand,
            block_var=block_var,
            block_index=index,
            comment=comment_str,
        )


# ---------------------------------------------------------------------------
# Phase 4: Generate Code
# ---------------------------------------------------------------------------


def _generate_code(
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    *,
    subroutines: list[_SubroutineInfo] | None = None,
) -> str:
    """Generate the complete Python source file."""
    lines: list[str] = []

    # Module docstring
    lines.append('"""Auto-generated pyrung program from CSV v2."""')
    lines.append("")

    # Imports
    _emit_imports(lines, collection)
    lines.append("")

    # Tag declarations
    if collection.tags:
        lines.append("# --- Tags ---")
        _emit_tag_declarations(lines, collection)
        lines.append("")

    # Range declarations
    if collection.ranges:
        lines.append("# --- Ranges ---")
        _emit_range_declarations(lines, collection)
        lines.append("")

    # Program body
    lines.append("# --- Program ---")
    _emit_program(lines, rungs, collection, nicknames, subroutines=subroutines)
    lines.append("")

    # Tag map
    lines.append("# --- Tag Map ---")
    _emit_tag_map(lines, collection)
    lines.append("")

    return "\n".join(lines) + "\n"


def _emit_imports(lines: list[str], collection: _OperandCollection) -> None:
    """Emit import statements."""
    # Core imports
    core_imports: list[str] = ["Program", "Rung"]

    # Block/TagType if ranges are used
    if collection.ranges:
        core_imports.append("Block")
        core_imports.append("TagType")

    # Tag types
    for tt in sorted(collection.used_types):
        if tt not in core_imports:
            core_imports.append(tt)

    # Condition helpers
    if collection.has_any_of:
        core_imports.append("any_of")
    for cw in sorted(collection.used_conditions):
        core_imports.append(cw)

    # Instructions
    instruction_map = {
        "out": "out",
        "latch": "latch",
        "reset": "reset",
        "copy": "copy",
        "blockcopy": "blockcopy",
        "fill": "fill",
        "calc": "calc",
        "on_delay": "on_delay",
        "off_delay": "off_delay",
        "count_up": "count_up",
        "count_down": "count_down",
        "shift": "shift",
        "search": "search",
        "pack_bits": "pack_bits",
        "pack_words": "pack_words",
        "pack_text": "pack_text",
        "unpack_to_bits": "unpack_to_bits",
        "unpack_to_words": "unpack_to_words",
        "event_drum": "event_drum",
        "time_drum": "time_drum",
        "call": "call",
        "return": "return_early",
    }
    for instr_name in sorted(collection.used_instructions):
        import_name = instruction_map.get(instr_name)
        if import_name and import_name not in core_imports:
            core_imports.append(import_name)

    if collection.has_branch:
        core_imports.append("branch")
    if collection.has_forloop:
        core_imports.append("forloop")
    if collection.has_subroutine:
        core_imports.append("subroutine")

    # Time units
    for tu in sorted(collection.used_time_units):
        core_imports.append(tu)

    # Copy modifiers
    for cm in sorted(collection.used_copy_modifiers):
        core_imports.append(cm)

    lines.append(f"from pyrung import {', '.join(core_imports)}")

    # Click imports
    click_imports: list[str] = ["TagMap"]
    for bv in sorted(collection.used_blocks):
        click_imports.append(bv)
    if collection.has_modbus_target:
        click_imports.append("ModbusTarget")
    if collection.has_subroutine:
        # send/receive are imported from click
        pass
    if "send" in collection.used_instructions:
        click_imports.append("send")
    if "receive" in collection.used_instructions:
        click_imports.append("receive")

    lines.append(f"from pyrung.click import {', '.join(click_imports)}")


def _emit_tag_declarations(lines: list[str], collection: _OperandCollection) -> None:
    """Emit tag variable declarations."""
    # Sort by block order, then by index
    block_order = {bv: i for i, (_, _, bv) in enumerate(_OPERAND_PREFIXES)}
    sorted_tags = sorted(
        collection.tags.values(),
        key=lambda t: (block_order.get(t.block_var, 99), t.block_index),
    )
    for decl in sorted_tags:
        line = f'{decl.var_name} = {decl.tag_type}("{decl.tag_name}")'
        if decl.comment:
            line += decl.comment
        lines.append(line)


def _emit_range_declarations(lines: list[str], collection: _OperandCollection) -> None:
    """Emit logical Block declarations for ranges."""
    for decl in sorted(collection.ranges.values(), key=lambda r: r.operand_str):
        lines.append(
            f'{decl.var_name} = Block("{decl.var_name}", TagType.{decl.tag_type}, '
            f"{decl.start}, {decl.end})"
        )


def _emit_program(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    *,
    subroutines: list[_SubroutineInfo] | None = None,
) -> None:
    """Emit the program body."""
    lines.append("with Program() as logic:")

    if not rungs and not subroutines:
        lines.append("    pass")
        return

    _emit_rung_sequence(lines, rungs, collection, nicknames, indent=1)

    # Emit subroutine blocks
    if subroutines:
        for sub in subroutines:
            lines.append("")
            lines.append(f'    with subroutine("{sub.name}"):')
            if sub.analyzed:
                _emit_rung_sequence(lines, sub.analyzed, collection, nicknames, indent=2)
            else:
                lines.append("        pass")


def _emit_rung_sequence(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
) -> None:
    """Emit a sequence of rungs (main program or subroutine body)."""
    if not rungs:
        pad = "    " * indent
        lines.append(f"{pad}pass")
        return

    i = 0
    while i < len(rungs):
        rung = rungs[i]

        if rung.is_forloop_start:
            _emit_forloop(lines, rungs, i, collection, nicknames, indent=indent)
            # Skip to after next()
            i += 1
            while i < len(rungs) and not rungs[i].is_forloop_next:
                i += 1
            i += 1  # skip the next() rung
            continue

        if rung.is_forloop_next:
            # Should have been consumed by forloop handler
            i += 1
            continue

        _emit_rung(lines, rung, collection, nicknames, indent=indent)
        i += 1


def _emit_forloop(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    start_idx: int,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
) -> None:
    """Emit a for/next block."""
    pad = "    " * indent
    for_rung = rungs[start_idx]

    # Build the rung conditions
    conditions_str = _build_conditions_str(for_rung, collection, nicknames)

    # Parse for() token to get count and kwargs
    af = for_rung.instructions[0].af_token if for_rung.instructions else ""
    match = _FUNC_RE.match(af)
    if match:
        args_str = match.group(3) or ""
        args, kwargs = _parse_af_args(args_str)
        count_arg = _sub_operand(args[0], collection, nicknames) if args else "1"
        kw_parts = []
        for k, v in kwargs:
            rendered_v = _sub_operand_kwarg(k, v, collection, nicknames)
            kw_parts.append(f"{k}={rendered_v}")
    else:
        count_arg = "1"
        kw_parts = []

    # Emit rung with forloop
    _emit_rung_header(lines, for_rung, conditions_str, indent)

    forloop_args = count_arg
    if kw_parts:
        forloop_args += ", " + ", ".join(kw_parts)
    lines.append(f"{pad}    with forloop({forloop_args}):")

    # Body rungs — forloop body instructions are bare (not wrapped in Rung)
    body_pad = "    " * (indent + 2)
    body_count = 0
    for j in range(start_idx + 1, len(rungs)):
        if rungs[j].is_forloop_next:
            break
        body_rung = rungs[j]
        for instr in body_rung.instructions:
            _emit_instruction(lines, instr, collection, nicknames, indent + 2)
        body_count += 1

    if body_count == 0:
        lines.append(f"{body_pad}pass")


def _emit_rung_header(
    lines: list[str],
    rung: _AnalyzedRung,
    conditions_str: str,
    indent: int,
) -> None:
    """Emit 'with Rung(...):' or 'with Rung(...) as r:' + comment lines."""
    pad = "    " * indent
    as_clause = " as r" if rung.comment else ""
    if conditions_str:
        lines.append(f"{pad}with Rung({conditions_str}){as_clause}:")
    else:
        lines.append(f"{pad}with Rung(){as_clause}:")
    if rung.comment:
        _emit_comment(lines, rung.comment, indent + 1)


def _emit_rung(
    lines: list[str],
    rung: _AnalyzedRung,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
) -> None:
    """Emit a single rung."""
    pad = "    " * indent

    if not rung.instructions:
        return

    conditions_str = _build_conditions_str(rung, collection, nicknames)

    # Check if we need branch() blocks
    has_branches = any(instr.branch_conditions for instr in rung.instructions)
    multi_output = len(rung.instructions) > 1

    if has_branches and multi_output:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in rung.instructions:
            if instr.branch_conditions:
                branch_cond = ", ".join(
                    _render_condition(c, collection, nicknames) for c in instr.branch_conditions
                )
                lines.append(f"{pad}    with branch({branch_cond}):")
                _emit_instruction(lines, instr, collection, nicknames, indent + 2)
            else:
                _emit_instruction(lines, instr, collection, nicknames, indent + 1)
    elif multi_output and not has_branches:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in rung.instructions:
            _emit_instruction(lines, instr, collection, nicknames, indent + 1)
    else:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in rung.instructions:
            _emit_instruction(lines, instr, collection, nicknames, indent + 1)


def _build_conditions_str(
    rung: _AnalyzedRung,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Build the condition string for a Rung() constructor."""
    parts: list[str] = []

    if rung.or_groups and len(rung.or_groups) > 1:
        or_parts: list[str] = []
        for group in rung.or_groups:
            group_rendered = [_render_condition(c, collection, nicknames) for c in group.conditions]
            if len(group_rendered) == 1:
                or_parts.append(group_rendered[0])
            elif group_rendered:
                # Wrap multi-condition groups in implicit AND (just comma-separated in any_of)
                or_parts.append(", ".join(group_rendered))
        if len(or_parts) == 1:
            parts.append(or_parts[0])
        else:
            parts.append(f"any_of({', '.join(or_parts)})")
    elif rung.or_groups and len(rung.or_groups) == 1:
        for c in rung.or_groups[0].conditions:
            parts.append(_render_condition(c, collection, nicknames))

    for c in rung.shared_conditions:
        parts.append(_render_condition(c, collection, nicknames))

    return ", ".join(parts)


def _render_condition(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Render a condition token to Python expression."""
    # Negation
    if token.startswith("~"):
        inner = _sub_operand(token[1:], collection, nicknames)
        return f"~{inner}"

    # Function wrapper: rise(X001), fall(X001), immediate(X001)
    match = _FUNC_RE.match(token)
    if match:
        func_name = match.group(2)
        args_str = match.group(3) or ""
        if func_name in _CONDITION_WRAPPERS:
            inner = _sub_operand(args_str, collection, nicknames)
            return f"{func_name}({inner})"

    # Comparison: DS1==5, DS1!=DS2
    cmp_match = _COMPARE_RE.match(token)
    if cmp_match:
        left = _sub_operand(cmp_match.group(1), collection, nicknames)
        op = cmp_match.group(2)
        right = _sub_operand(cmp_match.group(3), collection, nicknames)
        return f"{left} {op} {right}"

    # Plain operand
    return _sub_operand(token, collection, nicknames)


def _emit_instruction(
    lines: list[str],
    instr: _InstructionInfo,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
) -> None:
    """Emit a single instruction call."""
    pad = "    " * indent
    af = instr.af_token

    if not af:
        return

    rendered = _render_af_token(af, collection, nicknames)

    # Handle pin chaining
    pin_strs: list[str] = []
    for pin in instr.pins:
        pin_rendered = _render_pin(pin, collection, nicknames)
        pin_strs.append(pin_rendered)

    if pin_strs:
        lines.append(f"{pad}{rendered}{''.join(pin_strs)}")
    else:
        lines.append(f"{pad}{rendered}")


def _render_af_token(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Render an AF token to a pyrung DSL call."""
    match = _FUNC_RE.match(token)
    if not match:
        return _sub_operand(token, collection, nicknames)

    func_name = match.group(2)
    args_str = match.group(3) or ""

    # Map 'return' → 'return_early'
    py_func = "return_early" if func_name == "return" else func_name

    if not args_str:
        return f"{py_func}()"

    args, kwargs = _parse_af_args(args_str)

    rendered_parts: list[str] = []
    for arg in args:
        rendered_parts.append(_sub_operand(arg, collection, nicknames))
    for key, value in kwargs:
        if key in _DROP_KWARGS:
            continue
        rendered_v = _sub_operand_kwarg(key, value, collection, nicknames)
        rendered_parts.append(f"{key}={rendered_v}")

    return f"{py_func}({', '.join(rendered_parts)})"



def _render_pin(
    pin: _PinInfo,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Render a pin as a chained method call."""
    if pin.conditions:
        cond = _render_condition(pin.conditions[0], collection, nicknames)
        if pin.arg:
            arg = _sub_operand(pin.arg, collection, nicknames)
            return f".{pin.name}({cond}, {arg})"
        return f".{pin.name}({cond})"
    if pin.arg:
        arg = _sub_operand(pin.arg, collection, nicknames)
        return f".{pin.name}({arg})"
    return f".{pin.name}()"


def _emit_comment(lines: list[str], comment: str, indent: int) -> None:
    """Emit a rung comment assignment."""
    pad = "    " * indent
    if "\n" in comment:
        # Multi-line comment
        escaped = comment.replace("\\", "\\\\").replace('"', '\\"')
        comment_lines = escaped.split("\n")
        lines.append(
            f'{pad}r.comment = "' + comment_lines[0] + "\\n" + "\\n".join(comment_lines[1:]) + '"'
        )
    else:
        escaped = comment.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{pad}r.comment = "{escaped}"')


def _emit_tag_map(lines: list[str], collection: _OperandCollection) -> None:
    """Emit the TagMap constructor."""
    lines.append("mapping = TagMap({")

    block_order = {bv: i for i, (_, _, bv) in enumerate(_OPERAND_PREFIXES)}
    sorted_tags = sorted(
        collection.tags.values(),
        key=lambda t: (block_order.get(t.block_var, 99), t.block_index),
    )

    for decl in sorted_tags:
        lines.append(f"    {decl.var_name}: {decl.block_var}[{decl.block_index}],")

    # Add ranges
    for decl in sorted(collection.ranges.values(), key=lambda r: r.operand_str):
        lines.append(f"    {decl.var_name}: {decl.block_var}.select({decl.start}, {decl.end}),")

    lines.append("}, include_system=False)")


# ---------------------------------------------------------------------------
# Token Parsing Helpers
# ---------------------------------------------------------------------------


def _parse_af_args(args_str: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse AF token arguments into positional args and keyword args.

    Handles nested parens and brackets for things like:
        out(Y001)
        calc(DS1+DS2,DS3,mode=int)
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


# Kwargs whose values are string enums (not operands/numbers).
_STRING_KWARGS = {"condition"}

# Kwargs to drop entirely from the generated code (informational in CSV).
_DROP_KWARGS = {"mode"}


def _sub_operand_kwarg(
    key: str,
    value: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Substitute a kwarg value, quoting string enum values."""
    if key in _STRING_KWARGS:
        return f'"{value}"'
    # oneshot=1 → oneshot=True
    if key == "oneshot" and value == "1":
        return "True"
    return _sub_operand(value, collection, nicknames)


def _sub_operand(
    text: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Substitute operand names with variable names in a text fragment.

    Handles plain operands, ranges, expressions, and nested constructs.
    """
    if not text:
        return text

    # Check if entire text is a known operand
    if text in collection.tags:
        return collection.tags[text].var_name

    # Check if entire text is a known range → use logical block's .select()
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

    # Check for copy modifiers: as_text(DS1,...), as_value(DS1), etc.
    match = _FUNC_RE.match(text)
    if match:
        func_name = match.group(2)
        inner_args_str = match.group(3) or ""
        if func_name in _COPY_MODIFIERS:
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames) for a in args]
            for k, v in kwargs:
                rendered.append(f"{k}={_sub_operand(v, collection, nicknames)}")
            return f"{func_name}({', '.join(rendered)})"
        if func_name == "ModbusTarget":
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames) for a in args]
            for k, v in kwargs:
                rendered.append(f"{k}={_sub_operand(v, collection, nicknames)}")
            return f"ModbusTarget({', '.join(rendered)})"
        if func_name in {"all", "any"}:
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames) for a in args]
            return f"{func_name}({', '.join(rendered)})"

    # Check for list/array: [C1,C2,C3]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1]
        if not inner:
            return "[]"
        items, _ = _parse_af_args(inner)
        rendered = [_sub_operand(item, collection, nicknames) for item in items]
        return f"[{', '.join(rendered)}]"

    # Check for ranges like DS100..DS102
    range_match = _RANGE_RE.match(text)
    if range_match:
        range_str = range_match.group(0)
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

    # Expression with operators: substitute operands within
    # Use regex replacement for operand tokens
    result = _RANGE_RE.sub(lambda m: _sub_range(m, collection, nicknames), text)
    result = _OPERAND_RE.sub(
        lambda m: collection.tags[m.group(0)].var_name
        if m.group(0) in collection.tags
        else m.group(0),
        result,
    )
    return result


def _sub_range(
    match: re.Match[str],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
) -> str:
    """Substitute a range match."""
    range_str = match.group(0)
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
# Nickname Loading
# ---------------------------------------------------------------------------


def _load_nicknames_from_csv(csv_path: Path) -> dict[str, str]:
    """Load a {display_address: nickname} map from a Click nickname CSV."""
    import pyclickplc

    records = pyclickplc.read_csv(str(csv_path))
    result: dict[str, str] = {}
    for record in records.values():
        if record.nickname:
            result[record.display_address] = record.nickname
    return result


# ---------------------------------------------------------------------------
# Subroutine Parsing
# ---------------------------------------------------------------------------


def _slugify(name: str) -> str:
    """Convert a subroutine name to a filename slug (matching ladder.py)."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug if slug else "subroutine"


def _find_call_names(raw_rungs: list[_RawRung]) -> dict[str, str]:
    """Scan main rungs for call("name") tokens → {slug: original_name}."""
    call_names: dict[str, str] = {}
    call_re = re.compile(r'^call\("(.*)"\)$')
    for rung in raw_rungs:
        for row in rung.rows:
            af = row[-1] if row else ""
            m = call_re.match(af)
            if m:
                name = m.group(1)
                call_names[_slugify(name)] = name
    return call_names


def _parse_subroutines(
    dir_path: Path,
    call_names: dict[str, str],
) -> list[_SubroutineInfo]:
    """Parse sub_*.csv files and match to call() names."""
    sub_paths = sorted(
        p
        for p in dir_path.iterdir()
        if p.is_file() and p.suffix.lower() == ".csv" and p.name.startswith("sub_")
    )

    subs: list[_SubroutineInfo] = []
    for sub_path in sub_paths:
        slug = sub_path.stem[4:]  # remove "sub_" prefix
        name = call_names.get(slug, slug)
        raw = _parse_csv(sub_path)
        analyzed = _analyze_rungs(raw)
        subs.append(_SubroutineInfo(name=name, analyzed=analyzed))

    return subs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def csv_to_pyrung(
    csv_path: str | Path,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
    output_path: str | Path | None = None,
) -> str:
    """Convert a Click ladder CSV (v2) to pyrung Python source code.

    Args:
        csv_path: Path to the CSV file (main.csv) or a directory containing
            main.csv and optional sub_*.csv subroutine files.
        nickname_csv: Optional path to a Click nickname CSV file (Address.csv).
            Read via ``pyclickplc.read_csv()``, extracts ``{display_address: nickname}``
            pairs for variable name substitution.
        nicknames: Optional pre-parsed ``{operand: nickname}`` dict. Alternative
            to ``nickname_csv``; useful when the caller already has the map.
        output_path: Optional path to write the generated Python file.
            If ``None``, the code is returned as a string only.

    Returns:
        The generated Python source code as a string.

    Raises:
        ValueError: If both ``nickname_csv`` and ``nicknames`` are provided,
            or if the CSV format is invalid.
    """
    if nickname_csv is not None and nicknames is not None:
        raise ValueError("Provide nickname_csv or nicknames, not both.")

    csv_path = Path(csv_path)

    nick_map: dict[str, str] | None = None
    if nickname_csv is not None:
        nick_map = _load_nicknames_from_csv(Path(nickname_csv))
    elif nicknames is not None:
        nick_map = nicknames

    # Determine if csv_path is a directory or a file
    if csv_path.is_dir():
        main_path = csv_path / "main.csv"
        if not main_path.exists():
            raise ValueError(f"main.csv not found in {csv_path}")
        dir_path = csv_path
    else:
        main_path = csv_path
        dir_path = csv_path.parent

    # Phase 1: Parse main CSV
    raw_rungs = _parse_csv(main_path)

    # Phase 1b: Parse subroutine CSVs (if any sub_*.csv files exist)
    call_names = _find_call_names(raw_rungs)
    subroutines = _parse_subroutines(dir_path, call_names) if call_names else []

    # Phase 2: Analyze topology
    analyzed = _analyze_rungs(raw_rungs)

    # Phase 3: Collect operands (from main + subroutines)
    all_analyzed = list(analyzed)
    for sub in subroutines:
        all_analyzed.extend(sub.analyzed)
    collection = _collect_operands(all_analyzed, nick_map)

    # Mark subroutine usage if we have subroutines
    if subroutines:
        collection.has_subroutine = True

    # Phase 4: Generate code
    code = _generate_code(analyzed, collection, nick_map, subroutines=subroutines)

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")

    return code
