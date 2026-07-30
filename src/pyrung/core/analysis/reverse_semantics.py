"""Shared structural semantics for crossing reverse results.

Crossing consumers may support different constraint kinds, but they must agree
on the Boolean structure surrounding those constraints.  This module owns that
small common layer: fallthrough is inert, an empty ``Eq`` makes its conjunction
impossible, an empty conjunction makes the whole DNF true, and surviving DNF
branches retain their grouping.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from pyrung.core.crossing import Constraint, Eq, ReverseResult


class ReverseShape(Enum):
    """The normalized top-level meaning of a :class:`ReverseResult`."""

    FALLTHROUGH = auto()
    CONTRADICTION = auto()
    TRIVIAL = auto()
    CONSTRAINED = auto()


@dataclass(frozen=True)
class NormalizedReverse:
    """A reverse result with common DNF identities resolved."""

    shape: ReverseShape
    branches: tuple[tuple[Constraint, ...], ...] = ()
    exact: bool = False

    @property
    def fallthrough(self) -> bool:
        return self.shape is ReverseShape.FALLTHROUGH

    @property
    def contradiction(self) -> bool:
        return self.shape is ReverseShape.CONTRADICTION

    @property
    def trivial(self) -> bool:
        return self.shape is ReverseShape.TRIVIAL


def normalize_reverse_result(result: ReverseResult) -> NormalizedReverse:
    """Normalize DNF identities without interpreting constraint kinds.

    Contradictory conjunctions are removed from the disjunction.  A surviving
    empty conjunction dominates every other branch because ``True OR x`` is
    true.  Other branches are returned unchanged and in their original order.
    """
    if result.fallthrough:
        return NormalizedReverse(ReverseShape.FALLTHROUGH)

    live: list[tuple[Constraint, ...]] = []
    for branch in result.branches:
        if any(isinstance(c, Eq) and not c.values for c in branch):
            continue
        if not branch:
            return NormalizedReverse(
                ReverseShape.TRIVIAL,
                branches=((),),
                exact=result.exact,
            )
        live.append(branch)

    if not live:
        return NormalizedReverse(
            ReverseShape.CONTRADICTION,
            exact=result.exact,
        )
    return NormalizedReverse(
        ReverseShape.CONSTRAINED,
        branches=tuple(live),
        exact=result.exact,
    )


__all__ = [
    "NormalizedReverse",
    "ReverseShape",
    "normalize_reverse_result",
]
