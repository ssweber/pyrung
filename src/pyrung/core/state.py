"""Immutable system state for PLC simulation.

The core data structure for the Redux-style architecture.
All state transitions produce new SystemState instances.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, cast

from pyrsistent import PMap, PTypeError, pmap

try:
    # What a state pickled through when it was a PRecord: keeping it keeps the format.
    from pyrsistent._checked_types import _restore_pickle
except ImportError:  # private to pyrsistent, which may move it

    def _restore_pickle(cls: Any, data: Any) -> Any:
        return cls.create(data, _factory_fields=set())


def snapshot_dict(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Plain-dict copy of ``mapping``, fast for a ``PMap``.

    ``dict(pmap)`` sees a non-dict mapping and calls ``keys()`` plus a Python
    ``__getitem__`` per key; ``iteritems()`` walks the buckets directly (about
    6x faster on a few thousand keys).  Any other mapping takes ``dict()``.
    """
    if isinstance(mapping, PMap):
        return cast("dict[str, Any]", dict(mapping.iteritems()))
    return dict(mapping)


_EMPTY: PMap = pmap()
_FIELD_TYPES: dict[str, type] = {"scan_id": int, "timestamp": float, "tags": PMap, "memory": PMap}


class SystemState(Mapping[str, Any]):
    """Immutable snapshot of PLC state at a point in time.

    A read-only mapping of its four fields, evolved with :meth:`set`,
    :meth:`with_tags` and :meth:`with_memory`.

    Attributes:
        scan_id: Monotonically increasing scan counter.
        timestamp: Simulation clock in seconds.
        tags: Immutable mapping of tag names to values (bool, int, float, str).
        memory: Immutable mapping for internal state (timers, counters, etc).
    """

    __slots__ = ("scan_id", "timestamp", "tags", "memory", "__weakref__")

    scan_id: int
    timestamp: float
    tags: PMap
    memory: PMap

    def __new__(
        cls,
        *,
        scan_id: int = 0,
        timestamp: float = 0.0,
        tags: PMap = _EMPTY,
        memory: PMap = _EMPTY,
        **unknown: Any,
    ) -> SystemState:
        if unknown:
            raise _unknown_field(cls, next(iter(unknown)))
        return _build(cls, scan_id, timestamp, tags, memory)

    @classmethod
    def create(
        cls, kwargs: Mapping[str, Any], _factory_fields: Any = None, ignore_extra: bool = False
    ) -> SystemState:
        """Build a state from a mapping of its fields; a state is returned as is."""
        if isinstance(kwargs, cls):
            return kwargs
        if ignore_extra:
            kwargs = {name: kwargs[name] for name in _FIELD_TYPES if name in kwargs}
        return cls(**kwargs)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"SystemState is immutable, cannot set {name!r}: use set()")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"SystemState is immutable, cannot delete {name!r}")

    def __getitem__(self, key: str) -> Any:
        if key in _FIELD_TYPES:
            return getattr(self, key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        return key in _FIELD_TYPES

    def __iter__(self) -> Iterator[str]:
        return iter(_FIELD_TYPES)

    def __len__(self) -> int:
        return len(_FIELD_TYPES)

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        if isinstance(other, SystemState):
            return (self.scan_id, self.timestamp, self.tags, self.memory) == (
                other.scan_id,
                other.timestamp,
                other.tags,
                other.memory,
            )
        if isinstance(other, Mapping):
            return dict(self.items()) == dict(other.items())
        return NotImplemented

    def __hash__(self) -> int:
        # The hash of a mapping with these items: a pmap of a state's fields hashes the same.
        return hash(frozenset(self.items()))

    def __repr__(self) -> str:
        fields = ", ".join(f"{name}={value!r}" for name, value in self.items())
        return f"{type(self).__name__}({fields})"

    def __reduce__(self) -> tuple[Any, ...]:
        return _restore_pickle, (self.__class__, dict(self))

    def set(self, *args: Any, **kwargs: Any) -> SystemState:
        """Return this state with fields replaced: ``set(name, value)`` or ``set(name=value)``."""
        return self.update({args[0]: args[1]} if args else kwargs)

    def update(self, *maps: Mapping[str, Any]) -> SystemState:
        """Return this state with the fields in *maps* replaced, the last map winning."""
        fields = dict(self.items())
        for changes in maps:
            for name, value in changes.items():
                if name not in _FIELD_TYPES:
                    raise _unknown_field(type(self), name)
                fields[name] = value
        return self.scanned(**fields)

    def with_tags(self, updates: Mapping[str, bool | int | float | str]) -> SystemState:
        """Return new state with updated tags. Original unchanged."""
        return self.set(tags=self.tags.update(updates))

    def with_memory(self, updates: dict[str, Any]) -> SystemState:
        """Return new state with updated memory. Original unchanged."""
        return self.set(memory=self.memory.update(updates))

    def next_scan(self, dt: float) -> SystemState:
        """Return new state for next scan cycle.

        Args:
            dt: Time delta in seconds to add to timestamp.
        """
        return self.scanned(self.scan_id + 1, self.timestamp + dt, self.tags, self.memory)

    def committed(self, tags: PMap, memory: PMap, dt: float) -> SystemState:
        """Return the state after a scan: these tags and memory, on the next scan."""
        return self.scanned(self.scan_id + 1, self.timestamp + dt, tags, memory)

    def scanned(self, scan_id: int, timestamp: float, tags: PMap, memory: PMap) -> SystemState:
        """Return this state with every field replaced, or itself when none of them changes."""
        if (
            scan_id is self.scan_id
            and timestamp is self.timestamp
            and tags is self.tags
            and memory is self.memory
        ):
            return self
        return _build(type(self), scan_id, timestamp, tags, memory)


# The slots themselves: they set a field on any state, a subclass that shadows the
# field with a property too, past the __setattr__ that refuses everyone else.
_slots: Any = SystemState
_put_scan_id = _slots.scan_id.__set__
_put_timestamp = _slots.timestamp.__set__
_put_tags = _slots.tags.__set__
_put_memory = _slots.memory.__set__
_new = object.__new__


def _build(cls: type, scan_id: int, timestamp: float, tags: PMap, memory: PMap) -> Any:
    """A new *cls* holding these fields, each checked for its declared type."""
    if not (
        isinstance(scan_id, int)
        and isinstance(timestamp, float)
        and isinstance(tags, PMap)
        and isinstance(memory, PMap)
    ):
        values = (scan_id, timestamp, tags, memory)
        for (name, expected), value in zip(_FIELD_TYPES.items(), values, strict=True):
            if not isinstance(value, expected):
                actual = type(value)
                raise PTypeError(
                    cls,
                    name,
                    {expected},
                    actual,
                    f"Invalid type for field {cls.__name__}.{name}, was {actual.__name__}",
                )
    state = _new(cls)
    _put_scan_id(state, scan_id)
    _put_timestamp(state, timestamp)
    _put_tags(state, tags)
    _put_memory(state, memory)
    return state


def _unknown_field(cls: type, name: str) -> AttributeError:
    return AttributeError(f"'{name}' is not among the specified fields for {cls.__name__}")
