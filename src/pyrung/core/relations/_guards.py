"""Find each record's place in a recording render, and the tests that decide whether it runs.

The emitters build a body before they wrap it in its ``if``, and paste one body under
several branches.  So a guard comes from the written code, not from the order of emits.

A guard is the list of tests around one place of a record, outermost first, each with the
value it must have.  These tests count:

- the test of each enclosing ``if``, with the side the record is on
- the test of each enclosing ``a if test else b``
- each earlier operand of an enclosing ``and`` (True) or ``or`` (False), since Python
  stops before the later ones

The reader turns each test into records when it can.  A ``_r`` marker is its record.
A local assigned once, by a plain ``=``, is the test it was assigned.  ``bool()``, ``not``, ``and`` and
``or`` keep their meaning.  Any other test is ``Opaque``.

A guard that holds may still not show that the record runs.  Then the place is only
necessary (``exact=False``).  That happens in two cases:

- A ``return``, ``raise``, ``break``, ``continue``, ``try`` or ``while`` anywhere in the
  function, since code may leave before it reaches the record.
- A loop or a comprehension around the record, since it may run any number of times.

An emitter may paste one piece in several places.  Each place is its own ``Placed``, and
the record runs when the guard of any of them holds.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from pyrung.core.relations.records import Read, Record, Rise, TagValue

_MARKER = "_r"
_KERNEL_FUNCTION = "_kernel_step"
_EXITS = (ast.Return, ast.Raise, ast.Break, ast.Continue, ast.Try, ast.While)
_LOOPS = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Lambda,
)
# The names the kernel reads and writes state through, and the scan's time step.
_STATE_NAMES = frozenset({"tags", "_mem", "_prev", "memory", "prev", "blocks", "dt"})
# A tag's or a block's symbol in the kernel, such as ``_t_Timer_Acc``.
_SYMBOL_PREFIXES = ("_t_", "_b_")
# Kernel helpers that touch no state, so a call of one needs no record.
_PURE_HELPERS = frozenset({"_dt_to_ns"})


@dataclass(frozen=True)
class Atom:
    record: Record


@dataclass(frozen=True)
class Not:
    test: Test


@dataclass(frozen=True)
class AllOf:
    tests: tuple[Test, ...]


@dataclass(frozen=True)
class AnyOf:
    tests: tuple[Test, ...]


@dataclass(frozen=True)
class Opaque:
    text: str


Test: TypeAlias = Atom | Not | AllOf | AnyOf | Opaque
Guard: TypeAlias = tuple[tuple[Test, bool], ...]


@dataclass(frozen=True)
class Placed:
    """One place of a record, with the tests that must hold for the code to reach it."""

    record: Record
    guard: Guard
    exact: bool
    # What a Rise reads as its input now, as a test.  None for any other record.
    operand: Test | None = None


def _marker_id(node: ast.AST) -> int | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == _MARKER
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, int)
    ):
        return node.args[0].value
    return None


class _Function:
    def __init__(self, source: str, records: Sequence[Record]) -> None:
        tree = ast.parse(source)
        self.fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == _KERNEL_FUNCTION
        )
        self.records = records
        self.parents: dict[ast.AST, ast.AST] = {}
        # Each binding of a local.  A plain ``name = value`` keeps its value.  Any other
        # binding is None, so the local resolves to nothing.  Loop targets, tuples,
        # walruses and ``as`` names are such bindings.
        self.assigned: dict[str, list[ast.expr | None]] = {}
        self.markers: dict[int, list[ast.Call]] = {}
        self.exits = False
        for node in ast.walk(self.fn):
            for child in ast.iter_child_nodes(node):
                self.parents[child] = node
        for node in ast.walk(self.fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                parent = self.parents.get(node)
                plain = (
                    isinstance(parent, ast.Assign)
                    and len(parent.targets) == 1
                    and parent.targets[0] is node
                )
                value = parent.value if plain and isinstance(parent, ast.Assign) else None
                self.assigned.setdefault(node.id, []).append(value)
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                self.assigned.setdefault(node.name, []).append(None)
            elif isinstance(node, _EXITS):
                self.exits = True
            marker = _marker_id(node)
            if marker is not None:
                assert isinstance(node, ast.Call)
                self.markers.setdefault(marker, []).append(node)

    def resolve(self, expr: ast.expr, seen: frozenset[str] = frozenset()) -> Test:
        marker = _marker_id(expr)
        if marker is not None:
            return Atom(self.records[marker])
        if (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Name)
            and expr.func.id == "bool"
            and len(expr.args) == 1
        ):
            return self.resolve(expr.args[0], seen)
        if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
            return Not(self.resolve(expr.operand, seen))
        if isinstance(expr, ast.BoolOp):
            tests = tuple(self.resolve(value, seen) for value in expr.values)
            return AllOf(tests) if isinstance(expr.op, ast.And) else AnyOf(tests)
        if isinstance(expr, ast.Name) and expr.id not in seen:
            values = self.assigned.get(expr.id, [])
            if len(values) == 1 and values[0] is not None:
                return self.resolve(values[0], seen | {expr.id})
        if isinstance(expr, ast.Constant) and isinstance(expr.value, bool):
            return AllOf(()) if expr.value else AnyOf(())
        return Opaque(ast.unparse(expr))

    def guard(self, node: ast.AST) -> tuple[Guard, bool]:
        """The tests around *node*, innermost last, and whether they are exact."""
        tests: list[tuple[Test, bool]] = []
        exact = not self.exits
        child = node
        parent = self.parents.get(child)
        while parent is not None and parent is not self.fn:
            if isinstance(parent, ast.If) and child is not parent.test:
                inside = any(item is child for item in parent.body)
                tests.append((self.resolve(parent.test), inside))
            elif isinstance(parent, ast.IfExp) and child is not parent.test:
                tests.append((self.resolve(parent.test), child is parent.body))
            elif isinstance(parent, ast.BoolOp):
                index = next(i for i, value in enumerate(parent.values) if value is child)
                wanted = isinstance(parent.op, ast.And)
                for earlier in reversed(parent.values[:index]):
                    tests.append((self.resolve(earlier), wanted))
            elif isinstance(parent, _LOOPS) or isinstance(parent, ast.comprehension):
                exact = False
            child, parent = parent, self.parents.get(parent)
        return tuple(reversed(tests)), exact

    def operand(self, call: ast.Call) -> Test | None:
        inner = call.args[1]
        if isinstance(inner, ast.BoolOp) and isinstance(inner.op, ast.And) and inner.values:
            return self.resolve(inner.values[0])
        return None

    def inside_marker(self, node: ast.AST) -> bool:
        child = node
        parent = self.parents.get(child)
        while parent is not None and parent is not self.fn:
            if _marker_id(parent) is not None:
                return True
            if (
                isinstance(parent, ast.Assign)
                and any(target is child for target in parent.targets)
                and _marker_id(parent.value) is not None
            ):
                return True
            child, parent = parent, self.parents.get(parent)
        return False


def place(source: str, records: Sequence[Record]) -> tuple[Placed, ...]:
    """Each place of *records* in *source*, with its guard, in the order they were recorded.

    An emitter may build a piece and then leave it out, such as the TT formula of a timer
    with no TT bit.  That record has no place in the source and describes no code, so it
    is dropped.  :func:`uncovered` catches the opposite fault, code that no record covers.
    """
    function = _Function(source, records)
    placed: list[Placed] = []
    for marker, record in enumerate(records):
        for node in function.markers.get(marker, ()):
            guard, exact = function.guard(node)
            operand = function.operand(node) if isinstance(record, Rise) else None
            placed.append(Placed(record, guard, exact, operand))
    return tuple(placed)


def _is_state_access(node: ast.AST) -> bool:
    """A name that reaches a tag or memory, a compare, or a call of a kernel helper.

    Every use of ``tags`` or ``_mem`` counts, so an alias or a ``getattr`` of one does too.
    """
    if isinstance(node, ast.Compare):
        return True
    if isinstance(node, ast.Name):
        return node.id in _STATE_NAMES or node.id.startswith(_SYMBOL_PREFIXES)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        return name.startswith("_") and name != _MARKER and name not in _PURE_HELPERS
    return False


def _tag_key(node: ast.expr) -> str | None:
    """The tag that ``tags['name']`` names."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "tags"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value
    return None


def _is_load(node: ast.stmt, symbols: Mapping[str, str]) -> bool:
    """A statement of the kernel's start, which loads state into its names.

    A tag's load must store into that tag's own symbol, so a load of one tag into another
    tag's symbol is an access the check reports.
    """
    if isinstance(node, ast.Global):
        return True
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return False
    target, value = node.targets[0], node.value
    if isinstance(target, ast.Name) and isinstance(value, ast.Name):
        return (target.id, value.id) in {("_mem", "memory"), ("_prev", "prev")}
    if isinstance(target, ast.Name):
        tag = _tag_key(value)
        return tag is not None and symbols.get(tag) == target.id
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name)
        and target.value.id == "memory"
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == "_dt"
        and isinstance(value, ast.Name)
        and value.id == "dt"
    )


def _is_store_back(node: ast.stmt, symbols: Mapping[str, str]) -> bool:
    """A statement of the kernel's end, which stores a tag's own symbol back into ``tags``."""
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
        return False
    tag = _tag_key(node.targets[0])
    value = node.value
    return tag is not None and isinstance(value, ast.Name) and symbols.get(tag) == value.id


def uncovered(source: str, records: Sequence[Record], symbols: Mapping[str, str]) -> list[str]:
    """Each access of state in *source* that no record covers.

    An access is a use of a tag's symbol, of ``tags``, of memory or of ``dt``.  So is a
    compare and a call of a kernel helper.  It skips the kernel's copies between ``tags``
    and its symbols, which belong to no instruction.  Only the copies at the very start and
    the very end of the function count as the kernel's own.  *symbols* maps each tag to its
    symbol in the render.  A copy counts only when its tag and its symbol match.
    """
    function = _Function(source, records)
    body = function.fn.body
    first = 0
    while first < len(body) and _is_load(body[first], symbols):
        first += 1
    last = len(body)
    while last > first and _is_store_back(body[last - 1], symbols):
        last -= 1
    found: list[str] = []
    for statement in body[first:last]:
        for node in ast.walk(statement):
            if _is_state_access(node) and not function.inside_marker(node):
                parent = function.parents.get(node, node)
                found.append(f"line {getattr(node, 'lineno', '?')}: {ast.unparse(parent)}")
    return found


def constant(test: Test) -> bool | None:
    """The value of a test that has no reads, such as the rung test of a rung with no condition."""
    if isinstance(test, AllOf) and not test.tests:
        return True
    if isinstance(test, AnyOf) and not test.tests:
        return False
    return None


def read_field(test: Test, fields: Mapping[str, str]) -> str | None:
    """The field a test reads as a level, when it is one recorded read of a stand-in tag."""
    if isinstance(test, Atom) and isinstance(test.record, Read):
        term = test.record.term
        if isinstance(term, TagValue):
            return fields.get(term.name)
    return None
