"""Lazy Boolean branch contracts for controlling temporal needs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyrung.core.analysis.pilot.temporal_need as temporal_module
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
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
    def __init__(self, label: str, condition: Any) -> None:
        self.label = label
        self.condition = condition

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
