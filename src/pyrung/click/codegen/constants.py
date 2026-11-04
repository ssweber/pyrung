from __future__ import annotations

import re
from collections.abc import Callable, Iterator

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

# An address stands alone: the tail of ``FFC0h`` or ``0xFFC0`` is not ``C0``.
_OPERAND_PATTERN = r"(?<![A-Za-z0-9_])(?:CTD|CT|TD|TXT|SC|SD|DS|DD|DH|DF|X|Y|C|T)\d+(?![A-Za-z0-9_])"

_OPERAND_RE = re.compile(_OPERAND_PATTERN)

# Quoted text is skipped whole, so a text constant such as ``"C1"`` never reads
# as an address.  Raw CSV doubles its quotes and treats backslash as plain; the
# Python-rendered form uses backslash escapes.
_CSV_OPERAND_OR_TEXT_RE = re.compile(rf'"(?:[^"]|"")*"|(?P<operand>{_OPERAND_PATTERN})')
_PY_OPERAND_OR_TEXT_RE = re.compile(rf'"(?:[^"\\]|\\.)*"|(?P<operand>{_OPERAND_PATTERN})')


def _operand_matches(text: str) -> Iterator[re.Match[str]]:
    """Operand matches in raw CSV ``text``, leaving out anything inside quoted text."""
    for match in _CSV_OPERAND_OR_TEXT_RE.finditer(text):
        if match.group("operand") is not None:
            yield match


def _sub_operands(text: str, replace: Callable[[str], str]) -> str:
    """Replace each operand in Python-rendered ``text``; quoted text is untouched."""
    return _PY_OPERAND_OR_TEXT_RE.sub(
        lambda m: replace(m.group("operand")) if m.group("operand") is not None else m.group(0),
        text,
    )

# Matches a range like DS100..DS102
_RANGE_RE = re.compile(
    r"([A-Z]+)(\d+)\.\.([A-Z]+)(\d+)",
)


def _ascending_range_str(range_str: str) -> str:
    """``C1018..C1011`` -> ``C1011..C1018``; ascending ranges pass through.

    CLICK's shift register allows an end below the start (it then shifts
    downward); pyrung spells that window ``.reverse()``, so range metadata is
    kept under the ascending key.
    """
    match = _RANGE_RE.fullmatch(range_str)
    if match is None or match.group(1) != match.group(3):
        return range_str
    start, end = int(match.group(2)), int(match.group(4))
    if start <= end:
        return range_str
    return f"{match.group(1)}{end}..{match.group(3)}{start}"


# Matches a function-call token like out(Y001) or on_delay(T1,TD1,preset=100,unit=Tms)
_FUNC_RE = re.compile(r"^(\~?)(\w+)\((.*)?\)$")

# Matches a comparison condition like DS1==5 or DS1!=DS2
_COMPARE_RE = re.compile(r"^(.+?)(==|!=|<=|>=|<|>)(.+)$")

# Matches a pin row like .reset() or .jump(5)
_PIN_RE = re.compile(r"^\.(\w+)\((.*)\)$")

# Additional input pins emitted by timers, counters, shift registers, and drums.
_PIN_NAMES = frozenset({"reset", "clock", "jump", "jog", "down"})

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
    "time_event_drum",
    "toggle",
    "debounce",
    "flasher",
    "timed",
    "call",
    "return",
    "break",
    "continue",
    "for",
    "next",
    "raw",
    "send",
    "receive",
}

# Out coil variants whose phases own a Timer, named as ``<side>_done``/``<side>_acc``
_OUT_VARIANT_TIMER_INSTRUCTIONS = {"debounce", "flasher", "timed"}

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

_STRING_KWARGS = {"condition", "unit"}

# Kwargs to drop entirely from the generated code (informational in CSV).

_DROP_KWARGS = {"mode"}
