"""Plain immutable identities and contracts for one Pilot execution boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count

_CHECKPOINT_REFERENCE_VALUES = count(1)


def _new_checkpoint_reference_value() -> int:
    return next(_CHECKPOINT_REFERENCE_VALUES)


@dataclass(frozen=True, order=True)
class CheckpointRef:
    """Stable identity of one retained source before an Epoch owns it."""

    value: int = field(default_factory=_new_checkpoint_reference_value)
