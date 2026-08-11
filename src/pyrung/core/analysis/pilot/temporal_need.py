"""Lazy current-world reading primitives for active temporal requirements.

This module normalizes requirement Boolean structure only.  It never executes
a PLC, chooses a producer, or retains an iterator between Compass reads.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.pilot.intrascan_schedule import iter_guard_alternatives
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardRequirementAtom,
    GuardRequirementExpr,
)
from pyrung.core.analysis.pilot.world_key import _semantic_key


@dataclass(frozen=True)
class TemporalNeedAtom:
    """One live requirement leaf in one exact logical alternative."""

    requirement: ActiveRequirement
    condition: Any
    source_path: tuple[int, ...] = ()
    guard_atom: GuardRequirementAtom | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            "temporal-need-atom",
            _semantic_key(self.requirement.navigation_identity),
            _semantic_key(self.condition),
            self.source_path,
        )


@dataclass(frozen=True)
class TemporalNeedBranch:
    """One complete conjunctive branch yielded from the active expression."""

    atoms: tuple[TemporalNeedAtom, ...]

    @property
    def identity(self) -> tuple[Any, ...]:
        return ("temporal-need-branch", tuple(atom.identity for atom in self.atoms))


def _requirement_alternatives(
    requirement: ActiveRequirement,
) -> Iterator[tuple[TemporalNeedAtom, ...]]:
    condition = requirement.condition
    if isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
        for alternative in iter_guard_alternatives(condition):
            yield tuple(
                TemporalNeedAtom(
                    requirement=requirement,
                    condition=atom.condition,
                    source_path=atom.source_path,
                    guard_atom=atom,
                )
                for atom in alternative
            )
        return
    yield (TemporalNeedAtom(requirement=requirement, condition=condition),)


def iter_temporal_need_branches(
    requirements: Sequence[ActiveRequirement],
) -> Iterator[TemporalNeedBranch]:
    """Yield complete top-level-AND branches without materializing the product.

    Multiple active requirements are simultaneous obligations and therefore
    form an implicit top-level conjunction.  Each requirement may itself carry
    an exact ``ALL``/``ANY`` expression.  The recursion is deliberately lazy:
    callers consume at most their current bounded Compass budget.
    """

    ordered = tuple(requirements)
    if not ordered:
        return

    def combine(
        index: int,
        prefix: tuple[TemporalNeedAtom, ...],
    ) -> Iterator[TemporalNeedBranch]:
        if index == len(ordered):
            yield TemporalNeedBranch(prefix)
            return
        for alternative in _requirement_alternatives(ordered[index]):
            yield from combine(index + 1, (*prefix, *alternative))

    yield from combine(0, ())
