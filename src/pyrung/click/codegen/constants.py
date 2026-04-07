from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Operand prefix → (tag type constructor, block variable name)

# Order matters: longer prefixes first so CTD matches before CT, etc.

_CONDITION_COLS = 31

_HEADER_WIDTH = 33  # marker + 31 condition cols + AF

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

# Copy converter functions
_COPY_CONVERTERS = {"to_value", "to_ascii", "to_text", "to_binary"}

# Cell connectivity table — single source of truth for "what connects to what".
# Content tokens (contacts, comparisons, out() calls) default to ("left", "right").
# Adding a new read-only cell type is a one-line addition here.

_ADJACENCY: dict[str, tuple[str, ...]] = {
    "-": ("left", "right"),
    # Click's "|" column accepts power from the left into the vertical bus,
    # but it must not pass through to the right.
    "|": ("left", "up", "down"),
    "T": ("left", "right", "down"),
}

# Kwargs whose values are string enums (not operands/numbers).

_STRING_KWARGS = {"condition"}

# Kwargs to drop entirely from the generated code (informational in CSV).

_DROP_KWARGS = {"mode"}
