"""Materialize the action and wait options for one orientation.

``read_candidates`` is the typed current-world boundary. Its private
``_build_candidates`` implementation orchestrates separate reads for static
routes and charted completion, instruction-owned boundaries and prerequisites,
learned transitions, and program-awaited actions. Frozen private receipts keep
those sources distinct until ``_select_wait`` applies their precedence and
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

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pyrung.core.analysis.pilot.candidate_read as _candidate_read
from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.awaited_actions import AwaitedAction, unique_legal_awaited_action
from pyrung.core.analysis.pilot.candidate_admission import (
    _admit_trace_details,
    _admit_wait_read,
    _effect_operation_batches,
    _separate_prerequisites,
)
from pyrung.core.analysis.pilot.candidate_policy import (
    _action_allowed,
)
from pyrung.core.analysis.pilot.constrained_reachability import NavigationEvidence
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    expectation_from_selected_path,
    expectation_from_writer,
    expectation_snapshot,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    ChannelHeading,
    OrientationWorld,
    RouteEdgeContext,
    _ActionPair,
    is_action,
    is_composite_action,
)
from pyrung.core.analysis.pilot.route_options import (
    _compass_route_actions,
    _compass_route_plan,
    _edge_setup_releases,
    _general_chart_completion_plan,
    _live_chart_completion_edge,
    _route_context_actions,
)
from pyrung.core.analysis.pilot.wait_options import (
    _boundary_heading,
    _charted_program_edge,
    _expectation_from_route_writer,
    _prescribe_wait,
)
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.instruction.advance import constraint_holds

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.trace_tree import TraceAction

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


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


def _awaited_action_bearing(
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> AwaitedAction | None:
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
    from pyrung.core.analysis.pilot.trace_read import WorldView

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
        action_allowed=lambda action: action not in key_nogoods and _action_allowed(ctx, action),
        action_avoided=lambda action: _avoid_forces(ctx, [action], frame.snap),
        awaits_operator=True,
    )


def _read_route_and_wait(
    world: OrientationWorld,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._RouteAndCompletionRead:
    """Read the current trace, static route, and charted completion together."""

    frame = world.frame
    state = world.state
    ctx = world.context
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
    banked_program_work = bool(earned_work is not None and earned_work.has_banked_work(frame.snap))
    banked_trace_work = bool(admission.actions and banked_program_work)
    trace_blocks_route = bool(
        current_trace_actions
        or banked_trace_work
        or (getattr(state, "pending_departure", None) is not None and admission.active_actions)
    )
    route_plan = (
        None
        if trace_blocks_route
        else _compass_route_plan(
            frame,
            ctx,
            key_nogoods,
            state=state,
            prefer_first_edge_coast=banked_program_work,
        )
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
    if route_plan is None and not trace_blocks_route:
        route_plan = _general_chart_completion_plan(
            frame,
            ctx,
            key_nogoods,
            state=state,
            allow_conservative_nomination=banked_program_work,
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
            prefer_first_edge_coast=banked_program_work,
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
        (
            *route_plan.first_edge.co_actions,
            *_route_context_actions(
                route_plan.first_edge,
                frame,
                state,
                ctx,
                key_nogoods,
            ),
        )
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


def _read_learned_fallback(
    route_and_wait: _candidate_read._RouteAndCompletionRead,
    separated: _candidate_read._PrerequisiteSeparation,
    world: OrientationWorld,
    key_nogoods: set[_ActionPair],
) -> _candidate_read._LearnedFallback | None:
    """Read exactly one learned wait, action, or batch fallback."""

    frame = world.frame
    state = world.state
    ctx = world.context
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
            return NavigationEvidence.learned_cause_allowed(
                tag,
                source,
                cause,
                destination,
                world_key=frame.key,
                snapshot=frame.snap,
                knowledge=ctx.compass.knowledge,
                context=ctx,
                blocked_actions=ctx.blocked_actions,
                pair_nogoods=key_nogoods,
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
            return _candidate_read._LearnedBatch(
                _candidate_read.LearnedBatchRead(members, learned_expectation)
            )
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
    world: OrientationWorld,
    key_nogoods: set[_ActionPair],
) -> _candidate_read.CandidateRead:
    """Compose the final durable candidate read from explicit phase receipts."""

    frame = world.frame
    state = world.state
    ctx = world.context
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
                    selected_pairs=(
                        pair,
                        *(route.co_actions if route is not None else ()),
                    ),
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
                _edge_setup_releases(prescribed_edge, state, ctx),
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
    world: OrientationWorld,
) -> _candidate_read.CandidateRead:
    """Build one candidate read through explicit evidence-owning phases."""

    frame = world.frame
    state = world.state
    ctx = world.context
    key_nogoods = set(ctx.compass.knowledge.nogood_pairs(frame.key))
    route_and_wait = _read_route_and_wait(world, key_nogoods)
    separated = _separate_prerequisites(route_and_wait, frame, state, ctx)
    learned = _read_learned_fallback(
        route_and_wait,
        separated,
        world,
        key_nogoods,
    )
    awaited_action = _awaited_action_bearing(frame, ctx, key_nogoods)
    return _assemble_candidate_read(
        route_and_wait,
        separated,
        learned,
        awaited_action,
        world,
        key_nogoods,
    )


def read_candidates(world: OrientationWorld) -> _candidate_read.CandidateRead:
    """Return the complete candidate reading for one assembled current world."""

    if world.frame is None:
        raise ValueError("candidate reading requires a complete orientation frame")
    return _build_candidates(world)
