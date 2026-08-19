"""Plain immutable contracts for one Pilot execution.

Navigation requests a run with a configuration and stop condition.  Epoch owns
the physical history.  :class:`ExecutionReceipt` is the single association
between that request and the exact history it produced.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from itertools import count
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.pilot.world_key import _semantic_key
from pyrung.core.runner import PLC, Epoch, EpochQuery

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.coast import CoastReceipt, CoastTriggerEvent
    from pyrung.core.analysis.pilot.effects import (
        ConsumerBoundary,
        EffectObservationSnapshot,
    )

_CHECKPOINT_REFERENCE_VALUES = count(1)


def _new_checkpoint_reference_value() -> int:
    return next(_CHECKPOINT_REFERENCE_VALUES)


@dataclass(frozen=True, order=True)
class CheckpointRef:
    """Stable identity of one retained source before an Epoch owns it."""

    value: int = field(default_factory=_new_checkpoint_reference_value)


@dataclass(frozen=True)
class ScanProgressReceipt:
    """Proof that one exact accepted scan advanced the selected working edge.

    ``productive_scan`` identifies S1, while ``landing_scan`` may be the one
    retained S2 look-ahead. The receipt is bounded to this execution; it is
    not a general promise that later scans are productive.
    """

    source_scan: int
    productive_scan: int
    landing_scan: int
    kind: Literal[
        "target",
        "selected-producer",
        "frontier",
        "earned-work",
        "conductivity",
        "observation",
        "intrascan-stage",
        "intrascan-direct",
    ]
    selected_act: tuple[Any, ...]
    distance_after: int | None = None
    landing_owns_tip: bool = True


@dataclass(frozen=True)
class InvestigationProducerReceipt:
    """Verified writer occurrence discharging one traceback producer goal."""

    frontier_id: tuple[Any, ...]
    producer_goal_id: tuple[Any, ...]
    assertion_scan: int
    write_identity: tuple[Any, ...]
    retained_assignment: tuple[str, Any]


@dataclass(frozen=True)
class IntrascanActReceipt:
    """Exact stage or consumer write accepted from one evidence-owned scan."""

    evidence_identity: tuple[Any, ...]
    kind: Literal["stage", "consumer"]
    assertion_scan: int
    expected_write_identity: tuple[Any, ...]
    matched_write_identity: tuple[Any, ...]
    retained_assignment: tuple[str, Any]


@dataclass(frozen=True)
class ScanEntryConfiguration:
    """Desired user-style values applied at the next execution boundary."""

    assignments: tuple[tuple[str, Any], ...]

    def __post_init__(self) -> None:
        assignments = tuple(self.assignments)
        if not assignments:
            raise ValueError("scan-entry configuration cannot be empty")
        names = tuple(tag for tag, _value in assignments)
        if len(set(names)) != len(names):
            raise ValueError("scan-entry configuration cannot assign one tag twice")
        object.__setattr__(self, "assignments", assignments)

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            "scan-entry-configuration",
            tuple((tag, _semantic_key(value)) for tag, value in self.assignments),
        )


class MotionKind(Enum):
    """Execution semantics of a trial; event labels remain diagnostic only."""

    INTERVENTION = "intervention"
    COAST_TO_BEARING = "coast-to-bearing"
    COAST_HOLDING_WORLD = "coast-holding-world"

    @property
    def is_coast(self) -> bool:
        return self is not MotionKind.INTERVENTION


@dataclass(frozen=True)
class ChannelMotion:
    """One requested channel boundary and verification's owned landing."""

    channel_tag: str | None = None
    target_value: Any = None
    boundary: Any = None
    stop_reason: str | None = None

    @property
    def active(self) -> bool:
        return self.channel_tag is not None

    @property
    def reached(self) -> bool:
        return self.stop_reason == "reached"

    @property
    def departed(self) -> bool:
        return self.stop_reason == "departed"


class PulseHorizon(StrEnum):
    """How far physical pulse execution may run before yielding to Compass."""

    ASSERTION_SCAN = "assertion_scan"
    CONSUMER_BOUNDARY = "consumer_boundary"
    LOOKAHEAD_SCAN = "lookahead_scan"


@dataclass(frozen=True)
class StopCondition:
    """The exact observation boundary requested for one execution."""

    horizon: PulseHorizon
    consumer_boundary: ConsumerBoundary | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if (self.horizon is PulseHorizon.CONSUMER_BOUNDARY) != (
            self.consumer_boundary is not None
        ):
            raise ValueError("consumer-bound stop condition requires one exact boundary")


@dataclass(frozen=True)
class StopReceipt:
    """Where execution actually yielded for its requested stop condition."""

    condition: StopCondition
    stopped_scan: int
    reached: bool

    @property
    def consumer_boundary_reached(self) -> bool | None:
        """Return a result only for an explicitly consumer-bound execution."""

        if self.condition.horizon is not PulseHorizon.CONSUMER_BOUNDARY:
            return None
        return self.reached


@dataclass(frozen=True)
class ExecutionSpan:
    """Exact kernel scans from one immutable Epoch owner."""

    owner: EpochQuery = field(repr=False)
    kernel_scan_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        scans = tuple(self.kernel_scan_ids)
        if not scans:
            raise ValueError("an execution span must own at least one kernel scan")
        if any(
            later <= earlier
            for earlier, later in zip(scans, scans[1:], strict=False)
        ):
            raise ValueError("execution span scans must be strictly increasing")
        epoch = self.owner.epoch
        if any(scan < epoch.first_scan or scan > epoch.last_scan for scan in scans):
            raise ValueError("execution span scan lies outside its Epoch")
        object.__setattr__(self, "kernel_scan_ids", scans)

    @property
    def epoch(self) -> Epoch:
        return self.owner.epoch

    @property
    def first_scan(self) -> int:
        return self.kernel_scan_ids[0]

    @property
    def last_scan(self) -> int:
        return self.kernel_scan_ids[-1]


def capture_execution_spans(
    fork: PLC,
    kernel_scan_ids: tuple[int, ...],
) -> tuple[ExecutionSpan, ...]:
    """Bind an executed scan stream to stable detached Epoch queries."""

    scans = tuple(kernel_scan_ids)
    if not scans:
        return ()
    if any(
        later <= earlier
        for earlier, later in zip(scans, scans[1:], strict=False)
    ):
        raise ValueError("execution scan stream must be strictly increasing")

    lineage = fork._causal_lineage
    spans: list[ExecutionSpan] = []
    for epoch, first_scan, last_scan in lineage.epochs_covering(scans[0], scans[-1]):
        owned = tuple(scan for scan in scans if first_scan <= scan <= last_scan)
        if owned:
            spans.append(
                ExecutionSpan(
                    owner=lineage._detached_query_for(epoch),
                    kernel_scan_ids=owned,
                )
            )
    if tuple(scan for span in spans for scan in span.kernel_scan_ids) != scans:
        raise ValueError("execution scan stream has no complete Epoch ownership")
    return tuple(spans)


def execution_owner(work: PLC, scan_id: int) -> EpochQuery | None:
    """Resolve one physical scan to its unique immutable Epoch owner."""

    matches = tuple(
        owner
        for epoch, owner in work._causal_lineage.seal_through(scan_id)
        if epoch.first_scan <= scan_id <= epoch.last_scan
    )
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ExecutionReceipt:
    """One steer/run/observe cycle and its exact physical evidence."""

    before_snap: Mapping[str, Any]
    after_snap: Mapping[str, Any]
    channel_motion: ChannelMotion
    coast_receipt: CoastReceipt | None
    timeline: tuple[CoastTriggerEvent, ...]
    effect_observations: tuple[EffectObservationSnapshot, ...] = ()
    replay_motion: ChannelMotion = field(default_factory=ChannelMotion)
    scan_progress: ScanProgressReceipt | None = None
    investigation_producer: InvestigationProducerReceipt | None = None
    intrascan_act: IntrascanActReceipt | None = None
    spans: tuple[ExecutionSpan, ...] = ()
    source_scan: int | None = None
    applied_configurations: tuple[ScanEntryConfiguration, ...] = ()
    stop: StopReceipt | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "before_snap", MappingProxyType(dict(self.before_snap)))
        object.__setattr__(self, "after_snap", MappingProxyType(dict(self.after_snap)))
        object.__setattr__(self, "timeline", tuple(self.timeline))
        object.__setattr__(self, "effect_observations", tuple(self.effect_observations))
        object.__setattr__(self, "spans", tuple(self.spans))
        object.__setattr__(self, "applied_configurations", tuple(self.applied_configurations))
        scans = self.kernel_scan_ids
        if scans and self.source_scan is not None and scans[0] <= self.source_scan:
            raise ValueError("execution receipt scans must follow their source")

    @property
    def accelerators(self) -> tuple[tuple[str, Any], ...]:
        return self.coast_receipt.advances if self.coast_receipt is not None else ()

    @property
    def kernel_scan_ids(self) -> tuple[int, ...]:
        return tuple(scan for span in self.spans for scan in span.kernel_scan_ids)

    @property
    def epoch_ref(self) -> Any:
        references = tuple(dict.fromkeys(span.epoch.reference for span in self.spans))
        if len(references) != 1:
            raise ValueError("execution receipt does not have exactly one Epoch reference")
        return references[0]

    @property
    def consumer_boundary_reached(self) -> bool | None:
        return self.stop.consumer_boundary_reached if self.stop is not None else None

    def owner_at(self, scan_id: int) -> EpochQuery | None:
        return next(
            (span.owner for span in self.spans if scan_id in span.kernel_scan_ids),
            None,
        )

    def point_at(self, scan_id: int) -> ExecutionPoint | None:
        """Return this execution's exact logical and physical owner at one scan."""

        owner = self.owner_at(scan_id)
        return ExecutionPoint(self, owner, scan_id) if owner is not None else None


@dataclass(frozen=True)
class ExecutionPoint:
    """One exact kernel scan inside one immutable execution receipt."""

    execution: ExecutionReceipt = field(repr=False)
    owner: EpochQuery = field(repr=False)
    scan_id: int

    def __post_init__(self) -> None:
        if self.execution.owner_at(self.scan_id) is not self.owner:
            raise ValueError("execution point owner does not own its scan")

    @property
    def epoch_ref(self) -> Any:
        return self.owner.epoch.reference
