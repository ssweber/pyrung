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
from typing import TYPE_CHECKING, Any

from pyrung.core.instruction.advance import (
    AdvanceProfile,
    AdvanceStep,
    Constraint,
    constraint_holds,
)
from pyrung.core.validation._common import walk_instructions

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

_DEFAULT_DT = 0.01
_MEASURE_BUDGET = 2000


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


def next_advance(
    channel: str,
    constraint: Constraint,
    snapshot: Mapping[str, Any],
    program: Any,
    harness: Any = None,
) -> tuple[AdvanceOwner, AdvanceStep] | None:
    """Plan one operation for an unambiguously owned channel."""

    owner = build_advance_index(program, harness).resolve(channel)
    if owner is None:
        return None
    step = owner.profile.plan(constraint, snapshot)
    return None if step is None else (owner, step)


def estimate_scans(
    owner: AdvanceOwner,
    constraint: Constraint,
    plc: PLC,
    *,
    fork: PLC | None = None,
    budget: int = _MEASURE_BUDGET,
) -> int | None:
    """Estimate scans to a boundary, measuring a prepared fork if necessary."""

    profile = owner.profile
    dt = float(getattr(plc, "_dt", _DEFAULT_DT) or _DEFAULT_DT)
    if profile.linear is not None:
        analytic = profile.linear.estimate_scans(constraint, plc.state.tags, dt)
        if analytic is not None:
            return analytic
    return None if fork is None else measure_scans(constraint, fork, budget=budget)


def measure_scans(
    constraint: Constraint,
    fork: PLC,
    *,
    budget: int = _MEASURE_BUDGET,
) -> int | None:
    """Run a prepared fork until the named boundary, without guessing."""

    if constraint_holds(constraint, fork.state.tags) is True:
        return 0
    start = fork.state.scan_id
    fork.run_until(
        lambda state: constraint_holds(constraint, state.tags) is True,
        max_cycles=budget,
        fold=True,
    )
    if constraint_holds(constraint, fork.state.tags) is not True:
        return None
    return fork.state.scan_id - start
