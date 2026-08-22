"""Generic current-world construction used by Pilot orientation.

This module reads trace routes, assembles complete worlds, lowers admitted
candidates into executable overlays, and constructs typed bearings. It does
not interpret or advance WorkingTheory.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pilot.candidate_read import CandidateRead, _Candidate
from pyrung.core.analysis.pilot.execution import (
    PulseHorizon,
    StopCondition,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    AdmissionBasis,
    BatchPulse,
    Bearing,
    BearingObjective,
    ChannelHeading,
    ExpectationExemption,
    IntrascanPulse,
    InvestigationSelection,
    LocalProgressKind,
    NavigationConstraints,
    NeedProbe,
    ObserveScan,
    OrientationRead,
    OrientationWorld,
    ProbeRequest,
    ProgramContinuation,
    ProgramScan,
    Pulse,
    Stuck,
    TargetSpec,
    _ActionPair,
)
from pyrung.core.analysis.pilot.overlay import (
    _pilot_rung_execution_receipt,
)
from pyrung.core.analysis.pilot.trace import (
    target_reached,
    trace_back,
    trace_relational,
)
from pyrung.core.analysis.pilot.trace_read import TraceChoice, TraceReadConstraints
from pyrung.core.analysis.pilot.trace_routes import rank_trace_choices
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


def _frontier(read: OrientationRead) -> tuple[tuple[str, Any], ...]:
    """One complete target-relative frontier for every Orientation result."""
    world = read.world
    candidates = read.candidates
    completion_frontier = candidates.wait.frontier if candidates.wait is not None else ()
    pairs = tuple(completion_frontier) + tuple(frontier_pairs(world.frame.tree, world.snapshot))
    result: list[tuple[str, Any]] = []
    for pair in pairs:
        if pair not in result:
            result.append(pair)
    return tuple(result)


def _probe_or_stuck(
    compass: Any,
    read: OrientationRead,
    reason: str,
) -> NeedProbe | Stuck:
    world = read.world
    frontier = _frontier(read)
    count = compass.knowledge.probe_count(world.world_key)
    exclusions = tuple(compass.knowledge.nogood_identities(world.world_key))
    if count < _PROBE_BUDGET:
        request = ProbeRequest(frontier=frontier, reason=reason)
        return NeedProbe(
            world_key=world.world_key,
            frontier=frontier,
            request=request,
            rationale=f"static navigation evidence is unresolved: {reason}",
            provenance=("trace", "static-path", "learned-path"),
            orientation=read,
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
        orientation=read,
    )


def _bearing(
    read: OrientationRead,
    act: Any,
    *,
    target: TargetSpec,
    rationale: str,
    prerequisites: tuple[Any, ...] = (),
    investigation_selection: InvestigationSelection | None = None,
    allow_exploratory_probe: bool = False,
) -> Bearing:
    """Assemble one Bearing with its target-relative objective.

    The original ``TargetSpec`` and the complete unresolved frontier travel
    unchanged inside :class:`BearingObjective` through execution and
    verification; recovery consumes that receipt.
    """
    world = read.world
    candidates = read.candidates
    policy = getattr(act, "policy", None)
    if policy is None or (policy.expectation is None and policy.expectation_exemption is None):
        raise ValueError("an executable bearing must promise or explicitly exempt an effect")
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
        tag for configuration in entry_configurations for tag, _value in configuration.assignments
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
            if entry_configurations and policy.pulse_horizon is PulseHorizon.LOOKAHEAD_SCAN
            else policy.pulse_horizon
        ),
        (
            policy.consumer_boundary
            if policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
            else None
        ),
        (
            "observe the configured entry scan"
            if entry_configurations and policy.pulse_horizon is PulseHorizon.LOOKAHEAD_SCAN
            else "execute to the declared consumer"
            if policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
            else "observe the assertion scan"
            if policy.pulse_horizon is PulseHorizon.ASSERTION_SCAN
            else "observe one lookahead scan"
        ),
    )
    act = _classify_admission(read, act, target)
    if act.policy.admission_basis is AdmissionBasis.EXPLORATORY and not allow_exploratory_probe:
        raise ValueError("an exploratory proposal cannot become an executable Bearing")
    return Bearing(
        world_key=world.world_key,
        act=act,
        objective=BearingObjective(target=target, frontier=_frontier(read)),
        prerequisites=merged_prerequisites,
        entry_configurations=entry_configurations,
        stop_condition=stop_condition,
        rationale=rationale,
        orientation=read,
        investigation_selection=investigation_selection,
    )


_THEORY_PROGRESS = {
    LocalProgressKind.TEMPORAL_SETUP,
    LocalProgressKind.THEORY_CORRECTIVE,
    LocalProgressKind.TEMPORAL_EDGE,
    LocalProgressKind.INTRASCAN_STAGE,
    LocalProgressKind.INTRASCAN_DIRECT,
}


def _classify_admission(read: OrientationRead, act: Any, target: TargetSpec) -> Any:
    """Attach the one evidence basis which authorizes a proposed act.

    This classification consumes existing proposal receipts; it performs no
    new trace, projection, or execution.  ``TRACE_SETUP`` is intentionally not
    sufficient by itself: successfully assigning a guessed stable input does
    not establish that the guess is worth a PLC scan.
    """

    policy = act.policy
    if policy.admission_basis is not None:
        _validate_admission_basis(read, act, target)
        return act
    if policy.expectation is not None:
        basis = AdmissionBasis.PRODUCER_EFFECT
    elif getattr(act, "crossing", None) is not None:
        basis = AdmissionBasis.CROSSING_FIDELITY
    elif policy.heading is not None:
        basis = AdmissionBasis.CHANNEL_HEADING
    elif isinstance(act, IntrascanPulse | ProgramScan):
        basis = AdmissionBasis.INTRASCAN_EVIDENCE
    elif isinstance(act, ObserveScan):
        basis = AdmissionBasis.ENTRY_OBSERVATION
    elif isinstance(act, ProgramContinuation):
        basis = (
            AdmissionBasis.THEORY_REQUIREMENT
            if policy.source is ActSource.WIDENING or policy.local_progress in _THEORY_PROGRESS
            else AdmissionBasis.PROGRAM_CONTINUATION
        )
    elif policy.local_progress is LocalProgressKind.REARM:
        basis = AdmissionBasis.ACTIVATION_PREDECESSOR
    elif policy.local_progress in _THEORY_PROGRESS:
        basis = AdmissionBasis.THEORY_REQUIREMENT
    elif policy.applied and target_reached(
        {**read.world.snapshot, **dict(policy.applied)},
        target.tag,
        target.value,
        target.predicate,
    ):
        basis = AdmissionBasis.TARGET_SATISFACTION
    elif policy.source in {ActSource.LEARNED_ACTION, ActSource.LEARNED_BATCH}:
        basis = AdmissionBasis.LEARNED_TRANSITION
    elif policy.source in {ActSource.AWAITED_ACTION, ActSource.PROGRAM}:
        basis = AdmissionBasis.PROGRAM_INPUT
    else:
        basis = AdmissionBasis.EXPLORATORY
    return replace(act, policy=replace(policy, admission_basis=basis))


def _validate_admission_basis(read: OrientationRead, act: Any, target: TargetSpec) -> None:
    """Fail loud when a named admission basis lacks its required receipt."""

    policy = act.policy
    basis = policy.admission_basis
    if basis is AdmissionBasis.PRODUCER_EFFECT:
        valid = policy.expectation is not None
    elif basis is AdmissionBasis.CHANNEL_HEADING:
        valid = policy.heading is not None
    elif basis is AdmissionBasis.CROSSING_FIDELITY:
        valid = getattr(act, "crossing", None) is not None
    elif basis is AdmissionBasis.TARGET_SATISFACTION:
        valid = bool(
            policy.applied
            and target_reached(
                {**read.world.snapshot, **dict(policy.applied)},
                target.tag,
                target.value,
                target.predicate,
            )
        )
    elif basis is AdmissionBasis.LEARNED_TRANSITION:
        valid = policy.source in {ActSource.LEARNED_ACTION, ActSource.LEARNED_BATCH}
    elif basis is AdmissionBasis.PROGRAM_INPUT:
        valid = policy.source in {ActSource.AWAITED_ACTION, ActSource.PROGRAM}
    elif basis is AdmissionBasis.PROGRAM_CONTINUATION:
        valid = isinstance(act, ProgramContinuation)
    elif basis is AdmissionBasis.ACTIVATION_PREDECESSOR:
        valid = policy.local_progress is LocalProgressKind.REARM
    elif basis is AdmissionBasis.ENTRY_OBSERVATION:
        valid = isinstance(act, ObserveScan)
    elif basis is AdmissionBasis.INTRASCAN_EVIDENCE:
        valid = isinstance(act, IntrascanPulse | ProgramScan)
    elif basis is AdmissionBasis.THEORY_REQUIREMENT:
        valid = policy.local_progress in _THEORY_PROGRESS
    else:
        valid = basis is AdmissionBasis.EXPLORATORY
    if not valid:
        raise ValueError(f"admission basis {basis!r} lacks its required evidence")


def _candidate_applied(
    candidate: _Candidate,
    candidates: CandidateRead,
    context: Any,
) -> tuple[_ActionPair, ...]:
    """Lower one admitted candidate into its complete executable overlay."""

    pair = candidate.pair
    actions: list[_ActionPair] = [pair]
    seen: set[str] = {pair[0]}

    # A route-prescribed command carries its co-actions (the one-shot edge
    # gate); they must fire in the same scan or the command rung never executes.
    route = candidates.route
    if candidate.source is ActSource.ROUTE and route is not None:
        for co_action in route.co_actions:
            if co_action[0] not in seen:
                actions.append(co_action)
                seen.add(co_action[0])

    if candidate.source is ActSource.PROGRAM:
        for co_action in candidate.program_context_actions:
            # A pulse's own release/assert sequence is handled by execution;
            # only independent context belongs in the atomic action set.
            if co_action[0] not in seen:
                actions.append(co_action)
                seen.add(co_action[0])

    # A trace-selected convergence command may need the other conjuncts from
    # that same trace artifact. A ROUTE bearing is already a closed executable
    # artifact: its exact edge owns co-actions above and admitted prerequisites
    # below, so target-wide trace leaves must not hitchhike on it.
    if (
        candidate.source is not ActSource.ROUTE
        and candidate.tag in context.compass.action_tags
        and candidates.trace.active_actions
    ):
        for trace_action in candidates.trace.active_actions:
            if trace_action[0] not in seen:
                actions.append(trace_action)
                seen.add(trace_action[0])

    # Prerequisite holds execute on the fork but are absent from trace actions;
    # retain them so execution and scan-log identity describe the same overlay.
    for rung in candidates.prerequisites.pilot_rungs:
        tag, value = rung.dest, rung.value
        if tag not in seen:
            actions.append((tag, value))
            seen.add(tag)

    return tuple(actions)


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


def _activation_setup_act(act: Any, world: OrientationWorld) -> Any | None:
    """Return one predecessor/rearm Bearing act required before *act*.

    An edge transition or spent one-shot is a real program scan, not hidden
    pulse preparation.  This function lowers only that immediate setup phase;
    after it commits, the ordinary Pilot loop asks Compass again and rebuilds
    the originally intended act from the resulting World if it is still
    relevant.
    """

    if not isinstance(act, (Pulse, BatchPulse)):
        return None
    policy = act.policy
    snapshot = world.snapshot
    setup: list[_ActionPair] = []

    # Explicit rise/fall inputs need the opposite level established at a scan
    # boundary before the selected assertion.  The selected action's Boolean
    # value carries the direction: True is a rise assertion, False a fall.
    for tag, value in policy.applied:
        if tag not in world.context.edge_tags or type(value) is not bool:
            continue
        predecessor = not value
        if not _values_match(snapshot.get(tag), predecessor):
            setup.append((tag, predecessor))

    # Exact writer semantics can independently say that an instruction's
    # private one-shot bit is spent while its rung remains conductive.  Drop
    # the selected command inputs to their resting levels for one scan.
    heading = policy.heading
    route = heading.route if heading is not None else None
    if route is not None:
        for tag, rest in route.setup_releases:
            if not _values_match(snapshot.get(tag), rest):
                setup.append((tag, rest))

    actions = tuple(dict.fromkeys(setup))
    if not actions:
        return None
    setup_policy = ActPolicy(
        source=policy.source,
        action_pairs=actions,
        applied=actions,
        nogood_pair=actions[0] if len(actions) == 1 else None,
        provenance=(*policy.provenance, "ladder activation setup"),
        note="establish ladder activation predecessor and reread",
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
        local_progress=LocalProgressKind.REARM,
        pulse_horizon=PulseHorizon.ASSERTION_SCAN,
    )
    return Pulse(setup_policy) if len(actions) == 1 else BatchPulse(setup_policy)


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
