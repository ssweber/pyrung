"""Target-relevant designation and factual observation of bootstrap work.

The first program scan is not a selected steer, so this module creates a
conservative watchlist rather than promises that any effect must appear.
``bootstrap_designations`` is the one adjustable selection policy.  Exact
ordered observation remains owned by :class:`ScanRungWriteProjection`; this
module only adapts its factual results into bootstrap receipts.

No function here chooses a correction, requirement, deadline, or action.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.causal._rung_writes import OrderedEffectObservation
from pyrung.core.analysis.pdg import resolve_rung

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import (
        RungRead,
        RungWrite,
        ScanRungWriteProjection,
    )
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.trace import TraceNode

StaticRungAddress = tuple[str | None, int, tuple[int, ...]]


def _detached(value: Any) -> Any:
    """Recursively detach one diagnostic value from execution-owned objects."""

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
class BootstrapPathStep:
    """One selected target-tree node retained with its static writer address."""

    tag: str
    value: Any
    writer: StaticRungAddress | None


@dataclass(frozen=True)
class BootstrapDesignation:
    """One conservative program-written effect worth watching in scan 1."""

    tag: str
    value: Any
    path: tuple[BootstrapPathStep, ...]
    producer: StaticRungAddress
    consumer: StaticRungAddress | None
    required_shape: tuple[tuple[str, Any], ...]
    producer_rung: object = field(compare=False, repr=False)
    consumer_rung: object | None = field(compare=False, repr=False)


@dataclass(frozen=True)
class BootstrapDesignationSnapshot:
    """Detached immutable diagnostic view of a bootstrap designation."""

    tag: str
    value: Any
    path: tuple[tuple[str, Any, StaticRungAddress | None], ...]
    producer: StaticRungAddress
    consumer: StaticRungAddress | None
    required_shape: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class BootstrapOccurrenceSnapshot:
    """Detached exact read/write occurrence used by one effect verdict."""

    kind: Literal["read", "write"]
    ordinal: int
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
class BootstrapEffectSnapshot:
    """Detached immutable diagnostic view of one appeared designated effect."""

    disposition: str
    designation: BootstrapDesignationSnapshot
    appeared: BootstrapOccurrenceSnapshot
    consumer_read: BootstrapOccurrenceSnapshot | None
    displacement: BootstrapOccurrenceSnapshot | None
    displaced_read: BootstrapOccurrenceSnapshot | None
    observed_reads: tuple[BootstrapOccurrenceSnapshot, ...]
    detail: str = ""


@dataclass(frozen=True)
class BootstrapEffect:
    """Internal designation plus exact ordered projection observation."""

    designation: BootstrapDesignation
    observation: OrderedEffectObservation

    def diagnostic_snapshot(self) -> BootstrapEffectSnapshot:
        """Return an inert view containing no run/instruction/occurrence objects."""

        observation = self.observation
        return BootstrapEffectSnapshot(
            disposition=observation.disposition,
            designation=designation_snapshot(self.designation),
            appeared=_write_snapshot(observation.appeared),
            consumer_read=(
                _read_snapshot(observation.consumer_read)
                if observation.consumer_read is not None
                else None
            ),
            displacement=(
                _write_snapshot(observation.displacement)
                if observation.displacement is not None
                else None
            ),
            displaced_read=(
                _read_snapshot(observation.displaced_read)
                if observation.displaced_read is not None
                else None
            ),
            observed_reads=tuple(_read_snapshot(read) for read in observation.observed_reads),
            detail=observation.detail,
        )


def _writer_address(pdg: ProgramGraph, node_index: int) -> StaticRungAddress:
    node = pdg.rung_nodes[node_index]
    return (node.subroutine, node.rung_index, node.branch_path)


def _consumer_shape(
    consumer: TraceNode,
    effect: TraceNode,
    pdg: ProgramGraph,
) -> tuple[tuple[str, Any], ...]:
    """Conservative local values read by the selected consumer rung.

    Trace also carries anti-clobber requirements from later rungs.  Filtering
    direct children through the selected consumer's static read set prevents
    those terminal-survival constraints from being misreported as consumer
    shape.
    """

    if consumer.writer_rung is None:
        return ()
    node = pdg.rung_nodes[consumer.writer_rung]
    read_tags = node.condition_reads | node.guard_reads | node.data_reads
    candidates = [
        (child.tag, child.value)
        for child in consumer.children
        if child.tag in read_tags and not child.heuristic and not child.relational
    ]
    effect_pair = (effect.tag, effect.value)
    if effect.tag in read_tags and effect_pair not in candidates:
        candidates.insert(0, effect_pair)
    unique: list[tuple[str, Any]] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def bootstrap_designations(
    trace: TraceNode,
    pdg: ProgramGraph,
    program: Any,
    *,
    steerable: frozenset[str],
    channel_tags: frozenset[str] = frozenset(),
) -> tuple[BootstrapDesignation, ...]:
    """Select every concrete program-written effect on the selected trace.

    Relevance comes from the selected tree path and exact producer, not a tag
    category. Steerable leaves, writerless data/guard reads, heuristic or
    relational proposals, unrelated scan writes, and absent effects remain
    outside factual observation. ``channel_tags`` is accepted as contextual
    policy input but does not narrow complete selected-trace truth.
    """

    _ = channel_tags
    result: list[BootstrapDesignation] = []

    def _walk(
        node: TraceNode,
        consumer: TraceNode | None,
        path: tuple[BootstrapPathStep, ...],
    ) -> None:
        # A heuristic or relational parent makes every descendant proposal part
        # of that unsupported path. Do not let a concrete-looking writer below
        # it re-enter the bootstrap watchlist independently.
        if node.heuristic or node.relational:
            return
        writer_address = (
            _writer_address(pdg, node.writer_rung) if node.writer_rung is not None else None
        )
        current_path = (*path, BootstrapPathStep(node.tag, node.value, writer_address))
        if node.writer_rung is not None and node.tag not in steerable:
            producer_node = pdg.rung_nodes[node.writer_rung]
            producer_rung = resolve_rung(program, producer_node)
            consumer_node = None
            consumer_address = None
            if consumer is not None:
                assert consumer.writer_rung is not None
                consumer_node = pdg.rung_nodes[consumer.writer_rung]
                consumer_address = _writer_address(pdg, consumer.writer_rung)
            consumer_rung = (
                resolve_rung(program, consumer_node) if consumer_node is not None else None
            )
            # An unresolved static rung cannot be matched to an exact dynamic
            # occurrence and therefore is not safe designation evidence.
            if producer_rung is not None and (consumer is None or consumer_rung is not None):
                result.append(
                    BootstrapDesignation(
                        tag=node.tag,
                        value=node.value,
                        path=current_path,
                        producer=_writer_address(pdg, node.writer_rung),
                        consumer=consumer_address,
                        required_shape=(
                            _consumer_shape(consumer, node, pdg) if consumer is not None else ()
                        ),
                        producer_rung=producer_rung,
                        consumer_rung=consumer_rung,
                    )
                )
        for child in node.children:
            _walk(
                child,
                node if node.writer_rung is not None else consumer,
                current_path,
            )

    _walk(trace, None, ())
    return tuple(result)


def observe_bootstrap_effects(
    designations: tuple[BootstrapDesignation, ...],
    projection: ScanRungWriteProjection,
) -> tuple[BootstrapEffect, ...]:
    """Intersect designations with appeared effects and observe only that set."""

    result: list[BootstrapEffect] = []
    for designation in designations:
        observations = projection.observe_appeared_handoff(
            designation.tag,
            designation.value,
            producer_rung=designation.producer_rung,
            consumer_rung=designation.consumer_rung,
            producer_address=designation.producer,
            consumer_address=designation.consumer,
            required_shape=designation.required_shape,
        )
        for observation in observations:
            result.append(BootstrapEffect(designation, observation))
    result.sort(key=lambda effect: effect.observation.appeared.ordinal)
    return tuple(result)


def designation_snapshot(
    designation: BootstrapDesignation,
) -> BootstrapDesignationSnapshot:
    """Detach one designation from its program-owned rung objects."""

    return BootstrapDesignationSnapshot(
        tag=designation.tag,
        value=_detached(designation.value),
        path=tuple((step.tag, _detached(step.value), step.writer) for step in designation.path),
        producer=designation.producer,
        consumer=designation.consumer,
        required_shape=tuple((tag, _detached(value)) for tag, value in designation.required_shape),
    )


def _read_snapshot(read: RungRead) -> BootstrapOccurrenceSnapshot:
    return BootstrapOccurrenceSnapshot(
        kind="read",
        ordinal=read.ordinal,
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


def _write_snapshot(write: RungWrite) -> BootstrapOccurrenceSnapshot:
    return BootstrapOccurrenceSnapshot(
        kind="write",
        ordinal=write.ordinal,
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
