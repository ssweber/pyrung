"""Materialize the action and wait options for one orientation.

``_build_candidates`` combines the current trace tree, constrained static
routes, learned transitions, program-awaited actions, existing corrections,
and prerequisite holds. It returns the current read's deterministic action
order together with any prescribed wait, completion frontier, or no-bearing
diagnosis.

Candidate construction reads the current world and knowledge but does not
execute a trial, apply observations, or commit state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import product
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _atom_condition,
    _avoid_forces,
    _rung_execution_receipt,
    _target_unresolved_condition,
    _until_unresolved_condition,
    wait_edge_nogood,
)
from pyrung.core.analysis.pilot.availability import _WriterAvailability
from pyrung.core.analysis.pilot.compass import (
    is_action,
    is_composite_action,
    unique_legal_current_reading,
)
from pyrung.core.analysis.pilot.navigation import ActSource, pulse_identity
from pyrung.core.analysis.pilot.trace import (
    TraceReadConstraints,
    _all_nodes,
    frontier_pairs,
    trace_back,
)
from pyrung.core.analysis.pilot.types import _ActionPair
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.charts import StaticPath
    from pyrung.core.analysis.pilot.trace import TraceAction

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One action option with exactly one provenance category."""

    tag: str
    value: Any
    source: ActSource
    provenance: tuple[str, ...] = ()
    wake: int | None = None
    # The first compass edge's executable promise. Trial verification uses this for every
    # route-prescribed action, not only a coast/zoom: landing elsewhere means
    # the program displaced the route and must be investigated.
    bearing_channel_tag: str | None = None
    bearing_channel_value: Any = None
    # A program-owned current (currents.py): the one operator action the program
    # is dwelling on at the current state of an opaque-loop channel, surfaced when
    # the trace dead-ends and the compass route is the avoided command.  Ordered
    # like a prescribed edge (a recognized bearing), but below route/influence so
    # it is the fallback, never overriding an available route.
    current_note: str = ""
    # An external input required by the exact program producer selected for an
    # automatic route edge. It is a current-world bearing below an established
    # route/current and above an unrelated trace action.
    program_note: str = ""
    program_context_actions: tuple[_ActionPair, ...] = ()

    @property
    def pair(self) -> _ActionPair:
        return (self.tag, self.value)

    @property
    def route_prescribed(self) -> bool:
        return self.source is ActSource.ROUTE

    @property
    def influence_prescribed(self) -> bool:
        return self.source is ActSource.INFLUENCE

    @property
    def current_prescribed(self) -> bool:
        return self.source is ActSource.CURRENT

    @property
    def program_prescribed(self) -> bool:
        return self.source is ActSource.PROGRAM


@dataclass(frozen=True)
class _CandidateList:
    active_trace_actions: tuple[_ActionPair, ...]
    trace_actions: tuple[_ActionPair, ...]
    trace_action_details: tuple[TraceAction, ...]
    route_candidates: tuple[_ActionPair, ...]
    candidates: tuple[_Candidate, ...]
    wake_cap: int
    route_plan: StaticPath | None = None
    wait_prescribed: bool = False
    wait_reason: str | None = None
    # An instruction-owned frontier's immediate observable boundary. This is
    # one coast heading, not a route: after it lands PILOT retraces.
    advance_boundary: _ActionPair | None = None
    advance_condition: Any = None
    # A composite learned edge (skiff pair probe): the whole action set must
    # fire in one window.  Tried as a single batch trial before the singles —
    # verified live through the same gate pipeline as any candidate.
    prescribed_batch: tuple[_ActionPair, ...] | None = None
    prerequisite_rungs: tuple[PilotRung, ...] = ()
    stuck_reason: str | None = None
    # Co-actions that must fire in the same scan as a route-prescribed command
    # candidate (the one-shot edge gate, e.g. ``rise(CmdChgRequest)``).  Carried
    # off the chosen compass edge so the command rung actually executes.
    route_co_actions: tuple[_ActionPair, ...] = ()
    # Convergence command buttons currently held off-resting.  A command decoder
    # is last-write-wins, so pressing one button while another is still held
    # fires the wrong command; a convergence pulse releases these.
    held_command_tags: frozenset[str] = frozenset()
    # The completion re-read's unmet frontier: a prescribed wait's charted
    # completion condition, re-traced against the live world (``_prescribe_wait``).
    # Names the pressable lever behind the wait (``x_RotateFB``) so the terminal
    # frontier clause points past the pipeline cut. Orientation stamps it onto
    # its completed frame; empty when no wait carries completion.
    completion_frontier: tuple[_ActionPair, ...] = ()
    # Exact-producer reading behind this iteration's automatic edge. The
    # orientation layer consumes its witnessed local boundary; recording keeps
    # the same value available for diagnostics.
    program_step: Any = None


def _tree_work_anchors(tree: Any, route: Any) -> tuple[_ActionPair, ...]:
    """Concrete current-trace facts that can identify live work."""

    anchors: list[_ActionPair] = []
    via_hint = getattr(route, "via_hint", None)
    if via_hint is not None:
        anchors.append(via_hint)
        return tuple(anchors)
    for node in _all_nodes(tree):
        if node.relational or node.value is None:
            continue
        pair = (node.tag, node.value)
        if pair not in anchors:
            anchors.append(pair)
    return tuple(anchors)


def _current_work_evidence(frame: Any, state: Any, route: Any) -> tuple[str, ...]:
    """Recognize work a technician can point to in the current world.

    Reverted journey history and mere tenure are intentionally absent.  Every
    reason is backed by a fact in the live revertible world and disappears as
    soon as that fact is clobbered or the trace no longer depends on it.
    """

    anchors = _tree_work_anchors(frame.tree, route)
    anchor_tags = {tag for tag, _value in anchors}
    reasons: list[str] = []

    def _matches_anchor(tag: str, value: Any) -> bool:
        return any(
            anchor_tag == tag and _values_match(anchor_value, value)
            for anchor_tag, anchor_value in anchors
        )

    for rung in getattr(state, "rungs", ()):
        tag = getattr(rung, "dest", None)
        value = getattr(rung, "value", None)
        if (
            tag is not None
            and _matches_anchor(tag, value)
            and _values_match(frame.snap.get(tag), value)
        ):
            reasons.append(f"held:{tag}={value!r}")

    pending = getattr(state, "pending_departure", None)
    if pending is not None and pending.channel_tag in anchor_tags:
        current = frame.snap.get(pending.channel_tag)
        if not _values_match(current, pending.from_value):
            reasons.append(f"pending:{pending.channel_tag}={current!r}")

    committed = tuple(getattr(state, "committed_acts", ()))
    if committed:
        context = committed[-1].context
        before = context.before_snap
        after = context.after_snap
        if getattr(context.motion, "is_coast", False):
            tree_tags = {node.tag for node in _all_nodes(frame.tree)}
            for tag, value in after.items():
                if (
                    tag in tree_tags
                    and not _values_match(before.get(tag), value)
                    and _values_match(frame.snap.get(tag), value)
                ):
                    reasons.append(f"operation:{tag}")

        for tag, desired in anchors:
            if (
                tag in after
                and not _values_match(before.get(tag), after.get(tag))
                and _values_match(after.get(tag), desired)
                and _values_match(frame.snap.get(tag), desired)
            ):
                reasons.append(f"established:{tag}={desired!r}")

        gauge = getattr(state, "gauge", None)
        components = getattr(gauge, "components", ()) if gauge is not None else ()
        if (
            gauge is not None
            and components
            and any(component.tag in anchor_tags for component in components)
            and gauge.ordinal_advanced(before, after)
        ):
            reasons.append("gauge:advanced")

    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class _WaitPrescription:
    """One grounded wait bearing and the evidence that justified it.

    This is only the decision portion of :class:`WaitRead`.  Exact-producer
    inputs must all survive that reading's ordinary admission before the
    prescription may authorize a coast.
    """

    prescribed: bool
    reason: str | None = None
    frontier: tuple[_ActionPair, ...] = ()
    program_step: Any = None
    boundary: Any = None


@dataclass(frozen=True)
class WaitRead:
    """One wait prescription together with every action its read discovered.

    The prescription cannot cross candidate construction on its own.  Its
    completion and exact-producer details travel with it into one ordinary
    admission pass.
    """

    prescription: _WaitPrescription
    details: tuple[TraceAction, ...] = ()


@dataclass(frozen=True)
class _TraceAdmission:
    """One application of the candidate pool's ordinary admission rules."""

    active_actions: tuple[_ActionPair, ...]
    actions: tuple[_ActionPair, ...]
    details: tuple[TraceAction, ...]
    detail_by_pair: dict[_ActionPair, TraceAction]
    managed_boolean_rungs: tuple[PilotRung, ...]
    establish_pending: bool


@dataclass(frozen=True)
class _AdmittedWait:
    """A complete wait read after the candidate pool admitted its details."""

    read: WaitRead
    admission: _TraceAdmission

    @property
    def admitted_pairs(self) -> frozenset[_ActionPair]:
        return frozenset(
            (
                *self.admission.actions,
                *((rung.dest, rung.value) for rung in self.admission.managed_boolean_rungs),
            )
        )

    @property
    def admitted_supplement(self) -> bool:
        return any(detail.pair in self.admitted_pairs for detail in self.read.details)

    @property
    def viable(self) -> bool:
        """Whether every exact-producer input survived this admission.

        The program cannot be observed crossing an owned boundary unless every
        external input that exact producer currently requires will be applied
        by the same candidate result.
        """

        step = self.read.prescription.program_step
        required_pairs = step.required_pairs if step is not None else frozenset()
        return self.read.prescription.prescribed and (
            not required_pairs or required_pairs <= self.admitted_pairs
        )

    @property
    def prescription(self) -> _WaitPrescription:
        prescription = self.read.prescription
        if prescription.prescribed and not self.viable:
            return replace(prescription, prescribed=False)
        return prescription


def _hold_values(hold_value: Any) -> tuple[Any, ...]:
    """Steady values a scalar or oscillating hold can pin its tag to."""

    rules = getattr(hold_value, "rules", None)
    if rules is not None:
        return tuple(rule.value for rule in rules)
    return (hold_value,)


def hold_defeats_needed(
    tag: str,
    hold_value: Any,
    needed: Sequence[tuple[str, Any]],
    pdg: Any,
    program: Any,
) -> bool:
    """Whether an option hold provably pins a checkpoint need.

    ``needed`` is ordered target-most first, so the first value for a tag is its
    requirement and deeper values are en-route stopovers. Direct contradictions
    and held guards that force a contradicting literal write are self-defeating.
    """

    return _holds_defeat_needed(((tag, hold_value),), needed, pdg, program)


def _holds_defeat_needed(
    holds: Sequence[tuple[str, Any]],
    needed: Sequence[tuple[str, Any]],
    pdg: Any,
    program: Any,
) -> bool:
    """Static write-vs-need proof for one executable hold assignment."""

    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.simplified import Atom, _conditions_list_to_expr, _expr_forced_true
    from pyrung.core.analysis.steerable import _literal_write

    needed_first: dict[str, Any] = {}
    for needed_tag, needed_value in needed:
        if isinstance(needed_value, Atom):
            continue
        needed_first.setdefault(needed_tag, needed_value)
    if not needed_first:
        return False

    held_values = {tag: _hold_values(value) for tag, value in holds}
    if not held_values:
        return False
    if any(
        tag in needed_first and any(not _values_match(value, needed_first[tag]) for value in values)
        for tag, values in held_values.items()
    ):
        return True

    for node in pdg.rung_nodes:
        read_tags = tuple(tag for tag in node.condition_reads if tag in held_values)
        if not read_tags:
            continue
        rung = resolve_rung(program, node)
        if rung is None:
            continue
        expr = _conditions_list_to_expr(getattr(rung, "_conditions", []))
        assignments = (
            dict(zip(read_tags, values, strict=True))
            for values in product(*(held_values[tag] for tag in read_tags))
        )
        if not any(_expr_forced_true(expr, assignment) is True for assignment in assignments):
            continue
        for needed_tag, needed_value in needed_first.items():
            written = _literal_write(rung, needed_tag)
            if written is not None and not _values_match(written, needed_value):
                return True
    return False


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
        for node in _all_nodes(tree)
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
# Compass routing
# ---------------------------------------------------------------------------


def _learned_edge_allowed(
    tag: str,
    source: Any,
    cause: Any,
    destination: Any,
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> bool:
    """Apply every live constraint before a learned edge enters any path query."""

    if is_action(cause):
        members = cast(tuple[_ActionPair, ...], cause) if is_composite_action(cause) else (cause,)
        return all(
            pair not in key_nogoods
            and ctx.route_allowed(pair)
            and not _avoid_forces(ctx, (pair,), frame.snap)
            for pair in members
        )
    return wait_edge_nogood(tag, source, destination) not in key_nogoods


def _compass_route_plan(
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair] | None = None,
    excluded_edges: frozenset[tuple[Any, ...]] = frozenset(),
) -> StaticPath | None:
    if not ctx.compass.graphs:
        return None

    from pyrung.core.analysis.pilot.charts import _best_static_path

    nogoods = key_nogoods if key_nogoods is not None else set()

    def _edge_open(edge: Any) -> bool:
        if edge.identity in excluded_edges:
            return False
        if ctx.compass.knowledge.static_edge_status(
            edge, getattr(frame, "key", None), frame.snap
        ) in {
            "contradicted",
            "no_change",
        }:
            return False
        if edge.action is None:
            # A completion edge proven sterile at this world (a rejected wait)
            # is walked around, exactly like a nogood press — BFS then returns
            # the surviving route.
            return (
                wait_edge_nogood(edge.role.channel_tag, edge.from_value, edge.to_value)
                not in nogoods
            )
        exact_artifact = pulse_identity((edge.action, *edge.co_actions))
        return (
            edge.action not in nogoods
            and not ctx.compass.knowledge.act_is_nogood(
                getattr(frame, "key", None),
                exact_artifact,
            )
            and ctx.route_allowed(edge.action)
            and not _avoid_forces(ctx, [edge.action], frame.snap)
        )

    plans: list[StaticPath] = []
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        if not n.children and n.tag not in ctx.opaque_loop:
            continue
        if _values_match(frame.snap.get(n.tag), n.value):
            continue
        plan = _best_static_path(
            n.tag,
            n.value,
            frame.snap,
            ctx.compass.graphs,
            edge_allowed=_edge_open,
        )
        if plan is not None:
            plans.append(plan)

    if not plans:
        return None
    return min(
        plans,
        key=lambda p: (_plan_off_target(p, ctx), _plan_ungrounded(p), _route_plan_score(p)),
    )


def _edge_grounded(edge: Any) -> bool:
    """Whether *edge* carries a concrete from-value (not the ``ANY_FROM`` sentinel).

    Only a grounded edge is a coastable (WAIT-prescribable) claim: a wildcard edge
    says nothing about the state the register advances *from*, so "hold and wait"
    on it has no dwell semantics.
    """
    from pyrung.core.analysis.pilot.charts import ANY_FROM

    return edge.from_value is not ANY_FROM


def _fmt_from(value: Any) -> str:
    """Format an edge from-value for reason strings — the ``ANY_FROM`` sentinel
    renders as ``'*'``, never as a raw ``<object object at 0x...>``."""
    from pyrung.core.analysis.pilot.charts import ANY_FROM

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
        if edge.action not in key_nogoods and ctx.route_allowed(edge.action):
            return (edge.action,)
        return ()

    direct: list[_ActionPair] = []
    for tag, value in edge.enablers:
        if _values_match(frame.snap.get(tag), value):
            continue
        pair = (tag, value)
        if tag in ctx.steerable and pair not in key_nogoods and ctx.route_allowed(pair):
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

    managed = {rung.dest for rung in state.rungs}
    overlay = _rung_execution_receipt(state.rungs, frame.snap)
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
        if value is False:
            # The shared overlay will lower this input, but that lowering is
            # still the live trace action. Keep it visible so PILOT gives the
            # program one scan to observe the release before considering a
            # command that closes the operation.
            continue
        active_owner = overlay.owner(tag)
        if active_owner is not None:
            # A replay-proved correction owns this input until its observed
            # release boundary. The backward trace is still diagnostic, but it
            # cannot append an opposite last-write-wins rung while that proof is
            # active. Once the guard is false, the fresh trace may steer it.
            lowered.add(detail.pair)
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


# ---------------------------------------------------------------------------
# Candidate building — the compass in one call
# ---------------------------------------------------------------------------


def _current_bearing(frame: Any, ctx: Any) -> Any:
    """The program-owned current's operator action for the current state, or
    ``None``.

    Consulted only when the target register is an opaque-loop pipeline channel
    (the shape a program-owned command detour lives on).  Delegates to the
    read-side recognizer ``currents.current_readings`` over a
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
    return unique_legal_current_reading(
        world,
        channel,
        ctx.pipeline_roles,
        route_allowed=ctx.route_allowed,
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

    The wait's bearing (``StaticTransitionEdge.completion`` — charts.py) is ordinary
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


def _advance_heading(boundary: Any, frame: Any, state: Any) -> _ActionPair | None:
    """Lower an owned relational boundary to an exact observable heading."""
    from pyrung.core.analysis.pilot.advance import build_advance_index
    from pyrung.core.crossing import Cmp, Eq

    if isinstance(boundary, Eq) and len(boundary.values) == 1:
        return (boundary.tag, next(iter(boundary.values)))
    if not isinstance(boundary, Cmp) or boundary.op not in {">=", "<="}:
        return None
    owner = build_advance_index(
        state.work.program,
        getattr(state.work, "_harness", None),
    ).resolve(boundary.tag)
    if owner is None:
        return None
    target = frame.snap.get(str(boundary.bound)) if boundary.bound_is_tag else boundary.bound
    if target is None:
        return None
    return (boundary.tag, target)


def _prescribe_wait(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    reason: str | None = None,
) -> _WaitPrescription:
    """Mint a prescribed-wait bearing from one current-world edge read.

    The single owner of "a wait is prescribed" for both mint paths. A route
    completion edge (zoom) must be *grounded* — a wildcard from-value has no
    dwell semantics, so it refuses the wait (returns ``prescribed=False``). An
    influence-path wait passes ``edge=None`` with an explicit ``reason`` —
    always coastable. Automatic sibling edges carry one exact-producer
    ``ProgramStep`` reading in the returned prescription.

    This function never returns actions.  :func:`_read_wait` collects any
    completion or exact-producer details separately so `_build_candidates` can
    pass them through ordinary trace admission.
    """
    if edge is None:
        return _WaitPrescription(True, reason)
    if not _edge_grounded(edge):
        return _WaitPrescription(False)

    route_reason = (
        f"let-run {edge.role.channel_tag}: {_fmt_from(edge.from_value)}->{edge.to_value!r}"
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
            return _WaitPrescription(
                False,
                f"{route_reason}; exact program producer is ambiguous",
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
            via_pred=ctx.via_pred,
            harness=getattr(state.work, "_harness", None),
        )
        step = read_program_step(
            world,
            producers[0],
            state.work,
            state.rungs,
            resting=ctx.resting,
        )
        if step.status is ProgramStepStatus.KEEP_RUNNING:
            heading = _advance_heading(step.boundary, frame, state)
            if heading is None:
                return _WaitPrescription(
                    False,
                    f"{route_reason}; owned boundary has no exact coast heading",
                    program_step=step,
                )
            movement = next(
                (
                    (tag, before, after)
                    for tag, before, after in reversed(step.projected_changes)
                    if tag == step.channel and not _values_match(before, after)
                ),
                None,
            )
            observation = (
                f" ({movement[0]}: {movement[1]!r}->{movement[2]!r})"
                if movement is not None
                else f"; {step.reason}"
            )
            return _WaitPrescription(
                True,
                f"{route_reason}{observation}",
                frontier=(heading,),
                program_step=step,
                boundary=step.boundary,
            )
        if step.status is ProgramStepStatus.NEEDS_INPUT:
            boundary = step.uniform_handoff_boundary
            if boundary is not None:
                heading = _advance_heading(boundary, frame, state)
                if heading is None:
                    return _WaitPrescription(
                        False,
                        f"{route_reason}; owned boundary has no exact coast heading",
                        program_step=step,
                    )
                return _WaitPrescription(
                    True,
                    (f"{route_reason}; supply its current input and hand off to {heading[0]}"),
                    frontier=(heading,),
                    program_step=step,
                    boundary=boundary,
                )
            frontier = tuple(
                action.pair
                for action in step.required_inputs
                if action.pulse or not _values_match(frame.snap.get(action.tag), action.value)
            )
            return _WaitPrescription(
                False,
                f"{route_reason}; {step.reason}",
                frontier=frontier,
                program_step=step,
            )
        if step.status is ProgramStepStatus.INTERRUPTED:
            return _WaitPrescription(
                True,
                f"{route_reason}; {step.reason}",
                program_step=step,
            )
        return _WaitPrescription(
            False,
            f"{route_reason}; {step.reason}",
            program_step=step,
        )

    return _WaitPrescription(True, route_reason)


def _read_wait(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    reason: str | None = None,
) -> WaitRead:
    """Read one wait edge while keeping every discovered action attached."""

    prescription = _prescribe_wait(edge, frame, state, ctx, reason=reason)
    step = prescription.program_step
    details = (
        step.inputs_with_lifetime
        if step is not None and prescription.prescribed
        else step.required_inputs
        if step is not None
        else ()
    )
    if edge is not None and not edge.program_producers and edge.completion:
        details, frontier = _completion_reread(edge, frame, state, ctx)
        prescription = replace(prescription, frontier=frontier)
    return WaitRead(prescription, details)


def _admit_trace_details(
    details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _TraceAdmission:
    """Apply one admission policy to every current-world trace reading.

    The broad target trace and supplemental completion/program reads differ in
    provenance, not privilege.  Duplicate details preserve the target trace's
    evidence while composing an owned lifetime discovered by the narrower
    read.  Nothing enters candidate ranking by being appended after this pass.
    """

    detail_by_pair: dict[_ActionPair, TraceAction] = {}
    ordered_pairs: list[_ActionPair] = []
    for detail in details:
        pair = detail.pair
        existing = detail_by_pair.get(pair)
        if existing is None:
            detail_by_pair[pair] = detail
            ordered_pairs.append(pair)
        elif existing.until is None and detail.until is not None:
            detail_by_pair[pair] = replace(existing, until=detail.until)

    active_trace_actions = tuple(
        pair
        for pair in ordered_pairs
        for detail in (detail_by_pair[pair],)
        if ctx.route_allowed(pair)
        and (
            not _values_match(frame.snap.get(pair[0]), pair[1])
            or pair[0] in ctx.edge_tags
            or detail.pulse
            or detail.until is not None
        )
    )
    trace_actions = tuple(pair for pair in active_trace_actions if pair not in key_nogoods)
    trace_action_details = tuple(detail_by_pair[pair] for pair in trace_actions)

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

    return _TraceAdmission(
        active_actions=active_trace_actions,
        actions=trace_actions,
        details=trace_action_details,
        detail_by_pair=detail_by_pair,
        managed_boolean_rungs=managed_boolean_rungs,
        establish_pending=establish_pending,
    )


def _admit_wait_read(
    read: WaitRead,
    base_details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _AdmittedWait:
    """Admit one whole wait read through the candidate pool's only policy."""

    return _AdmittedWait(
        read=read,
        admission=_admit_trace_details(
            (*base_details, *read.details),
            frame,
            state,
            ctx,
            key_nogoods,
        ),
    )


def _build_candidates(
    frame: Any,
    state: Any,
    ctx: Any,
) -> _CandidateList:
    key_nogoods = set(ctx.compass.knowledge.nogood_pairs(frame.key))
    gauge = getattr(state, "gauge", None)
    # Clear-only (ack-cleared momentary) commands join the pulse-treatment set: the
    # program clears them every scan, so their idiom is pulse-and-release.  Holding
    # one steady as a prerequisite would assert a momentary command (a mode-change
    # request) forever — so they are pulsed like edge/action tags, never held.
    _act_or_edge = ctx.compass.action_tags | ctx.edge_tags | ctx.clear_only

    # Convergence command buttons currently held off-resting (and not a deliberate
    # forced hold).  A convergence pulse must release these or the program's
    # last-write-wins decoder fires the wrong command (a stuck CmdAbort overriding
    # CmdReset).  Empty for non-convergence programs → no effect.
    held_command_tags = frozenset(
        t
        for t in ctx.compass.action_tags
        if t not in {r.dest for r in state.rungs}
        and not _values_match(frame.snap.get(t), ctx.resting.get(t, False))
    )

    # This preliminary admission determines whether the broad target trace
    # already owns the move. A selected wait edge may add a narrower current-
    # world reading below; the combined pool is then admitted again through the
    # same function before candidate ranking.
    admission = _admit_trace_details(
        tuple(frame.raw_trace_action_details),
        frame,
        state,
        ctx,
        key_nogoods,
    )

    # A trace leaf whose selected writer is available now (or after its named
    # prerequisite) is live local work. So is a trace continuation when Gauge
    # can see work already banked beyond a proved reset floor: at recipe Step
    # 103, open the door before Unhold even if the full future writer chain is
    # conservatively unavailable. Unknown/unavailable leaves with no banked
    # work do not veto the current process boundary; from ABORTED the charted
    # Clear edge owns the move even though the deep target trace can already
    # name a later mode-change request.
    current_trace_actions = tuple(
        pair
        for pair in admission.actions
        for detail in (admission.detail_by_pair.get(pair),)
        if detail is not None and detail.availability <= _WriterAvailability.AFTER_PREREQ
    )
    banked_trace_work = bool(
        admission.actions and gauge is not None and gauge.has_banked_work(frame.snap)
    )
    live_plan = _compass_route_plan(frame, ctx, key_nogoods)
    route_plan = (
        None
        if current_trace_actions
        or banked_trace_work
        or (getattr(state, "pending_departure", None) is not None and admission.active_actions)
        else live_plan
    )
    # A zoom iteration: the route's next edge is a completion (no action),
    # so the frontier self-advances under held state.  Prerequisites are the
    # level signals that must be held while timers accumulate.  A pending
    # establish gate is never a zoom: the frontier cannot self-advance past the
    # closed gate, so hold stage 0 as the bearing instead.
    _is_zoom = (
        not admission.establish_pending
        and route_plan is not None
        and route_plan.first_edge.action is None
    )
    # An automatic chart edge is only a possible route until its exact program
    # producer is checked in this controlled world. If that producer offers no
    # executable wait or live input, remove just that edge from this read and
    # ask the same graph for the next route. This is how HELD walks around a
    # structurally possible Complete edge to the currently conductive Unhold
    # edge, without retaining a route suffix or poisoning another world.
    preflight_wait: _AdmittedWait | None = None
    excluded_edges: set[tuple[Any, ...]] = set()
    while _is_zoom:
        assert route_plan is not None
        preflight_wait = _admit_wait_read(
            _read_wait(route_plan.first_edge, frame, state, ctx),
            tuple(frame.raw_trace_action_details),
            frame,
            state,
            ctx,
            key_nogoods,
        )
        if (
            preflight_wait.viable
            or preflight_wait.admitted_supplement
            or not route_plan.first_edge.program_producers
        ):
            break
        excluded_edges.add(route_plan.first_edge.identity)
        alternate = _compass_route_plan(
            frame,
            ctx,
            key_nogoods,
            frozenset(excluded_edges),
        )
        if alternate is None:
            break
        route_plan = alternate
        preflight_wait = None
        _is_zoom = not admission.establish_pending and route_plan.first_edge.action is None

    if _is_zoom or admission.establish_pending:
        route_candidates: tuple[_ActionPair, ...] = ()
    else:
        route_candidates = _compass_route_actions(route_plan, frame, ctx, key_nogoods)
    # Co-actions for the route command (the one-shot edge gate); pulsed in the
    # same scan as the command candidate via _candidate_applied.
    route_co_actions: tuple[_ActionPair, ...] = (
        tuple(route_plan.first_edge.co_actions)
        if route_candidates and route_plan is not None
        else ()
    )

    wait = _WaitPrescription(False)
    completion_frontier: tuple[_ActionPair, ...] = ()
    program_step: Any = None
    program_pairs: set[_ActionPair] = set()
    advance_condition: Any = None
    # A grounded completion edge may add a narrower current-world read below the
    # opaque pipeline cut. Those details join the broad target details and the
    # whole pool re-enters ordinary admission before prerequisite splitting.
    zoom_wait = _WaitPrescription(False)
    if _is_zoom:
        assert route_plan is not None  # _is_zoom is True only when route_plan exists
        assert preflight_wait is not None
        zoom_wait = preflight_wait.prescription
        completion_frontier = zoom_wait.frontier
        program_step = zoom_wait.program_step
        advance_condition = zoom_wait.boundary
        if program_step is not None:
            program_pairs = set(program_step.required_pairs)
        admission = preflight_wait.admission
        if admission.establish_pending:
            _is_zoom = False
            zoom_wait = replace(zoom_wait, prescribed=False)

    active_trace_actions = admission.active_actions
    trace_actions = admission.actions
    trace_action_details = admission.details
    detail_by_pair = admission.detail_by_pair
    establish_pending = admission.establish_pending

    # Prerequisite/command split: on zoom iterations, and on a self-advancing
    # coast leaf that has no compass route (a harness-linked sensor ramp, or a
    # timer/counter threshold reached via the terminal let-run rather than a
    # route zoom).  Prerequisites are non-action, non-edge steerable inputs that
    # must be *held* while the frontier self-advances — e.g. the Enable that
    # drives a sensor toward its threshold.  Without this they would be pulsed
    # and reverted as no-progress commands, and the coast would never ramp.  On
    # plain iterations, all trace actions are commands — pulse-and-judge.
    _is_coast = any(
        getattr(n, "advance", None) is not None and not n.satisfied for n in frame.tree.leaves()
    )
    advance_boundary: _ActionPair | None = (
        completion_frontier[0]
        if advance_condition is not None and len(completion_frontier) == 1
        else None
    )
    if _is_coast:
        for node in frame.tree.leaves():
            step = getattr(node, "advance", None)
            if step is None or node.satisfied:
                continue
            advance_boundary = (
                getattr(node, "owner_boundary", None)
                if getattr(node, "linear_boundary", False)
                else None
            ) or _advance_heading(step.until, frame, state)
            if advance_boundary is not None:
                advance_condition = (
                    getattr(node, "owner_condition", None)
                    if getattr(node, "linear_boundary", False)
                    else None
                ) or step.until
                break
    prerequisite_rungs = list(admission.managed_boolean_rungs)
    if _is_zoom or _is_coast:
        # Edge-gated accumulator drivers (oscillate flag) toggle each scan via a
        # PilotRung instead of holding steady — a steady hold fires the edge
        # only once.  Routed as prerequisites so the terminal let-run animates and
        # records them; captured before the level loop because they are edge tags
        # the plain loop would otherwise leave as one-shot commands.
        # A steady hold that, held every scan, forces a rung writing a register the
        # tree still needs to a contradicting literal defeats the very frontier that
        # proposed it (``Heat_xInit=1`` forces ``fill(1, Heat_CurStep)`` while the
        # tree needs ``Heat_CurStep=3``).  Never install such a hold: skip it and
        # surface the skip.  Static, name-free (write-vs-need); belt-and-suspenders
        # on top of clear-only/writer-selection for levers those don't reroute.
        needed = frontier_pairs(frame.tree, frame.snap)
        pulse_tags = {d.tag for d in trace_action_details if d.pulse}
        seen_prereq: set[str] = set()
        for tag, value in trace_actions:
            if tag in seen_prereq or tag in {r.dest for r in state.rungs}:
                continue
            detail = detail_by_pair.get((tag, value))
            if detail is None or detail.until is None:
                continue
            scope = _until_unresolved_condition(state.work, detail.until)
            if tag in pulse_tags:
                seen_prereq.add(tag)
                if ctx.route_allowed((tag, value)):
                    prerequisite_rungs.extend(_oscillating_rungs(tag, ctx, scope, state.work))
            elif tag not in _act_or_edge and not _values_match(frame.snap.get(tag), value):
                if hold_defeats_needed(tag, value, needed, ctx.pdg, ctx.program):
                    continue
                seen_prereq.add(tag)
                # Action gate for a prerequisite hold: a hold that drives an
                # avoided tag is a path that depends on it — never install it.
                if ctx.route_allowed((tag, value)) and not _avoid_forces(
                    ctx, [(tag, value)], frame.snap
                ):
                    prerequisite_rungs.append(PilotRung(tag, value, scope))
        prereq_tags = {r.dest for r in prerequisite_rungs}
        trace_actions = tuple(p for p in trace_actions if p[0] not in prereq_tags)
        active_trace_actions = tuple(p for p in active_trace_actions if p[0] not in prereq_tags)

    # Learned motion is a fallback reading, not a second policy layered over
    # the backward trace or a charted program edge.  If the live read already
    # exposes a continuation, keep it; only consult learned transitions when
    # the local/static readers are silent.
    inf_candidates: list[_ActionPair] = []
    prescribed_action: _ActionPair | None = None
    prescribed_batch: tuple[_ActionPair, ...] | None = None
    probed_leaf_states: set[tuple[str, Any]] = set()
    # Stage 0 is the sole bearing while an establish gate is pending — silence the
    # compass so it can't prescribe a move on the target register past the closed
    # gate (or wait on a frontier that can't self-advance until the gate settles).
    local_bearing_open = bool(trace_actions or route_candidates or _is_zoom)
    for n in [] if establish_pending or local_bearing_open else _all_nodes(frame.tree):
        # Leaves only — with two "map unreadable here" exceptions where the
        # learned transition table is the only chart available: a live-guard
        # frontier (readable arm traced, so it has children, but the writer
        # guard is a live word), and a pipeline channel whose static value
        # graph produced NO plan (route_plan is None) but for which learned
        # transitions exist (skiff probes or route seeds).
        unreadable = getattr(n, "live_guard", False) or (
            getattr(n, "pipeline_internal", False)
            and route_plan is None
            and ctx.compass.knowledge.has_transitions(
                n.tag, world_key=frame.key, snapshot=frame.snap
            )
        )
        if (
            (n.children and not unreadable)
            or n.satisfied
            or n.is_steerable
            or (getattr(n, "pipeline_internal", False) and not unreadable)
        ):
            continue
        cur_val = frame.snap.get(n.tag)
        if _values_match(cur_val, n.value):
            continue
        leaf_state = (n.tag, cur_val)
        if leaf_state in probed_leaf_states:
            continue
        probed_leaf_states.add(leaf_state)

        def _learned_edge_open(
            source: Any,
            cause: Any,
            destination: Any,
            *,
            tag: str = n.tag,
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
            n.tag,
            cur_val,
            n.value,
            cause_allowed=_learned_edge_open,
            world_key=frame.key,
            snapshot=frame.snap,
        )
        if path:
            first_step = path[0]
            if not is_action(first_step):
                influence_wait = _prescribe_wait(
                    None,
                    frame,
                    state,
                    ctx,
                    reason=f"{n.tag}: {cur_val!r}->{n.value!r}",
                )
                wait = replace(
                    wait,
                    prescribed=influence_wait.prescribed,
                    reason=influence_wait.reason,
                )
                break
            if is_composite_action(first_step):
                # A skiff-learned joint edge: the whole action set must fire in
                # one window.  Propose it as a batch trial, not a single.
                # (The static type of a cause is a single action pair; a
                # composite is a tuple OF pairs, which is what the shape test
                # just established.)
                members = cast("tuple[_ActionPair, ...]", tuple(first_step))
                if all(pair not in key_nogoods and ctx.route_allowed(pair) for pair in members):
                    prescribed_batch = members
                    break
                continue
            if first_step not in key_nogoods and ctx.route_allowed(first_step):
                inf_candidates.append(first_step)
                prescribed_action = first_step
                break

    # Wake is an *ordering* input, never a filter.  An input with an
    # unusually large downstream write cone (a factory-reset call, or a master
    # enable feeding everything) poisons a batch and should be tried *last* — but
    # it must never be dropped, or a legitimately-needed lever with a large wake
    # makes the target silently unreachable.  Here we only split the
    # over-cap actions off the *batch-facing* ``trace_actions`` (widening /
    # convergence co-pulse) so they don't poison a batch trial; they are added
    # back as individual candidates below, after the ordinary trace actions.
    wake_cap = 20
    over_wake_actions: tuple[_ActionPair, ...] = ()
    if len(trace_actions) > 1:
        radii = {t: len(ctx.pdg.downstream_slice(t, follow_calls=True)) for t, _v in trace_actions}
        median_r = sorted(radii.values())[len(radii) // 2] if radii else 0
        wake_cap = max(median_r * 3, 20)
        over_wake_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) > wake_cap)
        trace_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) <= wake_cap)

    candidates: list[_Candidate] = []
    seen_cand: set[_ActionPair] = set()
    route_candidate_set = set(route_candidates)

    def _candidate_for(pair: _ActionPair) -> _Candidate:
        detail = detail_by_pair.get(pair)
        prescribed_edge = (
            route_plan.first_edge
            if route_plan is not None and pair in route_candidate_set
            else None
        )
        return _Candidate(
            tag=pair[0],
            value=pair[1],
            source=(
                ActSource.ROUTE
                if pair in route_candidate_set
                else ActSource.INFLUENCE
                if prescribed_action is not None and pair == prescribed_action
                else ActSource.PROGRAM
                if pair in program_pairs
                else ActSource.TRACE
            ),
            provenance=detail.provenance if detail is not None else (),
            wake=(
                detail.wake
                if detail is not None and detail.wake is not None
                else len(ctx.pdg.downstream_slice(pair[0], follow_calls=True))
            ),
            bearing_channel_tag=(
                detail.operation_boundary[0]
                if detail is not None and detail.operation_boundary is not None
                else prescribed_edge.role.channel_tag
                if prescribed_edge is not None
                else route_plan.role.channel_tag
                if pair in program_pairs and route_plan is not None
                else None
            ),
            bearing_channel_value=(
                detail.operation_boundary[1]
                if detail is not None and detail.operation_boundary is not None
                else prescribed_edge.to_value
                if prescribed_edge is not None
                else route_plan.first_edge.to_value
                if pair in program_pairs and route_plan is not None
                else None
            ),
            program_note=(
                f"exact program producer rung {program_step.producer.rung_index} "
                f"currently needs {pair[0]}={pair[1]!r}"
                if pair in program_pairs and program_step is not None
                else ""
            ),
            program_context_actions=(
                tuple(program_step.context_actions)
                if pair in program_pairs and program_step is not None
                else ()
            ),
        )

    # A chart candidate is the exact edge out of the current process state.
    # File it before deeper backward-trace leaves; this is a source category,
    # not a numeric rank, and is recomputed from the next world after every act.
    for pair in route_candidates:
        if ctx.route_allowed(pair) and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in trace_actions:
        if pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in inf_candidates:
        if ctx.route_allowed(pair) and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    # High-wake trace actions split off the batch above still get a turn as
    # individual candidates. Construction order files them at the tail, so they
    # are tried last rather than excluded outright.
    for pair in over_wake_actions:
        if pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    # Program-owned current: when the target register is an opaque-loop channel
    # whose backward trace dead-ends and whose compass route is the avoided
    # command, the trace surfaces no operator action for a program-owned detour
    # (the mid-recipe ack while HELD).  Recognize it directly — the one operator
    # push the program is dwelling on at the current state — and surface it as a
    # fallback bearing.  Fail-closed: only a *unique* legal, non-avoided push on a
    # recognized channel is returned; ambiguity / no-channel keep today's
    # behavior.  It appends *after* every read source, so a route/influence/trace
    # move keeps priority and this only matters where the loop is otherwise stuck.
    current_action = _current_bearing(frame, ctx)
    if current_action is not None and not candidates:
        pair = current_action.action
        if (
            ctx.route_allowed(pair)
            and pair not in seen_cand
            and pair not in key_nogoods
            and (not _values_match(frame.snap.get(pair[0]), pair[1]) or pair[0] in ctx.edge_tags)
        ):
            seen_cand.add(pair)
            candidates.append(
                replace(
                    _candidate_for(pair),
                    source=ActSource.CURRENT,
                    current_note=current_action.note,
                    bearing_channel_tag=ctx.target.tag,
                    bearing_channel_value=current_action.to_state,
                )
            )
    # Preserve the readers' deterministic order. The backward trace already
    # selected its writer and ordered its leaves; route/current/learned readers
    # are admitted only after that local read is silent. Re-ranking this list
    # would invent another navigation policy after the world was already read.

    # Zoom-wait mint: apply the reason from the early completion re-read (its
    # producers already entered the trace pool above).  An influence-path wait
    # (site 1, in the compass leaf loop) takes precedence when it fired.
    if _is_zoom and not wait.prescribed:
        wait = replace(
            wait,
            prescribed=zoom_wait.prescribed,
            reason=zoom_wait.reason,
        )

    if advance_boundary is not None and not candidates and not wait.prescribed:
        wait = replace(
            wait,
            prescribed=True,
            reason=f"advance {advance_boundary[0]} to its next boundary {advance_boundary[1]!r}",
        )

    # Stuck diagnosis: no candidates from any reading source.  A skiff-learned
    # composite edge surfaces as ``prescribed_batch`` (a bearing, not a plan), and
    # a prescribed wait is an Act-tier bearing, so either means the loop has a move
    # to try -- not stuck.
    stuck_reason: str | None = None
    if (
        not candidates
        and not prerequisite_rungs
        and not wait.prescribed
        and prescribed_batch is None
    ):
        stuck_reason = _diagnose_stuck_reason(frame, ctx)

    return _CandidateList(
        active_trace_actions=active_trace_actions,
        trace_actions=trace_actions,
        trace_action_details=trace_action_details,
        route_candidates=route_candidates,
        candidates=tuple(candidates),
        wake_cap=wake_cap,
        route_plan=route_plan,
        wait_prescribed=wait.prescribed,
        wait_reason=wait.reason,
        advance_boundary=advance_boundary,
        advance_condition=advance_condition,
        prescribed_batch=prescribed_batch,
        prerequisite_rungs=tuple(prerequisite_rungs),
        stuck_reason=stuck_reason,
        route_co_actions=route_co_actions,
        held_command_tags=held_command_tags,
        completion_frontier=completion_frontier,
        program_step=program_step,
    )


# ---------------------------------------------------------------------------
# Pulse-action helpers
# ---------------------------------------------------------------------------


def _candidate_applied(
    candidate: _Candidate,
    candidates: _CandidateList,
    ctx: Any,
) -> tuple[_ActionPair, ...]:
    pair = candidate.pair
    actions: list[_ActionPair] = [pair]
    seen: set[str] = {pair[0]}

    # A route-prescribed command carries its co-actions (the one-shot edge gate);
    # they must fire in the same scan or the command rung never executes.
    if candidate.source is ActSource.ROUTE:
        for co in candidates.route_co_actions:
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

    # A convergence-pipeline command (CtrlCmd-style) co-pulses the remaining
    # trace actions so a level prerequisite and the command land together.
    if candidate.tag in ctx.compass.action_tags and candidates.active_trace_actions:
        # A pair rejected as a standalone act remains valid context for a
        # different atomic act.  Fresh orientation therefore keeps it out of
        # the candidate queue while still allowing the joint pulse to be
        # judged under its own Bearing identity.
        for ta in candidates.active_trace_actions:
            if ta[0] not in seen:
                actions.append(ta)
                seen.add(ta[0])

    # Releasing the other held convergence buttons is part of the same command:
    # without it the decoder fires a stuck button instead.  Recorded in the pulse
    # so replay reproduces a fully-specified, unambiguous command surface.
    if candidate.tag in ctx.compass.action_tags:
        for other in sorted(candidates.held_command_tags):
            if other not in seen:
                actions.append((other, ctx.resting.get(other, False)))
                seen.add(other)

    # Prerequisite holds (trace actions split into rungs for coast/zoom)
    # are applied to the fork but were removed from trace_actions — record them
    # so the scan_log faithfully captures everything the fork sees.
    for rung in candidates.prerequisite_rungs:
        tag, value = rung.dest, rung.value
        if tag not in seen:
            actions.append((tag, value))
            seen.add(tag)

    return tuple(actions)
