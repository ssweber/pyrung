"""Index instruction-owned channels and estimate their next boundary.

PILOT asks this module only whether a result channel has one unambiguous owner.
The owner's :class:`AdvanceProfile` then describes the next operation.  Route
selection and input ranking remain elsewhere.
"""

from __future__ import annotations

import functools
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import _condition_to_expr
from pyrung.core.instruction.advance import (
    AdvanceProfile,
    ConditionDemand,
)
from pyrung.core.validation._common import walk_instructions


@dataclass(frozen=True)
class AdvanceOwner:
    """One instruction or harness coupling that owns result channels."""

    profile: AdvanceProfile
    instruction: Any


@dataclass(frozen=True)
class AdvanceIndex:
    """Immutable channel ownership for one program and optional harness."""

    owners: Mapping[str, AdvanceOwner]
    conflicts: Mapping[str, tuple[AdvanceOwner, ...]]

    def resolve(self, channel: str | Any) -> AdvanceOwner | None:
        name = getattr(channel, "name", channel)
        return self.owners.get(str(name))

    def conflict(self, channel: str | Any) -> tuple[AdvanceOwner, ...]:
        name = getattr(channel, "name", channel)
        return self.conflicts.get(str(name), ())

    def conflict_message(self, channel: str | Any) -> str | None:
        name = str(getattr(channel, "name", channel))
        owners = self.conflict(name)
        if not owners:
            return None
        kinds = ", ".join(
            type(owner.instruction).__name__
            if owner.instruction is not None
            else "harness coupling"
            for owner in owners
        )
        return f"advance ownership for {name!r} is ambiguous: {kinds}"


@functools.lru_cache(maxsize=16)
def _program_owners(program: Any) -> tuple[AdvanceOwner, ...]:
    """Build program-owned profiles once per immutable program."""

    result: list[AdvanceOwner] = []
    for instruction in walk_instructions(program):
        profile = instruction.advance_profile()
        if profile is not None:
            result.append(AdvanceOwner(profile, instruction))
    return tuple(result)


def iter_advance_owners(program: Any, harness: Any = None) -> Iterator[AdvanceOwner]:
    """Yield every program and analog-coupling advance owner."""

    yield from _program_owners(program)
    if harness is not None:
        for profile in harness.advance_profiles():
            yield AdvanceOwner(profile, None)


def build_advance_index(program: Any, harness: Any = None) -> AdvanceIndex:
    """Return unambiguous ownership, omitting every conflicting channel."""

    candidates: dict[str, list[AdvanceOwner]] = {}
    for owner in iter_advance_owners(program, harness):
        for channel in owner.profile.channels:
            bucket = candidates.setdefault(channel.name, [])
            if owner not in bucket:
                bucket.append(owner)

    owners: dict[str, AdvanceOwner] = {}
    conflicts: dict[str, tuple[AdvanceOwner, ...]] = {}
    for name, channel_owners in candidates.items():
        if len(channel_owners) == 1:
            owners[name] = channel_owners[0]
        else:
            conflicts[name] = tuple(channel_owners)
    return AdvanceIndex(
        owners=MappingProxyType(owners),
        conflicts=MappingProxyType(conflicts),
    )


def estimate_owned_boundary_scans(plc: Any, boundary: Any) -> int | None:
    """Estimate a boundary using its owner and the runner's timing instruments."""

    owner = build_advance_index(
        plc.program,
        getattr(plc, "_harness", None),
    ).resolve(getattr(boundary, "tag", ""))
    if owner is None or owner.profile.linear is None:
        return None
    return owner.profile.linear.estimate_scans(
        boundary,
        plc.state.tags,
        plc._dt,
    )


def demand_holds(demand: ConditionDemand | None, snapshot: Mapping[str, Any]) -> bool:
    """Whether an owner-declared demand has its requested truth value."""

    if demand is None or demand.condition is None:
        return True
    actual = _eval_expr_from_state(_condition_to_expr(demand.condition), dict(snapshot))
    return actual is bool(demand.value)
