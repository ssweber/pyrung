"""Find the Real tags a run may set to NaN or +/-Inf.

A REAL register holds NaN and +/-Inf (``storage.store_real``).  A value domain lists one
value for each way the program's comparisons can answer, and a finite value compares like
one of them.  NaN compares like none of them, because every comparison with it is false
but ``!=``.  An Inf compares like the largest value, but a formula turns it into NaN
(``Inf - Inf``).  So the classification gives a Real that may hold either no domain, and a
copy of it takes no values from it (``classify.py``).

These are the ways a Real gets one:

- A ``math()`` whose result can pass +/-3.4028235E+38 stores +/-Inf.  A formula of finite
  operands gives NaN or Inf through ``0 ** -1``, a power, or a division by a Real near 0.
- A copy, fill or block copy of a Real that may hold one.
- A ``pack_words()`` or ``pack_bits()`` into a Real stores raw bits, and some are NaN.
- A ``pack_text()`` of a number past the Real range stores +/-Inf.
- The outside writes a tag ``tag_written_from_outside`` names, and a ``receive()`` payload
  holds what a peer sends.  Both can be the words of a NaN.
- A write through a pointer with no declared bounds, into a block of more than 1000
  elements (``_INDIRECT_BLOCK_CAP`` in pdg.py), lists no targets.  So it may store into
  any element its pointer can reach.

A Real with ``choices=`` or with ``min=`` and ``max=`` holds only those values.  The facts
take a declaration as given everywhere else too.

The check is an interval analysis.  Each other Real starts at its power-on value.  Each
writer widens it by the values it can store, until no interval moves.  An operand that is
an integer tag spans its type.  A Real whose interval keeps moving widens to the whole
finite range.  A Real whose writer can store a value past it, or whose writer this module
cannot bound, may hold NaN or Inf.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.reverse_edges import literal_value_from_value, tag_name_from_value
from pyrung.core.storage import REAL_MAX
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.core.tag import TagType

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program

Bounds = tuple[float, float]

#: The whole finite range of a REAL.
_FINITE: Bounds = (-REAL_MAX, REAL_MAX)
#: Changes of one interval before it widens to :data:`_FINITE`.
_WIDEN_AFTER = 3
#: The integer types, and the values each holds.
_INTEGER_SPANS: dict[TagType, Bounds] = {
    TagType.BOOL: (0, 1),
    TagType.INT: (-32768, 32767),
    TagType.DINT: (-(2**31), 2**31 - 1),
    TagType.WORD: (0, 65535),
}
#: A function of a finite operand, and the values it gives.  A domain error is 0.
_FUNCTION_SPANS: dict[str, Bounds] = {
    "sin": (-1.0, 1.0),
    "cos": (-1.0, 1.0),
    "atan": (-math.pi / 2, math.pi / 2),
    "asin": (-math.pi / 2, math.pi / 2),
    "acos": (0.0, math.pi),
    # The logarithm of the least positive double is about -745.
    "log": (-746.0, 710.0),
    "log10": (-324.0, 309.0),
}


def non_finite_reals(
    program: Program, graph: ProgramGraph, receive_dests: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Return the Real tags of *graph* that a run may set to NaN or +/-Inf.

    The module docstring lists the ways.
    """
    from pyrung.core.validation._common import walk_instructions

    from .absorb import _all_write_targets

    reals = [name for name, tag in graph.tags.items() if tag.type is TagType.REAL]
    if not reals:
        return frozenset()
    # A write through a pointer may store into a tag without being listed as one of the
    # tag's writers. That tag may then hold what the write stores
    # (``ProgramGraph.unlisted_writers_of``).
    unlisted = graph.unlisted_written()
    bounds: dict[str, Bounds | None] = {}
    moving: set[str] = set()
    for name in reals:
        tag = graph.tags[name]
        declared = _declared_bounds(tag)
        if declared is not None:
            bounds[name] = declared
        elif tag.readonly:
            bounds[name] = (tag.default, tag.default)
        elif name in SYSTEM_TAGS_BY_NAME and name not in graph.writers_of:
            # The runtime writes it, and only with finite values.
            bounds[name] = _FINITE
        elif graph.written_from_outside(name) or name in receive_dests or name in unlisted:
            bounds[name] = None
        else:
            bounds[name] = (tag.default, tag.default)
            moving.add(name)

    writers: dict[str, list[Any]] = {}
    for instr in walk_instructions(program):
        for target, _kind in _all_write_targets(instr):
            if target in moving:
                writers.setdefault(target, []).append(instr)

    reader = _Bounds(graph, bounds)
    changes = dict.fromkeys(writers, 0)
    changed = True
    while changed:
        changed = False
        for target, instrs in writers.items():
            now = bounds[target]
            if now is None:
                continue
            new: Bounds | None = now
            for instr in instrs:
                stored = reader.stored(instr, graph.tags[target])
                if stored is None:
                    new = None
                    break
                new = (min(new[0], stored[0]), max(new[1], stored[1]))
            if new is not None and not (-REAL_MAX <= new[0] and new[1] <= REAL_MAX):
                new = None
            if new == now:
                continue
            changes[target] += 1
            if new is not None and changes[target] > _WIDEN_AFTER:
                new = _FINITE
            bounds[target] = new
            changed = True
    return frozenset(name for name, value in bounds.items() if value is None)


def _declared_bounds(tag: Any) -> Bounds | None:
    if tag.choices:
        values = [v for v in tag.choices if isinstance(v, (int, float))]
        return (min(values), max(values)) if values else None
    if isinstance(tag.min, (int, float)) and isinstance(tag.max, (int, float)):
        return (tag.min, tag.max)
    return None


def _corners(op: Any, left: Bounds, right: Bounds) -> Bounds | None:
    try:
        corners = [float(op(a, b)) for a in left for b in right]
    except (ArithmeticError, ValueError):
        return None
    if not all(math.isfinite(c) for c in corners):
        return None
    return (min(corners), max(corners))


def _size(bounds: Bounds) -> float:
    return max(abs(bounds[0]), abs(bounds[1]))


class _Bounds:
    """The values an operand, a formula or a write can give, as an interval.

    ``None`` means it may be NaN or Inf, or this class cannot bound it.
    """

    def __init__(self, graph: ProgramGraph, reals: dict[str, Bounds | None]) -> None:
        self.graph = graph
        self.reals = reals

    def stored(self, instr: Any, target: Any) -> Bounds | None:
        """Return the values *instr* can store into the Real *target*."""
        from pyrung.core.instruction.calc import CalcInstruction
        from pyrung.core.instruction.coils import ResetInstruction
        from pyrung.core.instruction.data_transfer import (
            BlockCopyInstruction,
            CopyInstruction,
            FillInstruction,
        )

        if isinstance(instr, CopyInstruction):
            if instr.convert is not None:
                return _converted(instr.convert)
            return self.value(instr.source)
        if isinstance(instr, FillInstruction):
            return self.value(instr.value)
        if isinstance(instr, BlockCopyInstruction):
            if instr.convert is not None:
                return _converted(instr.convert)
            return self.source_range(instr.source)
        if isinstance(instr, CalcInstruction):
            if instr.mode == "hex":
                return (0, 65535)
            return self.value(instr.expression)
        if isinstance(instr, ResetInstruction):
            return (0.0, 0.0)
        # A pack, a function's output, or a writer this class does not know.
        return None

    def source_range(self, source: Any) -> Bounds | None:
        from pyrung.core.validation._common import _resolve_tag_names

        names = _resolve_tag_names(source)
        if not names:
            return self.indirect(source)
        return self.hull(self.tag(name) for name in names)

    def hull(self, each: Any) -> Bounds | None:
        found: Bounds | None = None
        for bounds in each:
            if bounds is None:
                return None
            found = (
                bounds if found is None else (min(found[0], bounds[0]), max(found[1], bounds[1]))
            )
        return found

    def tag(self, name: str) -> Bounds | None:
        tag = self.graph.tags.get(name)
        if tag is None:
            tag = SYSTEM_TAGS_BY_NAME.get(name)
        if tag is None:
            return None
        if tag.type is TagType.REAL:
            return self.reals.get(name, _FINITE)
        return _INTEGER_SPANS.get(tag.type)

    def indirect(self, ref: Any) -> Bounds | None:
        """Return the values of a read through a pointer, of any element of its block."""
        block = getattr(ref, "block", None)
        kind = getattr(block, "type", None)
        if block is None or kind is None:
            return None
        if kind is not TagType.REAL:
            return _INTEGER_SPANS.get(kind)
        elements = [name for name in self.graph.tags if self.graph.tag_positions(name, (block,))]
        if any(self.reals.get(name, _FINITE) is None for name in elements):
            return None
        return _FINITE

    def value(self, node: Any) -> Bounds | None:
        """Return the values the operand or formula *node* can give."""
        from pyrung.core.expression import (
            BinaryExpr,
            MathFuncExpr,
            RandIntExpr,
            RangeAggregateExpr,
            ShiftFuncExpr,
            SumExpr,
            UnaryExpr,
        )

        literal = literal_value_from_value(node)
        if literal is not None:
            if isinstance(literal, (int, float)) and math.isfinite(literal):
                return (literal, literal)
            return None
        name = tag_name_from_value(node)
        if name is not None:
            return self.tag(name)
        if isinstance(node, BinaryExpr):
            return self.binary(node)
        if isinstance(node, UnaryExpr):
            inner = self.value(node.operand)
            if inner is None:
                return None
            if node.symbol == "+":
                return inner
            if node.symbol == "-":
                return (-inner[1], -inner[0])
            if node.symbol == "abs":
                return (0, _size(inner))
            if node.symbol == "~":
                return (-inner[1] - 1, -inner[0] - 1)
            return None
        if isinstance(node, MathFuncExpr):
            inner = self.value(node.operand)
            if inner is None:
                return None
            if node.name == "sqrt":
                return (0.0, math.sqrt(max(inner[1], 0.0)))
            if node.name == "radians":
                return _corners(lambda a, _b: math.radians(a), inner, (0, 0))
            if node.name == "degrees":
                return _corners(lambda a, _b: math.degrees(a), inner, (0, 0))
            return _FUNCTION_SPANS.get(node.name)
        if isinstance(node, RangeAggregateExpr):
            return self.source_range(node.block_range)
        if isinstance(node, SumExpr):
            members = self.source_range(node.block_range)
            count = len(node.block_range.tags()) if hasattr(node.block_range, "tags") else 0
            if members is None or not count:
                return None
            return (count * members[0], count * members[1])
        if isinstance(node, RandIntExpr):
            return self.hull(self.value(bound) for bound in (node.lo, node.hi))
        if isinstance(node, ShiftFuncExpr):
            if node.name in ("lro", "rro"):
                return (0, 65535)
            inner = self.value(node.value)
            if node.name == "rsh" and inner is not None:
                return (-_size(inner) - 1, _size(inner) + 1)
            return None
        if getattr(node, "block", None) is not None:
            return self.indirect(node)
        return None

    def binary(self, node: Any) -> Bounds | None:
        left = self.value(node.left)
        right = self.value(node.right)
        if left is None or right is None:
            return None
        symbol = node.symbol
        if symbol in ("+", "-", "*"):
            return _corners(node.op, left, right)
        if symbol in ("/", "//"):
            if right[0] > 0 or right[1] < 0:
                return _corners(node.op, left, right)
            if _whole(node.right):
                # A division by 0 is 0, and any other whole divisor is at least 1 in size.
                return (-_size(left) - 1, _size(left) + 1)
            return None
        if symbol == "%":
            return (-_size(right), _size(right))
        if symbol == "**":
            exponent = literal_value_from_value(node.right)
            if type(exponent) is int and 0 <= exponent <= 64:
                try:
                    size = float(_size(left)) ** exponent
                except OverflowError:
                    return None
                return (-size, size) if math.isfinite(size) else None
            return None
        if symbol in ("&", "|", "^"):
            bits = int(max(_size(left), _size(right))).bit_length() + 1
            return (-(2.0**bits), 2.0**bits)
        if symbol == ">>":
            return (-_size(left) - 1, _size(left) + 1)
        return None


def _whole(node: Any) -> bool:
    """Return True when *node* is always a whole number, so a nonzero value of it is at least 1."""
    from .classify import _integer_dividend

    return _integer_dividend(node)


def _converted(convert: Any) -> Bounds | None:
    """Return what a copy of text into a number stores: a digit's value or a character's code."""
    mode = getattr(convert, "mode", None)
    if mode in ("value", "ascii"):
        return (0, 255)
    return None
