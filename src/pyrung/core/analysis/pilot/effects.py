"""Selected effect obligations and exact execution observations.

Selection policy and execution fact capture deliberately meet only at the
immutable :class:`EffectObligation`.  Trace/navigation decide the required
shape; the causal projection reports every exact occurrence without rebuilding
that policy from the landing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.causal._rung_writes import RungRead, RungWrite
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import (
        OrderedEffectObservation,
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

    @property
    def dynamic_address(self) -> tuple[Any, ...]:
        """Stable full address within one exact executed scan projection."""

        return (
            self.rung,
            self.execution_kind,
            self.caller_rung,
            self.call_stack,
            self.depth,
            self.call_invocation,
            self.run_order,
            self.ordinal,
        )


@dataclass(frozen=True)
class EffectEpochSnapshot:
    """Detached interval metadata for the Epoch which owns an observation."""

    first_scan: int
    last_scan: int
    initial_scan_id: int


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
    execution_epoch: EffectEpochSnapshot | None = None


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
    execution_epoch: Any = field(default=None, compare=False, repr=False)
    execution_owner: Any = field(default=None, compare=False, repr=False)
    # Internal live proof surface.  A later replay query may reconstruct equal
    # occurrences as different objects; Phase-4/5 exact inversion must consume
    # the very projection which produced these observation members.
    execution_projection: ScanRungWriteProjection | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def diagnostic_snapshot(self) -> EffectObservationSnapshot:
        return EffectObservationSnapshot(
            disposition=self.disposition,
            obligation=obligation_snapshot(self.obligation),
            appeared=occurrence_snapshot(self.appeared) if self.appeared is not None else None,
            consumer_read=(
                occurrence_snapshot(self.consumer_read) if self.consumer_read is not None else None
            ),
            displacement=(
                occurrence_snapshot(self.displacement) if self.displacement is not None else None
            ),
            displaced_read=(
                occurrence_snapshot(self.displaced_read)
                if self.displaced_read is not None
                else None
            ),
            observed_reads=tuple(occurrence_snapshot(read) for read in self.observed_reads),
            detail=self.detail,
            execution_epoch=(
                EffectEpochSnapshot(
                    self.execution_epoch.first_scan,
                    self.execution_epoch.last_scan,
                    self.execution_epoch.initial_scan_id,
                )
                if self.execution_epoch is not None
                else None
            ),
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
    consumer_node_index = _consumer_for_producer(path, pdg)
    if consumer_node_index is None:
        return ()
    node = pdg.rung_nodes[consumer_node_index]
    if hasattr(node, "subroutine") and hasattr(node, "rung_index"):
        consumer = next(
            step
            for step in reversed(path[:-1])
            if (
                getattr(pdg.rung_nodes[step.node_index], "subroutine", object()),
                getattr(pdg.rung_nodes[step.node_index], "rung_index", object()),
            )
            == (node.subroutine, node.rung_index)
        )
    else:
        consumer = next(
            step for step in reversed(path[:-1]) if step.node_index == consumer_node_index
        )
    read_tags = node.condition_reads | node.guard_reads | node.data_reads
    candidates = [pair for pair in consumer.local_requirements if pair[0] in read_tags]
    effect_pair = (producer.tag, producer.value)
    if producer.tag in read_tags and effect_pair not in candidates:
        candidates.insert(0, effect_pair)
    # Occurrence order is part of the contract.  In particular, two reads of
    # the same tag/value are two handoffs, not one set member.
    return tuple(candidates)


def _consumer_for_producer(
    path: tuple[EffectPathStep, ...],
    pdg: ProgramGraph,
) -> int | None:
    """Select the nearest path ancestor which actually reads the effect.

    A nested branch can contribute another writer step without consuming its
    child's produced tag.  Adjacency in the trace path is therefore not a
    producer/consumer proof; the PDG read footprint is.
    """

    if len(path) < 2:
        return None
    producer = path[-1]
    for candidate in reversed(path[:-1]):
        node = pdg.rung_nodes[candidate.node_index]
        reads = node.condition_reads | node.guard_reads | node.data_reads
        if producer.tag not in reads:
            continue
        # A branch node's structural footprint can include conditions executed
        # by its parent run.  Prefer the outermost same-rung ancestor which
        # reads the effect; that is the dynamic occurrence which observes the
        # inherited parent condition.  Keep the branch only when no ancestor
        # owns that read.
        branch_path = getattr(node, "branch_path", ())
        ancestors = tuple(
            (index, owner)
            for index, owner in enumerate(pdg.rung_nodes)
            if hasattr(node, "subroutine")
            and hasattr(node, "rung_index")
            and getattr(owner, "subroutine", object()) == node.subroutine
            and getattr(owner, "rung_index", object()) == node.rung_index
            and len(getattr(owner, "branch_path", ())) < len(branch_path)
            and branch_path[: len(getattr(owner, "branch_path", ()))]
            == getattr(owner, "branch_path", ())
            and producer.tag in (owner.condition_reads | owner.guard_reads | owner.data_reads)
        )
        if ancestors:
            return min(ancestors, key=lambda item: len(item[1].branch_path))[0]
        return candidate.node_index
    return None


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
    selected_pairs: tuple[tuple[str, Any], ...] = (),
    snapshot: Mapping[str, Any] | None = None,
    steerable: frozenset[str] = frozenset(),
) -> EffectExpectation | None:
    """Mint one final obligation while the exact selected path is available."""

    if not path:
        return None
    producer = path[-1]
    consumer_node_index = _consumer_for_producer(path, pdg)
    shape = required_shape(path, pdg)
    # Expectations are execution receipts, not forecasts for the rest of a
    # target trace.  Mint one only when this selected artifact makes the exact
    # producer runnable and its selected consumer is due now.  Otherwise a
    # fresh Orientation owns the later stage.  Returning a terminal producer
    # promise here would still reject ordinary prerequisite staging as ABSENT.
    if snapshot is not None:
        selected_state = dict(snapshot)
        selected_state.update(selected_pairs)
        producer_ready = all(
            _values_match(selected_state.get(tag), value)
            for tag, value in producer.local_requirements
        )
        consumer_ready = consumer_node_index is None or all(
            tag == producer.tag
            and _values_match(value, producer.value)
            or _values_match(selected_state.get(tag), value)
            for tag, value in shape
        )
        if not producer_ready or not consumer_ready:
            return None
    producer_node = pdg.rung_nodes[producer.node_index]
    consumer_node = pdg.rung_nodes[consumer_node_index] if consumer_node_index is not None else None
    producer_rung = resolve_rung(program, producer_node)
    consumer_rung = resolve_rung(program, consumer_node) if consumer_node is not None else None
    if producer_rung is None or (consumer_node is not None and consumer_rung is None):
        return None
    obligation = EffectObligation(
        tag=producer.tag,
        value=producer.value,
        producer=_writer_address(pdg, producer.node_index),
        consumer=(
            _writer_address(pdg, consumer_node_index) if consumer_node_index is not None else None
        ),
        required_shape=shape,
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
                    execution_projection=(
                        projection_tuple[0] if len(projection_tuple) == 1 else None
                    ),
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
                        execution_projection=later_projection,
                    )
                )
                continue
            if observation.disposition == "STRANDED":
                preceding_reads = _consumer_reads_preceding_write(
                    obligation,
                    observation.appeared,
                    projection,
                )
                if len(preceding_reads) > 1:
                    result.append(
                        EffectObservation(
                            obligation,
                            "UNKNOWN",
                            appeared=observation.appeared,
                            observed_reads=preceding_reads,
                            detail="consumer occurrence before scan wrap is ambiguous",
                            execution_projection=projection,
                        )
                    )
                    continue
                if len(preceding_reads) == 1:
                    wrapped = _observe_wrapped_handoff(
                        obligation,
                        observation.appeared,
                        projection,
                        projection_tuple,
                    )
                    if wrapped is not None:
                        result.append(wrapped)
                        continue
            result.append(_from_ordered(obligation, observation, projection))
    return tuple(result)


def fulfilled_expectation_observations(
    expectation: EffectExpectation,
    observations: Iterable[EffectObservation],
) -> tuple[EffectObservation, ...]:
    """One exact surviving occurrence for every selected obligation.

    A producer can execute again during pulse settlement. Once an occurrence
    has reached the selected consumer, a later repeat cannot retroactively
    invalidate that already fulfilled obligation; any later consequence is
    owned by the committed receipt and progress monitor.
    """

    observed = tuple(observations)
    fulfilled: list[EffectObservation] = []
    for obligation in expectation.obligations:
        survived = next(
            (
                item
                for item in observed
                if item.obligation is obligation and item.disposition == "SURVIVED"
            ),
            None,
        )
        if survived is None:
            return ()
        fulfilled.append(survived)
    return tuple(fulfilled)


def _consumer_reads_preceding_write(
    obligation: EffectObligation,
    appeared: RungWrite,
    projection: ScanRungWriteProjection,
) -> tuple[RungRead, ...]:
    """Exact consumer reads which make this handoff due after a cycle wrap."""

    if obligation.consumer_rung is None:
        return ()
    return tuple(
        read
        for run in projection.runs
        if run.rung is obligation.consumer_rung
        and projection._runs_share_selected_transaction(
            appeared.run,
            run,
            obligation.producer,
            obligation.consumer,
        )
        for read in projection.reads_for_run(run)
        if read.occurrence.name == obligation.tag and read.ordinal < appeared.ordinal
    )


def _observe_wrapped_handoff(
    obligation: EffectObligation,
    appeared: RungWrite,
    producer_projection: ScanRungWriteProjection,
    projections: tuple[ScanRungWriteProjection, ...],
) -> EffectObservation | None:
    """Observe the next exact consumer occurrence after scan order wraps.

    Cross-scan entry reads intentionally carry the executor's ``entry`` source
    marker rather than a previous scan's write object. Continuity is therefore
    proved from adjacent exact projections: the selected write wins the
    producer scan, the value is unchanged at the next entry, and no write
    intervenes before the selected consumer reads it.
    """

    next_projection = next(
        (
            candidate
            for candidate in projections
            if candidate.scan_id == producer_projection.scan_id + 1
        ),
        None,
    )
    if next_projection is None:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="consumer is due after scan wrap but the adjacent scan is unobserved",
            execution_projection=producer_projection,
        )
    if (
        producer_projection.final_write(obligation.tag, obligation.value) is not appeared
        or producer_projection.exit_tags.get(obligation.tag) != obligation.value
        or next_projection.entry_tags.get(obligation.tag) != obligation.value
    ):
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="cross-scan handoff continuity is not exact",
            execution_projection=next_projection,
        )

    consumer_runs = tuple(
        run
        for run in next_projection.runs
        if run.rung is obligation.consumer_rung
        and _wrapped_runs_share_selected_transaction(
            obligation,
            appeared,
            next_projection,
            run,
        )
    )
    if len(consumer_runs) != 1:
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            detail="selected adjacent-scan consumer occurrence is unavailable or ambiguous",
            execution_projection=next_projection,
        )

    consumer_run = consumer_runs[0]
    reads = next_projection.reads_for_run(consumer_run)
    effect_read = next(
        (
            read
            for read in reads
            if read.occurrence.name == obligation.tag and read.occurrence.value == obligation.value
        ),
        None,
    )
    consumer_boundary = (
        effect_read.ordinal
        if effect_read is not None
        else max((read.ordinal for read in reads), default=-1) + 1
    )
    displacement = next(
        (
            write
            for write in next_projection.writes
            if write.run.enabled
            and write.transition.tag_name == obligation.tag
            and write.ordinal < consumer_boundary
        ),
        None,
    )
    if displacement is not None:
        return EffectObservation(
            obligation,
            "OVERWRITTEN",
            appeared=appeared,
            consumer_read=effect_read,
            displacement=displacement,
            observed_reads=next_projection.enabling_reads_observed_by_write(displacement),
            detail="an adjacent-scan write replaced the pending handoff",
            execution_projection=next_projection,
        )
    if effect_read is not None and effect_read.occurrence.source != "entry":
        return EffectObservation(
            obligation,
            "UNKNOWN",
            appeared=appeared,
            consumer_read=effect_read,
            observed_reads=next_projection.reads_for_run(consumer_run),
            detail="adjacent-scan consumer read lacks exact entry continuity",
            execution_projection=next_projection,
        )
    if effect_read is None:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            observed_reads=reads,
            detail="adjacent-scan consumer did not read the pending handoff",
            execution_projection=next_projection,
        )

    next_ordinal = -1
    matched: list[RungRead] = []
    for required_tag, required_value in obligation.required_shape:
        observed = next(
            (
                read
                for read in reads
                if read.occurrence.name == required_tag and read.ordinal > next_ordinal
            ),
            None,
        )
        if observed is None:
            return EffectObservation(
                obligation,
                "UNKNOWN",
                appeared=appeared,
                consumer_read=effect_read,
                observed_reads=reads,
                detail=f"required consumer read {required_tag!r} did not occur",
                execution_projection=next_projection,
            )
        matched.append(observed)
        next_ordinal = observed.ordinal
        if observed.occurrence.value != required_value:
            return EffectObservation(
                obligation,
                "STRANDED" if not effect_read.run.enabled else "UNKNOWN",
                appeared=appeared,
                consumer_read=effect_read,
                displaced_read=observed,
                observed_reads=reads,
                detail="adjacent-scan consumer required shape did not hold",
                execution_projection=next_projection,
            )
    if not effect_read.run.enabled:
        return EffectObservation(
            obligation,
            "STRANDED",
            appeared=appeared,
            consumer_read=effect_read,
            observed_reads=reads,
            detail="adjacent-scan consumer read the effect but its guard was false",
            execution_projection=next_projection,
        )
    return EffectObservation(
        obligation,
        "SURVIVED",
        appeared=appeared,
        consumer_read=effect_read,
        observed_reads=tuple(matched),
        execution_projection=next_projection,
    )


def _wrapped_runs_share_selected_transaction(
    obligation: EffectObligation,
    appeared: RungWrite,
    projection: ScanRungWriteProjection,
    consumer_run: Any,
) -> bool:
    """Match dynamic call identity across adjacent scan invocations."""

    producer_sub, producer_rung, producer_branch = obligation.producer
    assert obligation.consumer is not None
    consumer_sub, consumer_rung, consumer_branch = obligation.consumer
    same_subroutine = producer_sub is not None and producer_sub == consumer_sub
    same_branched_rung = (
        producer_sub == consumer_sub
        and producer_rung == consumer_rung
        and bool(producer_branch or consumer_branch)
    )
    if same_subroutine or same_branched_rung:
        if (
            appeared.run.caller_rung != consumer_run.caller_rung
            or appeared.run.call_stack != consumer_run.call_stack
        ):
            return False
    if same_subroutine:
        return appeared.call_invocation == projection._call_invocation_by_run.get(id(consumer_run))
    return True


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
    made by the assertion. Pulse/Batch observations begin at the assertion scan
    and include the exact next scan when settlement executed it: a producer
    below its consumer is pending for that next cycle, not proved STRANDED just
    because the consumer already ran earlier in the assertion scan. Additional
    exact pen/event scans may classify a later consumer; folded gaps remain
    ``UNKNOWN`` rather than being replayed as a complete logical corridor.
    """

    if expectation is None:
        return ()
    if action_scan is not None:
        exact_scan_ids = {action_scan}
        if action_scan < fork.state.scan_id:
            exact_scan_ids.add(action_scan + 1)
        exact_scan_ids.update(
            event.scan for event in timeline if action_scan < event.scan <= fork.state.scan_id
        )
        projections = tuple(
            projection
            for scan_id in sorted(exact_scan_ids)
            if (projection := fork._replay_rung_write_projection_at(scan_id)) is not None
        )
        if projections:
            return _bind_execution_epoch(
                observe_expectation(expectation, projections),
                fork,
                fallback_scan=projections[-1].scan_id,
            )
        return _bind_execution_epoch(
            _unknown_observations(expectation, "assertion scan projection is unavailable"),
            fork,
            fallback_scan=action_scan,
        )

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
        return _bind_execution_epoch(
            observe_expectation(expectation, projections),
            fork,
            fallback_scan=landing_scan,
        )
    if not projections:
        return _bind_execution_epoch(
            _unknown_observations(
                expectation,
                "coast has no exact recorded effect scan",
            ),
            fork,
            fallback_scan=landing_scan,
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
    return _bind_execution_epoch(tuple(results), fork, fallback_scan=landing_scan)


def _bind_execution_epoch(
    observations: tuple[EffectObservation, ...],
    fork: Any,
    *,
    fallback_scan: int,
) -> tuple[EffectObservation, ...]:
    """Attach the immutable Epoch owner of each exact observation.

    A scan number is not an execution identity after forks and replay.  Sealing
    through the observation gives receipts a stable epoch/query pair rather
    than the lineage's mutable live-query adapter.
    """

    observation_scans = tuple(
        tuple(
            occurrence.scan_id
            for occurrence in (
                observation.appeared,
                observation.consumer_read,
                observation.displacement,
                observation.displaced_read,
                *observation.observed_reads,
            )
            if occurrence is not None
        )
        for observation in observations
    )
    latest_scan = max(
        (scan for scans in observation_scans for scan in scans),
        default=fallback_scan,
    )
    sealed = fork._causal_lineage.seal_through(latest_scan)

    result: list[EffectObservation] = []
    for observation, scans in zip(observations, observation_scans, strict=True):
        scan = max(scans, default=fallback_scan)
        owned = next(
            (
                (epoch, owner)
                for epoch, owner in sealed
                if epoch.first_scan <= scan <= epoch.last_scan
            ),
            None,
        )
        if owned is None:
            result.append(
                replace(
                    observation,
                    disposition="UNKNOWN",
                    detail=(f"{observation.detail}; " if observation.detail else "")
                    + "exact execution epoch is unavailable",
                )
            )
            continue
        epoch, owner = owned
        result.append(
            replace(
                observation,
                execution_epoch=epoch,
                execution_owner=owner,
            )
        )
    return tuple(result)


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
    projection: ScanRungWriteProjection,
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
        execution_projection=projection,
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


def occurrence_snapshot(read: RungRead | RungWrite) -> EffectOccurrenceSnapshot:
    """Detach one exact projection-owned occurrence for receipts/events."""

    if isinstance(read, RungWrite):
        return _write_snapshot(read)
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
