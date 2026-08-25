"""Immutable correction evidence emitted by verification-time excursion replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.overlay import PilotRung


@dataclass(frozen=True)
class _ConfirmedCorrection:
    """One replay-proven excursion correction with its exact executable lifetime."""

    identity: tuple[tuple[Any, ...], ...]
    pilot_rungs: tuple[PilotRung, ...]
    sources: tuple[str, ...]
    justification: str
