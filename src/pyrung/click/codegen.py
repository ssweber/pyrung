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
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap

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
    "raw",
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
class _OrLevel:
    """One ``any_of()`` grouping in the condition sequence."""

    groups: list[_OrGroup]


@dataclass
class _AnalyzedRung:
    """Fully analyzed rung topology."""

    comment: str | None
    condition_seq: list[str | _OrLevel]  # ordered AND conditions and OR levels
    instructions: list[_InstructionInfo]
    is_forloop_start: bool = False
    is_forloop_body: bool = False
    is_forloop_next: bool = False


def _strip_wire_prefix(cell: str) -> str:
    """Strip ``T:`` wire-down prefix from a contact cell token."""
    if cell.startswith("T:"):
        return cell[2:]
    return cell


def _extract_conditions(row: list[str], start: int, end: int) -> list[str]:
    """Extract non-wire condition tokens from columns [start, end)."""
    tokens: list[str] = []
    for col in range(start, end):
        cell = row[col + 1]  # +1 because row[0] is marker
        if cell and cell not in {"-", "T", "|"}:
            tokens.append(_strip_wire_prefix(cell))
    return tokens


# Cell connectivity table — single source of truth for "what connects to what".
# Content tokens (contacts, comparisons, out() calls) default to ("left", "right").
# Adding a new read-only cell type is a one-line addition here.
_ADJACENCY: dict[str, tuple[str, ...]] = {
    "-": ("left", "right"),
    "|": ("up", "down"),
    "T": ("left", "right", "down"),
}


def _cell_exits(cell: str) -> tuple[str, ...]:
    """Exit directions for a cell type (content tokens default to left/right)."""
    if not cell:
        return ()
    if cell.startswith("T:"):
        return ("left", "right", "down")
    return _ADJACENCY.get(cell, ("left", "right"))


def _cell_at(rows: list[list[str]], r: int, c: int) -> str:
    """Cell value at condition column *c* on row *r* (empty if out of bounds)."""
    if 0 <= r < len(rows) and 0 <= c < _CONDITION_COLS:
        return rows[r][c + 1]  # +1 to skip marker column
    return ""


def _is_pin_row(row: list[str]) -> bool:
    """Check if a row is a pin row (AF starts with '.')."""
    af = row[-1]
    return bool(af and af.startswith("."))


@dataclass
class _PathResult:
    """One path through the grid: collected conditions → AF instruction."""

    conditions: list[str]
    af_token: str
    af_row: int


def _walk_grid(
    rows: list[list[str]],
    pin_row_set: set[int],
) -> list[_PathResult]:
    """Find root cells and DFS-walk from each."""
    n_rows = len(rows)
    paths: list[_PathResult] = []

    # Primary roots: first non-blank cell in column 0 per non-pin row.
    roots: list[tuple[int, int]] = []
    primary_col0_root_rows: set[int] = set()
    for r in range(n_rows):
        if r in pin_row_set:
            continue
        if _cell_at(rows, r, 0):
            roots.append((r, 0))
            primary_col0_root_rows.add(r)

    # Supplemental roots: AF-blank rows with blank column 0 whose first
    # non-blank cell is a T/T: vertical-chain head.
    for r in range(n_rows):
        if r in pin_row_set or r in primary_col0_root_rows:
            continue
        if _cell_at(rows, r, 0):
            continue
        if rows[r][-1]:
            continue

        first_col = -1
        first_cell = ""
        for c in range(_CONDITION_COLS):
            cell = _cell_at(rows, r, c)
            if cell:
                first_col = c
                first_cell = cell
                break
        if first_col < 0:
            continue
        if not (first_cell == "T" or first_cell.startswith("T:")):
            continue

        # Only root at the head of a vertical chain.
        if r > 0 and (r - 1) not in pin_row_set:
            above = _cell_at(rows, r - 1, first_col)
            if above and "down" in _cell_exits(above):
                continue

        roots.append((r, first_col))

    # Fallback: if column 0 is entirely blank, scan for the leftmost occupied
    # column (handles non-canonical decoded grids).
    if not roots:
        for r in range(n_rows):
            if r in pin_row_set:
                continue
            for c in range(_CONDITION_COLS):
                if _cell_at(rows, r, c):
                    roots.append((r, c))
                    break

    for root_r, root_c in roots:
        _dfs(
            rows,
            root_r,
            root_c,
            set(),
            pin_row_set,
            paths,
            (),
            primary_col0_root_rows,
        )

    # Preserve walk order, but drop exact duplicates.
    deduped: list[_PathResult] = []
    seen: set[tuple[tuple[str, ...], str, int]] = set()
    for p in paths:
        key = (tuple(p.conditions), p.af_token, p.af_row)
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    return deduped


def _dfs(
    rows: list[list[str]],
    r: int,
    c: int,
    visited: set[tuple[int, int]],
    pin_row_set: set[int],
    paths: list[_PathResult],
    conditions: tuple[str, ...],
    primary_col0_root_rows: set[int],
) -> None:
    """Recursive DFS walk. Priority: right → down → up → up-right."""
    cell = _cell_at(rows, r, c)
    if not cell or (r, c) in visited:
        return
    visited.add((r, c))

    # Record meaningful tokens (skip wire markers)
    is_content = cell not in {"-", "|", "T"}
    token = _strip_wire_prefix(cell) if is_content else cell
    conds = conditions + (token,) if is_content else conditions

    exits = _cell_exits(cell)
    has_right = "right" in exits
    has_down = "down" in exits

    # --- RIGHT ---
    if has_right:
        nc = c + 1
        if nc < _CONDITION_COLS:
            next_cell = _cell_at(rows, r, nc)
            if next_cell and (r, nc) not in visited:
                _dfs(
                    rows,
                    r,
                    nc,
                    visited,
                    pin_row_set,
                    paths,
                    conds,
                    primary_col0_root_rows,
                )
            elif not next_cell:
                # End of horizontal run — check AF on this row
                af = rows[r][-1]
                if af and not af.startswith("."):
                    paths.append(_PathResult(list(conds), af, r))
        else:
            # Past last condition column → AF exit
            af = rows[r][-1]
            if af and not af.startswith("."):
                paths.append(_PathResult(list(conds), af, r))

    # --- DOWN (forced bidirectional from T) ---
    if has_down:
        nr = r + 1
        if 0 <= nr < len(rows) and nr not in pin_row_set:
            below = _cell_at(rows, nr, c)
            if below and (nr, c) not in visited:
                # T:-prefixed contacts represent OR fork inputs. Stepping down
                # from them starts a parallel branch and must not carry the
                # current contact token into that branch.
                down_conds = conditions if cell.startswith("T:") else conds

                if cell.startswith("T:"):
                    below_is_content = below not in {"-", "|", "T"}
                    below_is_plain_content = below_is_content and not below.startswith("T:")
                    if not (below_is_plain_content and nr in primary_col0_root_rows):
                        _dfs(
                            rows,
                            nr,
                            c,
                            visited,
                            pin_row_set,
                            paths,
                            down_conds,
                            primary_col0_root_rows,
                        )
                else:
                    _dfs(
                        rows,
                        nr,
                        c,
                        visited,
                        pin_row_set,
                        paths,
                        down_conds,
                        primary_col0_root_rows,
                    )

    # --- UP (forced bidirectional — a T above pulls us up) ---
    if r > 0 and (r - 1) not in pin_row_set:
        above = _cell_at(rows, r - 1, c)
        if (
            above
            and "down" in _cell_exits(above)
            and not above.startswith("T:")
            and (r - 1, c) not in visited
        ):
            _dfs(
                rows,
                r - 1,
                c,
                visited,
                pin_row_set,
                paths,
                conds,
                primary_col0_root_rows,
            )

    # --- UP-RIGHT diagonal (T's down-wire drawn at left edge of its cell) ---
    # A cell connects diagonally up-right to a T when the cell directly to the
    # right is blank (gap), meaning there is no bridge occupying that position.
    if r > 0 and c + 1 < _CONDITION_COLS and (r - 1) not in pin_row_set:
        diag = _cell_at(rows, r - 1, c + 1)
        right_cell = _cell_at(rows, r, c + 1)
        if diag and "down" in _cell_exits(diag) and not right_cell:
            if (r - 1, c + 1) not in visited:
                _dfs(
                    rows,
                    r - 1,
                    c + 1,
                    visited,
                    pin_row_set,
                    paths,
                    conds,
                    primary_col0_root_rows,
                )

    visited.discard((r, c))


# --- Path grouping helpers ---------------------------------------------------


def _longest_common_prefix(lists: list[list[str]]) -> list[str]:
    """Return the longest list that is a prefix of every element in *lists*."""
    if not lists:
        return []
    prefix = lists[0]
    for lst in lists[1:]:
        i = 0
        while i < len(prefix) and i < len(lst) and prefix[i] == lst[i]:
            i += 1
        prefix = prefix[:i]
    return list(prefix)


def _longest_common_suffix(lists: list[list[str]]) -> list[str]:
    """Return the longest list that is a suffix of every element in *lists*."""
    if not lists:
        return []
    reversed_lists = [lst[::-1] for lst in lists]
    return _longest_common_prefix(reversed_lists)[::-1]


def _build_condition_tree(cond_lists: list[list[str]]) -> list[str | _OrLevel]:
    """Build an ordered sequence of AND conditions and OR levels from path lists.

    Recursively groups paths by common prefix tokens.  When all paths share the
    same first token it becomes a plain AND condition; when they diverge the
    distinct first tokens form an ``_OrLevel``.
    """
    # Drop fully-consumed (empty) paths
    active = [cl for cl in cond_lists if cl]
    if not active:
        return []

    # Group by first token (preserving insertion order)
    groups: dict[str, list[list[str]]] = {}
    for cl in active:
        groups.setdefault(cl[0], []).append(cl[1:])

    if len(groups) == 1:
        # All paths share the same first token → shared AND condition
        token = next(iter(groups))
        return [token] + _build_condition_tree(groups[token])

    # Multiple distinct first tokens → OR level
    or_groups: list[_OrGroup] = [_OrGroup(conditions=[tok]) for tok in groups]
    result: list[str | _OrLevel] = [_OrLevel(groups=or_groups)]

    # Collect remaining tails from all groups and recurse
    all_remainders: list[list[str]] = []
    for remainders in groups.values():
        all_remainders.extend(remainders)
    result.extend(_build_condition_tree(all_remainders))
    return result


def _group_paths(
    paths: list[_PathResult],
) -> tuple[list[str | _OrLevel], list[_InstructionInfo], list[int]]:
    """Determine OR vs branch structure from walk paths.

    Returns ``(condition_seq, instructions, af_rows)``.
    """
    if not paths:
        return [], [], []

    unique_afs = list(dict.fromkeys(p.af_token for p in paths))

    if len(unique_afs) == 1:
        # All paths reach the same AF → possible OR
        if len(paths) == 1:
            p = paths[0]
            return (
                list(p.conditions),
                [_InstructionInfo(p.af_token, [], [])],
                [p.af_row],
            )

        # Multiple paths → build condition tree (handles prefix, suffix, nested ORs)
        cond_lists = [p.conditions for p in paths]

        # Extract common trailing AND (suffix) first
        suffix = _longest_common_suffix(cond_lists)
        n_suffix = len(suffix)
        trimmed = [cl[: len(cl) - n_suffix] if n_suffix else list(cl) for cl in cond_lists]

        # Build tree from remaining conditions
        seq = _build_condition_tree(trimmed)
        # Append suffix as trailing AND conditions
        seq.extend(suffix)

        af = unique_afs[0]
        af_row = paths[0].af_row
        return (
            seq,
            [_InstructionInfo(af, [], [])],
            [af_row],
        )

    # Different AFs → branch.  Shared conditions = common prefix.
    cond_lists = [p.conditions for p in paths]
    prefix = _longest_common_prefix(cond_lists)
    n_prefix = len(prefix)

    instructions: list[_InstructionInfo] = []
    af_rows: list[int] = []
    for p in paths:
        branch_conds = p.conditions[n_prefix:]
        instructions.append(_InstructionInfo(p.af_token, branch_conds, []))
        af_rows.append(p.af_row)

    return list(prefix), instructions, af_rows


# --- Top-level Phase 2 entry points -----------------------------------------


def _analyze_rungs(raw_rungs: list[_RawRung]) -> list[_AnalyzedRung]:
    """Analyze topology of each rung."""
    analyzed: list[_AnalyzedRung] = []

    # Strip trailing end() rung (auto-appended by to_ladder, not part of user logic).
    if raw_rungs:
        last = raw_rungs[-1]
        last_af = last.rows[0][-1] if last.rows else ""
        if last_af == "end()":
            raw_rungs = raw_rungs[:-1]

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
    """Analyze a single rung's topology via graph walk."""
    comment = "\n".join(rung.comment_lines) if rung.comment_lines else None
    rows = rung.rows

    if not rows:
        return _AnalyzedRung(
            comment=comment,
            condition_seq=[],
            instructions=[],
        )

    # Separate pin rows from content rows
    pin_row_set = {i for i, row in enumerate(rows) if _is_pin_row(row)}

    # Walk the grid
    paths = _walk_grid(rows, pin_row_set)

    if not paths:
        # No paths discovered — handle rows with only an AF and no conditions,
        # or rows that are entirely blank in the condition columns.
        for i, row in enumerate(rows):
            if i not in pin_row_set:
                af = row[-1]
                if af and not af.startswith("."):
                    pins = _collect_pins([rows[j] for j in sorted(pin_row_set)])
                    return _AnalyzedRung(
                        comment=comment,
                        condition_seq=[],
                        instructions=[
                            _InstructionInfo(af_token=af, branch_conditions=[], pins=pins)
                        ],
                        is_forloop_start=is_forloop_start,
                        is_forloop_body=is_forloop_body,
                        is_forloop_next=is_forloop_next,
                    )
        return _AnalyzedRung(
            comment=comment,
            condition_seq=[],
            instructions=[],
        )

    # Group walk results into OR / branch structure
    condition_seq, instructions, af_rows = _group_paths(paths)

    # Attach pin rows to their nearest preceding instruction (by row index)
    if pin_row_set and instructions:
        for pin_idx in sorted(pin_row_set):
            best = -1
            for i, ar in enumerate(af_rows):
                if ar <= pin_idx:
                    best = i
            if best >= 0:
                pin_row = rows[pin_idx]
                af = pin_row[-1]
                match = _PIN_RE.match(af)
                if match:
                    pin_conds = _extract_conditions(pin_row, 0, _CONDITION_COLS)
                    instructions[best].pins.append(
                        _PinInfo(
                            name=match.group(1),
                            arg=match.group(2),
                            conditions=pin_conds,
                        )
                    )

    return _AnalyzedRung(
        comment=comment,
        condition_seq=condition_seq,
        instructions=instructions,
        is_forloop_start=is_forloop_start,
        is_forloop_body=is_forloop_body,
        is_forloop_next=is_forloop_next,
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
class _FieldHw:
    """Hardware location for one field of a structure."""

    block_var: str  # "ds", "c", etc.
    start: int
    end: int


@dataclass
class _StructureDecl:
    """A structured type declaration (named_array or udt)."""

    name: str
    structure_type: str  # "named_array" or "udt"
    base_type: str | None  # e.g. "Int" for named_array; None for udt
    count: int
    stride: int | None
    fields: list[tuple[str, str, object]]  # (field_name, type_name, default)
    hw_block_var: str  # "ds", "c", etc. (primary, for named_array)
    hw_start: int  # first hw address (for named_array)
    hw_end: int  # last hw address (for named_array)
    field_retentive: dict[str, bool] = field(default_factory=dict)
    field_hw: dict[str, _FieldHw] = field(default_factory=dict)  # per-field hw (for udt)


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
    structures: list[_StructureDecl] = field(default_factory=list)
    structure_owned_operands: set[str] = field(default_factory=set)


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
    *,
    structured_map: TagMap | None = None,
) -> _OperandCollection:
    """Scan all rungs and collect operand declarations."""
    collection = _OperandCollection()

    for rung in rungs:
        if any(isinstance(e, _OrLevel) for e in rung.condition_seq):
            collection.has_any_of = True

        if rung.is_forloop_start:
            collection.has_forloop = True

        # Scan conditions
        all_conditions: list[str] = []
        for elem in rung.condition_seq:
            if isinstance(elem, str):
                all_conditions.append(elem)
            else:
                for group in elem.groups:
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

    # Enrich with structured metadata if available
    if structured_map is not None:
        _enrich_with_structures(collection, structured_map)

    return collection


def _enrich_with_structures(
    collection: _OperandCollection,
    structured_map: TagMap,
) -> None:
    """Mark structure-owned operands and build _StructureDecl entries."""

    seen_structures: dict[str, _StructureDecl] = {}

    for operand in list(collection.tags):
        owner = structured_map.owner_of(operand)
        if owner is None:
            continue
        if owner.structure_type not in ("named_array", "udt"):
            continue

        collection.structure_owned_operands.add(operand)

        if owner.structure_name in seen_structures:
            continue

        # Build _StructureDecl from StructuredImport metadata
        si = structured_map.structure_by_name(owner.structure_name)
        if si is None:
            continue

        runtime = cast(Any, si.runtime)
        field_names = runtime.field_names

        _TAG_TYPE_MAP = {
            "BOOL": "Bool",
            "INT": "Int",
            "DINT": "Dint",
            "REAL": "Real",
            "WORD": "Word",
            "CHAR": "Char",
        }

        fields: list[tuple[str, str, object]] = []
        field_retentive: dict[str, bool] = {}
        for fn in field_names:
            block = runtime._blocks[fn]
            type_name = _TAG_TYPE_MAP.get(block.type.name, block.type.name)
            default = block.slot_config(1).default
            fields.append((fn, type_name, default))
            field_retentive[fn] = block.slot_config(1).retentive

        # Determine base_type for named_array (all fields share same type)
        base_type: str | None = None
        if si.kind == "named_array":
            base_type = _TAG_TYPE_MAP.get(runtime.type.name, runtime.type.name)

        # Determine hw_block_var and hw address range
        hw_block_var = ""
        hw_start = 0
        hw_end = 0

        def _resolve_hw_tag(slot_tag: Any) -> Any:
            """Resolve a logical slot to its hardware tag."""
            hw = structured_map._block_slot_forward_by_name.get(slot_tag.name)
            if hw is None:
                hw = structured_map._block_slot_forward_by_id.get(id(slot_tag))
            if hw is None:
                tag_entry = structured_map._tag_forward.get(slot_tag.name)
                if tag_entry is not None:
                    hw = tag_entry.hardware
            return hw

        _MEM_TO_BLOCK = {
            "X": "x",
            "Y": "y",
            "C": "c",
            "DS": "ds",
            "DD": "dd",
            "DH": "dh",
            "DF": "df",
            "T": "t",
            "TD": "td",
            "CT": "ct",
            "CTD": "ctd",
            "SC": "sc",
            "SD": "sd",
            "TXT": "txt",
            "XD": "xd",
            "YD": "yd",
        }

        from pyclickplc.addresses import parse_address

        # Build per-field hardware info
        per_field_hw: dict[str, _FieldHw] = {}
        for fn in field_names:
            fblock = runtime._blocks[fn]
            first_hw = _resolve_hw_tag(fblock[1])
            last_hw = _resolve_hw_tag(fblock[si.count])
            if first_hw is not None and last_hw is not None:
                mem_type, fstart = parse_address(first_hw.name)
                _, fend = parse_address(last_hw.name)
                bvar = _MEM_TO_BLOCK.get(mem_type, mem_type.lower())
                per_field_hw[fn] = _FieldHw(block_var=bvar, start=fstart, end=fend)
                collection.used_blocks.add(bvar)

        # For named_array, use overall span; for udt, use per-field
        first_field_block = runtime._blocks[field_names[0]]
        first_slot = first_field_block[1]
        hw_tag = _resolve_hw_tag(first_slot)
        if hw_tag is not None:
            mem_type, addr = parse_address(hw_tag.name)
            hw_start = addr
            hw_block_var = _MEM_TO_BLOCK.get(mem_type, mem_type.lower())

        last_field_block = runtime._blocks[field_names[-1]]
        last_slot = last_field_block[si.count]
        last_hw_tag = _resolve_hw_tag(last_slot)
        if last_hw_tag is not None:
            _, hw_end = parse_address(last_hw_tag.name)

        decl = _StructureDecl(
            name=si.name,
            structure_type=si.kind,
            base_type=base_type,
            count=si.count,
            stride=si.stride,
            fields=fields,
            hw_block_var=hw_block_var,
            hw_start=hw_start,
            hw_end=hw_end,
            field_retentive=field_retentive,
            field_hw=per_field_hw,
        )
        seen_structures[si.name] = decl
        collection.structures.append(decl)

        # Ensure types from structure fields are imported
        if si.kind == "udt":
            for _, type_name, _ in fields:
                collection.used_types.add(type_name)
        # Ensure hw block var is in used_blocks
        if hw_block_var:
            collection.used_blocks.add(hw_block_var)


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

    # raw() args are class name + hex blob, not operands — skip scanning.
    if func_name == "raw":
        return

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
    structured_map: TagMap | None = None,
) -> str:
    """Generate the complete Python source file."""
    lines: list[str] = []

    # Module docstring
    lines.append('"""Auto-generated pyrung program from CSV v2."""')
    lines.append("")

    # Imports
    _emit_imports(lines, collection)
    lines.append("")

    # Tag declarations (skip structure-owned)
    has_flat_tags = any(op not in collection.structure_owned_operands for op in collection.tags)
    if has_flat_tags:
        lines.append("# --- Tags ---")
        _emit_tag_declarations(lines, collection)
        lines.append("")

    # Range declarations
    if collection.ranges:
        lines.append("# --- Ranges ---")
        _emit_range_declarations(lines, collection)
        lines.append("")

    # Structure declarations
    if collection.structures:
        lines.append("# --- Structures ---")
        _emit_structure_declarations(lines, collection)
        lines.append("")

    # Program body
    lines.append("# --- Program ---")
    _emit_program(
        lines,
        rungs,
        collection,
        nicknames,
        subroutines=subroutines,
        structured_map=structured_map,
    )
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

    # Structure imports
    has_named_array = any(s.structure_type == "named_array" for s in collection.structures)
    has_udt = any(s.structure_type == "udt" for s in collection.structures)
    has_retentive_field = any(
        any(v for v in s.field_retentive.values()) for s in collection.structures
    )
    if has_named_array:
        core_imports.append("named_array")
    if has_udt:
        core_imports.append("udt")
    if has_retentive_field:
        core_imports.append("Field")

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
        if decl.operand in collection.structure_owned_operands:
            continue
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


def _emit_structure_declarations(lines: list[str], collection: _OperandCollection) -> None:
    """Emit @named_array / @udt class declarations."""
    for decl in collection.structures:
        if decl.structure_type == "named_array":
            _emit_named_array_decl(lines, decl)
        elif decl.structure_type == "udt":
            _emit_udt_decl(lines, decl)


def _emit_named_array_decl(lines: list[str], decl: _StructureDecl) -> None:
    """Emit a @named_array decorator + class."""
    stride_part = ""
    if decl.stride is not None and decl.stride != len(decl.fields):
        stride_part = f", stride={decl.stride}"
    count_part = f"count={decl.count}" if decl.count > 1 else ""
    deco_args = decl.base_type or "Int"
    if count_part:
        deco_args += f", {count_part}"
    deco_args += stride_part
    lines.append(f"@named_array({deco_args})")
    lines.append(f"class {decl.name}:")
    for field_name, _type_name, default in decl.fields:
        retentive = decl.field_retentive.get(field_name, False)
        if retentive:
            lines.append(f"    {field_name} = Field(retentive=True)")
        else:
            default_repr = _format_field_default(default)
            lines.append(f"    {field_name} = {default_repr}")


def _emit_udt_decl(lines: list[str], decl: _StructureDecl) -> None:
    """Emit a @udt decorator + class."""
    count_part = f"count={decl.count}" if decl.count > 1 else ""
    lines.append(f"@udt({count_part})")
    lines.append(f"class {decl.name}:")
    for field_name, type_name, default in decl.fields:
        retentive = decl.field_retentive.get(field_name, False)
        if retentive:
            lines.append(f"    {field_name}: {type_name} = Field(retentive=True)")
        else:
            default_repr = _format_field_default(default)
            lines.append(f"    {field_name}: {type_name} = {default_repr}")


def _format_field_default(default: object) -> str:
    """Format a field default value for code emission."""
    if isinstance(default, bool):
        return "True" if default else "False"
    if isinstance(default, float):
        return repr(default)
    if isinstance(default, int):
        return str(default)
    if isinstance(default, str):
        return repr(default)
    return repr(default)


def _emit_program(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    *,
    subroutines: list[_SubroutineInfo] | None = None,
    structured_map: TagMap | None = None,
) -> None:
    """Emit the program body."""
    lines.append("with Program() as logic:")

    if not rungs and not subroutines:
        lines.append("    pass")
        return

    _emit_rung_sequence(
        lines, rungs, collection, nicknames, indent=1, structured_map=structured_map
    )

    # Emit subroutine blocks
    if subroutines:
        for sub in subroutines:
            lines.append("")
            lines.append(f'    with subroutine("{sub.name}"):')
            if sub.analyzed:
                _emit_rung_sequence(
                    lines,
                    sub.analyzed,
                    collection,
                    nicknames,
                    indent=2,
                    structured_map=structured_map,
                )
            else:
                lines.append("        pass")


def _emit_rung_sequence(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
    structured_map: TagMap | None = None,
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
            _emit_forloop(
                lines,
                rungs,
                i,
                collection,
                nicknames,
                indent=indent,
                structured_map=structured_map,
            )
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

        _emit_rung(lines, rung, collection, nicknames, indent=indent, structured_map=structured_map)
        i += 1


def _emit_forloop(
    lines: list[str],
    rungs: list[_AnalyzedRung],
    start_idx: int,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
    structured_map: TagMap | None = None,
) -> None:
    """Emit a for/next block."""
    pad = "    " * indent
    for_rung = rungs[start_idx]

    # Build the rung conditions
    conditions_str = _build_conditions_str(for_rung, collection, nicknames, structured_map)

    # Parse for() token to get count and kwargs
    af = for_rung.instructions[0].af_token if for_rung.instructions else ""
    match = _FUNC_RE.match(af)
    if match:
        args_str = match.group(3) or ""
        args, kwargs = _parse_af_args(args_str)
        count_arg = _sub_operand(args[0], collection, nicknames, structured_map) if args else "1"
        kw_parts = []
        for k, v in kwargs:
            rendered_v = _sub_operand_kwarg(k, v, collection, nicknames, structured_map)
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
            _emit_instruction(lines, instr, collection, nicknames, indent + 2, structured_map)
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
    structured_map: TagMap | None = None,
) -> None:
    """Emit a single rung."""
    pad = "    " * indent

    if not rung.instructions:
        return

    conditions_str = _build_conditions_str(rung, collection, nicknames, structured_map)

    # Check if we need branch() blocks
    has_branches = any(instr.branch_conditions for instr in rung.instructions)
    multi_output = len(rung.instructions) > 1

    if has_branches and multi_output:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in rung.instructions:
            if instr.branch_conditions:
                branch_cond = ", ".join(
                    _render_condition(c, collection, nicknames, structured_map)
                    for c in instr.branch_conditions
                )
                lines.append(f"{pad}    with branch({branch_cond}):")
                _emit_instruction(lines, instr, collection, nicknames, indent + 2, structured_map)
            else:
                _emit_instruction(lines, instr, collection, nicknames, indent + 1, structured_map)
    elif multi_output and not has_branches:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in rung.instructions:
            _emit_instruction(lines, instr, collection, nicknames, indent + 1, structured_map)
    else:
        _emit_rung_header(lines, rung, conditions_str, indent)

        for instr in rung.instructions:
            _emit_instruction(lines, instr, collection, nicknames, indent + 1, structured_map)


def _build_conditions_str(
    rung: _AnalyzedRung,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Build the condition string for a Rung() constructor."""
    parts: list[str] = []

    for elem in rung.condition_seq:
        if isinstance(elem, str):
            parts.append(_render_condition(elem, collection, nicknames, structured_map))
        else:
            # _OrLevel — render any_of(...)
            or_parts: list[str] = []
            for group in elem.groups:
                group_rendered = [
                    _render_condition(c, collection, nicknames, structured_map)
                    for c in group.conditions
                ]
                if len(group_rendered) == 1:
                    or_parts.append(group_rendered[0])
                elif group_rendered:
                    or_parts.append(", ".join(group_rendered))
            if len(or_parts) == 1:
                parts.append(or_parts[0])
            else:
                parts.append(f"any_of({', '.join(or_parts)})")

    return ", ".join(parts)


def _render_condition(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render a condition token to Python expression."""
    # Negation
    if token.startswith("~"):
        inner = _sub_operand(token[1:], collection, nicknames, structured_map)
        return f"~{inner}"

    # Function wrapper: rise(X001), fall(X001), immediate(X001)
    match = _FUNC_RE.match(token)
    if match:
        func_name = match.group(2)
        args_str = match.group(3) or ""
        if func_name in _CONDITION_WRAPPERS:
            inner = _sub_operand(args_str, collection, nicknames, structured_map)
            return f"{func_name}({inner})"

    # Comparison: DS1==5, DS1!=DS2
    cmp_match = _COMPARE_RE.match(token)
    if cmp_match:
        left = _sub_operand(cmp_match.group(1), collection, nicknames, structured_map)
        op = cmp_match.group(2)
        right = _sub_operand(cmp_match.group(3), collection, nicknames, structured_map)
        return f"{left} {op} {right}"

    # Plain operand
    return _sub_operand(token, collection, nicknames, structured_map)


def _emit_instruction(
    lines: list[str],
    instr: _InstructionInfo,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    indent: int,
    structured_map: TagMap | None = None,
) -> None:
    """Emit a single instruction call."""
    pad = "    " * indent
    af = instr.af_token

    if not af:
        return

    rendered = _render_af_token(af, collection, nicknames, structured_map)

    # Handle pin chaining
    pin_strs: list[str] = []
    for pin in instr.pins:
        pin_rendered = _render_pin(pin, collection, nicknames, structured_map)
        pin_strs.append(pin_rendered)

    if pin_strs:
        lines.append(f"{pad}{rendered}{''.join(pin_strs)}")
    else:
        lines.append(f"{pad}{rendered}")


def _render_af_token(
    token: str,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render an AF token to a pyrung DSL call."""
    match = _FUNC_RE.match(token)
    if not match:
        return _sub_operand(token, collection, nicknames, structured_map)

    func_name = match.group(2)
    args_str = match.group(3) or ""

    # Map 'return' → 'return_early'
    py_func = "return_early" if func_name == "return" else func_name

    # raw(ClassName,hex) → raw("ClassName", blob=bytes.fromhex("hex"))
    if func_name == "raw":
        parts = args_str.split(",", 1)
        class_name = parts[0].strip()
        hex_blob = parts[1].strip() if len(parts) > 1 else ""
        return f'raw("{class_name}", blob=bytes.fromhex("{hex_blob}"))'

    if not args_str:
        return f"{py_func}()"

    args, kwargs = _parse_af_args(args_str)

    rendered_parts: list[str] = []
    for arg in args:
        rendered_parts.append(_sub_operand(arg, collection, nicknames, structured_map))
    for key, value in kwargs:
        if key in _DROP_KWARGS:
            continue
        rendered_v = _sub_operand_kwarg(key, value, collection, nicknames, structured_map)
        rendered_parts.append(f"{key}={rendered_v}")

    return f"{py_func}({', '.join(rendered_parts)})"


def _render_pin(
    pin: _PinInfo,
    collection: _OperandCollection,
    nicknames: dict[str, str] | None,
    structured_map: TagMap | None = None,
) -> str:
    """Render a pin as a chained method call."""
    if pin.conditions:
        cond = _render_condition(pin.conditions[0], collection, nicknames, structured_map)
        if pin.arg:
            arg = _sub_operand(pin.arg, collection, nicknames, structured_map)
            return f".{pin.name}({cond}, {arg})"
        return f".{pin.name}({cond})"
    if pin.arg:
        arg = _sub_operand(pin.arg, collection, nicknames, structured_map)
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
    has_structures = bool(collection.structures)

    if has_structures:
        lines.append("mapping = TagMap([")
    else:
        lines.append("mapping = TagMap({")

    block_order = {bv: i for i, (_, _, bv) in enumerate(_OPERAND_PREFIXES)}
    sorted_tags = sorted(
        collection.tags.values(),
        key=lambda t: (block_order.get(t.block_var, 99), t.block_index),
    )

    if has_structures:
        # Structure-level map_to entries
        for sdecl in collection.structures:
            if sdecl.structure_type == "named_array":
                # Named arrays use a single contiguous range
                lines.append(
                    f"    *{sdecl.name}.map_to("
                    f"{sdecl.hw_block_var}.select({sdecl.hw_start}, {sdecl.hw_end})),"
                )
            else:
                # UDTs may span multiple memory types → per-field map_to
                for fn, _, _ in sdecl.fields:
                    fhw = sdecl.field_hw.get(fn)
                    if fhw is None:
                        continue
                    if fhw.start == fhw.end:
                        lines.append(f"    {sdecl.name}.{fn}.map_to({fhw.block_var}[{fhw.start}]),")
                    else:
                        lines.append(
                            f"    {sdecl.name}.{fn}.map_to("
                            f"{fhw.block_var}.select({fhw.start}, {fhw.end})),"
                        )
        # Flat tags (non-structure-owned)
        for decl in sorted_tags:
            if decl.operand in collection.structure_owned_operands:
                continue
            lines.append(f"    {decl.var_name}.map_to({decl.block_var}[{decl.block_index}]),")
        # Flat ranges
        for decl in sorted(collection.ranges.values(), key=lambda r: r.operand_str):
            lines.append(
                f"    {decl.var_name}.map_to({decl.block_var}.select({decl.start}, {decl.end})),"
            )
        lines.append("], include_system=False)")
    else:
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
    structured_map: TagMap | None = None,
) -> str:
    """Substitute a kwarg value, quoting string enum values."""
    if key in _STRING_KWARGS:
        return f'"{value}"'
    # oneshot=1 → oneshot=True
    if key == "oneshot" and value == "1":
        return "True"
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
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            for k, v in kwargs:
                rendered.append(f"{k}={_sub_operand(v, collection, nicknames, structured_map)}")
            return f"{func_name}({', '.join(rendered)})"
        if func_name == "ModbusTarget":
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            for k, v in kwargs:
                rendered.append(f"{k}={_sub_operand(v, collection, nicknames, structured_map)}")
            return f"ModbusTarget({', '.join(rendered)})"
        if func_name in {"all", "any"}:
            args, kwargs = _parse_af_args(inner_args_str)
            rendered = [_sub_operand(a, collection, nicknames, structured_map) for a in args]
            return f"{func_name}({', '.join(rendered)})"

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

    def _sub_operand_token(m: re.Match[str]) -> str:
        op = m.group(0)
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
    structured_map: TagMap | None = None
    if nickname_csv is not None:
        from pyrung.click.tag_map import TagMap as _TagMap

        structured_map = _TagMap.from_nickname_file(Path(nickname_csv))
        nick_map = {
            slot.hardware_address: slot.logical_name
            for slot in structured_map.mapped_slots()
            if slot.source == "user"
        }
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
    collection = _collect_operands(all_analyzed, nick_map, structured_map=structured_map)

    # Mark subroutine usage if we have subroutines
    if subroutines:
        collection.has_subroutine = True

    # Phase 4: Generate code
    code = _generate_code(
        analyzed, collection, nick_map, subroutines=subroutines, structured_map=structured_map
    )

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")

    return code
