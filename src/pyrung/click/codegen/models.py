from __future__ import annotations

from dataclasses import dataclass, field

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


@dataclass
class _PathResult:
    """One path through the grid: collected conditions → AF instruction."""

    conditions: list[str]
    af_token: str
    af_row: int


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
    used_copy_converters: set[str] = field(default_factory=set)  # to_value, to_text, etc.
    has_any_of: bool = False
    has_branch: bool = False
    has_subroutine: bool = False
    has_forloop: bool = False
    has_modbus_target: bool = False
    structures: list[_StructureDecl] = field(default_factory=list)
    structure_owned_operands: set[str] = field(default_factory=set)
