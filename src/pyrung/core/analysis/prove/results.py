"""Result types for the prove subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Proven:
    """Invariant holds across all reachable states."""

    states_explored: int
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class Counterexample:
    """Invariant violated — trace reaches the failure, subject to caveats."""

    trace: list[TraceStep]
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class TraceStep:
    inputs: dict[str, Any]
    scans: int = 1


@dataclass(frozen=True)
class _ParentLink:
    parent_key: tuple[Any, ...] | None
    inputs: dict[str, Any]
    scans: int
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class Intractable:
    """Verification cannot complete within resource bounds."""

    reason: str
    dimensions: int
    estimated_space: int
    tags: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StateDiff:
    """Difference between two reachable state sets."""

    added: frozenset[frozenset[tuple[str, Any]]]
    removed: frozenset[frozenset[tuple[str, Any]]]


PENDING = "Pending"
