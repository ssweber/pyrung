"""Materialize the action and wait options for one orientation.

``_build_candidates`` orchestrates separate reads for static routes and charted
completion, instruction-owned boundaries and prerequisites, learned
transitions, and program-awaited actions. Frozen private receipts keep those
sources distinct until ``_select_wait`` applies their precedence and
``_assemble_candidate_read`` creates the sole durable ``CandidateRead``.

Wait precedence is explicit: a prescribed learned wait wins, otherwise a
charted completion wins, and a standalone instruction boundary is used only
when there are no action candidates and no prescribed wait. Charted completion
may borrow an instruction-owned heading while retaining its route context.
``_AdmittedWait.viable`` and ``WaitRead.without_prescription`` ensure failed
admission removes coast authority without discarding the evidence it exposed.

Candidate construction reads the current world and knowledge but does not
execute a trial, apply observations, or commit state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import pyrung.core.analysis.pilot.candidate_read as _candidate_read
from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.awaited_actions import AwaitedAction
from pyrung.core.analysis.pilot.candidate_policy import (
    _action_allowed,
    hold_defeats_needed,
)
from pyrung.core.analysis.pilot.compass import (
    is_action,
    is_composite_action,
    unique_legal_awaited_action,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    expectation_from_selected_path,
    expectation_from_writer,
    expectation_snapshot,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    ChannelHeading,
    CrossingFidelity,
    LandingReceiptAuthority,
    RouteEdgeContext,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _until_unresolved_condition,
)
from pyrung.core.analysis.pilot.pipeline_graph import ANY_FROM, target_reachable_values
from pyrung.core.analysis.pilot.route_options import (
    _compass_route_actions,
    _compass_route_plan,
    _edge_grounded,
    _fmt_from,
    _general_chart_completion_plan,
    _learned_edge_allowed,
    _live_chart_completion_edge,
    _managed_boolean_rungs,
    _oscillating_rungs,
)
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_read import TraceReadConstraints
from pyrung.core.analysis.pilot.trace_tree import frontier_pairs
from pyrung.core.analysis.pilot.types import _ActionPair
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.instruction.advance import constraint_holds

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.trace_tree import TraceAction

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


def _effect_operation_batches(
    details: Sequence[TraceAction],
    snapshot: Mapping[str, Any],
    pdg: Any,
    program: Any,
    steerable: frozenset[str],
) -> tuple[_candidate_read.CrossingBatchRead, ...]:
    """Compose inputs which cover one exact writer's local conjunction.

    Target-wide trace actions are not an executable batch.  They become one
    only when their exact effect paths converge on the same selected writer
    and, collectively, cover every currently-unsatisfied local requirement of
    that writer.  Each action must own the immediate operation boundary it
    covers; a deeper leaf which merely passes through the requirement cannot
    hitchhike.  The resulting promise is the common writer itself.
    """

    operations: dict[tuple[int, str, str], dict[str, Any]] = {}
    for detail in details:
        for index, step in enumerate(detail.effect_path[:-1]):
            requirements = tuple(dict.fromkeys(step.local_requirements))
            if len(requirements) < 2:
                continue
            key = (step.node_index, step.tag, repr(step.value))
            operation = operations.setdefault(
                key,
                {
                    "step": step,
                    "path": detail.effect_path[: index + 1],
                    "by_requirement": {},
                },
            )
            if len(detail.effect_path[: index + 1]) < len(operation["path"]):
                operation["path"] = detail.effect_path[: index + 1]
            child = detail.effect_path[index + 1]
            for requirement in requirements:
                if (
                    detail.operation_boundary is not None
                    and detail.operation_boundary[0] == requirement[0]
                    and _values_match(detail.operation_boundary[1], requirement[1])
                    and child.tag == requirement[0]
                    and _values_match(child.value, requirement[1])
                ):
                    operation["by_requirement"].setdefault(requirement, []).append(detail.pair)

    reads: list[_candidate_read.CrossingBatchRead] = []
    seen: set[tuple[_ActionPair, ...]] = set()
    for operation in operations.values():
        step = operation["step"]
        pending = tuple(
            requirement
            for requirement in dict.fromkeys(step.local_requirements)
            if not _values_match(snapshot.get(requirement[0]), requirement[1])
        )
        if len(pending) < 2 or any(
            requirement not in operation["by_requirement"] for requirement in pending
        ):
            continue
        choices = tuple(
            tuple(dict.fromkeys(operation["by_requirement"][requirement]))
            for requirement in pending
        )
        # More than one action for a requirement is an OR, not permission to
        # enumerate speculative mixtures.  Leave that ambiguity to ordinary
        # orientation; a local operation batch is exact only when every open
        # input has one uniquely selected action receipt.
        if any(len(choice) != 1 for choice in choices):
            continue
        for selected in (tuple(choice[0] for choice in choices),):
            actions = tuple(dict.fromkeys(selected))
            if len(actions) < 2 or len({tag for tag, _value in actions}) != len(actions):
                continue
            if actions in seen:
                continue
            expectation = expectation_from_selected_path(
                operation["path"],
                pdg,
                program,
                boundary=None,
                selected_pairs=actions,
                snapshot=snapshot,
                steerable=steerable,
                require_ready=False,
            )
            if expectation is None:
                continue
            seen.add(actions)
            reads.append(
                _candidate_read.CrossingBatchRead(
                    actions=actions,
                    fidelity=CrossingFidelity(
                        constraints=(),
                        reason=f"exact local operation for {step.tag}={step.value!r}",
                        verify_required=True,
                        exact=True,
                        proposed=False,
                    ),
                    expectation=expectation,
                )
            )
    return tuple(reads)


# ---------------------------------------------------------------------------
# Stuck taxonomy
# ---------------------------------------------------------------------------

_STUCK_TRACE_OPAQUE = "trace_opaque"
_STUCK_TRACE_EMPTY = "trace_empty"
_STUCK_TRACE_GUARD = "trace_guard"


def _diagnose_stuck_reason(
    frame: Any,
    ctx: Any,
) -> str | None:
    """Classify *why* the instruments can't produce a bearing.

    Returns ``None`` when the tree has steerable leaves (not stuck).
    """
    tree = frame.tree
    leaves = list(tree.leaves())
    steerable = [n for n in leaves if n.is_steerable and not n.satisfied]
    if steerable:
        return None

    # A self-advancing (coast) leaf means let-run, not trace, owns the bearing —
    # a converging frontier (timer/counter Acc, or a harness-linked ramp toward a
    # threshold).  Not stuck: escalate to the terminal let-run rather than bail at
    # a dead-end.  This is the trace -> let-run rung of the compass escalation.
    if any(getattr(n, "advance", None) is not None and not n.satisfied for n in leaves):
        return None

    satisfied = [n for n in leaves if n.satisfied]
    if len(satisfied) == len(leaves) and leaves:
        return None

    # Writer found, all conditions satisfied (empty children) — the output
    # instruction just hasn't fired yet. This readiness has the same meaning at
    # every trace depth: a timer result can make a nested Step writer ready one
    # scan before the outer target writer observes Step. Let the loop coast.
    if any(
        node.writer_rung is not None
        and not node.children
        and not node.satisfied
        and not node.is_steerable
        for node in tree.iter_nodes()
    ):
        return None

    dead_ends = [
        n
        for n in leaves
        if not n.satisfied and not n.is_steerable and not getattr(n, "pipeline_internal", False)
    ]
    if not dead_ends:
        has_writers = any(ctx.pdg.writers_of.get(n.tag) for n in leaves if not n.satisfied)
        if not has_writers:
            return _STUCK_TRACE_EMPTY
        return _STUCK_TRACE_GUARD

    for n in dead_ends:
        if ctx.pdg.writers_of.get(n.tag):
            return _STUCK_TRACE_OPAQUE

    return _STUCK_TRACE_EMPTY


# ---------------------------------------------------------------------------
# Candidate building — the compass in one call
# ---------------------------------------------------------------------------


def _awaited_action_bearing(frame: Any, ctx: Any) -> AwaitedAction | None:
    """The program-awaited operator action for the current state, or
    ``None``.

    Consulted only when the target register is an opaque-loop pipeline channel
    (the shape a program-owned command detour lives on).  Delegates to the
    read-side recognizer ``awaited_actions.awaited_actions`` over a
    ``WalkContext`` assembled from the live frame; fail-closed everywhere else.
    """
    channel = ctx.target.tag
    if channel not in ctx.opaque_loop:
        return None
    from pyrung.core.analysis.pilot.types import WorldView

    world = WorldView(
        snapshot=frame.snap,
        pdg=ctx.pdg,
        program=ctx.program,
        steerable=ctx.steerable,
        opaque_loop=ctx.opaque_loop,
        prior=ctx.domain_prior,
    )
    return unique_legal_awaited_action(
        world,
        channel,
        ctx.pipeline_roles,
        action_allowed=lambda action: _action_allowed(ctx, action),
        action_avoided=lambda action: _avoid_forces(ctx, [action], frame.snap),
        awaits_operator=True,
    )


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
        from pyrung.core.analysis.pilot.types import WorldView

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


def _admit_trace_details(
    details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._TraceAdmission:
    """Apply one admission policy to every current-world trace reading.

    The broad target trace and supplemental completion/program reads differ in
    provenance, not privilege.  Duplicate details preserve the target trace's
    evidence while composing an owned lifetime discovered by the narrower
    read.  Nothing enters candidate ranking by being appended after this pass.
    """

    detail_by_pair: dict[_ActionPair, TraceAction] = {}
    ordered_details: list[TraceAction] = []
    for detail in details:
        pair = detail.pair
        matching_index = next(
            (
                index
                for index, existing in enumerate(ordered_details)
                if existing.pair == pair and existing.effect_path == detail.effect_path
            ),
            None,
        )
        if matching_index is None:
            ordered_details.append(detail)
        else:
            existing = ordered_details[matching_index]
            preferred = detail if detail.availability < existing.availability else existing
            lifetime_owner = next(
                (candidate for candidate in (existing, detail) if candidate.until is not None),
                None,
            )
            detail = replace(
                preferred,
                until=(lifetime_owner.until if lifetime_owner is not None else preferred.until),
                operation=(
                    lifetime_owner.operation
                    if lifetime_owner is not None and lifetime_owner.operation is not None
                    else preferred.operation
                ),
            )
            ordered_details[matching_index] = detail

        operational = detail_by_pair.get(pair)
        if operational is None:
            detail_by_pair[pair] = detail
            continue
        # The broad target trace and an exact ProgramStep read can describe
        # different effect paths for the same physical input. Compose only the
        # orthogonal execution facts: the narrower reader may prove present
        # availability while the outer route owns the honest lifetime. Effect
        # paths/provenance remain separate in ``ordered_details``.
        preferred = detail if detail.availability < operational.availability else operational
        lifetime_owner = next(
            (candidate for candidate in (operational, detail) if candidate.until is not None),
            None,
        )
        detail_by_pair[pair] = replace(
            preferred,
            until=(lifetime_owner.until if lifetime_owner is not None else preferred.until),
            operation=(
                lifetime_owner.operation
                if lifetime_owner is not None and lifetime_owner.operation is not None
                else preferred.operation
            ),
        )

    active_details = tuple(
        detail
        for detail in ordered_details
        for pair in (detail.pair,)
        if _action_allowed(ctx, pair)
        and (
            not _values_match(frame.snap.get(pair[0]), pair[1])
            or pair[0] in ctx.edge_tags
            or detail.pulse
            or detail.until is not None
        )
    )
    spent_edges = frozenset(
        tag
        for tag in getattr(ctx, "edge_tags", ())
        if not _values_match(
            frame.snap.get(tag),
            getattr(ctx, "resting", {}).get(tag, False),
        )
        and any(
            detail.tag == tag
            and _values_match(
                detail.value,
                getattr(ctx, "resting", {}).get(tag, False),
            )
            for detail in active_details
        )
    )
    if spent_edges:
        # A selected trace can contain both the next assertion and its release
        # edge.  When the input is still asserted, the release is the current
        # operation: asserting again would hide that scan inside _apply_pulse
        # and verify a later route promise against pre-release state.  Preserve
        # trace order otherwise; fresh orientation will reread after the
        # ordinary release bearing lands.
        active_details = tuple(
            sorted(
                active_details,
                key=lambda detail: (
                    0
                    if detail.tag in spent_edges
                    and _values_match(
                        detail.value,
                        getattr(ctx, "resting", {}).get(detail.tag, False),
                    )
                    else 1
                ),
            )
        )
    trace_action_details = tuple(
        detail for detail in active_details if detail.pair not in key_nogoods
    )
    trace_actions = tuple(dict.fromkeys(detail.pair for detail in trace_action_details))
    active_trace_actions = tuple(dict.fromkeys(detail.pair for detail in active_details))

    managed_boolean_rungs, lowered_rung_pairs = _managed_boolean_rungs(
        trace_action_details, frame, state, ctx
    )
    if lowered_rung_pairs:
        trace_actions = tuple(pair for pair in trace_actions if pair not in lowered_rung_pairs)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair not in lowered_rung_pairs
        )
        trace_action_details = tuple(
            detail for detail in trace_action_details if detail.pair not in lowered_rung_pairs
        )

    establish_details = tuple(detail for detail in trace_action_details if detail.establish)
    establish_pending = bool(establish_details)
    if establish_pending:
        establish_pairs = {detail.pair for detail in establish_details}
        trace_actions = tuple(pair for pair in trace_actions if pair in establish_pairs)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair in establish_pairs
        )
        trace_action_details = establish_details

    return _candidate_read._TraceAdmission(
        active_actions=active_trace_actions,
        actions=trace_actions,
        read_details=tuple(ordered_details),
        details=trace_action_details,
        detail_by_pair=MappingProxyType(detail_by_pair),
        managed_boolean_rungs=managed_boolean_rungs,
        establish_pending=establish_pending,
    )


def _admit_wait_read(
    read: _candidate_read.WaitRead,
    base_details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._AdmittedWait:
    """Admit one whole wait read through the candidate pool's only policy."""

    return _candidate_read._AdmittedWait(
        read=read,
        admission=_admit_trace_details(
            (*base_details, *read.details),
            frame,
            state,
            ctx,
            key_nogoods,
        ),
    )


def _read_route_and_wait(
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._RouteAndCompletionRead:
    """Read the current trace, static route, and charted completion together."""

    admission = _admit_trace_details(
        tuple(frame.raw_trace_action_details),
        frame,
        state,
        ctx,
        key_nogoods,
    )
    earned_work = getattr(state, "earned_work", None)
    current_trace_actions = tuple(
        pair
        for pair in admission.actions
        for detail in (admission.detail_by_pair.get(pair),)
        if detail is not None and detail.availability <= _WriterAvailability.AFTER_PREREQ
    )
    banked_trace_work = bool(
        admission.actions and earned_work is not None and earned_work.has_banked_work(frame.snap)
    )
    route_blocked = bool(
        current_trace_actions
        or banked_trace_work
        or (getattr(state, "pending_departure", None) is not None and admission.active_actions)
    )
    route_plan = (
        None if route_blocked else _compass_route_plan(frame, ctx, key_nogoods, state=state)
    )
    admitted_completion: _candidate_read._AdmittedWait | None = None
    if route_plan is not None and route_plan.first_edge.action is not None:
        general = _general_chart_completion_plan(
            frame,
            ctx,
            key_nogoods,
            state=state,
            allow_conservative_nomination=True,
        )
        same_first_transition = bool(
            general is not None
            and general.first_edge.role.channel_tag == route_plan.first_edge.role.channel_tag
            and _values_match(general.first_edge.from_value, route_plan.first_edge.from_value)
            and _values_match(general.first_edge.to_value, route_plan.first_edge.to_value)
        )
        if general is not None and not same_first_transition:
            general_admitted = _admit_wait_read(
                _prescribe_wait(general.first_edge, frame, state, ctx),
                tuple(frame.raw_trace_action_details),
                frame,
                state,
                ctx,
                key_nogoods,
            )
            if general_admitted.viable or not general.first_edge.program_producers:
                route_plan = general
                admitted_completion = general_admitted
    if route_plan is None and not route_blocked:
        route_plan = _general_chart_completion_plan(
            frame,
            ctx,
            key_nogoods,
            state=state,
        )
    if route_plan is not None:
        completion_edge = _live_chart_completion_edge(
            route_plan.first_edge,
            frame,
            state,
            ctx,
        )
        if completion_edge is not None:
            route_plan = replace(
                route_plan,
                edges=(completion_edge, *route_plan.edges[1:]),
            )
    is_charted_completion = (
        not admission.establish_pending
        and route_plan is not None
        and route_plan.first_edge.action is None
    )
    unavailable_producer_edges: set[tuple[Any, ...]] = set()
    while is_charted_completion:
        assert route_plan is not None
        if admitted_completion is None:
            admitted_completion = _admit_wait_read(
                _prescribe_wait(route_plan.first_edge, frame, state, ctx),
                tuple(frame.raw_trace_action_details),
                frame,
                state,
                ctx,
                key_nogoods,
            )
        if (
            admitted_completion.viable
            or admitted_completion.admitted_supplement
            or not route_plan.first_edge.program_producers
        ):
            break
        unavailable_producer_edges.add(route_plan.first_edge.identity)
        alternate = _compass_route_plan(
            frame,
            ctx,
            key_nogoods,
            frozenset(unavailable_producer_edges),
            state=state,
        )
        if alternate is None:
            break
        route_plan = alternate
        admitted_completion = None
        is_charted_completion = (
            not admission.establish_pending and route_plan.first_edge.action is None
        )

    route_candidates = (
        ()
        if is_charted_completion or admission.establish_pending
        else _compass_route_actions(route_plan, frame, ctx, key_nogoods)
    )
    route_co_actions = (
        tuple(route_plan.first_edge.co_actions)
        if route_candidates and route_plan is not None
        else ()
    )
    charted_completion: _candidate_read.WaitRead | None = None
    if is_charted_completion:
        assert admitted_completion is not None
        charted_completion = admitted_completion.candidate_read
        admission = admitted_completion.admission

    route = (
        _candidate_read.RouteRead(route_plan, route_candidates, route_co_actions)
        if route_plan is not None
        else None
    )
    return _candidate_read._RouteAndCompletionRead(admission, route, charted_completion)


def _separate_prerequisites(
    route_and_wait: _candidate_read._RouteAndCompletionRead,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _candidate_read._PrerequisiteSeparation:
    """Separate executable holds without selecting among wait sources."""

    admission = route_and_wait.trace
    is_charted_completion = route_and_wait.charted_wait is not None
    is_coast = any(
        getattr(node, "advance", None) is not None and not node.satisfied
        for node in frame.tree.leaves()
    )
    instruction_boundary: ChannelHeading | None = None
    instruction_node: Any | None = None
    if is_coast:
        for node in frame.tree.leaves():
            step = getattr(node, "advance", None)
            if step is None or node.satisfied:
                continue
            boundary_pair = (
                getattr(node, "owner_boundary", None)
                if getattr(node, "linear_boundary", False)
                else None
            )
            if boundary_pair is not None:
                boundary = (
                    getattr(node, "owner_condition", None)
                    if getattr(node, "linear_boundary", False)
                    else None
                ) or step.until
                channel_tag, target_value = boundary_pair
                instruction_boundary = ChannelHeading(
                    channel_tag=channel_tag,
                    target_value=target_value,
                    boundary=boundary,
                )
            else:
                instruction_boundary = _boundary_heading(step.until, frame, state)
            if instruction_boundary is not None:
                instruction_node = node
                break
    if instruction_boundary is not None:
        trace_owned_rendezvous = any(
            detail.until is not None and not detail.pulse for detail in admission.details
        )
        hard_blockers = tuple(
            node
            for node in frame.tree.leaves()
            if node is not instruction_node
            and not node.satisfied
            and not node.is_steerable
            and getattr(node, "advance", None) is None
            and not getattr(node, "pipeline_internal", False)
        )
        if hard_blockers and not trace_owned_rendezvous:
            # A standalone instruction boundary is only coastable inside its
            # selected route shape.  Reaching a timer Done through an alternate
            # enable while a non-steerable sibling guard is false is not route
            # progress and must leave the next trace alternative visible. A
            # trace-owned lifetime is different: it is the selected producer's
            # rendezvous receipt, so durable siblings are positioned before
            # coasting that instruction boundary.
            instruction_boundary = None

    prerequisite_pilot_rungs = list(admission.managed_boolean_rungs)
    trace_actions = admission.actions
    active_trace_actions = admission.active_actions
    trace_action_details = admission.details
    if is_charted_completion or is_coast:
        completion_detail_pairs = (
            frozenset(detail.pair for detail in route_and_wait.charted_wait.details)
            if is_charted_completion and route_and_wait.charted_wait is not None
            else frozenset()
        )
        route = route_and_wait.route
        if route is not None:
            edge = route.plan.first_edge
            route_actions = () if edge.action is None else (edge.action, *edge.co_actions)
            route_request = (
                () if edge.request_tag is None else ((edge.request_tag, edge.request_value),)
            )
            route_needed = (
                *route_actions,
                *route_request,
                *edge.source_constraints,
                *edge.enablers,
                *edge.completion,
            )
        else:
            route_needed = ()
        # A prerequisite cannot make the selected route non-executable.  Put
        # the chart's immediate edge first because it is the concrete bearing;
        # the broader target trace follows as supporting evidence.
        needed = (*route_needed, *frontier_pairs(frame.tree, frame.snap))
        prerequisite_pilot_rungs = [
            rung
            for rung in prerequisite_pilot_rungs
            if not hold_defeats_needed(rung.dest, rung.value, needed, ctx.pdg, ctx.program)
        ]
        pulse_tags = {detail.tag for detail in trace_action_details if detail.pulse}
        seen_prereq: set[str] = set()
        for tag, value in trace_actions:
            if tag in seen_prereq or tag in {rung.dest for rung in state.pilot_rungs}:
                continue
            detail = admission.detail_by_pair.get((tag, value))
            if detail is None or detail.until is None:
                continue
            if (
                detail.availability > _WriterAvailability.AFTER_PREREQ
                and not _values_match(value, ctx.resting.get(tag, False))
                and (tag, value) not in completion_detail_pairs
                and (is_charted_completion or instruction_boundary is None)
            ):
                # A chart route and a target-wide trace are separate readings:
                # only the chart's selected completion detail may position an
                # unavailable future input for that coast. A standalone
                # instruction boundary is different. It came from this same
                # trace, so the trace-owned lifetime is the exact rendezvous
                # receipt for concurrent inputs which must persist while the
                # instruction advances. Releases remain valid so a spent
                # transaction cannot block later structural motion.
                continue
            scope = _until_unresolved_condition(state.work, detail.until)
            if tag in pulse_tags:
                seen_prereq.add(tag)
                if _action_allowed(ctx, (tag, value)):
                    prerequisite_pilot_rungs.extend(_oscillating_rungs(tag, ctx, scope, state.work))
            elif (
                tag not in ctx.edge_tags
                and tag not in ctx.clear_only
                and not detail.pulse
                and not _values_match(frame.snap.get(tag), value)
            ):
                if hold_defeats_needed(tag, value, needed, ctx.pdg, ctx.program):
                    continue
                seen_prereq.add(tag)
                if _action_allowed(ctx, (tag, value)) and not _avoid_forces(
                    ctx, [(tag, value)], frame.snap
                ):
                    prerequisite_pilot_rungs.append(PilotRung(tag, value, scope))
        prereq_tags = {rung.dest for rung in prerequisite_pilot_rungs}
        trace_actions = tuple(pair for pair in trace_actions if pair[0] not in prereq_tags)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair[0] not in prereq_tags
        )

    updated_trace = replace(
        admission,
        active_actions=active_trace_actions,
        actions=trace_actions,
        details=trace_action_details,
    )
    return _candidate_read._PrerequisiteSeparation(
        updated_trace,
        _candidate_read.PrerequisiteRead(tuple(prerequisite_pilot_rungs)),
        instruction_boundary,
    )


def _read_learned_fallback(
    route_and_wait: _candidate_read._RouteAndCompletionRead,
    separated: _candidate_read._PrerequisiteSeparation,
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._LearnedFallback | None:
    """Read exactly one learned wait, action, or batch fallback."""

    route = route_and_wait.route
    route_plan = route.plan if route is not None else None
    route_candidates = route.candidates if route is not None else ()
    local_bearing_open = bool(
        separated.trace.actions or route_candidates or route_and_wait.charted_wait is not None
    )
    probed_leaf_states: set[tuple[str, Any]] = set()
    nodes = (
        () if separated.trace.establish_pending or local_bearing_open else frame.tree.iter_nodes()
    )
    for node in nodes:
        unreadable = getattr(node, "live_guard", False) or (
            getattr(node, "pipeline_internal", False)
            and route_plan is None
            and ctx.compass.knowledge.has_transitions(
                node.tag,
                world_key=frame.key,
                snapshot=frame.snap,
            )
        )
        if (
            (node.children and not unreadable)
            or node.satisfied
            or node.is_steerable
            or (getattr(node, "pipeline_internal", False) and not unreadable)
        ):
            continue
        current_value = frame.snap.get(node.tag)
        if _values_match(current_value, node.value):
            continue
        leaf_state = (node.tag, current_value)
        if leaf_state in probed_leaf_states:
            continue
        probed_leaf_states.add(leaf_state)

        def _learned_edge_open(
            source: Any,
            cause: Any,
            destination: Any,
            *,
            tag: str = node.tag,
        ) -> bool:
            return _learned_edge_allowed(
                tag,
                source,
                cause,
                destination,
                frame,
                ctx,
                key_nogoods,
            )

        path = ctx.compass.knowledge.find_path(
            node.tag,
            current_value,
            node.value,
            cause_allowed=_learned_edge_open,
            world_key=frame.key,
            snapshot=frame.snap,
        )
        if not path:
            continue
        first_step = path[0]
        first_destination = ctx.compass.knowledge.transition_dest(
            node.tag,
            current_value,
            first_step,
            world_key=frame.key,
            snapshot=frame.snap,
        )
        learned_expectation = _unique_learned_expectation(
            ctx.compass.knowledge.tag_entries(
                node.tag,
                world_key=frame.key,
                snapshot=frame.snap,
            ),
            source=current_value,
            cause=first_step,
            destination=first_destination,
        )
        if not is_action(first_step):
            return _candidate_read._LearnedWait(
                _prescribe_wait(
                    None,
                    frame,
                    state,
                    ctx,
                    reason=f"{node.tag}: {current_value!r}->{node.value!r}",
                )
            )
        if is_composite_action(first_step):
            members = cast("tuple[_ActionPair, ...]", tuple(first_step))
            if all(pair not in key_nogoods and _action_allowed(ctx, pair) for pair in members):
                return _candidate_read._LearnedBatch(
                    _candidate_read.LearnedBatchRead(members, learned_expectation)
                )
            continue
        if first_step not in key_nogoods and _action_allowed(ctx, first_step):
            return _candidate_read._LearnedAction(first_step, learned_expectation)
    return None


def _unique_learned_expectation(
    entries: Any,
    *,
    source: Any,
    cause: Any,
    destination: Any,
) -> EffectExpectation | None:
    """Return one semantic first-edge promise, never an artifact-order choice."""

    unique: list[tuple[tuple[Any, ...], EffectExpectation]] = []
    for entry_source, entry_cause, entry in entries:
        expectation = entry.expectation
        if (
            expectation is None
            or not _values_match(entry_source, source)
            or entry_cause != cause
            or not _values_match(entry.to_val, destination)
        ):
            continue
        snapshot = expectation_snapshot(expectation)
        if not any(snapshot == existing for existing, _expectation in unique):
            unique.append((snapshot, expectation))
    return unique[0][1] if len(unique) == 1 else None


def _select_wait(
    *,
    charted_completion: _candidate_read.WaitRead | None,
    instruction_boundary: ChannelHeading | None,
    learned: _candidate_read._LearnedFallback | None,
    has_candidates: bool,
) -> _candidate_read.WaitRead | None:
    """Select one wait from three explicit evidence sources.

    This is the sole chooser among learned motion, charted completion, and an
    instruction-owned boundary. A prescribed learned wait wins; otherwise
    charted completion wins. A standalone instruction boundary is selected
    only when there are no action candidates and neither earlier source
    supplied a prescription.

    When charted completion and an instruction boundary describe the same
    current work, the instruction heading is rebound with the chart route
    context before precedence is applied.
    """

    charted = charted_completion
    if (
        charted is not None
        and instruction_boundary is not None
        and charted.program_step is None
        and charted.prescription is not None
    ):
        route_context = (
            charted.prescription.heading.route if charted.prescription.heading is not None else None
        )
        heading = replace(instruction_boundary, route=route_context)
        charted = replace(
            charted,
            prescription=replace(charted.prescription, heading=heading),
        )

    selected = learned.read if isinstance(learned, _candidate_read._LearnedWait) else None
    if charted is not None and (selected is None or selected.prescription is None):
        selected = charted
    if (
        instruction_boundary is not None
        and not has_candidates
        and (selected is None or selected.prescription is None)
    ):
        reason = (
            f"advance {instruction_boundary.channel_tag} to its next boundary "
            f"{instruction_boundary.target_value!r}"
        )
        selected = _candidate_read.WaitRead(
            _candidate_read.WaitPrescription(
                instruction_boundary,
                reason,
                frontier=(),
            )
        )
    return selected


def _assemble_candidate_read(
    route_and_wait: _candidate_read._RouteAndCompletionRead,
    separated: _candidate_read._PrerequisiteSeparation,
    learned: _candidate_read._LearnedFallback | None,
    awaited_action: AwaitedAction | None,
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
    *,
    state: Any = None,
) -> _candidate_read.CandidateRead:
    """Compose the final durable candidate read from explicit phase receipts."""

    trace = separated.trace
    route = route_and_wait.route
    route_plan = route.plan if route is not None else None
    route_candidates = route.candidates if route is not None else ()
    trace_actions = trace.actions
    downstream_reach_cap = 20
    broad_reach_actions: tuple[_ActionPair, ...] = ()
    if len(trace_actions) > 1:
        reach_by_tag = {
            tag: len(ctx.pdg.downstream_slice(tag, follow_calls=True))
            for tag, _value in trace_actions
        }
        median_reach = sorted(reach_by_tag.values())[len(reach_by_tag) // 2] if reach_by_tag else 0
        downstream_reach_cap = max(median_reach * 3, 20)
        broad_reach_actions = tuple(
            (tag, value)
            for tag, value in trace_actions
            if reach_by_tag.get(tag, 0) > downstream_reach_cap
        )
        trace_actions = tuple(
            (tag, value)
            for tag, value in trace_actions
            if reach_by_tag.get(tag, 0) <= downstream_reach_cap
        )

    learned_action = learned.action if isinstance(learned, _candidate_read._LearnedAction) else None
    learned_action_expectation = (
        learned.expectation if isinstance(learned, _candidate_read._LearnedAction) else None
    )
    program_step = (
        route_and_wait.charted_completion.program_step
        if route_and_wait.charted_completion is not None
        else None
    )
    program_pairs = program_step.required_pairs if program_step is not None else frozenset()
    tree = getattr(frame, "tree", None)
    crossing_branch_reads = (
        tree.ordered_crossing_branches()
        if tree is not None and hasattr(tree, "ordered_crossing_branches")
        else ()
    )
    structural_crossing_batches: tuple[_candidate_read.CrossingBatchRead, ...] = tuple(
        _candidate_read.CrossingBatchRead(
            actions=branch.pairs,
            fidelity=branch.fidelity,
            expectation=expectation_from_selected_path(
                branch.effect_path,
                ctx.pdg,
                ctx.program,
                boundary=None,
                selected_pairs=branch.pairs,
                snapshot=frame.snap,
                steerable=ctx.steerable,
            )
            if branch.effect_path
            else None,
        )
        for branch in crossing_branch_reads
        # Crossing conjunctions are executable artifacts in their own right.
        # Pair nogoods project only from the identical singleton artifact; a
        # multi-action overlay is vetoed member-wise only by explicit policy.
        if all(_action_allowed(ctx, pair) for pair in branch.pairs)
        and not (len(branch.pairs) == 1 and branch.pairs[0] in key_nogoods)
        # Re-check every predecessor fact against the complete planned overlay.
        # This catches a selected action invalidating a conjunct that happened
        # to be true in the snapshot when the crossing was lowered.
        and all(
            constraint_holds(
                constraint,
                {**frame.snap, **dict(branch.pairs)},
            )
            is True
            for constraint in branch.constraints
        )
    )
    operation_batches = (
        _effect_operation_batches(
            trace.details,
            frame.snap,
            ctx.pdg,
            ctx.program,
            getattr(ctx, "steerable", frozenset()),
        )
        if trace.details and getattr(ctx, "program", None) is not None
        else ()
    )
    crossing_batches = tuple(
        batch
        for index, batch in enumerate((*operation_batches, *structural_crossing_batches))
        if batch.actions
        not in {
            prior.actions for prior in (*operation_batches, *structural_crossing_batches)[:index]
        }
    )
    candidates: list[_candidate_read._Candidate] = []
    seen_candidates: set[_ActionPair] = set()
    route_candidate_set = set(route_candidates)

    def _candidate_for(
        pair: _ActionPair,
        detail_override: TraceAction | None = None,
    ) -> _candidate_read._Candidate:
        source = (
            ActSource.ROUTE
            if pair in route_candidate_set
            else ActSource.LEARNED_ACTION
            if pair == learned_action
            else ActSource.PROGRAM
            if pair in program_pairs
            else ActSource.TRACE
        )
        # A same-pair trace detail is not a receipt for another reader's
        # selected producer.  Trace alternatives pass their exact detail;
        # route/program/learned readers must lower their own receipt.
        detail = detail_override if source is ActSource.TRACE else None
        prescribed_edge = (
            route_plan.first_edge
            if route_plan is not None and pair in route_candidate_set
            else None
        )
        route_writer_effect = (
            prescribed_edge.route.writer_effect if prescribed_edge is not None else None
        )
        route_effect_paths = (
            tuple(
                trace_detail.effect_path
                for trace_detail in trace.details
                if prescribed_edge is not None
                and trace_detail.pair == pair
                and trace_detail.effect_path
                and trace_detail.effect_path[-1].node_index == prescribed_edge.route.writer_node
                and route_writer_effect is not None
                and trace_detail.effect_path[-1].tag == route_writer_effect[0]
                and _values_match(trace_detail.effect_path[-1].value, route_writer_effect[1])
            )
            if prescribed_edge is not None
            else ()
        )
        route_effect_path = route_effect_paths[0] if len(route_effect_paths) == 1 else None
        route_expectation = None
        if prescribed_edge is not None:
            if route_effect_path is not None:
                route_expectation = expectation_from_selected_path(
                    route_effect_path,
                    ctx.pdg,
                    ctx.program,
                    boundary=(prescribed_edge.role.channel_tag, prescribed_edge.to_value),
                    selected_pairs=(pair, *prescribed_edge.co_actions),
                    snapshot=frame.snap,
                    steerable=getattr(ctx, "steerable", frozenset()),
                )
            if route_expectation is None:
                # The complete target path may name a later consumer whose
                # structural source has not been reached yet. The selected
                # edge still owns its immediate carrier/request handoff; keep
                # that exact writer receipt instead of dropping all effect
                # evidence for this bearing.
                route_expectation = _expectation_from_route_writer(
                    ctx,
                    prescribed_edge,
                    frame.snap,
                )
        route_context = None
        if prescribed_edge is not None:
            effect_tag, effect_value = prescribed_edge.route.writer_effect
            route_context = RouteEdgeContext(
                prescribed_edge.role.channel_tag,
                prescribed_edge.from_value,
                prescribed_edge.to_value,
                effect_tag,
                effect_value,
            )
        program_handoff = (
            program_step.handoff_by_action.get(pair)
            if source is ActSource.PROGRAM and program_step is not None
            else None
        )
        program_receipt_details = (
            tuple(
                trace_detail
                for trace_detail in trace.details
                if trace_detail.pair == pair
                and trace_detail.effect_path
                and trace_detail.effect_path[-1].node_index == program_step.producer.rung_index
                and trace_detail.effect_path[-1].tag == program_step.producer.command_tag
                and _values_match(
                    trace_detail.effect_path[-1].value,
                    program_step.producer.command_value,
                )
            )
            if source is ActSource.PROGRAM and program_step is not None
            else ()
        )
        program_context_actions = (
            tuple(
                dict.fromkeys(
                    (
                        *(
                            required.pair
                            for required in program_step.required_inputs
                            if required.pair != pair
                        ),
                        *program_step.context_actions,
                    )
                )
            )
            if source is ActSource.PROGRAM and program_step is not None
            else ()
        )
        program_heading = (
            _boundary_heading(program_handoff.boundary, frame, state)
            if program_handoff is not None and state is not None
            else None
        )
        program_consumer_expectations = tuple(
            expectation
            for receipt_detail in program_receipt_details
            if (
                expectation := expectation_from_selected_path(
                    receipt_detail.effect_path,
                    ctx.pdg,
                    ctx.program,
                    boundary=receipt_detail.operation_boundary,
                    selected_pairs=(pair, *program_context_actions),
                    snapshot=frame.snap,
                    steerable=getattr(ctx, "steerable", frozenset()),
                )
            )
            is not None
            and expectation.obligations[0].consumer is not None
        )
        program_route_edge = (
            _charted_program_edge(ctx, program_step, frame.snap)
            if source is ActSource.PROGRAM and program_step is not None
            else None
        )
        program_route_expectation = (
            _expectation_from_route_writer(ctx, program_route_edge, frame.snap)
            if program_route_edge is not None
            else None
        )
        program_expectation = (
            program_consumer_expectations[0]
            if len(program_consumer_expectations) == 1
            else program_route_expectation
            if program_route_expectation is not None
            and program_route_expectation.obligations[0].consumer is not None
            else next(
                (
                    expectation_from_selected_path(
                        required.effect_path,
                        ctx.pdg,
                        ctx.program,
                        boundary=(
                            program_heading.channel_tag,
                            program_heading.target_value,
                        )
                        if program_heading is not None
                        else None,
                        selected_pairs=(pair, *program_context_actions),
                        snapshot=frame.snap,
                        steerable=getattr(ctx, "steerable", frozenset()),
                    )
                    for required in program_step.required_inputs
                    if required.pair == pair and required.effect_path
                ),
                None,
            )
            if source is ActSource.PROGRAM and program_step is not None
            else None
        )
        return _candidate_read._Candidate(
            tag=pair[0],
            value=pair[1],
            source=source,
            provenance=detail.provenance if detail is not None else (),
            downstream_reach=(
                detail.downstream_reach
                if detail is not None and detail.downstream_reach is not None
                else len(ctx.pdg.downstream_slice(pair[0], follow_calls=True))
            ),
            bearing_channel_tag=(
                detail.operation_boundary[0]
                if detail is not None and detail.operation_boundary is not None
                else prescribed_edge.role.channel_tag
                if prescribed_edge is not None
                else program_heading.channel_tag
                if program_heading is not None
                else route_plan.role.channel_tag
                if pair in program_pairs and route_plan is not None
                else None
            ),
            bearing_channel_value=(
                detail.operation_boundary[1]
                if detail is not None and detail.operation_boundary is not None
                else prescribed_edge.to_value
                if prescribed_edge is not None
                else program_heading.target_value
                if program_heading is not None
                else route_plan.first_edge.to_value
                if pair in program_pairs and route_plan is not None
                else None
            ),
            bearing_boundary=(program_heading.boundary if program_heading is not None else None),
            route_context=route_context,
            program_note=(
                f"exact program producer rung {program_step.producer.rung_index} "
                f"currently needs {pair[0]}={pair[1]!r}"
                if pair in program_pairs and program_step is not None
                else ""
            ),
            program_context_actions=(
                program_context_actions
                if pair in program_pairs and program_step is not None
                else ()
            ),
            expectation=(
                expectation_from_selected_path(
                    detail.effect_path,
                    ctx.pdg,
                    ctx.program,
                    boundary=detail.operation_boundary,
                    selected_pairs=(pair,),
                    snapshot=frame.snap,
                    steerable=getattr(ctx, "steerable", frozenset()),
                )
                if detail is not None
                else route_expectation
                if prescribed_edge is not None
                else program_expectation
                if source is ActSource.PROGRAM and program_step is not None
                else learned_action_expectation
                if source is ActSource.LEARNED_ACTION
                else None
            ),
        )

    for pair in route_candidates:
        if _action_allowed(ctx, pair) and pair not in seen_candidates:
            seen_candidates.add(pair)
            candidates.append(_candidate_for(pair))
    if program_step is not None:
        # ProgramStep is the present-tense reading of the selected first-edge
        # producer.  Its admitted input must precede broad target-trace leaves
        # which describe work beyond that producer (and may explicitly be
        # UNAVAILABLE_FROM_HERE).  This is ordering by execution evidence, not
        # a new source of action authority: the pair still had to survive the
        # shared trace-admission pass above.
        for required in program_step.required_inputs:
            pair = required.pair
            if pair in trace_actions and _action_allowed(ctx, pair) and pair not in seen_candidates:
                seen_candidates.add(pair)
                candidates.append(_candidate_for(pair))
    for pair in trace_actions:
        for detail in (detail for detail in trace.details if detail.pair == pair):
            candidate = _candidate_for(pair, detail)
            if any(
                existing.pair == pair and existing.expectation == candidate.expectation
                for existing in candidates
            ):
                continue
            seen_candidates.add(pair)
            candidates.append(candidate)
    if (
        learned_action is not None
        and _action_allowed(ctx, learned_action)
        and learned_action not in seen_candidates
    ):
        seen_candidates.add(learned_action)
        candidates.append(_candidate_for(learned_action))
    for pair in broad_reach_actions:
        for detail in (detail for detail in trace.details if detail.pair == pair):
            candidate = _candidate_for(pair, detail)
            if any(
                existing.pair == pair and existing.expectation == candidate.expectation
                for existing in candidates
            ):
                continue
            seen_candidates.add(pair)
            candidates.append(candidate)

    if awaited_action is not None and not any(
        candidate.source is ActSource.TRACE for candidate in candidates
    ):
        pair = awaited_action.action
        if (
            _action_allowed(ctx, pair)
            and pair not in seen_candidates
            and pair not in key_nogoods
            and (not _values_match(frame.snap.get(pair[0]), pair[1]) or pair[0] in ctx.edge_tags)
        ):
            candidates.append(
                replace(
                    _candidate_for(pair),
                    source=ActSource.AWAITED_ACTION,
                    awaited_action_note=awaited_action.note,
                    bearing_channel_tag=(
                        awaited_action.channel_tag or awaited_action.target_tag or None
                    ),
                    bearing_channel_value=awaited_action.to_state,
                    # The structural reading proves the request -> channel
                    # handoff, but its selected writer may own an ephemeral
                    # request register. Verify the durable channel heading;
                    # do not demand that the consumed request survive S1/S2.
                    expectation=(
                        None
                        if awaited_action.channel_tag
                        else expectation_from_writer(
                            ctx.pdg,
                            ctx.program,
                            writer_node=awaited_action.writer_node,
                            tag=awaited_action.target_tag,
                            value=awaited_action.to_state,
                            required_shape=awaited_action.required_shape,
                            boundary=(awaited_action.target_tag, awaited_action.to_state),
                        )
                        if awaited_action.writer_node >= 0
                        and awaited_action.target_tag
                        and awaited_action.to_state is not None
                        else None
                    ),
                )
            )

    wait = _select_wait(
        charted_completion=route_and_wait.charted_wait,
        instruction_boundary=separated.instruction_boundary,
        learned=learned,
        has_candidates=bool(candidates or crossing_batches),
    )
    learned_batch = learned.read if isinstance(learned, _candidate_read._LearnedBatch) else None
    stuck_reason: str | None = None
    if (
        not candidates
        and not separated.prerequisites.pilot_rungs
        and (wait is None or wait.prescription is None)
        and learned_batch is None
        and not crossing_batches
    ):
        stuck_reason = _diagnose_stuck_reason(frame, ctx)

    final_trace = replace(trace, actions=trace_actions)
    widening_expectations: list[tuple[tuple[_ActionPair, ...], EffectExpectation]] = []
    for width in range(2, len(final_trace.active_actions) + 1):
        artifact = final_trace.active_actions[:width]
        primary = artifact[0]
        primary_paths = tuple(
            detail
            for detail in final_trace.details
            if detail.pair == primary and detail.effect_path
        )
        # The co-actions make the wider artifact executable; they do not
        # independently select which producer the artifact promises.  A
        # primary action with multiple exact paths remains unresolved.
        if len(primary_paths) != 1:
            continue
        expectation = expectation_from_selected_path(
            primary_paths[0].effect_path,
            ctx.pdg,
            ctx.program,
            boundary=primary_paths[0].operation_boundary,
            selected_pairs=artifact,
            snapshot=frame.snap,
            steerable=getattr(ctx, "steerable", frozenset()),
        )
        if expectation is not None:
            widening_expectations.append((artifact, expectation))
    return _candidate_read.CandidateRead(
        trace=final_trace,
        options=tuple(candidates),
        downstream_reach_cap=downstream_reach_cap,
        route=route,
        wait=wait,
        prerequisites=separated.prerequisites,
        learned_batch=learned_batch,
        crossing_batches=crossing_batches,
        diagnosis=_candidate_read.CandidateDiagnosis(stuck_reason)
        if stuck_reason is not None
        else None,
        widening_expectations=tuple(widening_expectations),
    )


def _build_candidates(
    frame: Any,
    state: Any,
    ctx: Any,
) -> _candidate_read.CandidateRead:
    """Build one candidate read through explicit evidence-owning phases."""

    key_nogoods = set(ctx.compass.knowledge.nogood_pairs(frame.key))
    route_and_wait = _read_route_and_wait(frame, state, ctx, key_nogoods)
    separated = _separate_prerequisites(route_and_wait, frame, state, ctx)
    learned = _read_learned_fallback(
        route_and_wait,
        separated,
        frame,
        state,
        ctx,
        key_nogoods,
    )
    awaited_action = _awaited_action_bearing(frame, ctx)
    return _assemble_candidate_read(
        route_and_wait,
        separated,
        learned,
        awaited_action,
        frame,
        ctx,
        key_nogoods,
        state=state,
    )


# ---------------------------------------------------------------------------
# Pulse-action helpers
# ---------------------------------------------------------------------------


def _candidate_applied(
    candidate: _candidate_read._Candidate,
    candidates: _candidate_read.CandidateRead,
    ctx: Any,
) -> tuple[_ActionPair, ...]:
    pair = candidate.pair
    actions: list[_ActionPair] = [pair]
    seen: set[str] = {pair[0]}

    # A route-prescribed command carries its co-actions (the one-shot edge gate);
    # they must fire in the same scan or the command rung never executes.
    route = candidates.route
    if candidate.source is ActSource.ROUTE and route is not None:
        for co in route.co_actions:
            if co[0] not in seen:
                actions.append(co)
                seen.add(co[0])

    if candidate.source is ActSource.PROGRAM:
        for co in candidate.program_context_actions:
            # A pulse's own release/assert sequence is handled by _apply_pulse;
            # only independent context belongs in the atomic action set.
            if co[0] not in seen:
                actions.append(co)
                seen.add(co[0])

    # A trace-selected convergence command may need the other conjuncts from
    # that same trace artifact.  A ROUTE bearing is already a closed executable
    # artifact: its exact edge owns ``co_actions`` above and its admitted steady
    # prerequisites below.  Folding target-wide trace leaves into it would let
    # an unavailable downstream writer hitchhike on a current state command
    # (for example, production/heat inputs riding on PackML Clear).
    if (
        candidate.source is not ActSource.ROUTE
        and candidate.tag in ctx.compass.action_tags
        and candidates.trace.active_actions
    ):
        # A pair rejected as a standalone act remains valid context for a
        # different atomic act.  Fresh orientation therefore keeps it out of
        # the candidate queue while still allowing the joint pulse to be
        # judged under its own Bearing identity.
        for ta in candidates.trace.active_actions:
            if ta[0] not in seen:
                actions.append(ta)
                seen.add(ta[0])

    # Prerequisite holds (trace actions split into rungs for a bearing coast)
    # are applied to the fork but were removed from trace_actions — record them
    # so the scan_log faithfully captures everything the fork sees.
    for rung in candidates.prerequisites.pilot_rungs:
        tag, value = rung.dest, rung.value
        if tag not in seen:
            actions.append((tag, value))
            seen.add(tag)

    return tuple(actions)
