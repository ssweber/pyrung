"""Immutable records for the lifecycle of confirmed corrective overlays."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.overlay import PilotRung
    from pyrung.core.analysis.pilot.world_key import _StateKey


class CorrectionStatus(Enum):
    """Evidence maturity for an investigation-owned overlay."""

    PROBATIONARY = "probationary"
    ACTIVE = "active"
    REVOKED = "revoked"

    @property
    def effective(self) -> bool:
        """Whether the correction still participates in the live overlay."""

        return self is not CorrectionStatus.REVOKED


@dataclass(frozen=True)
class _CorrectionReceipt:
    """Bounded replay proof and lifecycle for one investigation correction."""

    receipt_id: int
    origin_key: _StateKey
    correction: _ConfirmedCorrection
    status: CorrectionStatus = CorrectionStatus.PROBATIONARY
    admitted_origins: frozenset[_StateKey] = frozenset()

    @property
    def identity(self) -> tuple[tuple[Any, ...], ...]:
        return self.correction.identity

    @property
    def pilot_rungs(self) -> tuple[PilotRung, ...]:
        return self.correction.pilot_rungs

    @property
    def sources(self) -> tuple[str, ...]:
        return self.correction.sources

    @property
    def justification(self) -> str:
        return self.correction.justification


@dataclass(frozen=True)
class _ConfirmedCorrection:
    """One replay-proven correction, including its exact executable lifetime."""

    identity: tuple[tuple[Any, ...], ...]
    pilot_rungs: tuple[PilotRung, ...]
    sources: tuple[str, ...]
    justification: str
