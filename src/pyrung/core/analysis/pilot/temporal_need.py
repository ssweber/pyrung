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
    OperandAuthority,
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
    def occurrence(self) -> Any:
        """Exact read occurrence at which this scalar condition was demanded."""

        if self.guard_atom is not None:
            return self.guard_atom.deadline
        return self.requirement.demanding_occurrence

    @property
    def operand_authority(self) -> OperandAuthority:
        """Ownership attached to this exact scalar occurrence."""

        if self.guard_atom is not None:
            return self.guard_atom.operand_authority
        return self.requirement.operand_authority

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
    def occurrence_demands(self) -> tuple[OccurrenceDemand, ...]:
        """Physical scalar demands, retaining every logical supporter."""

        return _occurrence_demands(self.atoms)

    @property
    def identity(self) -> tuple[Any, ...]:
        return ("temporal-need-branch", tuple(atom.identity for atom in self.atoms))


@dataclass(frozen=True)
class OccurrenceDemand:
    """One physical missing occurrence supported by one or more obligations.

    A terminal and a nonterminal obligation can observe the same false scalar
    read.  They remain distinct logical owners, but they are not two physical
    traceback hops.  This read model makes that relationship explicit before
    Compass chooses any navigation action.
    """

    condition: Any
    occurrence: Any
    supporting_atoms: tuple[TemporalNeedAtom, ...]

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            "occurrence-demand",
            _semantic_key(self.condition),
            _semantic_key(self.occurrence),
        )

    @property
    def supporting_requirements(self) -> tuple[ActiveRequirement, ...]:
        """All distinct live requirements which own this physical demand."""

        retained: list[ActiveRequirement] = []
        identities: list[Any] = []
        for atom in self.supporting_atoms:
            identity = _semantic_key(atom.requirement.navigation_identity)
            if identity in identities:
                continue
            identities.append(identity)
            retained.append(atom.requirement)
        return tuple(retained)

    @property
    def operand_authorities(self) -> frozenset[OperandAuthority]:
        """Every ownership claim; conflicts remain visible and fail closed."""

        return frozenset(atom.operand_authority for atom in self.supporting_atoms)

    @property
    def selected_writers(self) -> tuple[Any, ...]:
        """Every distinct writer designation retained by the supporters."""

        writers: list[Any] = []
        identities: list[Any] = []
        for requirement in self.supporting_requirements:
            writer = getattr(requirement, "selected_writer", None)
            identity = _semantic_key(writer)
            if identity in identities:
                continue
            identities.append(identity)
            writers.append(writer)
        return tuple(writers)

    @property
    def obstruction_occurrences(self) -> tuple[Any, ...]:
        """All exact harmful writes claimed by the logical supporters."""

        occurrences: list[Any] = []
        identities: list[Any] = []
        for requirement in self.supporting_requirements:
            occurrence = getattr(requirement, "obstruction_occurrence", None)
            if occurrence is None:
                continue
            identity = _semantic_key(occurrence)
            if identity in identities:
                continue
            identities.append(identity)
            occurrences.append(occurrence)
        return tuple(occurrences)


def _occurrence_demands(
    atoms: Sequence[TemporalNeedAtom],
) -> tuple[OccurrenceDemand, ...]:
    """Group logical aliases only when condition and exact occurrence agree."""

    groups: list[tuple[tuple[Any, ...], list[TemporalNeedAtom]]] = []
    for atom in atoms:
        key = (
            _semantic_key(atom.condition),
            _semantic_key(atom.occurrence),
        )
        matching = next((group for group in groups if group[0] == key), None)
        if matching is None:
            groups.append((key, [atom]))
        else:
            matching[1].append(atom)
    return tuple(
        OccurrenceDemand(
            condition=group[0].condition,
            occurrence=group[0].occurrence,
            supporting_atoms=tuple(group),
        )
        for _key, group in groups
    )


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
