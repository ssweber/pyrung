"""Immutable evidence emitted by legacy correction investigation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.overlay import PilotRung


@dataclass(frozen=True)
class _ConfirmedCorrection:
    """One replay-proven correction, including its exact executable lifetime."""

    identity: tuple[tuple[Any, ...], ...]
    pilot_rungs: tuple[PilotRung, ...]
    sources: tuple[str, ...]
    justification: str
