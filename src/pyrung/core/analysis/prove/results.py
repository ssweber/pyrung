"""Result types for the prove subsystem.

Journal framework
-----------------
``prove(logic, condition, journal=True)`` attaches a ``Journal`` to the
result — a ``MappingProxyType[str, TagEntry]`` keyed by tag name,
recording every decision the pipeline made about each tag.

- ``Decision(pass_name, kind, outcome, reason, detail)`` — one decision
  from one pass.
- ``TagEntry(name, outcome, domain, domain_source, decisions)`` — final
  state of one tag.
- ``Journal`` supports ``[]``, ``in``, ``iter()``, ``len()``,
  ``str()``.  Also carries ``notes`` for skip_optimizations flags and
  depth truncation.

When ``journal=False`` (default), no ``Decision`` objects are created
and ``result.journal`` is ``None``.  Zero overhead on the default path.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any


@dataclass(frozen=True)
class Decision:
    """One decision made by the prover about a tag during the pass pipeline.

    kind vocabulary: classification, exclusion, domain, elision, absorption,
        absorption_skipped, absorption_blocked, input_partition,
        exclusive_group, pairing, recovery.
    outcome vocabulary: stateful, nondeterministic, combinational, infeasible,
        excluded, elided:provenance, elided:concrete, absorbed, recovered,
        edge_bearing, free, skipped, blocked.
    """

    pass_name: str
    kind: str
    outcome: str
    reason: str
    detail: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class TagEntry:
    """Per-tag journal of prover decisions."""

    name: str
    outcome: str
    domain: tuple[Any, ...] | None
    domain_source: str | None
    decisions: tuple[Decision, ...]


@dataclass(frozen=True)
class Journal:
    """Structured log of all prover decisions for a verification run."""

    tags: MappingProxyType[str, TagEntry]
    notes: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[TagEntry]:
        return iter(self.tags.values())

    def __getitem__(self, name: str) -> TagEntry:
        return self.tags[name]

    def __contains__(self, name: object) -> bool:
        return name in self.tags

    def __len__(self) -> int:
        return len(self.tags)

    def __str__(self) -> str:
        lines: list[str] = []
        if self.notes:
            lines.append("Notes:")
            for note in self.notes:
                lines.append(f"  - {note}")
            lines.append("")
        for entry in self:
            lines.append(f"{entry.name}: {entry.outcome}")
            if entry.domain is not None:
                lines.append(f"  domain: {entry.domain} (source: {entry.domain_source})")
            for d in entry.decisions:
                lines.append(f"  [{d.pass_name}] {d.kind} -> {d.outcome}: {d.reason}")
                if d.detail:
                    for k, v in d.detail:
                        lines.append(f"    {k}: {v}")
        return "\n".join(lines)

    if TYPE_CHECKING:

        def __hash__(self) -> int: ...


@dataclass(frozen=True)
class Proven:
    """Invariant holds across all reachable states."""

    states_explored: int
    caveats: tuple[str, ...] = ()
    journal: Journal | None = None
    aggressive_counterexample: Counterexample | None = None
    _debug_context: Any = None


@dataclass(frozen=True)
class Counterexample:
    """Invariant violated — trace reaches the failure, subject to caveats."""

    trace: list[TraceStep]
    caveats: tuple[str, ...] = ()
    journal: Journal | None = None
    _debug_context: Any = None


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
    journal: Journal | None = None
    _debug_context: Any = None


@dataclass(frozen=True)
class StateDiff:
    """Difference between two reachable state sets."""

    added: frozenset[frozenset[tuple[str, Any]]]
    removed: frozenset[frozenset[tuple[str, Any]]]


PENDING = "Pending"
