"""Read activation semantics for exact ladder instruction occurrences.

This module interprets program structure and executor memory only.  It does not
choose routes, create navigation actions, or execute scans.  Callers use its
immutable readings to decide whether an exact writer is currently armed or
whether its rung must first be made false for an ordinary scan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pyrung.core.analysis.write_sites import instruction_writes_tag


class OneShotState(StrEnum):
    """Current executor state of the exact instructions writing one effect."""

    NOT_ONESHOT = "not_oneshot"
    ARMED = "armed"
    SPENT = "spent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActivationReading:
    """Activation reading for every exact instruction writing one rung effect."""

    tag: str
    oneshot_state: OneShotState
    memory_keys: tuple[str, ...] = ()

    @property
    def needs_rearm(self) -> bool:
        """Whether an ordinary false-rung scan is required before assertion."""

        return self.oneshot_state is OneShotState.SPENT


def _walk_instructions(instructions: Iterable[Any]) -> Iterable[Any]:
    for instruction in instructions:
        yield instruction
        children = getattr(instruction, "instructions", None)
        if children is not None:
            yield from _walk_instructions(children)


def read_activation(
    rung: Any,
    tag: str,
    memory: Mapping[str, Any] | None,
) -> ActivationReading:
    """Read one rung's exact writers of *tag* against executor memory.

    Mixed or structurally opaque writer sets remain ``UNKNOWN``.  In
    particular, an ordinary writer beside a one-shot writer is not treated as
    a spent occurrence merely because one instruction's private bit is set.
    """

    writers = tuple(
        instruction
        for instruction in _walk_instructions(getattr(rung, "_instructions", ()))
        if instruction_writes_tag(instruction, tag)
    )
    if not writers:
        return ActivationReading(tag, OneShotState.UNKNOWN)
    if not all(bool(getattr(instruction, "oneshot", False)) for instruction in writers):
        state = (
            OneShotState.NOT_ONESHOT
            if all(not bool(getattr(instruction, "oneshot", False)) for instruction in writers)
            else OneShotState.UNKNOWN
        )
        return ActivationReading(tag, state)

    keys = tuple(instruction.memory_key("_oneshot") for instruction in writers)
    if memory is None or any(key not in memory for key in keys):
        return ActivationReading(tag, OneShotState.UNKNOWN, keys)
    spent = tuple(memory.get(key) is True for key in keys)
    if all(spent):
        return ActivationReading(tag, OneShotState.SPENT, keys)
    if not any(spent):
        return ActivationReading(tag, OneShotState.ARMED, keys)
    return ActivationReading(tag, OneShotState.UNKNOWN, keys)
