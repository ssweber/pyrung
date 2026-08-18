"""Immutable requests and results for one backward Trace read.

These values describe a trace without owning the recursive walk that produces
it.  The recursion engine in ``trace.py`` lowers a ``TraceReadConstraints``
value to its private environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class UnsupportedConstruct(Exception):
    """Trace encountered a program construct for which it has no read rule.

    Raised at read time and caught at exactly one drive boundary in
    ``pilot.py``; ``recording.py`` renders the caret/source diagnostic. Test
    mode propagates the exception; drive mode degrades to a named terminal
    result instead of probing a construct the reader did not understand.
    """

    def __init__(
        self,
        construct_kind: str,
        unsupported: Any,
        provenance: tuple[str, ...] = (),
    ) -> None:
        self.construct_kind = construct_kind
        self.unsupported = unsupported
        self.provenance = provenance
        self.source_file = getattr(unsupported, "source_file", None)
        self.source_line = getattr(unsupported, "source_line", None)
        name = type(unsupported).__name__
        context = f" at {provenance[-1]}" if provenance else ""
        super().__init__(f"unsupported {construct_kind} {name}{context}")


@dataclass(frozen=True)
class DomainPrior:
    """Prover-derived domain prior for resolving inequality atoms.

    ``nd_domains`` maps a free input to its domain; ``stateful_domains`` maps a
    program-owned tag to values its writers can produce; and ``func_deps`` is
    the affine projection map for derived scratch. These resolve inequalities
    to reachable satisfying values. Empty priors retain snapshot-boundary
    behavior: the prior aids completeness but carries no correctness authority.
    """

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    stateful_domains: dict[str, tuple[Any, ...]] | None = None
    func_deps: dict[str, tuple[str, int, Any]] | None = None


def _compact_route(route: tuple[str, ...], *, max_items: int = 8) -> tuple[str, ...]:
    """Keep a route label readable without discarding its endpoints."""

    compact: list[str] = []
    seen: set[str] = set()
    for item in route:
        if item in seen:
            continue
        seen.add(item)
        compact.append(item)
    if len(compact) <= max_items:
        return tuple(compact)
    head = compact[: max_items - 2]
    return (*head, "...", compact[-1])


@dataclass(frozen=True)
class TraceChoice:
    """One enumerated route through a multi-writer or OR trace.

    ``route_condition`` is the concrete pair that distinguishes the route and
    may be named by ``avoid=`` to exclude it.
    """

    id: str
    label: str
    route: tuple[str, ...]
    writer_locks: tuple[tuple[str, Any, int], ...] = ()
    or_locks: tuple[tuple[str, str, int], ...] = ()
    route_condition: tuple[str, Any] | None = None

    def __str__(self) -> str:
        detail = " -> ".join(_compact_route(self.route))
        return f"route={self.id}: {self.label}" + (f" ({detail})" if detail else "")

    def writer_lock_map(self) -> dict[tuple[str, Any], int]:
        return {(tag, value): rung for tag, value, rung in self.writer_locks}

    def or_lock_map(self) -> dict[tuple[str, str], int]:
        return {(tag, key): index for tag, key, index in self.or_locks}


@dataclass(frozen=True)
class TraceReadConstraints:
    """The complete caller-owned constraint set for one backward trace read."""

    clear_only: frozenset[str] = frozenset()
    opaque_loop: frozenset[str] = frozenset()
    pipeline_internal_tags: frozenset[str] = frozenset()
    route: TraceChoice | None = None
    prior: DomainPrior | None = None
    avoid_pred: Any = None
    rejected_actions: frozenset[tuple[str, Any]] = frozenset()
    # Recovery constraints participate in read identity and later action
    # admission. Trace never turns them into assignments.
    active_requirements: tuple[Any, ...] = ()
    harness: Any = None
    execution_memory: Mapping[str, Any] | None = None

    @classmethod
    def from_context(
        cls,
        ctx: Any,
        work: Any,
        *,
        route: TraceChoice | None,
        avoid_pred: Any,
        rejected_actions: frozenset[tuple[str, Any]] = frozenset(),
    ) -> TraceReadConstraints:
        """Read the invariant trace constraints from an explicit Pilot context."""

        return cls(
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=route,
            prior=ctx.domain_prior,
            avoid_pred=avoid_pred,
            rejected_actions=rejected_actions,
            active_requirements=tuple(getattr(ctx, "active_requirements", ())),
            harness=getattr(work, "_harness", None),
            execution_memory=getattr(getattr(work, "state", None), "memory", None),
        )
