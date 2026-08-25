"""Persistent executable World values and their rollback checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrsistent import PRecord
from pyrsistent import field as _precord_field

from pyrung.core.analysis.pilot.execution import CheckpointRef, ExecutionPoint
from pyrung.core.analysis.pilot.world_key import _StateKey

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.navigation_contracts import BearingObjective


class _World(PRecord):
    """The persistent, revertible half of Pilot state.

    The runner is mutable, but each checkpoint retains a dedicated fork. The
    surrounding operation journal, trend, overlay, and dwell credit are
    persistent values restored together by pointer assignment.
    """

    work = _precord_field()
    committed_acts = _precord_field()
    best_trend = _precord_field()
    pilot_rungs = _precord_field()
    dwell_scans = _precord_field()

    def execution_at(self, scan_id: int) -> ExecutionPoint | None:
        """Resolve one exact scan through its committed operation owner."""

        matches = tuple(
            point for act in self.committed_acts if (point := act.point_at(scan_id)) is not None
        )
        if len(matches) > 1:
            raise RuntimeError("one physical scan belongs to multiple committed executions")
        return matches[0] if matches else None


@dataclass(frozen=True, eq=False)
class _CheckpointOwner:
    """Stable identity for one rollback receipt as its World changes."""

    reference: CheckpointRef = field(default_factory=CheckpointRef)


@dataclass(frozen=True)
class _Checkpoint:
    """A trend-qualified rollback anchor retaining one exact World value."""

    key: _StateKey
    world: _World
    trend: int
    objective: BearingObjective
    owner: _CheckpointOwner = field(
        default_factory=_CheckpointOwner,
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class _RecoveryOrigin:
    """Exact rollback owner and bounded incident evidence for one recovery."""

    checkpoint_owner: _CheckpointOwner
    anchor_scan: int
    before_snap: Mapping[str, Any]


@dataclass(frozen=True)
class _CausalCheckpoint:
    """A target-owned source boundary retained before a progress judgment."""

    key: _StateKey | None
    world: _World
    objective: BearingObjective
    configured_inputs: frozenset[str] = frozenset()
    owner: _CheckpointOwner = field(
        default_factory=_CheckpointOwner,
        compare=False,
        repr=False,
    )
