"""What a stop predicate reads.

A coast may run scans without judging its predicate only while nothing the
predicate read changes, so it has to know what that is.  A condition says
(:func:`_extract_condition_reads`).  A callable is judged once on a state whose
field and tag reads are recorded (:func:`_traced_call`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from pyrsistent import PMap

_PMAP: Any = PMap  # its bucket accessors are private API

if TYPE_CHECKING:
    from pyrung.core.runner import PLC
    from pyrung.core.state import SystemState


def _extract_condition_reads(condition: Any) -> frozenset[str]:
    """Every tag name a ``run_until`` condition reads, regardless of form.

    Bare contacts, ``rise``/``fall`` edges, comparison left sides, tag operands
    on comparison right sides, and both legs of ``ArithAtom`` compounds.  A junk
    name (a Char *value* operand that happens to be a string) is harmless: it
    only makes a caller watch one name more.
    """
    from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Or, _condition_to_expr
    from pyrung.core.tag import Tag

    out: set[str] = set()

    def add_operand(operand: Any) -> None:
        if isinstance(operand, Tag):
            out.add(operand.name)
        elif isinstance(operand, str):
            out.add(operand)

    def visit(e: Any) -> None:
        if isinstance(e, Atom):
            out.add(e.tag)
            add_operand(e.operand)
        elif isinstance(e, ArithAtom):
            out.add(e.left)
            out.add(e.right)
            add_operand(e.operand)
        elif isinstance(e, (And, Or)):
            for term in e.terms:
                visit(term)

    visit(_condition_to_expr(condition))
    return frozenset(out)


# Reads of an opaque stop predicate.  A coast's undeclared predicate is judged on
# a state whose field and tag reads are recorded: a loop may run on only while the
# predicate read no value it moves (it then takes the same path and returns the
# same answer on every scan in between).
_READ_TRACES: list[set[str]] = []
_READ_ALL = "*"  # iterated or compared the whole tag map
_FIELD_READ = ":"  # prefix of a state field read (scan_id, timestamp, memory)


class _TracedTags(PMap):
    """A tag map that records which keys are read while a trace is active."""

    __slots__ = ()

    def __getitem__(self, key: Any) -> Any:
        if _READ_TRACES:
            _READ_TRACES[-1].add(key)
        return _PMAP._getitem(self._buckets, key)

    def __contains__(self, key: Any) -> bool:
        if _READ_TRACES:
            _READ_TRACES[-1].add(key)
        return _PMAP._contains(self._buckets, key)

    def iteritems(self) -> Any:
        if _READ_TRACES:
            _READ_TRACES[-1].add(_READ_ALL)
        return PMap.iteritems(self)

    def __eq__(self, other: object) -> bool:
        if _READ_TRACES:
            _READ_TRACES[-1].add(_READ_ALL)
        return PMap.__eq__(self, other)

    __hash__ = PMap.__hash__


def _traced_state_class() -> type:
    global _TRACED_STATE
    if _TRACED_STATE is None:
        from pyrung.core.state import SystemState

        class _TracedState(SystemState):
            """A SystemState whose field reads (and tag reads) are recorded."""

            __slots__ = ()

            def __getitem__(self, key: Any) -> Any:
                value = _PMAP._getitem(self._buckets, key)
                if key == "tags":
                    return _PMAP.__new__(_TracedTags, value._size, value._buckets)
                if _READ_TRACES:
                    _READ_TRACES[-1].add(_FIELD_READ + str(key))
                return value

        _TRACED_STATE = _TracedState
    return _TRACED_STATE


_TRACED_STATE: type | None = None


@contextmanager
def _reads_untraced() -> Iterator[None]:
    """Keep the reads inside the block out of the active trace.

    For a predicate whose contract is to be judged only on the scans a coast
    really executes (PILOT's opaque ``avoid=`` callables): its reads must not
    bound the windows that skip the others.
    """
    _READ_TRACES.append(set())
    try:
        yield
    finally:
        _READ_TRACES.pop()


def _traced_call(
    predicate: Callable[[SystemState], bool], runner: PLC
) -> tuple[bool, frozenset[str]]:
    """``predicate(runner.state)`` and every tag (or state field) it read.

    The tip itself is swapped for the traced state during the call, so a
    predicate that reads ``plc.state`` instead of its argument is traced too.
    """
    state = runner._state
    traced = _PMAP.__new__(_traced_state_class(), state._size, state._buckets)
    reads: set[str] = set()
    _READ_TRACES.append(reads)
    runner._state = traced
    try:
        hit = bool(predicate(traced))
    finally:
        runner._state = state
        _READ_TRACES.pop()
    return hit, frozenset(reads)
