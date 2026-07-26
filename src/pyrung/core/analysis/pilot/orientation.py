"""Current-world reading, complete frame assembly, and orientation policy."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pilot._ops import _pilot_world_key, _StateKeyConfig
from pyrung.core.analysis.pilot.navigation import (
    BatchPulse,
    Bearing,
    BearingObjective,
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
    _CandidateSource,
    _current_work_evidence,
)
from pyrung.core.analysis.pilot.trace import (
    TraceAction,
    TraceChoice,
    TraceNode,
    TraceReadConstraints,
    frontier_pairs,
    rank_trace_choices,
    trace_back,
    trace_relational,
)
from pyrung.core.analysis.pilot.types import _IterationFrame
from pyrung.core.analysis.sp_values import _values_match

_PROBE_BUDGET = 2


def _trace_for_route(
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
    route: TraceChoice | None,
    rejected_actions: frozenset[tuple[str, Any]],
) -> TraceNode:
    """Read one target tree under one root-route choice."""

    state = world.state
    ctx = world.context
    snapshot = world.snapshot
    read = TraceReadConstraints.from_context(
        ctx,
        state.work,
        route=route,
        avoid_pred=constraints.avoid_predicate,
        rejected_actions=rejected_actions,
    )
    if target.predicate is not None:
        return trace_relational(
            target.predicate,
            snapshot,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            constraints=read,
        )
    return trace_back(
        target.tag,
        target.value,
        snapshot,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        constraints=read,
    )


def _exact_rejected_actions(exclusions: frozenset[Any]) -> frozenset[tuple[str, Any]]:
    """Singleton action artifacts that Trace may use to rank alternatives.

    A Pulse's complete applied tuple is the execution artifact. Only a
    one-member tuple is equivalent to a trace leaf; projecting a rejected joint
    act onto either member would discard an untested co-action context.
    """

    return frozenset(
        identity[1][0]
        for identity in exclusions
        if len(identity) >= 2
        and identity[0] == "pulse"
        and isinstance(identity[1], tuple)
        and len(identity[1]) == 1
    )


def _route_rejected_actions(
    tree: Any,
    world: OrientationWorld,
    rejected_actions: frozenset[tuple[str, Any]],
) -> tuple[tuple[str, Any], ...] | None:
    """Exact rejected actions when every live action on *tree* is exhausted."""

    ctx = world.context
    active: list[tuple[str, Any]] = []
    for detail in tree.ordered_action_details():
        pair = detail.pair
        if pair in active:
            continue
        if (
            not _values_match(world.snapshot.get(detail.tag), detail.value)
            or detail.tag in ctx.edge_tags
            or detail.pulse
            or detail.until is not None
        ):
            active.append(pair)
    if active and all(pair in rejected_actions for pair in active):
        return tuple(active)
    return None


def _read_route_trees(
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> tuple[tuple[TraceChoice | None, TraceNode], ...]:
    """Read every admissible root alternative from this current world.

    Explicit ``via=`` remains one constrained read.  Inferred alternatives are
    not commitments or a queue: exact current-world rejections remove their
    dead acts and the remaining trees are returned together for one decision.
    """

    ctx = world.context
    key_config = world.state.key_config
    exclusions = (
        ctx.compass.knowledge.nogood_identities(
            _pilot_world_key(world.snapshot, key_config, world.state.rungs)
        )
        if key_config is not None
        else frozenset()
    )
    rejected_actions = _exact_rejected_actions(exclusions)

    # Explicit positive user intent is a constraint, so no unconstrained sibling
    # is introduced when its current tree has no bearing.
    if ctx.route is not None:
        tree = _trace_for_route(world, target, constraints, ctx.route, rejected_actions)
        return ((ctx.route, tree),)

    if target.predicate is not None:
        return ((None, _trace_for_route(world, target, constraints, None, rejected_actions)),)

    _choices, ranked = rank_trace_choices(
        target.tag,
        target.value,
        world.snapshot,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        constraints=TraceReadConstraints.from_context(
            ctx,
            world.state.work,
            route=None,
            avoid_pred=constraints.avoid_predicate,
            rejected_actions=rejected_actions,
        ),
    )
    live = tuple(
        (choice, tree)
        for choice, tree in ranked
        if _route_rejected_actions(tree, world, rejected_actions) is None
    )
    if live:
        return live

    # Keep one complete, unlocked frontier for an honest terminal diagnosis
    # when enumeration found no route or every exact act is already a nogood.
    return ((None, _trace_for_route(world, target, constraints, None, rejected_actions)),)


def _assemble_world(
    world: OrientationWorld,
    target: TargetSpec,
    selected_route: TraceChoice | None,
    tree: TraceNode,
    key_config: _StateKeyConfig,
) -> OrientationWorld:
    """Assemble one alternative's complete immutable orientation frame."""

    state = world.state
    ctx = replace(world.context, route=selected_route)
    world = replace(
        world,
        context=ctx,
        root_route=selected_route,
    )
    key = _pilot_world_key(world.snapshot, key_config, state.rungs)
    details = tuple(
        TraceAction(
            tag=action.tag,
            value=action.value,
            provenance=action.provenance,
            wake=len(ctx.pdg.downstream_slice(action.tag, follow_calls=True)),
            until=action.until,
            pulse=action.pulse,
            establish=action.establish,
            operation_boundary=action.operation_boundary,
            heuristic=action.heuristic,
            note=action.note,
            availability=action.availability,
            writer_path=action.writer_path,
        )
        for action in tree.ordered_action_details()
    )
    frame = _IterationFrame(
        snap=world.snapshot,
        tree=tree,
        key=key,
        distance_before=tree.unsatisfied_count(),
        raw_trace_actions=tuple(detail.pair for detail in details),
        raw_trace_action_details=details,
    )
    return replace(
        world,
        world_key=key,
        frame=frame,
        key_config=key_config,
    )


def _read_worlds(
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> tuple[OrientationWorld, ...]:
    """Read all current alternatives against one snapshot and one world key."""

    snapshot = dict(world.state.work.state.tags)
    seed = replace(world, snapshot=snapshot)
    route_trees = _read_route_trees(seed, target, constraints)
    key_config = world.state.key_config
    if key_config is None:
        tree_tags = {target.tag}
        for _route, tree in route_trees:
            tree_tags.update(tree.pivot_tags())
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
    return tuple(
        _assemble_world(seed, target, route, tree, key_config) for route, tree in route_trees
    )


def _frontier(world: OrientationWorld, candidates: Any) -> tuple[tuple[str, Any], ...]:
    """One complete target-relative frontier for every Orientation result."""
    pairs = tuple(candidates.completion_frontier) + tuple(
        frontier_pairs(world.frame.tree, world.snapshot)
    )
    result: list[tuple[str, Any]] = []
    for pair in pairs:
        if pair not in result:
            result.append(pair)
    return tuple(result)


def _probe_or_stuck(
    compass: Any,
    world: OrientationWorld,
    candidates: Any,
    reason: str,
    _constraints: NavigationConstraints,
) -> NeedProbe | Stuck:
    frontier = _frontier(world, candidates)
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
    if count < _PROBE_BUDGET:
        request = ProbeRequest(frontier=frontier, reason=reason)
        return NeedProbe(
            world_key=world.world_key,
            frontier=frontier,
            request=request,
            rationale=f"static navigation evidence is unresolved: {reason}",
            provenance=("trace", "static-path", "learned-path"),
            trace=trace,
        )
    evidence = (f"probe budget {count}",)
    rationale = f"no admissible bearing remains after {count} probe round(s)"
    return Stuck(
        world_key=world.world_key,
        reason_code=reason,
        frontier=frontier,
        exclusions=exclusions,
        evidence=evidence,
        rationale=rationale,
        trace=trace,
    )


def _bearing(
    world: OrientationWorld,
    act: Any,
    candidates: Any,
    *,
    target: TargetSpec,
    rationale: str,
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
        objective=BearingObjective(target=target, frontier=_frontier(world, candidates)),
        prerequisites=tuple(candidates.prerequisite_rungs),
        rationale=rationale,
        trace=trace,
    )


def _orient_read(
    compass: Any,
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> OrientationResult:
    """Materialize the best result from one already-read alternative."""

    if world.frame is None:
        raise ValueError("single-alternative orientation requires a complete frame")
    candidates = _build_candidates(
        world.frame,
        world.state,
        world.context,
    )

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
        heading = program_heading if program_heading is not None else advance_boundary
        route_prescribed = route_plan is not None
        route_channel = route_plan.role.channel_tag if route_plan is not None else None
        route_from = route_plan.first_edge.from_value if route_plan is not None else None
        route_target = route_plan.first_edge.to_value if route_plan is not None else None
        preserve_route_heading = route_prescribed and heading is not None
        act = Coast(
            "bearing",
            channel_tag=heading[0] if heading is not None else route_channel,
            target_value=heading[1] if heading is not None else route_target,
            boundary=candidates.advance_condition,
            route_prescribed=route_prescribed,
            route_channel_tag=route_channel if preserve_route_heading else None,
            route_from_value=route_from if preserve_route_heading else None,
            route_target_value=route_target if preserve_route_heading else None,
        )
        if not compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                rationale=candidates.wait_reason or "charted completion edge",
            )

    if candidates.prescribed_batch:
        act = BatchPulse(tuple(candidates.prescribed_batch), "learned")
        if not compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                rationale="learned joint transition",
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
            target=target,
            rationale=(
                option.current_note
                or getattr(option, "program_note", None)
                or ("static route edge" if option.source is _CandidateSource.ROUTE else "")
                or ("learned transition" if option.source is _CandidateSource.INFLUENCE else "")
                or "ranked trace action"
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
                target=target,
                rationale=f"widen trace context to {width} atomic actions",
            )

    if candidates.stuck_reason is not None:
        return _probe_or_stuck(
            compass,
            world,
            candidates,
            candidates.stuck_reason,
            constraints,
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
            target=target,
            rationale=rationale,
        )

    return _probe_or_stuck(compass, world, candidates, "all_rejected", constraints)


def _is_maintenance(result: OrientationResult) -> bool:
    """Whether a read has no concrete continuation and can only let time pass."""

    return isinstance(result, Bearing) and (
        isinstance(result.act, Dwell)
        or isinstance(result.act, Coast)
        and result.act.mode == "terminal"
    )


def _read_group(
    compass: Any,
    worlds: tuple[OrientationWorld, ...],
    target: TargetSpec,
    constraints: NavigationConstraints,
    *,
    maintenance_owns: bool = False,
) -> tuple[OrientationResult | None, tuple[OrientationResult, ...]]:
    """Read alternatives once under the caller's work-ownership disposition.

    Alternative order remains the trace reader's deterministic order. There is
    no cross-alternative score and no retained cursor. Fresh reads may look
    past maintenance for a concrete bearing; once live work owns the group,
    maintenance is itself the continuation and ends the read.
    """

    results: list[OrientationResult] = []
    maintenance: OrientationResult | None = None
    for world in worlds:
        result = _orient_read(compass, world, target, constraints)
        results.append(result)
        if isinstance(result, Bearing):
            if maintenance_owns or not _is_maintenance(result):
                return result, tuple(results)
            if maintenance is None:
                maintenance = result
    return maintenance, tuple(results)


def _combined_nonbearing(results: tuple[OrientationResult, ...]) -> OrientationResult:
    """Return one complete probe/stop after every current alternative was read."""

    frontier = tuple(
        dict.fromkeys(pair for result in results for pair in getattr(result, "frontier", ()))
    )
    probe = next((result for result in results if isinstance(result, NeedProbe)), None)
    if probe is not None:
        return replace(
            probe,
            frontier=frontier,
            request=replace(probe.request, frontier=frontier),
        )
    stuck = next((result for result in results if isinstance(result, Stuck)), None)
    if stuck is None:
        raise RuntimeError("current-world alternatives produced no disposition")
    return replace(stuck, frontier=frontier)


def orient(
    compass: Any,
    world: OrientationWorld,
    target: TargetSpec,
    constraints: NavigationConstraints,
) -> OrientationResult:
    """Read all live work, choose its smallest continuation, and forget the read.

    An open operation is read before fresh work. Within that operation the
    ordinary single-read Orientation selects one act. No root alternative,
    suffix, score, or "next route" position survives the observation.
    """

    if world.context.compass is not compass:
        raise ValueError("orientation world is bound to a different Compass value")
    read_context = replace(
        world.context,
        target=target,
        blocked_route_actions=constraints.blocked_actions,
        avoid_pred=constraints.avoid_predicate,
    )
    seed = replace(world, context=read_context)
    worlds = (seed,) if seed.frame is not None else _read_worlds(seed, target, constraints)
    open_worlds: list[OrientationWorld] = []
    fresh_worlds: list[OrientationWorld] = []
    for alternative in worlds:
        evidence = _current_work_evidence(
            alternative.frame,
            alternative.state,
            alternative.root_route,
        )
        (open_worlds if evidence else fresh_worlds).append(alternative)

    # Live work owns the next move. If it has no concrete lever, its coast or
    # dwell is still maintenance of that operation. If every apparent open
    # residual yields no Bearing, the operation has closed; stale established
    # facts do not prevent a fresh current-world read.
    open_results: tuple[OrientationResult, ...] = ()
    if open_worlds:
        selected, open_results = _read_group(
            compass,
            tuple(open_worlds),
            target,
            constraints,
            maintenance_owns=True,
        )
        if selected is not None:
            return selected

    selected, fresh_results = _read_group(
        compass,
        tuple(fresh_worlds),
        target,
        constraints,
    )
    if selected is not None:
        return selected
    return _combined_nonbearing((*open_results, *fresh_results))
