"""Result types of the static facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Intractable:
    """The facts could not be built.

    ``tags`` names the tags at fault and ``hints`` says what to change.
    ``_debug_context`` holds the partial classification, which the pass pipeline
    keeps building on.
    """

    reason: str
    dimensions: int
    estimated_space: int
    tags: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    _debug_context: Any = None


# A timer's Done between off and done in how()'s domain prior and PILOT's
# world key (``absorb._done_acc_state``).
PENDING = "Pending"
