"""What a stop predicate reads.

A coast may run scans without judging its predicate only while nothing the
predicate read changes, so it has to know what that is.  A condition says
(:func:`_extract_condition_reads`).  A callable is judged once on a state whose
field and tag reads are recorded (:func:`_traced_call`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from pyrsistent import PMap

from pyrung.core.state import SystemState, _put_tags

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


def _extract_condition_reads(condition: Any) -> frozenset[str]:
    """Every tag name a ``run_until`` condition reads, regardless of form.

    Bare contacts and bare tags, ``rise``/``fall`` edges, comparison left sides, tag
    operands on comparison right sides, and both legs of ``ArithAtom`` compounds.  A
    junk name (a Char *value* operand that happens to be a string) is harmless: it
    only makes a caller watch one name more.  A condition this cannot see into (an
    indirect or expression comparison) gives a junk name in place of what it reads:
    a caller that relies on the set being whole asks :func:`_exact_condition_reads`.
    """
    from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Or, _condition_to_expr
    from pyrung.core.condition import _as_condition
    from pyrung.core.tag import ImmediateRef, Tag

    if isinstance(condition, (Tag, ImmediateRef)):
        condition = _as_condition(condition)

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


def _exact_condition_reads(condition: Any) -> frozenset[str] | None:
    """The tags a condition reads, or None if it reads anything it does not name.

    A coast runs on while what its predicate reads holds still, so a read missing here
    runs it past the stop.  An indirect comparison reads a pointer and the slot it
    selects, and an expression reads its terms; None sends the caller to
    :func:`_traced_call`, which records them.
    """
    return _extract_condition_reads(condition) if _names_every_read(condition) else None


def _names_every_read(condition: Any) -> bool:
    from pyrung.core.condition import (
        AllCondition,
        AnyCondition,
        BitCondition,
        CompareEq,
        CompareGe,
        CompareGt,
        CompareLe,
        CompareLt,
        CompareNe,
        IntTruthyCondition,
        NormallyClosedCondition,
        _EdgeCondition,
    )
    from pyrung.core.tag import ImmediateRef, Tag

    if isinstance(condition, (Tag, ImmediateRef)):
        return True
    if isinstance(condition, (AllCondition, AnyCondition)):
        return all(_names_every_read(term) for term in condition.conditions)
    if isinstance(
        condition, (BitCondition, IntTruthyCondition, NormallyClosedCondition, _EdgeCondition)
    ):
        return True
    if isinstance(condition, (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)):
        value = condition.value
        if isinstance(value, Tag):
            return not value.readonly  # lowers to its default, dropping the name
        return value is None or isinstance(value, (bool, int, float, str))
    return False


# Reads of an opaque stop predicate.  A coast's undeclared predicate is judged on
# a state whose field and tag reads are recorded: a loop may run on only while the
# predicate read no value it moves (it then takes the same path and returns the
# same answer on every scan in between).
_READ_TRACES: list[set[str]] = []
_READ_ALL = "*"  # iterated, compared, counted or evolved the whole tag map
_FIELD_READ = ":"  # prefix of a state field read (scan_id, timestamp, memory)


def _whole_map_read(name: str) -> Any:
    """A property for what every read of a map goes through: reading it is a read of all of it."""

    def get(self: Any) -> Any:
        if _READ_TRACES:
            _READ_TRACES[-1].add(_READ_ALL)
        return getattr(self._untraced, name)

    return property(get)


def _key_read(key: Any) -> Any:
    """The name a key reads, whatever a ``str`` subclass says it is when printed."""
    return str.__str__(key) if isinstance(key, str) else key


class _TracedTags(PMap):
    """A tag map that records which keys are read while a trace is active.

    A key read through the two methods here is recorded as that key.  Anything else that
    reads the map (iterating, counting, comparing, hashing, evolving, ``PMap.__getitem__``
    named directly, ``_buckets`` by name) goes through the two properties below, and that
    is a read of the whole map.  ``PMap``'s own slots stay empty, so a read past the
    properties raises, and a predicate that lets the raise out keeps the coast interpreted.
    """

    __slots__ = ("_untraced",)

    __class__ = property(lambda _self: PMap)
    _buckets = _whole_map_read("_buckets")
    _size = _whole_map_read("_size")

    def __getitem__(self, key: Any) -> Any:
        if _READ_TRACES:
            _READ_TRACES[-1].add(_key_read(key))
        return self._untraced[key]

    def __contains__(self, key: Any) -> bool:
        if _READ_TRACES:
            _READ_TRACES[-1].add(_key_read(key))
        return key in self._untraced


_TracedTags.__name__ = _TracedTags.__qualname__ = PMap.__name__


def _recorded_field(name: str) -> Any:
    """A property that records each read of a field of the state being traced."""
    read = _FIELD_READ + name

    def get(self: Any) -> Any:
        if _READ_TRACES:
            _READ_TRACES[-1].add(read)
        return getattr(self._untraced, name)

    return property(get)


class _TracedState(SystemState):
    """A SystemState whose field reads (and tag reads) are recorded.

    Every read through the state and map API is recorded: attributes, subscripts,
    ``items()``, equality, hashing, ``repr``, copying and evolving all come through these
    properties or the recording tag map in the ``tags`` slot.  The ``scan_id``,
    ``timestamp`` and ``memory`` slots stay empty, so a read past the properties raises,
    and a predicate that lets the raise out keeps the coast interpreted.

    The state and map answer ``__class__`` and ``__name__`` as the real ones do.

    An accepted limit: a predicate can overshoot a coast if it branches on the exact type or
    identity of the state or map (``type(s) is SystemState``), names the trace's own
    ``_untraced``, catches the AttributeError of a slot descriptor, or reads ``plc.playhead``
    (it moves with the scans, and is no read of a state).  The trace does not see what these
    read on a real state.  Lockstep catches them under ``--fold-check``.
    """

    __slots__ = ("_untraced",)

    __class__ = property(lambda _self: SystemState)
    scan_id = _recorded_field("scan_id")
    timestamp = _recorded_field("timestamp")
    memory = _recorded_field("memory")

    def __new__(cls, **fields: Any) -> Any:
        # What ``copy`` and ``type(s)(...)`` make: a plain state, from fields they read.
        return SystemState(**fields)

    def scanned(self, scan_id: int, timestamp: float, tags: PMap, memory: PMap) -> SystemState:
        # The same check as the base, then a plain state: these comparisons record the
        # fields, and a read of the result is a read of values already recorded.
        if (
            scan_id is self.scan_id
            and timestamp is self.timestamp
            and tags is self.tags
            and memory is self.memory
        ):
            return self
        return SystemState(scan_id=scan_id, timestamp=timestamp, tags=tags, memory=memory)


_TracedState.__name__ = _TracedState.__qualname__ = SystemState.__name__

_put_untraced: Any = cast(Any, _TracedState)._untraced.__set__
_put_untraced_map: Any = cast(Any, _TracedTags)._untraced.__set__


def _traced_state(state: SystemState) -> _TracedState:
    """A state answering as *state* does, recording what is read of it."""
    tags = object.__new__(_TracedTags)
    _put_untraced_map(tags, state.tags)
    traced = object.__new__(_TracedState)
    _put_tags(traced, tags)
    _put_untraced(traced, state)
    return traced


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
    traced = _traced_state(state)
    reads: set[str] = set()
    _READ_TRACES.append(reads)
    runner._state = traced
    try:
        hit = bool(predicate(traced))
    finally:
        runner._state = state
        _READ_TRACES.pop()
    return hit, frozenset(reads)
