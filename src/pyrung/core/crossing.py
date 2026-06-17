"""Crossings — the projected reverse contract (Phase 2, low module).

A *crossing* answers: given a constraint ``tag == value`` on a written tag, what
input constraint follows?  This module holds only the data carried across that
boundary — the immutable :class:`CrossingContext` a consumer fills with what it
knows, and the :class:`ReverseResult` a crossing returns.  The per-instruction
reverse *logic* lives one layer up, in ``core/analysis/crossings/`` (the
registry), keyed by instruction class — it cannot live here (instructions sit
below analysis; an evidence-bearing handler would be an import cycle).

Two reverse mechanisms share this contract:

- **Recorded** (Phase 1, ``causal/crossings_recorded.py``) — mechanical read-diff
  over an observed scan; no semantics.
- **Projected** (Phase 2, the registry) — semantics-bearing per-instruction
  inversion, used when there is *no* observed scan (walker forward-planning,
  prover seeding).

Soundness (``prove/CLAUDE.md``): a reverse may **over**-approximate the allowed
input domain (a superset is safe) but never **under**-approximate.  A crossing
that cannot invert returns :data:`REVERSE_FALLTHROUGH` — "add no constraint,
defer to the caller" — which is the sound direction.

This module runtime-imports nothing from ``analysis/``; it depends only on the
standard library so it can sit below every consumer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Sentinel for "no forward value is known".  The forward protocol is locked but
#: reverse-first — the walker's interpreted fork is the forward oracle, so
#: ``forward`` is mostly ``UNKNOWN`` today.
UNKNOWN: Any = object()


@dataclass(frozen=True)
class ReverseResult:
    """The input constraints implied by a ``tag == value`` target on a writer.

    - ``constraints`` — ``(tag, allowed-values)`` pairs; each names a tag whose
      value is constrained to the given set for the target to hold.  The empty
      set is the **unsatisfiable** encoding: ``[(dest, frozenset())]`` means "no
      value works" (a structural blocker) — every crossing agrees on this.
    - ``exact`` — the constraints are necessary *and* sufficient.  ``False`` is a
      sound superset (the caller must still verify).
    - ``fallthrough`` — the crossing could not invert; the caller keeps its
      existing behaviour (routes to the counterfactual fallback, or simply adds
      nothing).  A fallthrough result carries no constraints.
    """

    constraints: list[tuple[str, frozenset[Any]]] = field(default_factory=list)
    exact: bool = False
    fallthrough: bool = False


#: The "could not invert" result.  Behaviourally inert — add no constraint.
REVERSE_FALLTHROUGH = ReverseResult(fallthrough=True)


@dataclass(frozen=True)
class CrossingContext:
    """What a consumer knows when it asks a crossing to reverse.

    Each consumer fills only the fields it has.  ``value_at_scan`` carries
    *recorded* evidence (a callable ``(tag, scan_id) -> value``); **projected /
    prover-path contexts must leave it ``None``** so recorded evidence cannot
    leak into seeding (asserted by tests).
    """

    snapshot: Mapping[str, Any] = field(default_factory=dict)
    tags_by_name: Mapping[str, Any] = field(default_factory=dict)
    nondeterministic_dims: frozenset[str] = frozenset()
    nd_domains: Mapping[str, tuple[Any, ...]] | None = None
    value_at_scan: Callable[[str, int], Any] | None = None
    scan_id: int | None = None
    bounds_index: Any | None = None  # reserved; no producer yet, unread
