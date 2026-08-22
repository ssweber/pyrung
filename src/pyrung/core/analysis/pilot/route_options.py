"""Read and materialize static route and chart options.

This module binds Compass route geometry to current-world Trace evidence and
lowers route-owned Boolean overlays. It returns candidate material for the
orientation orchestrator; it never selects or executes a Bearing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.activation import read_activation
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.candidate_policy import _action_allowed
from pyrung.core.analysis.pilot.constrained_reachability import NavigationEvidence
from pyrung.core.analysis.pilot.guard_forcing import break_guard_holds
from pyrung.core.analysis.pilot.navigation_contracts import (
    EvidenceScope,
    _ActionPair,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotOverlayExecution,
    PilotRung,
    _atom_condition,
    _pilot_rung_execution_receipt,
    _target_unresolved_condition,
    _until_unresolved_condition,
)
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.pipeline_graph import StaticPath
    from pyrung.core.analysis.pilot.trace_tree import TraceAction


def _edge_commands_effective(
    edge: Any,
    snapshot: Mapping[str, Any],
    overlay: PilotOverlayExecution,
) -> bool:
    """Whether the edge's complete command recipe already owns this world."""

    commanded = () if edge.action is None else (edge.action, *edge.co_actions)
    return bool(commanded) and all(
        ((owner := overlay.owner(tag)) is not None and _values_match(owner.value, value))
        or _values_match(snapshot.get(tag), value)
        for tag, value in commanded
    )


def _edge_write_activation(edge: Any, state: Any, ctx: Any) -> Any:
    """Read the selected writer's exact one-shot state in this World."""

    if state is None:
        return None
    effect_tag, _effect_value = edge.route.writer_effect
    nodes = getattr(getattr(ctx, "pdg", None), "rung_nodes", ())
    if edge.route.writer_node >= len(nodes):
        return None
    node = nodes[edge.route.writer_node]
    program = getattr(ctx, "program", None)
    if program is None:
        return None
    rung = resolve_rung(program, node)
    execution_state = getattr(getattr(state, "work", None), "state", None)
    if rung is None or execution_state is None:
        return None
    return read_activation(
        rung,
        effect_tag,
        getattr(execution_state, "memory", None),
    )


def _edge_requires_setup(edge: Any, state: Any, ctx: Any) -> bool:
    """Whether the selected occurrence needs a predecessor/rearm Bearing."""

    commanded = () if edge.action is None else (edge.action, *edge.co_actions)
    if any(tag in getattr(ctx, "edge_tags", ()) for tag, _value in commanded):
        return True
    activation = _edge_write_activation(edge, state, ctx)
    return bool(activation is not None and activation.needs_rearm)


def _edge_setup_releases(edge: Any, state: Any, ctx: Any) -> tuple[_ActionPair, ...]:
    """Resting assignments needed to make the selected edge effective.

    Besides an exact spent one-shot, a selected command can be masked by a
    still-asserted alternative writer of one of its declared enablers.  Static
    selector graphs provide that relationship: release only live alternative
    actions for the same derived selector, then let Orientation expose the
    release as one setup Bearing.
    """

    execution_state = getattr(getattr(state, "work", None), "state", None)
    if execution_state is None:
        return ()
    snapshot = execution_state.tags
    resting = getattr(ctx, "resting", {})
    edge_tags = getattr(ctx, "edge_tags", set())
    commanded = tuple(pair for pair in (edge.action, *edge.co_actions) if pair is not None)
    commanded_tags = frozenset(tag for tag, _value in commanded)
    releases: dict[str, Any] = {}

    activation = _edge_write_activation(edge, state, ctx)
    if activation is not None and activation.needs_rearm:
        for tag, _value in commanded:
            rest = resting.get(tag, False)
            if tag not in edge_tags and not _values_match(snapshot.get(tag), rest):
                releases[tag] = rest

    compass = getattr(ctx, "compass", None)
    graphs = tuple(getattr(compass, "graphs", ()))
    chart_graphs = tuple(getattr(getattr(compass, "catalog", None), "chart_graphs", ()))
    for selector_tag, selector_value in edge.enablers:
        for graph in (*graphs, *chart_graphs):
            if graph.role.channel_tag != selector_tag:
                continue
            selected = any(
                candidate.action == edge.action
                and _values_match(candidate.to_value, selector_value)
                for candidate in graph.edges
            )
            if not selected:
                continue
            for alternative in graph.edges:
                pair = alternative.action
                if (
                    pair is None
                    or pair[0] in commanded_tags
                    or _values_match(alternative.to_value, selector_value)
                    or not _values_match(snapshot.get(pair[0]), pair[1])
                ):
                    continue
                rest = resting.get(pair[0], False)
                if not _values_match(snapshot.get(pair[0]), rest):
                    releases[pair[0]] = rest

    return tuple(releases.items())


def _compass_route_plan(
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair] | None = None,
    unavailable_producer_edges: frozenset[tuple[Any, ...]] = frozenset(),
    *,
    state: Any = None,
    prefer_first_edge_coast: bool = False,
) -> StaticPath | None:
    graphs = ctx.compass.graphs
    if not graphs:
        return None

    from pyrung.core.analysis.pilot.pipeline_graph import _best_static_path

    nogoods = key_nogoods if key_nogoods is not None else set()
    world_key = getattr(frame, "key", None)
    evidence_scope = EvidenceScope.capture(world_key, frame.snap.items())
    overlay = _pilot_rung_execution_receipt(
        getattr(state, "pilot_rungs", ()),
        frame.snap,
    )

    def _edge_open(edge: Any) -> bool:
        if edge.identity in unavailable_producer_edges:
            return False
        admission = NavigationEvidence.static_edge_admission(
            edge,
            world_key=world_key,
            snapshot=frame.snap,
            knowledge=ctx.compass.knowledge,
            blocked_actions=ctx.blocked_actions,
            context=ctx,
            evidence_scope=evidence_scope,
            pair_nogoods=nogoods,
        )
        return admission.allowed

    def _first_edge_open(edge: Any) -> bool:
        if not _edge_open(edge):
            return False
        # Only the first edge executes in this world. A later edge may
        # legitimately require the opposite of a temporary rearm overlay after
        # that overlay has yielded at its declared boundary.
        if not all(
            (owner := overlay.owner(tag)) is None or _values_match(owner.value, value)
            for tag, value in (*edge.source_constraints, *edge.enablers)
        ):
            return False
        already_effective = _edge_commands_effective(edge, frame.snap, overlay)
        if _edge_requires_setup(edge, state, ctx):
            # A high edge/one-shot trigger is spent, not reusable context.  Keep
            # the action edge selectable; Orientation will first materialize
            # its predecessor/rearm scan as a separate setup Bearing.
            already_effective = False
        # An already-effective command is not another candidate.  It admits
        # precisely one chart coast only when the selected writer's complete
        # live guard proves that the program can consume it in this world.
        return (
            not already_effective
            or _live_chart_completion_edge(
                edge,
                frame,
                state,
                ctx,
                overlay=overlay,
            )
            is not None
        )

    def _edge_available_now(edge: Any) -> bool:
        """Whether this edge's stable non-channel context holds in this world."""

        if not _first_edge_open(edge):
            return False
        commanded = frozenset(() if edge.action is None else (edge.action, *edge.co_actions))
        return all(
            tag == edge.role.channel_tag
            or (tag, value) in commanded
            or (
                ((owner := overlay.owner(tag)) is not None and _values_match(owner.value, value))
                or _values_match(frame.snap.get(tag), value)
            )
            for tag, value in (*edge.source_constraints, *edge.enablers)
        )

    def _first_edge_actionable(edge: Any) -> bool:
        """Prefer an explicit action over an unready actionless shortcut."""

        return edge.action is not None and _first_edge_open(edge)

    plans: list[StaticPath] = []
    for n in frame.tree.iter_nodes():
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        if not n.children and n.tag not in ctx.opaque_loop:
            continue
        if _values_match(frame.snap.get(n.tag), n.value):
            continue
        plan = None
        if prefer_first_edge_coast:
            plan = _best_static_path(
                n.tag,
                n.value,
                frame.snap,
                graphs,
                edge_allowed=_edge_open,
                first_edge_allowed=lambda edge: edge.action is None and _first_edge_open(edge),
            )
        if plan is None:
            plan = _best_static_path(
                n.tag,
                n.value,
                frame.snap,
                graphs,
                edge_allowed=_edge_available_now,
                first_edge_allowed=_edge_available_now,
            )
        if plan is None:
            plan = _best_static_path(
                n.tag,
                n.value,
                frame.snap,
                graphs,
                edge_allowed=_edge_open,
                first_edge_allowed=_edge_available_now,
            )
        if plan is None:
            plan = _best_static_path(
                n.tag,
                n.value,
                frame.snap,
                graphs,
                edge_allowed=_edge_open,
                first_edge_allowed=_first_edge_actionable,
            )
        if plan is None:
            plan = _best_static_path(
                n.tag,
                n.value,
                frame.snap,
                graphs,
                edge_allowed=_edge_open,
                first_edge_allowed=_first_edge_open,
            )
        if plan is not None:
            plans.append(plan)

    if not plans:
        return None
    return min(
        plans,
        key=lambda p: (_plan_off_target(p, ctx), _plan_ungrounded(p), _route_plan_score(p)),
    )


def _chart_edge_writer_trace(edge: Any, frame: Any, state: Any, ctx: Any) -> Any | None:
    """Read a chart's exact first-edge writer through ordinary Trace.

    The target-wide trace has already committed to one writer alternative and
    may legitimately omit the route charted through the current channel value.
    A chart can propose that alternative, but it cannot declare it executable:
    writer locking sends the exact effect back through Trace's normal guard,
    caller, availability, tide-table, avoid, and instruction readers.
    """

    effect_tag, effect_value = edge.route.writer_effect
    program = getattr(ctx, "program", None)
    pdg = getattr(ctx, "pdg", None)
    if effect_value is None or program is None or pdg is None:
        return None
    tree = trace_back(
        effect_tag,
        effect_value,
        frame.snap,
        pdg,
        program,
        getattr(ctx, "steerable", frozenset()),
        clear_only=getattr(ctx, "clear_only", frozenset()),
        opaque_loop=getattr(ctx, "opaque_loop", frozenset()),
        pipeline_internal_tags=getattr(ctx, "pipeline_internal_tags", frozenset()),
        prior=getattr(ctx, "domain_prior", None),
        avoid_pred=getattr(ctx, "avoid_pred", None),
        harness=getattr(getattr(state, "work", None), "_harness", None),
        execution_memory=getattr(
            getattr(getattr(state, "work", None), "state", None),
            "memory",
            None,
        ),
        writer_locks={(effect_tag, effect_value): edge.route.writer_node},
    )
    return tree if tree.writer_rung == edge.route.writer_node else None


def _route_context_actions(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> tuple[_ActionPair, ...]:
    """Lower exact first-edge level prerequisites into its atomic overlay.

    A chart edge can name a derived guard rather than the external input that
    establishes it.  The edge's writer-locked Trace artifact is the authority
    for that missing context: retain only presently unsatisfied, directly
    steerable levels.  Temporal operations and edge inputs keep their own
    release/assert contracts and cannot hitchhike here.
    """

    selected = _chart_edge_writer_trace(edge, frame, state, ctx)
    if selected is None:
        return ()

    commanded = frozenset(pair for pair in (edge.action, *edge.co_actions) if pair is not None)
    context: list[_ActionPair] = []
    seen = set(commanded)

    def _admit(pair: _ActionPair) -> None:
        tag, value = pair
        if (
            pair in seen
            or pair in key_nogoods
            or tag in getattr(ctx, "edge_tags", frozenset())
            or tag in getattr(ctx, "clear_only", frozenset())
            or tag not in getattr(ctx, "steerable", frozenset())
            or _values_match(frame.snap.get(tag), value)
            or not _action_allowed(ctx, pair)
            or _avoid_forces(ctx, [pair], frame.snap)
        ):
            return
        context.append(pair)
        seen.add(pair)

    for detail in selected.ordered_action_details():
        pair = detail.pair
        if (
            pair in seen
            or pair in key_nogoods
            or detail.availability > _WriterAvailability.AFTER_PREREQ
            or detail.establish
            or detail.heuristic
            or detail.until is not None
            or detail.operation is not None
        ):
            continue
        _admit(pair)

    # ``out(Tag)`` owns both polarities but False is an absence effect: ordinary
    # backward trace correctly has no firing writer to follow.  For a required
    # false Boolean enabler, prove the complement directly from every OTE rung
    # by finding stable inputs that force those guards off.  These are exact
    # writer guards, not free guesses; the forked scan still verifies whether
    # producer/consumer ordering realizes the composed edge.
    pdg = getattr(ctx, "pdg", None)
    program = getattr(ctx, "program", None)
    fixed = dict(edge.source_constraints)
    fixed.update(commanded)
    if pdg is not None and program is not None:
        for tag, value in edge.enablers:
            if not _values_match(value, False) or _values_match(frame.snap.get(tag), value):
                continue
            writers = tuple(pdg.writers_of.get(tag, frozenset()))
            if not writers or not all(tag in pdg.rung_nodes[index].ote_writes for index in writers):
                continue
            derived: list[_ActionPair] = []
            for index in writers:
                rung = resolve_rung(program, pdg.rung_nodes[index])
                holds = (
                    break_guard_holds(
                        rung,
                        frame.snap,
                        ctx,
                        fixed=fixed,
                        steerable=getattr(ctx, "steerable", frozenset()),
                    )
                    if rung is not None
                    else None
                )
                if holds is None:
                    derived = []
                    break
                derived.extend(holds)
            for pair in derived:
                _admit(pair)
    return tuple(context)


def _live_general_chart_completion_edge(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    allow_conservative_nomination: bool = False,
) -> Any | None:
    """Bind read-only chart geometry to an already-live Trace operation.

    Generalized charts never mint actions. Trace identifies the exact writer;
    ProgramStep and ordinary candidate admission then decide whether that
    writer owns current work. A conservative ``UNAVAILABLE_FROM_HERE`` trace
    verdict may be refined by an exact ProgramStep handoff receipt. Without
    that positive receipt the edge is declined before it becomes a bearing.
    """

    selected = _chart_edge_writer_trace(edge, frame, state, ctx)
    if selected is None or (
        not allow_conservative_nomination
        and selected.writer_availability > _WriterAvailability.AFTER_PREREQ
    ):
        return None

    effect_tag, effect_value = edge.route.writer_effect
    if effect_value is None:
        return None
    from pyrung.core.analysis.pilot.awaited_actions import Producer

    writer = ctx.pdg.rung_nodes[edge.route.writer_node]
    producer = Producer(
        rung_index=edge.route.writer_node,
        kind="program",
        co_writes=frozenset(writer.writes - {effect_tag}),
        command_tag=effect_tag,
        command_value=effect_value,
    )
    # Trace owns relevance and ProgramStep owns present-tense readiness. The
    # chart contributes only the next coordinate; an already-effective route
    # command remains context for this read, never another prescribed action.
    return replace(
        edge,
        from_value=frame.snap.get(edge.role.channel_tag),
        action=None,
        co_actions=(),
        program_producers=(producer,),
    )


def _general_chart_completion_plan(
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair] | None = None,
    *,
    state: Any,
    allow_conservative_nomination: bool = False,
) -> StaticPath | None:
    """Read one bounded chart heading for work Trace already owns.

    Unlike the opaque pipeline route reader, this reader cannot prescribe an
    action.  It only enriches an exact current-world producer with a channel
    boundary after all ordinary admission and availability evidence agrees.
    """

    graphs = ctx.compass.chart_graphs
    if not graphs:
        return None

    from pyrung.core.analysis.pilot.pipeline_graph import _best_static_path

    nogoods = key_nogoods if key_nogoods is not None else set()
    world_key = getattr(frame, "key", None)
    evidence_scope = EvidenceScope.capture(world_key, frame.snap.items())
    live_edges: dict[tuple[Any, ...], Any] = {}
    rejected_edges: set[tuple[Any, ...]] = set()

    def _edge_open(edge: Any) -> bool:
        return NavigationEvidence.static_edge_admission(
            edge,
            world_key=world_key,
            snapshot=frame.snap,
            knowledge=ctx.compass.knowledge,
            blocked_actions=ctx.blocked_actions,
            context=ctx,
            evidence_scope=evidence_scope,
            pair_nogoods=nogoods,
        ).allowed

    def _first_edge_live(edge: Any) -> bool:
        if not _edge_open(edge):
            return False
        if edge.identity in live_edges:
            return True
        if edge.identity in rejected_edges:
            return False
        live = _live_general_chart_completion_edge(
            edge,
            frame,
            state,
            ctx,
            allow_conservative_nomination=allow_conservative_nomination,
        )
        if live is None:
            rejected_edges.add(edge.identity)
            return False
        live_edges[edge.identity] = live
        return True

    def _first_edge_live_and_grounded(edge: Any) -> bool:
        return _edge_grounded(edge) and _first_edge_live(edge)

    plans: list[StaticPath] = []
    chart_channels = frozenset(graph.role.channel_tag for graph in graphs)
    for node in frame.tree.iter_nodes():
        if (
            node.satisfied
            or node.is_steerable
            or getattr(node, "pipeline_internal", False)
            or node.tag not in chart_channels
            or _values_match(frame.snap.get(node.tag), node.value)
        ):
            continue
        # A source-bound edge records the program's ordinary transition from
        # this exact channel value.  Search that topology first; otherwise a
        # shorter wildcard exception/reset writer can beat it inside one BFS
        # before the plan-level groundedness score ever sees the alternative.
        # Wildcards remain a fallback when no live grounded continuation can
        # reach the requested value.
        plan = _best_static_path(
            node.tag,
            node.value,
            frame.snap,
            graphs,
            edge_allowed=_edge_open,
            first_edge_allowed=_first_edge_live_and_grounded,
        )
        if plan is None:
            plan = _best_static_path(
                node.tag,
                node.value,
                frame.snap,
                graphs,
                edge_allowed=_edge_open,
                first_edge_allowed=_first_edge_live,
            )
        if plan is not None:
            plans.append(plan)

    if not plans:
        return None
    selected = min(
        plans,
        key=lambda plan: (
            _plan_off_target(plan, ctx),
            _plan_ungrounded(plan),
            _route_plan_score(plan),
        ),
    )
    live = live_edges.get(selected.first_edge.identity)
    if live is None:
        return None
    return replace(selected, edges=(live, *selected.edges[1:]))


def _live_chart_completion_edge(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    overlay: PilotOverlayExecution | None = None,
) -> Any | None:
    """Ground an already-asserted action edge in the exact current world.

    A route action may have been installed by an earlier setup phase while a
    late program rung prepared its derived control value. Once the selected
    writer's complete guard is true, reasserting that action is not a new
    steer: the next ordinary bearing is one observed program scan. Wildcard
    chart evidence alone is insufficient; the unique writer/caller guard is
    the grounding receipt.
    """

    if edge.action is None:
        return None
    if _edge_requires_setup(edge, state, ctx):
        # A consumed edge cannot become an actionless completion merely because
        # its input level and rung guard remain true. It must be released and
        # asserted again through separately oriented Bearings so rise/fall
        # state or instruction one-shot memory can rearm through execution.
        return None
    execution = (
        overlay
        if overlay is not None
        else _pilot_rung_execution_receipt(getattr(state, "pilot_rungs", ()), frame.snap)
    )
    if not _edge_commands_effective(edge, frame.snap, execution):
        return None

    from pyrung.core.analysis.pilot.evidence import selected_chart_producer_guard_rungs
    from pyrung.core.analysis.sp_values import _SnapshotView

    guards = selected_chart_producer_guard_rungs(edge, ctx.pdg, ctx.program)
    if not guards:
        return None
    view = _SnapshotView(frame.snap, {})
    try:
        if not all(guard._evaluate_conditions(view) for guard in guards):
            return None
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return None
    return replace(
        edge,
        from_value=frame.snap.get(edge.role.channel_tag),
        action=None,
        co_actions=(),
    )


def _edge_grounded(edge: Any) -> bool:
    """Whether *edge* carries a concrete from-value (not the ``ANY_FROM`` sentinel).

    Only a grounded edge is a coastable (WAIT-prescribable) claim: a wildcard edge
    says nothing about the state the register advances *from*, so "hold and wait"
    on it has no dwell semantics.
    """
    from pyrung.core.analysis.pilot.pipeline_graph import ANY_FROM

    return edge.from_value is not ANY_FROM


def _fmt_from(value: Any) -> str:
    """Format an edge from-value for reason strings — the ``ANY_FROM`` sentinel
    renders as ``'*'``, never as a raw ``<object object at 0x...>``."""
    from pyrung.core.analysis.pilot.pipeline_graph import ANY_FROM

    return "*" if value is ANY_FROM else repr(value)


def _plan_ungrounded(plan: StaticPath) -> int:
    """1 when *plan*'s first edge rides a wildcard (``ANY_FROM``) from-value.

    A wildcard edge is a *stateless* claim — the writer's condition never named
    the channel register, so the graph learned no from-state for it.  A derived
    mask register (``statemask = table[StateRequested]``) produces exactly this
    shape: every edge wildcard, every plan one edge "long".  Ranking purely by
    edge count lets such a plan hijack the route: the 1-edge wildcard
    ``ANY --C_Start--> mask`` beats the real 3-edge Clear->Reset->Start chain on
    the state register, pressing Start from ABORTED where it is a no-op.  A plan
    grounded in a concrete from-value carries real distance information, so it
    outranks any wildcard-first plan.  Ordering only — a wildcard plan is still
    tried when nothing grounded exists.
    """
    return 0 if _edge_grounded(plan.first_edge) else 1


def _plan_off_target(plan: StaticPath, ctx: Any) -> int:
    """0 when *plan* drives the overall target, 1 otherwise.

    The trace can surface other requirements on the same channel register as
    the target — e.g. reaching ``S_StateCurrent==11`` (Held) trails
    ``==10`` (Holding, a real predecessor) and ``==1`` (Clearing, an off-path
    artifact of tracing the completion bool through a counterfactual writer).
    Ranking purely by edge count lets the shortest of these hijack the route: at
    Stopped the 3-edge ``C_Abort`` plan toward ``==1`` beats the 6-edge
    ``C_Reset`` plan toward the real target and drives the wrong way.  The
    target's own ``find_path`` already includes its required intermediate
    values, so anchor to it and let off-target requirement plans lose ties.
    """
    on_target = plan.needed_tag == ctx.target.tag and _values_match(
        plan.needed_value, ctx.target.value
    )
    return 0 if on_target else 1


def _route_plan_score(plan: StaticPath) -> tuple[int, int, str]:
    direct = 0 if plan.needed_tag == plan.role.channel_tag else 1
    return (len(plan.edges), direct, plan.role.channel_tag)


def _compass_route_actions(
    plan: StaticPath | None,
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> tuple[_ActionPair, ...]:
    if plan is None:
        return ()

    edge = plan.first_edge
    if edge.action is not None:
        if edge.action not in key_nogoods and _action_allowed(ctx, edge.action):
            return (edge.action,)
        return ()

    direct: list[_ActionPair] = []
    for tag, value in edge.enablers:
        if _values_match(frame.snap.get(tag), value):
            continue
        pair = (tag, value)
        if tag in ctx.steerable and pair not in key_nogoods and _action_allowed(ctx, pair):
            direct.append(pair)

    return tuple(direct)


def _oscillating_rungs(tag: str, ctx: Any, scope: Any, plc: Any) -> tuple[PilotRung, ...]:
    """A two-rule toggle for an edge-gated accumulator driver.

    Drives *tag* to each polarity while it sits at the other, so it alternates
    every scan — the rising/falling edge train the counter's pulse contract
    explicitly requests. This is option lowering of an owner-declared pulse,
    not a corrective behavior-category hypothesis.
    """
    from pyrung.core.condition import AllCondition, CompareNe

    resting = bool(ctx.resting.get(tag, False))
    other = not resting
    source = plc._known_tags_by_name[tag]
    return (
        PilotRung(tag, other, AllCondition(scope, CompareNe(source, other))),
        PilotRung(tag, resting, AllCondition(scope, CompareNe(source, resting))),
    )


def _managed_boolean_rungs(
    details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
) -> tuple[tuple[PilotRung, ...], frozenset[_ActionPair]]:
    """Assert a rung-managed Boolean again under the new writer context.

    When an earlier guard expires, Boolean input-image lowering returns the input
    to False. If trace later needs that input again, it is already owned by
    PilotRungs, so a plain patch would lose to the overlay: append another rung
    guarded by the newly selected writer context. For ``rise(Input)`` only, add
    the input-polarity guard so the rung is a one-scan pulse; the False scan
    already happened naturally while no rung was active. A level prerequisite
    must remain asserted every scan while its context holds.
    """
    from pyrung.core.condition import AllCondition, CompareNe

    managed = {rung.dest for rung in state.pilot_rungs}
    overlay = _pilot_rung_execution_receipt(state.pilot_rungs, frame.snap)
    proposed: list[PilotRung] = []
    lowered: set[_ActionPair] = set()
    for detail in details:
        tag, value = detail.pair
        if (
            tag not in managed
            or type(value) is not bool
            or ((tag in ctx.edge_tags or detail.pulse) and value is not True)
        ):
            continue
        source = state.work._known_tags_by_name.get(tag)
        if source is None:
            continue
        active_owner = overlay.owner(tag)
        if active_owner is not None:
            # The exact overlay receipt, not Boolean polarity, decides whether
            # this input is available to a fresh route.  In particular a
            # trace-requested False cannot release an effective True owner by
            # repeatedly issuing a patch that the overlay will overwrite.
            lowered.add(detail.pair)
            continue
        if value is False:
            # The shared overlay will lower this input, but that lowering is
            # still the live trace action. Keep it visible so PILOT gives the
            # program one scan to observe the release before considering a
            # command that closes the operation.
            continue
        if detail.guard_atoms:
            try:
                context = tuple(_atom_condition(state.work, atom) for atom in detail.guard_atoms)
            except (KeyError, ValueError):
                continue
        elif detail.until is not None:
            context = (_until_unresolved_condition(state.work, detail.until),)
        else:
            context = (
                _target_unresolved_condition(
                    state.work,
                    ctx.target.tag,
                    ctx.target.value,
                    ctx.target.predicate,
                ),
            )
        guard = (
            AllCondition(*context, CompareNe(source, value))
            if tag in ctx.edge_tags or detail.pulse
            else AllCondition(*context)
        )
        proposed.append(PilotRung(tag, value, guard))
        lowered.add(detail.pair)
    return tuple(proposed), frozenset(lowered)
