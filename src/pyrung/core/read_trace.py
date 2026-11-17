"""What a stop predicate reads.

A coast may run scans without judging its predicate only while nothing that the
predicate read changes, so the coast has to know what the predicate reads.  A condition
says so itself (:func:`_extract_condition_reads`).  For a callable, the coast judges it
once on a state that records the field and tag reads (:func:`_traced_call`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from pyrsistent import PMap

from pyrung.core.state import SystemState, _put_tags

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


def _extract_condition_reads(condition: Any) -> frozenset[str]:
    """Return every tag name that a ``run_until`` condition reads, whatever its form.

    The names come from bare contacts and bare tags, ``rise``/``fall`` edges, the left side
    of a comparison, tag operands on the right side of a comparison, and both legs of
    ``ArithAtom`` compounds.  A junk name, such as a Char *value* operand that happens to be
    a string, is harmless.  It only makes a caller watch one name more.

    This function cannot see into an indirect or expression comparison.  For one, it returns
    a junk name in place of what the comparison reads.  A caller that needs the whole set
    of reads calls :func:`_exact_condition_reads`.
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
    """Return the tags that a condition reads, or None if it reads anything it does not name.

    A coast runs on while what its predicate reads holds still, so a read missing here
    runs the coast past the stop.  An indirect comparison reads a pointer and the slot that
    it selects.  An expression reads its terms.  When this function returns None, the caller
    uses :func:`_traced_call`, which records those reads.
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
            return not value.readonly  # a read-only tag lowers to its default, dropping the name
        return value is None or isinstance(value, (bool, int, float, str))
    return False


class _Traces(threading.local):
    """The reads of each stop predicate being judged, a stack for each thread.

    A run judges a predicate that it cannot see into on a state that records each field
    and tag read.  The run may skip judging the predicate only while nothing that the
    predicate read changes.  Each thread has its own stack, so one thread's predicate never
    records another thread's reads.
    """

    def __init__(self) -> None:
        self.stack: list[set[str]] = []


_TRACES = _Traces()
_READ_ALL = "*"  # iterated, compared, counted or evolved the whole tag map
_FIELD_READ = ":"  # prefix of a state field read (scan_id, timestamp, memory)


def _whole_map_read(name: str) -> Any:
    """Return a property that records a read of the whole map whenever the map is read."""

    def get(self: Any) -> Any:
        if traces := _TRACES.stack:
            traces[-1].add(_READ_ALL)
        return getattr(self._untraced, name)

    return property(get)


def _key_read(key: Any) -> Any:
    """Return the name that a key reads, whatever a ``str`` subclass prints for it."""
    return str.__str__(key) if isinstance(key, str) else key


class _TracedTags(PMap):
    """A tag map that records which keys are read while a trace is active.

    A read through the two methods here is recorded as a read of that key.  Anything else
    that reads the map goes through the two properties below, and that counts as a read of
    the whole map.  Examples are iterating, counting, comparing, hashing, evolving, naming
    ``PMap.__getitem__`` directly and naming ``_buckets``.  ``PMap``'s own slots stay empty,
    so a read that gets past the properties raises an error.  If a predicate lets the error
    out, the coast stays interpreted.
    """

    __slots__ = ("_untraced",)

    __class__ = property(lambda _self: PMap)
    _buckets = _whole_map_read("_buckets")
    _size = _whole_map_read("_size")

    def __getitem__(self, key: Any) -> Any:
        if traces := _TRACES.stack:
            traces[-1].add(_key_read(key))
        return self._untraced[key]

    def __contains__(self, key: Any) -> bool:
        if traces := _TRACES.stack:
            traces[-1].add(_key_read(key))
        return key in self._untraced


_TracedTags.__name__ = _TracedTags.__qualname__ = PMap.__name__


def _recorded_field(name: str) -> Any:
    """Return a property that records each read of a field of the state being traced."""
    read = _FIELD_READ + name

    def get(self: Any) -> Any:
        if traces := _TRACES.stack:
            traces[-1].add(read)
        return getattr(self._untraced, name)

    return property(get)


class _TracedState(SystemState):
    """A SystemState whose field reads (and tag reads) are recorded.

    The state API and the map API both record every read.  Attributes, subscripts,
    ``items()``, equality, hashing, ``repr``, copying and evolving all go through these
    properties or through the recording tag map in the ``tags`` slot.  The ``scan_id``,
    ``timestamp`` and ``memory`` slots stay empty, so a read that gets past the properties
    raises an error.  If a predicate lets the error out, the coast stays interpreted.

    The state and the map answer ``__class__`` and ``__name__`` as the real ones do.

    There are two limits that the trace accepts.  First, a predicate can overshoot a coast
    in three cases.  It branches on the exact type or identity of the state or map
    (``type(s) is SystemState``).  It names the trace's own ``_untraced``.  It catches the
    AttributeError of a slot descriptor.  The trace does not see what these read on a real
    state.  Lockstep catches them under ``--fold-check``.

    Second, a predicate that reads ``plc.playhead`` can also overshoot, and lockstep does not
    catch it.  The playhead moves with the scans and is not a read of a state.  A twin that
    is asked the same predicate reads the real runner's playhead, where the run landed.
    """

    __slots__ = ("_untraced",)

    __class__ = property(lambda _self: SystemState)
    scan_id = _recorded_field("scan_id")
    timestamp = _recorded_field("timestamp")
    memory = _recorded_field("memory")

    def __new__(cls, **fields: Any) -> Any:
        # ``copy`` and ``type(s)(...)`` call this.  They make a plain state from fields that
        # they have read.
        return SystemState(**fields)

    def scanned(self, scan_id: int, timestamp: float, tags: PMap, memory: PMap) -> SystemState:
        # Make the same check as the base class, then return a plain state.  These comparisons
        # record the fields, so a read of the result reads values that are already recorded.
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
    """Return a state that answers as *state* does and records what is read from it."""
    tags = object.__new__(_TracedTags)
    _put_untraced_map(tags, state.tags)
    traced = object.__new__(_TracedState)
    _put_tags(traced, tags)
    _put_untraced(traced, state)
    return traced


@contextmanager
def _reads_untraced() -> Iterator[None]:
    """Keep the reads inside the block out of the active trace.

    Use this for a predicate that a coast judges only on the scans it really executes.
    PILOT's opaque ``avoid=`` callables are like this.  Their reads must not bound the
    windows that skip the other scans.
    """
    _TRACES.stack.append(set())
    try:
        yield
    finally:
        _TRACES.stack.pop()


def _traced_call(
    predicate: Callable[[SystemState], bool], runner: PLC
) -> tuple[bool, frozenset[str]]:
    """Return ``predicate(runner.state)`` and every tag or state field that it read.

    During the call, the runner's tip is swapped for the traced state.  So the trace also
    covers a predicate that reads ``plc.state`` instead of its argument.
    """
    state = runner._state
    traced = _traced_state(state)
    reads: set[str] = set()
    _TRACES.stack.append(reads)
    runner._state = traced
    try:
        hit = bool(predicate(traced))
    finally:
        runner._state = state
        _TRACES.stack.pop()
    return hit, frozenset(reads)
