"""Current-world reading, complete frame assembly, and orientation policy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pilot._ops import _pilot_world_key, _StateKeyConfig
from pyrung.core.analysis.pilot.navigation import (
    BatchPulse,
    Bearing,
    Coast,
    Dwell,
    NavigationConstraints,
    NeedProbe,
    OrientationResult,
    OrientationTrace,
    OrientationWorld,
    ProbeRequest,
    Pulse,
    Stuck,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.options import (
    _build_candidates,
    _candidate_applied,
)
from pyrung.core.analysis.pilot.trace import (
    TraceAction,
    frontier_pairs,
    trace_back,
    trace_relational,
)
from pyrung.core.analysis.pilot.types import _IterationFrame
from pyrung.core.analysis.sp_values import _values_match

_PROBE_BUDGET = 2


def _read_world(
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> OrientationWorld:
    """Purely read one trace frame and compute its executable world key."""

    state = world.state
    ctx = world.context
    snapshot = dict(state.work.state.tags)
    if target.predicate is not None:
        tree = trace_relational(
            target.predicate,
            snapshot,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=ctx.route,
            prior=ctx.domain_prior,
            avoid_pred=constraints.avoid_predicate,
            via_pred=ctx.via_pred,
            harness=getattr(state.work, "_harness", None),
        )
    else:
        tree = trace_back(
            target.tag,
            target.value,
            snapshot,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=ctx.route,
            prior=ctx.domain_prior,
            avoid_pred=constraints.avoid_predicate,
            via_pred=ctx.via_pred,
            harness=getattr(state.work, "_harness", None),
        )
    key_config = state.key_config
    if key_config is None:
        tree_tags = tree.pivot_tags() | {target.tag}
        tree_tags.update(
            node.tag
            for node in tree.leaves()
            if not node.is_steerable and not getattr(node, "pipeline_internal", False)
        )
        key_config = _StateKeyConfig(
            stateful_names=tuple(sorted(tree_tags)),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
    key = _pilot_world_key(snapshot, key_config, state.rungs)
    details = tuple(
        TraceAction(
            tag=action.tag,
            value=action.value,
            provenance=action.provenance,
            wake=len(ctx.pdg.downstream_slice(action.tag, follow_calls=True)),
            until=action.until,
            pulse=action.pulse,
            establish=action.establish,
            heuristic=action.heuristic,
            note=action.note,
            availability=action.availability,
        )
        for action in tree.ordered_action_details()
    )
    frame = _IterationFrame(
        snap=snapshot,
        tree=tree,
        key=key,
        distance_before=tree.unsatisfied_count(),
        raw_trace_actions=tuple(detail.pair for detail in details),
        raw_trace_action_details=details,
    )
    return replace(
        world,
        world_key=key,
        snapshot=snapshot,
        frame=frame,
        key_config=key_config,
    )


def _frontier(world: OrientationWorld, candidates: Any) -> tuple[tuple[str, Any], ...]:
    if candidates.completion_frontier:
        return tuple(candidates.completion_frontier)
    return tuple(frontier_pairs(world.frame.tree, world.snapshot))


def _probe_or_stuck(
    compass: Any,
    world: OrientationWorld,
    candidates: Any,
    reason: str,
) -> NeedProbe | Stuck:
    frontier = _frontier(world, candidates)
    decline = compass.knowledge.probe_decline(world.world_key)
    count = compass.knowledge.probe_count(world.world_key)
    exclusions = tuple(compass.knowledge.nogood_identities(world.world_key))
    trace = OrientationTrace(
        world_key=world.world_key,
        world=world,
        candidates=candidates,
        considered_paths=((candidates.route_plan,) if candidates.route_plan is not None else ()),
        rankings=tuple(candidates.candidates),
        exclusions=exclusions,
    )
    if decline is None and count < _PROBE_BUDGET:
        request = ProbeRequest(frontier=frontier, reason=reason)
        return NeedProbe(
            world_key=world.world_key,
            frontier=frontier,
            request=request,
            rationale=f"static navigation evidence is unresolved: {reason}",
            provenance=("trace", "static-path", "learned-path"),
            trace=trace,
        )
    return Stuck(
        world_key=world.world_key,
        reason_code=reason,
        frontier=frontier,
        exclusions=exclusions,
        evidence=((decline,) if decline is not None else (f"probe budget {count}",)),
        rationale=decline or f"no admissible bearing remains after {count} probe round(s)",
        trace=trace,
    )


def _bearing(
    world: OrientationWorld,
    act: Any,
    candidates: Any,
    *,
    rationale: str,
    immediate_goal: Any,
) -> Bearing:
    trace = OrientationTrace(
        world_key=world.world_key,
        world=world,
        candidates=candidates,
        considered_paths=((candidates.route_plan,) if candidates.route_plan is not None else ()),
        rankings=tuple(candidates.candidates),
        exclusions=tuple(world.context.compass.knowledge.nogood_identities(world.world_key)),
        selected_bearing_id=repr(act_identity(act)),
    )
    return Bearing(
        world_key=world.world_key,
        act=act,
        prerequisites=tuple(candidates.prerequisite_rungs),
        immediate_goal=immediate_goal,
        rationale=rationale,
        trace=trace,
    )


def orient(
    compass: Any,
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> OrientationResult:
    """Return one act, one probe request, or one structured stop.

    The candidate reader still materializes all evidence-rich options so
    diagnostics and ranking remain inspectable.  This function is the only
    component that converts those readings into the public orientation result.
    """

    # The internal context is built from the same immutable Compass value.  A
    # mismatched handle would let a caller orient with stale knowledge.
    if world.context.compass is not compass:
        raise ValueError("orientation world is bound to a different Compass value")
    read_context = replace(
        world.context,
        target_tag=target.tag,
        target_value=target.value,
        target_predicate=target.predicate,
        blocked_route_actions=constraints.blocked_actions,
        avoid_pred=constraints.avoid_predicate,
    )
    read_world = replace(world, context=read_context)
    if read_world.frame is None:
        read_world = _read_world(read_world, target, constraints)
    candidates = _build_candidates(
        read_world.frame,
        read_world.state,
        read_world.context,
    )
    world = read_world

    if candidates.completion_frontier:
        # Candidate construction discovers the completion re-read, but
        # orientation owns the resulting frame. Consumers receive one complete
        # world and never have to stitch two readings from this call together.
        world = replace(
            world,
            frame=replace(
                world.frame,
                completion_frontier=candidates.completion_frontier,
            ),
        )

    if candidates.wait_prescribed:
        route_plan = candidates.route_plan
        advance_boundary = candidates.advance_boundary
        program_step = candidates.program_step
        preserve_channels = (
            frozenset(program_step.preserve_channels) if program_step is not None else frozenset()
        )
        preferred_channel = (
            route_plan.role.channel_tag
            if route_plan is not None and route_plan.role.channel_tag in preserve_channels
            else next(iter(sorted(preserve_channels)), None)
        )
        program_heading = (
            next(
                (
                    (
                        tag,
                        before if tag in preserve_channels else after,
                    )
                    for tag, before, after in reversed(program_step.projected_changes)
                    if tag
                    == (
                        preferred_channel if preferred_channel is not None else program_step.channel
                    )
                    and not _values_match(before, after)
                ),
                None,
            )
            if program_step is not None
            else None
        )
        act = Coast(
            "bearing",
            channel_tag=(
                program_heading[0]
                if program_heading is not None
                else route_plan.role.channel_tag
                if route_plan is not None
                else advance_boundary[0]
                if advance_boundary is not None
                else None
            ),
            target_value=(
                program_heading[1]
                if program_heading is not None
                else route_plan.first_edge.to_value
                if route_plan is not None
                else advance_boundary[1]
                if advance_boundary is not None
                else None
            ),
            route_prescribed=route_plan is not None,
            route_channel_tag=(
                route_plan.role.channel_tag
                if route_plan is not None and program_heading is not None
                else None
            ),
            route_from_value=(
                route_plan.first_edge.from_value
                if route_plan is not None and program_heading is not None
                else None
            ),
            route_target_value=(
                route_plan.first_edge.to_value
                if route_plan is not None and program_heading is not None
                else None
            ),
        )
        if not compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
            return _bearing(
                world,
                act,
                candidates,
                rationale=candidates.wait_reason or "charted completion edge",
                immediate_goal=(
                    program_heading[1]
                    if program_heading is not None
                    else candidates.route_plan.first_edge.to_value
                    if candidates.route_plan is not None
                    else advance_boundary[1]
                    if advance_boundary is not None
                    else target.value
                ),
            )

    if candidates.prescribed_batch:
        act = BatchPulse(tuple(candidates.prescribed_batch), "learned")
        if not compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
            return _bearing(
                world,
                act,
                candidates,
                rationale="learned joint transition",
                immediate_goal=target.value,
            )

    for option in candidates.candidates:
        applied = _candidate_applied(option, candidates, world.context)
        act = Pulse(option.pair, applied, option)
        if compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
            continue
        return _bearing(
            world,
            act,
            candidates,
            rationale=(
                option.current_note
                or getattr(option, "program_note", None)
                or ("static route edge" if option.route_prescribed else "")
                or ("learned transition" if option.influence_prescribed else "")
                or "ranked trace action"
            ),
            immediate_goal=(
                option.bearing_channel_value
                if option.bearing_channel_tag is not None
                else target.value
            ),
        )

    # Widening remains an atomic act, but no sequence of widths survives an
    # observation.  Each rejected width is world-keyed knowledge and the next
    # call recomputes before considering another width.
    active = tuple(candidates.active_trace_actions)
    for width in range(2, len(active) + 1):
        act = BatchPulse(active[:width], "widening")
        if not compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
            return _bearing(
                world,
                act,
                candidates,
                rationale=f"widen trace context to {width} atomic actions",
                immediate_goal=target.value,
            )

    if candidates.stuck_reason is not None:
        return _probe_or_stuck(
            compass,
            world,
            candidates,
            candidates.stuck_reason,
        )

    terminal: Coast | Dwell
    if compass.knowledge.coast_receipt(world.world_key) is None:
        terminal = Coast("terminal")
        rationale = "hold the current macro-state and allow program motion"
    else:
        terminal = Dwell()
        rationale = "terminal coast already observed; run one verified dwell"
    if not compass.knowledge.act_is_nogood(world.world_key, act_identity(terminal)):
        return _bearing(
            world,
            terminal,
            candidates,
            rationale=rationale,
            immediate_goal=target.value,
        )

    return _probe_or_stuck(compass, world, candidates, "all_rejected")
