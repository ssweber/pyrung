"""Target-relevant designation and factual observation of adjacent scan work.

The first program scan is not a selected steer, so its route is bound after
landing.  Ordinary steers already own a selected route, but may still need a
second receipt when an immediate side effect succeeds while the retained
landing leaves that route.  Both cases use the same conservative designation
policy rather than inventing effects from endpoint snapshots.

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
from pyrung.core.analysis.pilot.effects import EffectExpectation, EffectObligation
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.causal._rung_writes import (
        RungRead,
        RungWrite,
        ScanRungWriteProjection,
    )
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.trace_tree import TraceNode

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
    displacement_enabling_reads: tuple[BootstrapOccurrenceSnapshot, ...]
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
            displacement_enabling_reads=tuple(
                _read_snapshot(read) for read in observation.displacement_enabling_reads
            ),
            detail=observation.detail,
        )


def _writer_address(pdg: ProgramGraph, node_index: int) -> StaticRungAddress:
    node = pdg.rung_nodes[node_index]
    return (node.subroutine, node.rung_index, node.branch_path)


def _consumer_shape(
    consumer: TraceNode,
    effect: TraceNode,
    pdg: ProgramGraph,
    *,
    consumer_node: Any | None = None,
) -> tuple[tuple[str, Any], ...]:
    """Conservative local values read by the selected consumer rung.

    Trace also carries anti-clobber requirements from later rungs.  Filtering
    direct children through the selected consumer's static read set prevents
    those terminal-survival constraints from being misreported as consumer
    shape.
    """

    if consumer.writer_rung is None:
        return ()
    node = consumer_node or pdg.rung_nodes[consumer.writer_rung]
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


def _execution_consumer_node(
    pdg: ProgramGraph,
    producer: Any,
    selected_consumer: Any,
) -> Any:
    """Resolve a cross-scope trace edge to its exact caller rung.

    Backward trace nests a subroutine writer below the condition that admits
    its call.  The selected writer remains the semantic downstream consumer,
    but the physical read of that admission condition occurs on the caller
    rung.  When one exact caller joins the producer's scope to the selected
    consumer's scope, bind the handoff there so ordered execution can observe
    it before normal subroutine cleanup.
    """

    if producer.subroutine == selected_consumer.subroutine:
        return selected_consumer
    called = selected_consumer.subroutine
    if called is None:
        return selected_consumer
    callers = tuple(
        node
        for node in pdg.rung_nodes
        if node.subroutine == producer.subroutine and called in node.calls
    )
    return callers[0] if len(callers) == 1 else selected_consumer


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
                selected_consumer_node = pdg.rung_nodes[consumer.writer_rung]
                consumer_node = _execution_consumer_node(
                    pdg,
                    producer_node,
                    selected_consumer_node,
                )
                consumer_address = (
                    consumer_node.subroutine,
                    consumer_node.rung_index,
                    consumer_node.branch_path,
                )
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
                            _consumer_shape(
                                consumer,
                                node,
                                pdg,
                                consumer_node=consumer_node,
                            )
                            if consumer is not None
                            else ()
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


def bind_observed_route_designations(
    trace: TraceNode,
    pdg: ProgramGraph,
    program: Any,
    projection: ScanRungWriteProjection,
    *,
    steerable: frozenset[str],
    channel_tags: frozenset[str],
) -> tuple[BootstrapDesignation, ...]:
    """Bind selected chart values to their exact observed alternate writer.

    A charted state channel can enter a selected route value through a writer
    other than the route's nominal producer (an abort/reset rung is the common
    case). The value remains target-relevant, but its corrective authority must
    name the writer that actually ran. Add only exact appeared channel writes
    whose value is already present on the selected route; arbitrary off-route
    effects remain excluded.
    """

    selected = list(
        bootstrap_designations(
            trace,
            pdg,
            program,
            steerable=steerable,
            channel_tags=channel_tags,
        )
    )
    route_values = {
        (designation.tag, _detached(designation.value)): designation
        for designation in selected
        if designation.tag in channel_tags
    }
    for write in projection.writes:
        tag = write.transition.tag_name
        template = route_values.get((tag, _detached(write.transition.to_value)))
        if template is None:
            continue
        producer_nodes = tuple(
            node
            for node in pdg.rung_nodes
            if node.subroutine == write.rung_id.subroutine
            and node.rung_index == write.rung_id.rung_index
            and tag in node.writes
            and resolve_rung(program, node) is write.run.rung
        )
        if len(producer_nodes) != 1:
            continue
        node = producer_nodes[0]
        producer = (node.subroutine, node.rung_index, node.branch_path)
        path = (
            *template.path[:-1],
            BootstrapPathStep(tag, template.value, producer),
        )
        if write.run.rung is not template.producer_rung and not any(
            item.tag == tag
            and item.value == template.value
            and item.producer == producer
            and item.consumer == template.consumer
            for item in selected
        ):
            selected.append(
                BootstrapDesignation(
                    tag=tag,
                    value=template.value,
                    path=path,
                    producer=producer,
                    consumer=template.consumer,
                    required_shape=template.required_shape,
                    producer_rung=write.run.rung,
                    consumer_rung=template.consumer_rung,
                )
            )
    return tuple(selected)


def selected_route_landing_expectation(
    trace: TraceNode,
    pdg: ProgramGraph,
    program: Any,
    projections: tuple[ScanRungWriteProjection, ...],
    *,
    landing: Mapping[str, Any],
    steerable: frozenset[str],
    channel_tags: frozenset[str],
    charted_values: Mapping[str, tuple[Any, ...]] | None = None,
) -> EffectExpectation | None:
    """Select exact route effects whose retained channel landing went off-route.

    This is the target-route half of an execution receipt.  The act's ordinary
    expectation still describes its immediate selected producer.  Here we add
    at most the last appeared selected-route value for each state channel, and
    only when the final channel value is not represented by the selected trace.
    Intrascan can then interpret the exact later overwrite without treating all
    transient route motion as a promise or re-reading the landing as evidence.
    """

    if not projections or not channel_tags:
        return None
    off_route_tags = unexplained_route_landing_tags(
        trace,
        pdg,
        program,
        landing=landing,
        steerable=steerable,
        channel_tags=channel_tags,
        charted_values=charted_values,
    )
    if not off_route_tags:
        return None

    appeared: list[BootstrapEffect] = []
    for projection in projections:
        designations = bind_observed_route_designations(
            trace,
            pdg,
            program,
            projection,
            steerable=steerable,
            channel_tags=channel_tags,
        )
        appeared.extend(
            effect
            for effect in observe_bootstrap_effects(designations, projection)
            if effect.designation.tag in off_route_tags
        )
    if not appeared:
        return None

    last_by_tag: dict[str, BootstrapEffect] = {}
    for effect in appeared:
        current = last_by_tag.get(effect.designation.tag)
        occurrence = effect.observation.appeared
        if current is None or (
            occurrence.scan_id,
            occurrence.ordinal,
        ) > (
            current.observation.appeared.scan_id,
            current.observation.appeared.ordinal,
        ):
            last_by_tag[effect.designation.tag] = effect
    obligations = tuple(
        EffectObligation(
            tag=effect.designation.tag,
            value=effect.designation.value,
            producer=effect.designation.producer,
            consumer=effect.designation.consumer,
            required_shape=effect.designation.required_shape,
            boundary=(effect.designation.tag, effect.designation.value),
            # Unlike an ordinary static target promise, this obligation is
            # minted only after the selected root writer actually appeared in
            # an exact owned projection.  It may therefore explain a non-zero
            # off-route landing without turning target absence into failure.
            terminal_target=effect.designation.consumer is None,
            producer_rung=effect.designation.producer_rung,
            consumer_rung=effect.designation.consumer_rung,
        )
        for _tag, effect in sorted(last_by_tag.items())
    )
    return EffectExpectation(obligations) if obligations else None


def unexplained_route_landing_tags(
    trace: TraceNode,
    pdg: ProgramGraph,
    program: Any,
    *,
    landing: Mapping[str, Any],
    steerable: frozenset[str],
    channel_tags: frozenset[str],
    charted_values: Mapping[str, tuple[Any, ...]] | None = None,
) -> frozenset[str]:
    """Name retained channel values the selected route does not explain.

    This endpoint-only question is intentionally answerable before requesting
    ordered scan projections.  Exact occurrence research is still required to
    attribute every returned tag; an on-route landing has no unexplained value
    to attribute and therefore needs no historical projection replay.
    """

    if not channel_tags:
        return frozenset()
    base = bootstrap_designations(
        trace,
        pdg,
        program,
        steerable=steerable,
        channel_tags=channel_tags,
    )
    known_values = charted_values or {}
    return frozenset(
        tag
        for tag in channel_tags
        if not any(_values_match(landing.get(tag), value) for value in known_values.get(tag, ()))
        if not any(
            designation.tag == tag and _values_match(landing.get(tag), designation.value)
            for designation in base
        )
    )


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
