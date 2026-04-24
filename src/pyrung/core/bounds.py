"""Runtime bounds-check infrastructure for scan commit.

Precomputes a per-tag constraint index and provides a check function
that scans only written+constrained tags at commit time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.tag import Tag


@dataclass(frozen=True)
class TagConstraint:
    min: int | float | None
    max: int | float | None
    choices_keys: frozenset[int | float | str] | None


@dataclass(frozen=True)
class BoundsViolation:
    tag_name: str
    value: Any
    constraint: TagConstraint
    kind: str

    def __str__(self) -> str:
        if self.kind == "range":
            lo = self.constraint.min
            hi = self.constraint.max
            return f"BoundsViolation: {self.tag_name}={self.value!r} outside [{lo}, {hi}]"
        keys = self.constraint.choices_keys
        assert keys is not None
        return f"BoundsViolation: {self.tag_name}={self.value!r} not in choices {set(keys)}"


def build_constraint_index(
    tags_by_name: dict[str, Tag],
) -> dict[str, TagConstraint]:
    index: dict[str, TagConstraint] = {}
    for name, tag in tags_by_name.items():
        has_range = tag.min is not None or tag.max is not None
        has_choices = tag.choices is not None
        if has_range or has_choices:
            index[name] = TagConstraint(
                min=tag.min,
                max=tag.max,
                choices_keys=frozenset(tag.choices.keys()) if tag.choices else None,
            )
    return index


def check_bounds(
    pending: dict[str, Any],
    constrained: dict[str, TagConstraint],
) -> dict[str, BoundsViolation]:
    violations: dict[str, BoundsViolation] = {}

    if len(pending) < len(constrained):
        for name, value in pending.items():
            constraint = constrained.get(name)
            if constraint is None:
                continue
            v = _check_one(name, value, constraint)
            if v is not None:
                violations[name] = v
    else:
        for name, constraint in constrained.items():
            if name not in pending:
                continue
            v = _check_one(name, pending[name], constraint)
            if v is not None:
                violations[name] = v

    return violations


def _check_one(
    name: str,
    value: Any,
    constraint: TagConstraint,
) -> BoundsViolation | None:
    if constraint.min is not None or constraint.max is not None:
        try:
            if constraint.min is not None and value < constraint.min:
                return BoundsViolation(name, value, constraint, "range")
            if constraint.max is not None and value > constraint.max:
                return BoundsViolation(name, value, constraint, "range")
        except TypeError:
            return BoundsViolation(name, value, constraint, "range")

    if constraint.choices_keys is not None:
        if value not in constraint.choices_keys:
            return BoundsViolation(name, value, constraint, "choices")

    return None
