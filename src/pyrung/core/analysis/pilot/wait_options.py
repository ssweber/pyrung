"""Materialize wait prescriptions from exact route and program evidence.

This module rereads completion gates, binds instruction boundaries, and
constructs typed wait receipts. It does not rank candidate reads, select an
act, execute a Bearing, or mutate Pilot state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.candidate_read as _candidate_read
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    expectation_from_writer,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ChannelHeading,
    LandingReceiptAuthority,
    RouteEdgeContext,
    _ActionPair,
)
from pyrung.core.analysis.pilot.pipeline_graph import ANY_FROM, target_reachable_values
from pyrung.core.analysis.pilot.route_options import _edge_grounded, _fmt_from
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_read import TraceReadConstraints
from pyrung.core.analysis.pilot.trace_tree import frontier_pairs
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.trace_tree import TraceAction


def _completion_reread(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
) -> tuple[tuple[TraceAction, ...], tuple[_ActionPair, ...]]:
    """Re-trace a completion edge's charted gate pairs against the live world.

    The wait's bearing (``StaticTransitionEdge.completion`` — pipeline_graph.py) is ordinary
    transparent ladder below the pipeline boundary: each recorded ``(tag,
    value)`` is traced back with a fresh walk (clean ancestry, so the
    opaque-loop feedback guard admits the one-hop descent), and its steerable
    leaves + unmet frontier are collected.  Availability orders, never rejects:
    a satisfied or self-advancing completion yields nothing pressable and the
    wait proceeds unchanged.  Returns ``(action_details, frontier)`` — the
    frontier lists the pressable levers (steerable leaves, e.g. ``x_RotateFB``)
    ahead of the trace's non-steerable interior so the terminal clause names the
    true blocker, not the post-cut interior.
    """
    details: list[TraceAction] = []
    seen_action: set[_ActionPair] = set()
    frontier: list[_ActionPair] = []
    seen_frontier: set[tuple[str, str]] = set()

    def _add_frontier(tag: str, value: Any) -> None:
        key = (tag, repr(value))
        if key not in seen_frontier:
            seen_frontier.add(key)
            frontier.append((tag, value))

    for tag, value in edge.completion:
        if _values_match(frame.snap.get(tag), value):
            continue
        tree = trace_back(
            tag,
            value,
            frame.snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            constraints=TraceReadConstraints.from_context(
                ctx,
                state.work,
                route=None,
                avoid_pred=ctx.avoid_pred,
            ),
        )
        for action in tree.ordered_action_details():
            if action.pair not in seen_action:
                seen_action.add(action.pair)
                details.append(action)
            if not _values_match(frame.snap.get(action.tag), action.value):
                _add_frontier(action.tag, action.value)
        for ftag, fval in frontier_pairs(tree, frame.snap):
            _add_frontier(ftag, fval)
    return tuple(details), tuple(frontier)


def _boundary_heading(boundary: Any, frame: Any, state: Any) -> ChannelHeading | None:
    """Lower an owned relational boundary to an exact observable heading."""
    from pyrung.core.analysis.pilot.advance import build_advance_index
    from pyrung.core.crossing import AffineCmp, Cmp, Eq
    from pyrung.core.instruction.advance import scalar_boundary

    if isinstance(boundary, Eq) and len(boundary.values) == 1:
        return ChannelHeading(
            channel_tag=boundary.tag,
            target_value=next(iter(boundary.values)),
            boundary=boundary,
        )
    if not isinstance(boundary, (Cmp, AffineCmp)) or boundary.op not in {">=", "<="}:
        return None
    owner = build_advance_index(
        state.work.program,
        getattr(state.work, "_harness", None),
    ).resolve(boundary.tag)
    if owner is None:
        return None
    target = scalar_boundary(boundary, frame.snap)
    if target is None:
        return None
    return ChannelHeading(
        channel_tag=boundary.tag,
        target_value=target,
        boundary=boundary,
    )


class _RequirementReadTrace:
    """Minimal condition context that journals exact direct tag-read order."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.values = values
        self.reads: list[tuple[str, Any]] = []

    def get_tag(self, name: str, default: Any = None) -> Any:
        value = self.values.get(name, default)
        self.reads.append((name, value))
        return value

    def get_memory(self, _key: str, _default: Any = None) -> Any:
        raise RuntimeError("static consumer shape cannot resolve memory reads")

    @property
    def scan_id(self) -> int:
        raise RuntimeError("static consumer shape cannot resolve scan-relative reads")


def _ordered_consumer_required_shape(
    consumer_rung: Any,
    snapshot: Mapping[str, Any],
    requirements: Sequence[_ActionPair],
) -> tuple[_ActionPair, ...] | None:
    """Lower exact guard values in the consumer's real evaluation order.

    The selected edge supplies values, while the rung itself supplies
    occurrence order.  Evaluating against their overlay also respects normal
    series/parallel short-circuiting.  Unsupported scan-relative reads and
    contradictory or unobserved edge facts fail closed.
    """

    missing = object()
    known: dict[str, Any] = {}
    for tag, value in requirements:
        prior = known.get(tag, missing)
        if prior is not missing and not _values_match(prior, value):
            return None
        known[tag] = value
    if not known:
        return ()

    trace = _RequirementReadTrace({**snapshot, **known})
    try:
        # Branch runs journal only their local condition slice; inherited
        # parent contacts belong to the parent run's distinct address.
        start = getattr(consumer_rung, "_branch_condition_start", 0)
        for condition in consumer_rung._conditions[start:]:
            if not condition.evaluate(trace):
                return None
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return None

    ordered = tuple(
        (tag, value)
        for tag, value in trace.reads
        if tag in known and _values_match(value, known[tag])
    )
    if set(known) != {tag for tag, _value in ordered}:
        return None
    return ordered


def _charted_successor_consumer(
    ctx: Any,
    edge: Any,
    snapshot: Mapping[str, Any],
) -> tuple[int, tuple[_ActionPair, ...]] | None:
    """Name one exact automatic consumer on the charted target corridor.

    A state edge's value need not survive scan exit: the next program-owned
    edge can consume that exact occurrence and advance the same carrier.  The
    static chart may designate that consumer, but execution still has to prove
    the occurrence handoff.  Wildcard, action-owned, off-target, and ambiguous
    successors deliberately provide no consumer receipt.
    """

    target = getattr(ctx, "target", None)
    compass = getattr(ctx, "compass", None)
    if target is None or compass is None or target.tag != edge.role.channel_tag:
        return None

    effect_tag, effect_value = edge.route.writer_effect
    candidates: list[tuple[int, tuple[_ActionPair, ...]]] = []
    for graph in compass.chart_graphs:
        if graph.role.channel_tag != edge.role.channel_tag:
            continue
        reachable = target_reachable_values(graph, target.value)
        for successor in graph.edges:
            if (
                successor.from_value is ANY_FROM
                or successor.action is not None
                or not _values_match(successor.from_value, edge.to_value)
                or not any(_values_match(successor.to_value, value) for value in reachable)
            ):
                continue
            writer = ctx.pdg.rung_nodes[successor.route.writer_node]
            branch_path = getattr(writer, "branch_path", ())
            read_owners = tuple(
                (index, node)
                for index, node in enumerate(ctx.pdg.rung_nodes)
                if getattr(node, "subroutine", object()) == getattr(writer, "subroutine", object())
                and getattr(node, "rung_index", object()) == getattr(writer, "rung_index", object())
                and len(getattr(node, "branch_path", ())) <= len(branch_path)
                and branch_path[: len(getattr(node, "branch_path", ()))]
                == getattr(node, "branch_path", ())
                and effect_tag in (node.condition_reads | node.guard_reads | node.data_reads)
            )
            if not read_owners:
                continue
            consumer_node, consumer = min(
                read_owners,
                key=lambda item: len(getattr(item[1], "branch_path", ())),
            )
            consumer_reads = consumer.condition_reads | consumer.guard_reads | consumer.data_reads
            requirements = [
                pair
                for pair in (
                    *successor.source_constraints,
                    *successor.enablers,
                    *successor.completion,
                )
                if pair[0] in consumer_reads
            ]
            if effect_tag in consumer_reads:
                requirements.append((effect_tag, effect_value))
            from pyrung.core.analysis.pdg import resolve_rung

            consumer_rung = resolve_rung(ctx.program, consumer)
            shape = (
                _ordered_consumer_required_shape(
                    consumer_rung,
                    snapshot,
                    requirements,
                )
                if consumer_rung is not None
                else None
            )
            if shape is not None:
                candidate = (consumer_node, shape)
                if candidate not in candidates:
                    candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None


def _charted_program_edge(
    ctx: Any,
    step: Any,
    snapshot: Mapping[str, Any],
) -> Any | None:
    """Resolve a ProgramStep producer to one exact current chart edge."""

    compass = getattr(ctx, "compass", None)
    target = getattr(ctx, "target", None)
    if compass is None or target is None:
        return None
    candidates: list[Any] = []
    identities: list[tuple[Any, ...]] = []
    for graph in compass.chart_graphs:
        if graph.role.channel_tag != target.tag:
            continue
        current = snapshot.get(graph.role.channel_tag)
        for edge in graph.edges:
            effect_tag, effect_value = edge.route.writer_effect
            if (
                edge.from_value is ANY_FROM
                or not _values_match(edge.from_value, current)
                or step.producer.rung_index != edge.route.writer_node
                or step.producer.command_tag != effect_tag
                or not _values_match(step.producer.command_value, effect_value)
                or edge.identity in identities
            ):
                continue
            identities.append(edge.identity)
            candidates.append(edge)
    return candidates[0] if len(candidates) == 1 else None


def _expectation_from_route_writer(
    ctx: Any,
    edge: Any,
    snapshot: Mapping[str, Any],
) -> EffectExpectation | None:
    """Bind a chart receipt to its writer and exact downstream consumer."""

    tag, value = edge.route.writer_effect
    if value is None:
        return None
    successor = (
        _charted_successor_consumer(ctx, edge, snapshot)
        if edge.route.consumer_node is None
        else None
    )
    consumer_node = (
        edge.route.consumer_node
        if edge.route.consumer_node is not None
        else successor[0]
        if successor is not None
        else None
    )
    required_shape = (
        ((tag, value),)
        if edge.route.consumer_node is not None
        else successor[1]
        if successor is not None
        else ()
    )
    return expectation_from_writer(
        ctx.pdg,
        ctx.program,
        writer_node=edge.route.writer_node,
        tag=tag,
        value=value,
        consumer_node=consumer_node,
        required_shape=required_shape,
        boundary=(edge.role.channel_tag, edge.to_value),
    )


def _prescribe_wait(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    reason: str | None = None,
) -> _candidate_read.WaitRead:
    """Mint a prescribed-wait bearing from one current-world edge read.

    The single owner of "a wait is prescribed" for both mint paths. A route
    completion edge (bearing coast) must be *grounded* — a wildcard from-value has no
    dwell semantics, so its read has no prescription. An
    learned-path wait passes ``edge=None`` with an explicit ``reason`` —
    always coastable. Automatic sibling edges carry one exact-producer
    ``ProgramStep`` reading in the returned prescription.

    The returned read keeps completion and exact-producer details attached so
    `_build_candidates` can pass them through ordinary trace admission.
    """
    if edge is None:
        return _candidate_read.WaitRead(_candidate_read.WaitPrescription(None, reason))
    if not _edge_grounded(edge):
        return _candidate_read.WaitRead(
            None,
            declined_reason="completion edge has no grounded source value",
        )

    route_reason = (
        f"let-run {edge.role.channel_tag}: {_fmt_from(edge.from_value)}->{edge.to_value!r}"
    )
    effect_tag, effect_value = edge.route.writer_effect
    route_context = RouteEdgeContext(
        edge.role.channel_tag,
        edge.from_value,
        edge.to_value,
        effect_tag,
        effect_value,
    )
    route_expectation = _expectation_from_route_writer(ctx, edge, frame.snap)

    def _read(
        prescription: _candidate_read.WaitPrescription | None,
        *,
        step: Any = None,
        declined_reason: str | None = None,
        declined_frontier: tuple[_ActionPair, ...] = (),
    ) -> _candidate_read.WaitRead:
        if prescription is not None and step is not None:
            prescription = replace(
                prescription,
                landing_receipt_authority=LandingReceiptAuthority.PROGRAM_STEP,
            )
        details = (
            step.inputs_with_lifetime
            if step is not None and prescription is not None
            else step.required_inputs
            if step is not None
            else ()
        )
        return _candidate_read.WaitRead(
            prescription,
            details,
            declined_reason=declined_reason,
            program_step=step,
            declined_frontier=declined_frontier,
        )

    def _route_heading(
        heading: ChannelHeading | None = None,
        boundary: Any = None,
    ) -> ChannelHeading:
        heading = heading or ChannelHeading(
            channel_tag=edge.role.channel_tag,
            target_value=edge.to_value,
        )
        return replace(
            heading,
            boundary=boundary if boundary is not None else heading.boundary,
            route=route_context,
        )

    if edge.program_producers:
        from pyrung.core.analysis.pilot.program_step import (
            ProgramStepStatus,
            read_program_step,
        )
        from pyrung.core.analysis.pilot.trace_read import WorldView

        producers = tuple(
            {producer.rung_index: producer for producer in edge.program_producers}.values()
        )
        if len(producers) != 1:
            return _read(
                None,
                declined_reason=f"{route_reason}; exact program producer is ambiguous",
            )
        world = WorldView(
            snapshot=frame.snap,
            pdg=ctx.pdg,
            program=ctx.program,
            steerable=ctx.steerable,
            opaque_loop=ctx.opaque_loop,
            prior=ctx.domain_prior,
            clear_only=ctx.clear_only,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            pipeline_roles=ctx.pipeline_roles,
            avoid_pred=ctx.avoid_pred,
            harness=getattr(state.work, "_harness", None),
        )
        step = read_program_step(
            world,
            producers[0],
            state.work,
            state.pilot_rungs,
            resting=ctx.resting,
            structural_channels=(edge.role.channel_tag,),
        )
        route_writer = ctx.pdg.rung_nodes[edge.route.writer_node]
        route_writer_reads = (
            route_writer.condition_reads | route_writer.guard_reads | route_writer.data_reads
        )
        command_pair = (step.producer.command_tag, step.producer.command_value)
        local_shape_requirements = [
            pair
            for pair in (*edge.source_constraints, *edge.enablers, *edge.completion)
            if pair[0] in route_writer_reads
        ]
        if command_pair[0] in route_writer_reads:
            local_shape_requirements.append(command_pair)
        from pyrung.core.analysis.pdg import resolve_rung

        consumer_rung = resolve_rung(ctx.program, route_writer)
        local_shape = (
            _ordered_consumer_required_shape(
                consumer_rung,
                frame.snap,
                local_shape_requirements,
            )
            if consumer_rung is not None
            else None
        )
        producer_is_route_writer = (
            step.producer.rung_index == edge.route.writer_node
            and step.producer.command_tag == effect_tag
            and _values_match(step.producer.command_value, effect_value)
        )
        program_expectation = None
        if step.producer_observed and producer_is_route_writer:
            # ProgramStep has observed the chart edge's exact writer in the
            # first projected scan.  Its direct writer receipt is already the
            # whole promise; treating that same rung as both producer and
            # consumer invents a handoff and can discard the expectation when
            # no consumer shape exists.
            program_expectation = route_expectation
        elif step.producer_observed and local_shape is not None:
            program_expectation = expectation_from_writer(
                ctx.pdg,
                ctx.program,
                writer_node=step.producer.rung_index,
                tag=step.producer.command_tag,
                value=step.producer.command_value,
                consumer_node=edge.route.writer_node,
                required_shape=local_shape,
                boundary=(edge.role.channel_tag, edge.to_value),
            )
        preferred_channel = (
            edge.role.channel_tag
            if edge.role.channel_tag in step.preserve_channels
            else next(iter(sorted(step.preserve_channels)), None)
        )
        motion = step.observable_motion(preferred_channel)
        motion_heading = (
            ChannelHeading(
                channel_tag=motion.channel_tag,
                target_value=(
                    motion.before_value
                    if motion.channel_tag in step.preserve_channels
                    else motion.target_value
                ),
            )
            if motion is not None
            else None
        )
        if step.status is ProgramStepStatus.KEEP_RUNNING:
            boundary_heading = _boundary_heading(step.boundary, frame, state)
            if boundary_heading is None:
                return _read(
                    None,
                    step=step,
                    declined_reason=f"{route_reason}; owned boundary has no exact coast heading",
                )
            coast_heading = motion_heading or boundary_heading
            observation = (
                f" ({motion.channel_tag}: {motion.before_value!r}->{motion.target_value!r})"
                if motion is not None
                else f"; {step.reason}"
            )
            return _read(
                _candidate_read.WaitPrescription(
                    _route_heading(coast_heading, step.boundary),
                    f"{route_reason}{observation}",
                    frontier=(
                        (
                            boundary_heading.channel_tag,
                            boundary_heading.target_value,
                        ),
                    ),
                    expectation=program_expectation,
                ),
                step=step,
            )
        if step.status is ProgramStepStatus.NEEDS_INPUT:
            boundary = step.uniform_handoff_boundary
            if boundary is not None:
                boundary_heading = _boundary_heading(boundary, frame, state)
                if boundary_heading is None:
                    return _read(
                        None,
                        step=step,
                        declined_reason=(
                            f"{route_reason}; owned boundary has no exact coast heading"
                        ),
                    )
                return _read(
                    _candidate_read.WaitPrescription(
                        _route_heading(motion_heading or boundary_heading, boundary),
                        (
                            f"{route_reason}; supply its current input and hand off to "
                            f"{boundary_heading.channel_tag}"
                        ),
                        expectation=program_expectation,
                        frontier=(
                            (
                                boundary_heading.channel_tag,
                                boundary_heading.target_value,
                            ),
                        ),
                    ),
                    step=step,
                )
            frontier = tuple(
                action.pair
                for action in step.required_inputs
                if action.pulse or not _values_match(frame.snap.get(action.tag), action.value)
            )
            return _read(
                None,
                step=step,
                declined_reason=f"{route_reason}; {step.reason}",
                declined_frontier=frontier,
            )
        if step.status is ProgramStepStatus.INTERRUPTED:
            return _read(
                _candidate_read.WaitPrescription(
                    _route_heading(motion_heading),
                    f"{route_reason}; {step.reason}",
                    expectation=program_expectation,
                ),
                step=step,
            )
        return _read(None, step=step, declined_reason=f"{route_reason}; {step.reason}")

    prescription = _candidate_read.WaitPrescription(
        _route_heading(),
        route_reason,
        expectation=route_expectation,
    )
    details: tuple[TraceAction, ...] = ()
    frontier: tuple[_ActionPair, ...] = ()
    if edge is not None and not edge.program_producers and edge.completion:
        details, frontier = _completion_reread(edge, frame, state, ctx)
        prescription = replace(prescription, frontier=frontier)
    return _candidate_read.WaitRead(prescription, details)
