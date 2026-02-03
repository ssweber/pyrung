"""Immutable PLC Engine.

Redux-style architecture where logic is a pure function:
    Logic(Current_State) -> Next_State

Uses ScanContext for batched updates within a scan cycle,
reducing object allocation from O(instructions) to O(1) per scan.
"""

from pyrung.core.context import ScanContext
from pyrung.core.expression import (
    PI,
    Expression,
    acos,
    asin,
    atan,
    cos,
    degrees,
    log,
    log10,
    lro,
    lsh,
    radians,
    rro,
    rsh,
    sin,
    sqrt,
    tan,
)
from pyrung.core.memory_bank import IndirectTag, MemoryBank, MemoryBlock
from pyrung.core.program import (
    Program,
    Rung,
    any_of,
    branch,
    call,
    copy,
    count_down,
    count_up,
    fall,
    latch,
    nc,
    off_delay,
    on_delay,
    out,
    program,
    reset,
    rise,
    subroutine,
)
from pyrung.core.runner import PLCRunner
from pyrung.core.state import SystemState
from pyrung.core.tag import (
    Bit,
    Bool,
    Char,
    Dint,
    Float,
    Int,
    Int2,
    Real,
    Tag,
    TagType,
    Txt,
)
from pyrung.core.time_mode import TimeMode, TimeUnit

__all__ = [
    "PLCRunner",
    "ScanContext",
    "SystemState",
    "TimeMode",
    "TimeUnit",
    # Tags (IEC 61131-3 names)
    "Tag",
    "TagType",
    "Bool",
    "Int",
    "Dint",
    "Real",
    "Char",
    # Tags (deprecated aliases)
    "Bit",
    "Int2",
    "Float",
    "Txt",
    # Memory banks
    "MemoryBank",
    "MemoryBlock",
    "IndirectTag",
    # Program structure
    "Program",
    "Rung",
    "program",
    "branch",
    "subroutine",
    # Instructions
    "out",
    "latch",
    "reset",
    "copy",
    "call",
    "count_up",
    "count_down",
    "on_delay",
    "off_delay",
    # Conditions
    "nc",
    "rise",
    "fall",
    "any_of",
    # Expressions
    "Expression",
    "PI",
    "sqrt",
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "radians",
    "degrees",
    "log",
    "log10",
    "lsh",
    "rsh",
    "lro",
    "rro",
]
