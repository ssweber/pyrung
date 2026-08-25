"""Lazy Boolean branch contracts for controlling temporal needs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyrung.core.analysis.pilot.temporal_need as temporal_module
from pyrung.core.analysis.pilot.effects import EffectOccurrenceSnapshot
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
)
from pyrung.core.analysis.pilot.temporal_need import iter_temporal_need_branches
from pyrung.core.crossing import Cmp


def _atom(name: str, path: int) -> GuardRequirementAtom:
    return GuardRequirementAtom(
        Cmp(name, "==", True),
        (),
        SimpleNamespace(),
        (path,),
    )


class _Requirement:
    def __init__(
        self,
        label: str,
        condition: Any,
        *,
        occurrence: Any | None = None,
        authority: OperandAuthority = OperandAuthority.UNKNOWN,
        selected_writer: Any = None,
        obstruction_occurrence: Any = None,
    ) -> None:
        self.label = label
        self.condition = condition
        self.demanding_occurrence = occurrence or SimpleNamespace()
        self.operand_authority = authority
        self.selected_writer = selected_writer
        self.obstruction_occurrence = obstruction_occurrence

    @property
    def navigation_identity(self) -> tuple[str, str]:
        return ("requirement", self.label)


def test_top_level_and_keeps_nested_or_branches_separate() -> None:
    first = _atom("TemporalFirst", 0)
    second = _atom("TemporalSecond", 1)
    common = _atom("TemporalCommon", 2)
    requirements = (
        _Requirement("choice", GuardRequirementExpr(GuardLogic.ANY, (first, second))),
        _Requirement("common", common),
    )

    branches = tuple(iter_temporal_need_branches(requirements))  # type: ignore[arg-type]

    assert [[atom.condition.tag for atom in branch.atoms] for branch in branches] == [
        ["TemporalFirst", "TemporalCommon"],
        ["TemporalSecond", "TemporalCommon"],
    ]
    assert all(len(branch.atoms) == 2 for branch in branches)


def test_nested_and_is_never_split_into_subsets() -> None:
    first = _atom("TemporalJointFirst", 0)
    second = _atom("TemporalJointSecond", 1)
    sibling = _atom("TemporalSibling", 2)
    requirement = _Requirement(
        "choice",
        GuardRequirementExpr(
            GuardLogic.ANY,
            (GuardRequirementExpr(GuardLogic.ALL, (first, second)), sibling),
        ),
    )

    branches = tuple(iter_temporal_need_branches((requirement,)))  # type: ignore[arg-type]

    assert [[atom.condition.tag for atom in branch.atoms] for branch in branches] == [
        ["TemporalJointFirst", "TemporalJointSecond"],
        ["TemporalSibling"],
    ]


def test_branch_alternatives_are_consumed_lazily(monkeypatch: Any) -> None:
    consumed: list[int] = []
    first = _atom("TemporalLazyFirst", 0)
    second = _atom("TemporalLazySecond", 1)

    def alternatives(_condition: Any):
        consumed.append(1)
        yield (first,)
        consumed.append(2)
        yield (second,)

    monkeypatch.setattr(temporal_module, "iter_guard_alternatives", alternatives)
    requirement = _Requirement(
        "lazy",
        GuardRequirementExpr(GuardLogic.ANY, (first, second)),
    )

    branches = iter_temporal_need_branches((requirement,))  # type: ignore[arg-type]

    assert next(branches).atoms[0].condition.tag == "TemporalLazyFirst"
    assert consumed == [1]
    assert next(branches).atoms[0].condition.tag == "TemporalLazySecond"
    assert consumed == [1, 2]


def _occurrence(*, ordinal: int, scan_id: int = 7) -> EffectOccurrenceSnapshot:
    return EffectOccurrenceSnapshot(
        kind="read",
        ordinal=ordinal,
        scan_id=scan_id,
        run_order=ordinal,
        call_invocation=None,
        rung=(None, 4),
        execution_kind="rung",
        caller_rung=4,
        call_stack=(),
        depth=0,
        enabled=True,
        tag="SharedGuard",
        values=(100,),
        branch_path=(0,),
    )


def test_logical_aliases_share_one_occurrence_demand_with_all_supporters() -> None:
    occurrence = _occurrence(ordinal=12)
    obstruction = EffectOccurrenceSnapshot(
        **{
            **vars(occurrence),
            "kind": "write",
            "ordinal": 13,
            "values": (10, 22),
        }
    )
    condition = Cmp("SharedGuard", "!=", 100)
    terminal = _Requirement(
        "terminal-target",
        condition,
        occurrence=occurrence,
        authority=OperandAuthority.PROGRAM_WRITTEN,
        selected_writer=(None, 4, (0,)),
        obstruction_occurrence=obstruction,
    )
    nonterminal = _Requirement(
        "route-landing",
        condition,
        occurrence=occurrence,
        authority=OperandAuthority.PROGRAM_WRITTEN,
        selected_writer=(None, 4, (0,)),
        obstruction_occurrence=obstruction,
    )

    branch = next(iter_temporal_need_branches((terminal, nonterminal)))  # type: ignore[arg-type]

    assert len(branch.atoms) == 2
    assert len(branch.occurrence_demands) == 1
    demand = branch.occurrence_demands[0]
    assert demand.condition == condition
    assert demand.occurrence is occurrence
    assert demand.supporting_requirements == (terminal, nonterminal)
    assert demand.operand_authorities == frozenset((OperandAuthority.PROGRAM_WRITTEN,))
    assert demand.selected_writers == ((None, 4, (0,)),)
    assert demand.obstruction_occurrences == (obstruction,)


def test_same_condition_at_distinct_occurrences_remains_two_physical_demands() -> None:
    condition = Cmp("SharedGuard", "!=", 100)
    earlier = _Requirement(
        "earlier",
        condition,
        occurrence=_occurrence(ordinal=12),
        authority=OperandAuthority.PROGRAM_WRITTEN,
    )
    later = _Requirement(
        "later",
        condition,
        occurrence=_occurrence(ordinal=19),
        authority=OperandAuthority.PROGRAM_WRITTEN,
    )

    branch = next(iter_temporal_need_branches((earlier, later)))  # type: ignore[arg-type]

    assert len(branch.occurrence_demands) == 2
    assert tuple(demand.occurrence.ordinal for demand in branch.occurrence_demands) == (12, 19)


def test_occurrence_demand_retains_conflicting_authority_instead_of_picking_a_winner() -> None:
    occurrence = _occurrence(ordinal=12)
    condition = Cmp("SharedGuard", "!=", 100)
    program_owned = _Requirement(
        "program-owned",
        condition,
        occurrence=occurrence,
        authority=OperandAuthority.PROGRAM_WRITTEN,
    )
    configured = _Requirement(
        "configured",
        condition,
        occurrence=occurrence,
        authority=OperandAuthority.CONFIGURED,
    )

    demand = next(
        iter_temporal_need_branches((program_owned, configured))  # type: ignore[arg-type]
    ).occurrence_demands[0]

    assert demand.supporting_requirements == (program_owned, configured)
    assert demand.operand_authorities == frozenset(
        (OperandAuthority.PROGRAM_WRITTEN, OperandAuthority.CONFIGURED)
    )
