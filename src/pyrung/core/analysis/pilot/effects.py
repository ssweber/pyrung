"""Selected effect obligations and exact execution observations.

Selection policy and execution fact capture deliberately meet only at the
immutable :class:`EffectObligation`.  Trace/navigation decide the required
shape; the causal projection reports every exact occurrence without rebuilding
that policy from the landing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.pdg import resolve_rung

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import (
        OrderedEffectObservation,
        RungRead,
        RungWrite,
        ScanRungWriteProjection,
    )
    from pyrung.core.analysis.pdg import ProgramGraph

StaticRungAddress = tuple[str | None, int, tuple[int, ...]]
EffectDisposition = Literal[
    "ABSENT",
    "OVERWRITTEN",
    "STRANDED",
    "DISPLACED",
    "SURVIVED",
    "UNKNOWN",
]


def _detached(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, tuple | list):
        return tuple(_detached(member) for member in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_detached(member) for member in value), key=repr))
    if isinstance(value, Mapping):
        return MappingProxyType(
            {_detached(key): _detached(member) for key, member in value.items()}
        )
    return repr(value)


@dataclass(frozen=True)
class EffectPathStep:
    """One writer on a selected requirement-tree path.

    ``local_requirements`` are policy input, not recorded execution facts.
    They retain the writer's direct child requirements until the final
    producer/consumer pair is selected.
    """

    node_index: int
    tag: str
    value: Any
    local_requirements: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class EffectObligation:
    """One selected producer/effect promised to one obliged consumer."""

    tag: str
    value: Any
    producer: StaticRungAddress
    consumer: StaticRungAddress | None
    required_shape: tuple[tuple[str, Any], ...]
    boundary: tuple[str, Any] | None = None
    producer_rung: object = field(compare=False, repr=False, default=None)
    consumer_rung: object | None = field(compare=False, repr=False, default=None)


@dataclass(frozen=True)
class EffectExpectation:
    """Act-owned ordered conjunction of atomic obligations."""

    obligations: tuple[EffectObligation, ...]

    def __post_init__(self) -> None:
        if not self.obligations:
            raise ValueError("an effect expectation must contain an obligation")


@dataclass(frozen=True)
class EffectOccurrenceSnapshot:
    """Detached exact read/write occurrence retained by recording."""

    kind: Literal["read", "write"]
    ordinal: int
    scan_id: int
    run_order: int
    call_invocation: int | None
    rung: tuple[str | None, int]
    execution_kind: str
    caller_rung: int
    call_stack: tuple[str, ...]
    depth: int
    enabled: bool
    tag: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class EffectObligationSnapshot:
    tag: str
    value: Any
    producer: StaticRungAddress
    consumer: StaticRungAddress | None
    required_shape: tuple[tuple[str, Any], ...]
    boundary: tuple[str, Any] | None


@dataclass(frozen=True)
class EffectObservationSnapshot:
    """PLC-free factual result for one obligation occurrence."""

    disposition: EffectDisposition
    obligation: EffectObligationSnapshot
    appeared: EffectOccurrenceSnapshot | None = None
    consumer_read: EffectOccurrenceSnapshot | None = None
    displacement: EffectOccurrenceSnapshot | None = None
    displaced_read: EffectOccurrenceSnapshot | None = None
    observed_reads: tuple[EffectOccurrenceSnapshot, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class EffectObservation:
    """Internal exact observation, retaining projection occurrence identity."""

    obligation: EffectObligation
    disposition: EffectDisposition
    appeared: RungWrite | None = field(default=None, compare=False, repr=False)
    consumer_read: RungRead | None = field(default=None, compare=False, repr=False)
    displacement: RungWrite | None = field(default=None, compare=False, repr=False)
    displaced_read: RungRead | None = field(default=None, compare=False, repr=False)
    observed_reads: tuple[RungRead, ...] = field(default=(), compare=False, repr=False)
    detail: str = ""

    def diagnostic_snapshot(self) -> EffectObservationSnapshot:
        return EffectObservationSnapshot(
            disposition=self.disposition,
            obligation=obligation_snapshot(self.obligation),
            appeared=_write_snapshot(self.appeared) if self.appeared is not None else None,
            consumer_read=(
                _read_snapshot(self.consumer_read) if self.consumer_read is not None else None
            ),
            displacement=(
                _write_snapshot(self.displacement) if self.displacement is not None else None
            ),
            displaced_read=(
                _read_snapshot(self.displaced_read) if self.displaced_read is not None else None
            ),
            observed_reads=tuple(_read_snapshot(read) for read in self.observed_reads),
            detail=self.detail,
        )


def _writer_address(pdg: ProgramGraph, node_index: int) -> StaticRungAddress:
    node = pdg.rung_nodes[node_index]
    return (node.subroutine, node.rung_index, node.branch_path)


def required_shape(
    path: tuple[EffectPathStep, ...],
    pdg: ProgramGraph,
) -> tuple[tuple[str, Any], ...]:
    """Adjustable policy for the selected producer's consumer-local shape."""

    if len(path) < 2:
        return ()
    producer = path[-1]
    consumer = path[-2]
    node = pdg.rung_nodes[consumer.node_index]
    read_tags = node.condition_reads | node.guard_reads | node.data_reads
    candidates = [pair for pair in consumer.local_requirements if pair[0] in read_tags]
    effect_pair = (producer.tag, producer.value)
    if producer.tag in read_tags and effect_pair not in candidates:
        candidates.insert(0, effect_pair)
    # Occurrence order is part of the contract.  In particular, two reads of
    # the same tag/value are two handoffs, not one set member.
    return tuple(candidates)


def expectation_from_writer(
    pdg: ProgramGraph,
    program: Any,
    *,
    writer_node: int,
    tag: str,
    value: Any,
    consumer_node: int | None = None,
    required_shape: tuple[tuple[str, Any], ...] = (),
    boundary: tuple[str, Any] | None = None,
) -> EffectExpectation | None:
    """Mint an expectation from an already-selected static writer receipt.

    This adapter is intentionally narrower than trace lowering: route,
    program-step, awaited, and learned-edge readers must name their own writer
    rather than borrowing the first trace detail with the same physical pair.
    """

    rung_nodes = getattr(pdg, "rung_nodes", ())
    if writer_node < 0 or writer_node >= len(rung_nodes):
        return None
    producer_node = rung_nodes[writer_node]
    consumer = pdg.rung_nodes[consumer_node] if consumer_node is not None else None
    producer_rung = resolve_rung(program, producer_node)
    consumer_rung = resolve_rung(program, consumer) if consumer is not None else None
    if producer_rung is None or (consumer is not None and consumer_rung is None):
        return None
    return EffectExpectation(
        (
            EffectObligation(
                tag=tag,
                value=value,
                producer=_writer_address(pdg, writer_node),
                consumer=(
                    _writer_address(pdg, consumer_node) if consumer_node is not None else None
                ),
                required_shape=required_shape,
                boundary=boundary,
                producer_rung=producer_rung,
                consumer_rung=consumer_rung,
            ),
        )
    )


def expectation_from_selected_path(
    path: tuple[EffectPathStep, ...],
    pdg: ProgramGraph,
    program: Any,
    *,
    boundary: tuple[str, Any] | None,
) -> EffectExpectation | None:
    """Mint one final obligation while the exact selected path is available."""

    if not path:
        return None
    producer = path[-1]
    consumer = path[-2] if len(path) >= 2 else None
    producer_node = pdg.rung_nodes[producer.node_index]
    consumer_node = pdg.rung_nodes[consumer.node_index] if consumer is not None else None
    producer_rung = resolve_rung(program, producer_node)
    consumer_rung = resolve_rung(program, consumer_node) if consumer_node is not None else None
    if producer_rung is None or (consumer_node is not None and consumer_rung is None):
        return None
    obligation = EffectObligation(
        tag=producer.tag,
        value=producer.value,
        producer=_writer_address(pdg, producer.node_index),
        consumer=(_writer_address(pdg, consumer.node_index) if consumer is not None else None),
        required_shape=required_shape(path, pdg),
        boundary=boundary,
        producer_rung=producer_rung,
        consumer_rung=consumer_rung,
    )
    return EffectExpectation((obligation,))


def observe_expectation(
    expectation: EffectExpectation,
    projections: Iterable[ScanRungWriteProjection],
) -> tuple[EffectObservation, ...]:
    """Observe every exact appeared occurrence, or one ordinary ``ABSENT``.

    Bootstrap intentionally does not call this adapter: its designations are
    not promises and continue to intersect with appeared writes only.
    """

    projection_tuple = tuple(projections)
    result: list[EffectObservation] = []
    for obligation in expectation.obligations:
        appeared: list[tuple[ScanRungWriteProjection, OrderedEffectObservation]] = []
        for projection in projection_tuple:
            appeared.extend(
                (projection, observation)
                for observation in projection.observe_appeared_handoff(
                    obligation.tag,
                    obligation.value,
                    producer_rung=obligation.producer_rung,
                    consumer_rung=obligation.consumer_rung,
                    producer_address=obligation.producer,
                    consumer_address=obligation.consumer,
                    required_shape=obligation.required_shape,
                )
            )
        if not appeared:
            result.append(
                EffectObservation(
                    obligation,
                    "ABSENT",
                    detail="selected producer did not write the expected value",
                )
            )
            continue
        for projection, observation in appeared:
            later_writes = tuple(
                (later, write)
                for later in projection_tuple
                if later.scan_id > projection.scan_id
                for write in later.writes
                if write.run.enabled and write.transition.tag_name == obligation.tag
            )
            if (
                obligation.consumer_rung is None
                and observation.disposition == "SURVIVED"
                and later_writes
            ):
                later_projection, later_write = later_writes[0]
                result.append(
                    EffectObservation(
                        obligation=obligation,
                        disposition="OVERWRITTEN",
                        appeared=observation.appeared,
                        displacement=later_write,
                        observed_reads=later_projection.enabling_reads_observed_by_write(
                            later_write
                        ),
                        detail="a later corridor write replaced the terminal effect",
                    )
                )
                continue
            later_consumer_reads = tuple(
                read
                for later in projection_tuple
                if later.scan_id > projection.scan_id
                for run in later.runs
                if run.rung is obligation.consumer_rung
                for read in later.reads_for_run(run)
            )
            if observation.disposition == "STRANDED" and later_consumer_reads:
                result.append(
                    EffectObservation(
                        obligation=obligation,
                        disposition="UNKNOWN",
                        appeared=observation.appeared,
                        observed_reads=later_consumer_reads,
                        detail="consumer occurred in a later scan; cross-scan source continuity is unproved",
                    )
                )
            else:
                result.append(_from_ordered(obligation, observation))
    return tuple(result)


def observe_execution_window(
    expectation: EffectExpectation | None,
    fork: Any,
    *,
    scan_before: int,
    action_scan: int | None = None,
    coast_receipt: Any = None,
    timeline: Iterable[Any] = (),
) -> tuple[EffectObservation, ...]:
    """Observe only exact scans owned by the executed act.

    Edge release is execution setup, not the selected producer occurrence. A
    matching release-scan write therefore cannot satisfy or erase the promise
    made by the assertion. Pulse/Batch observations therefore read exactly the
    assertion scan. Coasts read only their recorded event and landing scans;
    unobserved or folded gaps remain ``UNKNOWN`` rather than being replayed as
    a complete logical corridor.
    """

    if expectation is None:
        return ()
    if action_scan is not None:
        projection = fork._replay_rung_write_projection_at(action_scan)
        if projection is not None:
            return observe_expectation(expectation, (projection,))
        return _unknown_observations(expectation, "assertion scan projection is unavailable")

    landing_scan = coast_receipt.end_scan if coast_receipt is not None else fork.state.scan_id
    exact_scan_ids = {
        event.scan for event in timeline if scan_before < event.scan <= fork.state.scan_id
    }
    if coast_receipt is not None:
        exact_scan_ids.update(
            event.scan
            for event in coast_receipt.events
            if scan_before < event.scan <= fork.state.scan_id
        )
        if scan_before < landing_scan <= fork.state.scan_id:
            exact_scan_ids.add(landing_scan)
    projections = tuple(
        projection
        for scan_id in sorted(exact_scan_ids)
        if (projection := fork._replay_rung_write_projection_at(scan_id)) is not None
    )
    complete_single_scan = (
        coast_receipt is not None
        and coast_receipt.logical_scans == 1
        and coast_receipt.skipped_scans == 0
        and len(projections) == 1
        and projections[0].scan_id == landing_scan
    )
    if complete_single_scan:
        return observe_expectation(expectation, projections)
    if not projections:
        return _unknown_observations(
            expectation,
            "coast has no exact recorded effect scan",
        )

    results: list[EffectObservation] = []
    for obligation in expectation.obligations:
        local_expectation = EffectExpectation((obligation,))
        observations = observe_expectation(local_expectation, projections)
        for observation in observations:
            if observation.disposition in {"OVERWRITTEN", "DISPLACED"}:
                results.append(observation)
            elif observation.disposition == "SURVIVED" and (
                obligation.consumer is not None
                or (
                    observation.appeared is not None
                    and observation.appeared.scan_id == landing_scan
                )
            ):
                results.append(observation)
            else:
                results.append(
                    EffectObservation(
                        obligation,
                        "UNKNOWN",
                        appeared=observation.appeared,
                        consumer_read=observation.consumer_read,
                        displacement=observation.displacement,
                        displaced_read=observation.displaced_read,
                        observed_reads=observation.observed_reads,
                        detail="coast corridor contains unobserved or folded scans",
                    )
                )
    return tuple(results)


def _unknown_observations(
    expectation: EffectExpectation,
    detail: str,
) -> tuple[EffectObservation, ...]:
    return tuple(
        EffectObservation(obligation, "UNKNOWN", detail=detail)
        for obligation in expectation.obligations
    )


def _from_ordered(
    obligation: EffectObligation,
    observation: OrderedEffectObservation,
) -> EffectObservation:
    return EffectObservation(
        obligation=obligation,
        disposition=observation.disposition,
        appeared=observation.appeared,
        consumer_read=observation.consumer_read,
        displacement=observation.displacement,
        displaced_read=observation.displaced_read,
        observed_reads=observation.observed_reads,
        detail=observation.detail,
    )


def obligation_snapshot(obligation: EffectObligation) -> EffectObligationSnapshot:
    return EffectObligationSnapshot(
        tag=obligation.tag,
        value=_detached(obligation.value),
        producer=obligation.producer,
        consumer=obligation.consumer,
        required_shape=tuple((tag, _detached(value)) for tag, value in obligation.required_shape),
        boundary=(
            (obligation.boundary[0], _detached(obligation.boundary[1]))
            if obligation.boundary is not None
            else None
        ),
    )


def expectation_snapshot(
    expectation: EffectExpectation | None,
) -> tuple[EffectObligationSnapshot, ...]:
    return (
        tuple(obligation_snapshot(obligation) for obligation in expectation.obligations)
        if expectation is not None
        else ()
    )


def _read_snapshot(read: RungRead) -> EffectOccurrenceSnapshot:
    return EffectOccurrenceSnapshot(
        kind="read",
        ordinal=read.ordinal,
        scan_id=read.scan_id,
        run_order=read.run_order,
        call_invocation=read.call_invocation,
        rung=(read.rung_id.subroutine, read.rung_id.rung_index),
        execution_kind=read.run.kind,
        caller_rung=read.run.caller_rung,
        call_stack=read.run.call_stack,
        depth=read.run.depth,
        enabled=read.run.enabled,
        tag=read.occurrence.name,
        values=(_detached(read.occurrence.value),),
    )


def _write_snapshot(write: RungWrite) -> EffectOccurrenceSnapshot:
    return EffectOccurrenceSnapshot(
        kind="write",
        ordinal=write.ordinal,
        scan_id=write.scan_id,
        run_order=write.run_order,
        call_invocation=write.call_invocation,
        rung=(write.rung_id.subroutine, write.rung_id.rung_index),
        execution_kind=write.run.kind,
        caller_rung=write.run.caller_rung,
        call_stack=write.run.call_stack,
        depth=write.run.depth,
        enabled=write.run.enabled,
        tag=write.transition.tag_name,
        values=(
            _detached(write.transition.from_value),
            _detached(write.transition.to_value),
        ),
    )
