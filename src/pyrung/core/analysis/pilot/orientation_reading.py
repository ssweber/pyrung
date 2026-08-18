"""Generic current-world construction used by Pilot orientation.

This module reads trace routes, assembles complete worlds, and constructs
typed bearings. It does not interpret or advance WorkingTheory.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pilot.candidate_read import CandidateRead
from pyrung.core.analysis.pilot.execution import (
    PulseHorizon,
    StopCondition,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    Bearing,
    BearingObjective,
    ChannelHeading,
    ExpectationExemption,
    InvestigationSelection,
    LocalProgressKind,
    NavigationConstraints,
    NeedProbe,
    ObserveScan,
    OrientationRead,
    OrientationWorld,
    ProbeRequest,
    ProgramScan,
    Stuck,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.overlay import (
    _pilot_rung_execution_receipt,
)
from pyrung.core.analysis.pilot.trace import (
    rank_trace_choices,
    trace_back,
    trace_relational,
)
from pyrung.core.analysis.pilot.trace_read import TraceChoice, TraceReadConstraints
from pyrung.core.analysis.pilot.trace_tree import TraceAction, TraceNode, frontier_pairs
from pyrung.core.analysis.pilot.types import _IterationFrame
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
    _StateKeyConfig,
)
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

    Inferred alternatives are not commitments or a queue: exact current-world
    rejections remove their dead acts and the remaining trees are returned
    together for one decision.
    """

    ctx = world.context
    key_config = world.state.key_config
    exclusions = (
        ctx.compass.knowledge.nogood_identities(
            _pilot_world_key(
                world.snapshot,
                key_config,
                world.state.pilot_rungs,
                getattr(world.state, "active_requirements", ()),
            )
        )
        if key_config is not None
        else frozenset()
    )
    rejected_actions = _exact_rejected_actions(exclusions)
    # An effective overlay owner is part of this executable world.  Feed its
    # Boolean opposite into the same route-exclusion input as empirical
    # nogoods, but only for this fresh read.  This prevents Orientation from
    # selecting a route whose first prerequisite is guaranteed to be
    # overwritten by an already-proved corrective hold.
    overlay = _pilot_rung_execution_receipt(
        world.state.pilot_rungs,
        world.snapshot,
    )
    rejected_actions = frozenset(
        (
            *rejected_actions,
            *(
                (rung.dest, not rung.value)
                for rung in overlay.effective
                if type(rung.value) is bool
            ),
        )
    )

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
    key = _pilot_world_key(
        world.snapshot,
        key_config,
        state.pilot_rungs,
        getattr(state, "active_requirements", ()),
    )
    details = tuple(
        TraceAction(
            tag=action.tag,
            value=action.value,
            provenance=action.provenance,
            downstream_reach=len(ctx.pdg.downstream_slice(action.tag, follow_calls=True)),
            until=action.until,
            pulse=action.pulse,
            establish=action.establish,
            operation_boundary=action.operation_boundary,
            heuristic=action.heuristic,
            note=action.note,
            availability=action.availability,
            writer_path=action.writer_path,
            effect_path=action.effect_path,
        )
        for action in tree.ordered_action_details()
    )
    frame = _IterationFrame(
        snap=world.snapshot,
        tree=tree,
        key=key,
        distance_before=tree.unsatisfied_count(),
        raw_trace_actions=tuple(dict.fromkeys(detail.pair for detail in details)),
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
    return tuple(_assemble_world(seed, route, tree, key_config) for route, tree in route_trees)


def _frontier(
    world: OrientationWorld,
    candidates: CandidateRead,
) -> tuple[tuple[str, Any], ...]:
    """One complete target-relative frontier for every Orientation result."""
    completion_frontier = candidates.wait.frontier if candidates.wait is not None else ()
    pairs = tuple(completion_frontier) + tuple(frontier_pairs(world.frame.tree, world.snapshot))
    result: list[tuple[str, Any]] = []
    for pair in pairs:
        if pair not in result:
            result.append(pair)
    return tuple(result)


def _probe_or_stuck(
    compass: Any,
    world: OrientationWorld,
    candidates: CandidateRead,
    reason: str,
) -> NeedProbe | Stuck:
    frontier = _frontier(world, candidates)
    count = compass.knowledge.probe_count(world.world_key)
    exclusions = tuple(compass.knowledge.nogood_identities(world.world_key))
    orientation_read = OrientationRead(
        world_key=world.world_key,
        world=world,
        candidates=candidates,
        considered_paths=((candidates.route.plan,) if candidates.route is not None else ()),
        rankings=tuple(candidates.options),
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
            orientation=orientation_read,
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
        orientation=orientation_read,
    )


def _bearing(
    world: OrientationWorld,
    act: Any,
    candidates: CandidateRead,
    *,
    target: TargetSpec,
    rationale: str,
    prerequisites: tuple[Any, ...] = (),
    investigation_selection: InvestigationSelection | None = None,
) -> Bearing:
    """Assemble one Bearing with its target-relative objective.

    The original ``TargetSpec`` and the complete unresolved frontier travel
    unchanged inside :class:`BearingObjective` through execution and
    verification; recovery consumes that receipt.
    """
    policy = getattr(act, "policy", None)
    if policy is None or (policy.expectation is None and policy.expectation_exemption is None):
        raise ValueError("an executable bearing must promise or explicitly exempt an effect")
    orientation_read = OrientationRead(
        world_key=world.world_key,
        world=world,
        candidates=candidates,
        considered_paths=((candidates.route.plan,) if candidates.route is not None else ()),
        rankings=tuple(candidates.options),
        exclusions=tuple(world.context.compass.knowledge.nogood_identities(world.world_key)),
        selected_bearing_id=repr(act_identity(act)),
    )
    # Single program scans must execute the already-established World. Route
    # prerequisites are hypotheses from the current static read; installing
    # them would turn observation/staging into an intervention and corrupt the
    # occurrence receipt. Every other act executes the selected route and
    # therefore inherits its prerequisites.
    inherited = (
        () if isinstance(act, (ObserveScan, ProgramScan)) else candidates.prerequisites.pilot_rungs
    )
    view = getattr(world.context, "theory_view", None)
    entry_configurations = tuple(getattr(view, "configurations", ()))
    configured_tags = frozenset(
        tag
        for configuration in entry_configurations
        for tag, _value in configuration.assignments
    )
    # WorkingTheory's entry configuration is the sole representation of these
    # user-style values.  Static route discovery can independently rediscover
    # the same preset as a conditional prerequisite; carrying both would turn
    # one correction back into a PilotRung after execution.
    merged_prerequisites = tuple(
        rung
        for rung in dict.fromkeys((*inherited, *prerequisites))
        if rung.dest not in configured_tags
    )
    stop_condition = StopCondition(
        (
            PulseHorizon.ASSERTION_SCAN
            if entry_configurations
            and policy.pulse_horizon is PulseHorizon.LOOKAHEAD_SCAN
            else policy.pulse_horizon
        ),
        (
            policy.consumer_boundary
            if policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
            else None
        ),
        (
            "observe the configured entry scan"
            if entry_configurations
            and policy.pulse_horizon is PulseHorizon.LOOKAHEAD_SCAN
            else "execute to the declared consumer"
            if policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
            else "observe the assertion scan"
            if policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN
            else "observe one lookahead scan"
        ),
    )
    return Bearing(
        world_key=world.world_key,
        act=act,
        objective=BearingObjective(target=target, frontier=_frontier(world, candidates)),
        prerequisites=merged_prerequisites,
        entry_configurations=entry_configurations,
        stop_condition=stop_condition,
        rationale=rationale,
        orientation=orientation_read,
        investigation_selection=investigation_selection,
    )


def _pulse_policy(
    option: Any,
    applied: tuple[tuple[str, Any], ...],
    world: OrientationWorld | None = None,
) -> ActPolicy:
    """Materialize one private candidate as navigation's durable act policy."""

    heading = (
        ChannelHeading(
            option.bearing_channel_tag,
            option.bearing_channel_value,
            boundary=option.bearing_boundary,
            route=option.route_context,
        )
        if option.bearing_channel_tag is not None
        else None
    )
    expectation = getattr(option, "expectation", None)
    return ActPolicy(
        source=option.source,
        action_pairs=(option.pair,),
        applied=applied,
        nogood_pair=option.pair,
        heading=heading,
        provenance=option.provenance,
        downstream_reach=option.downstream_reach,
        note=option.awaited_action_note or option.program_note,
        context_actions=option.program_context_actions,
        expectation=expectation,
        expectation_exemption=(
            ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
        ),
        local_progress=(
            LocalProgressKind.REARM
            if world is not None
            and option.tag in world.context.edge_tags
            and _values_match(
                option.value,
                world.context.resting.get(option.tag, False),
            )
            and not _values_match(
                world.snapshot.get(option.tag),
                world.context.resting.get(option.tag, False),
            )
            else LocalProgressKind.TRACE_SETUP
            if world is not None
            and option.source is ActSource.TRACE
            and _is_stable_setup(world, applied)
            and not _values_match(world.snapshot.get(option.tag), option.value)
            else None
        ),
    )


def _is_stable_setup(
    world: OrientationWorld | None,
    applied: tuple[tuple[str, Any], ...],
) -> bool:
    """Whether an exact pulse batch is a retainable current-world setup.

    This is deliberately lazy: ordinary edge/command bearings pay no setup
    semantics.  A trace or Crossing batch qualifies only when every applied
    input is a stable level and at least one level changes in this read.
    """

    return bool(
        world is not None
        and applied
        and any(not _values_match(world.snapshot.get(tag), value) for tag, value in applied)
        and all(
            tag not in getattr(world.context, "edge_tags", ())
            and tag not in getattr(world.context, "clear_only", ())
            for tag, _value in applied
        )
    )
