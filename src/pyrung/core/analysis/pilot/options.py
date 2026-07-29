"""Materialize the action and wait options for one orientation.

``_build_candidates`` orchestrates separate reads for static routes and charted
completion, instruction-owned boundaries and prerequisites, learned
transitions, and program-awaited actions. Frozen private receipts keep those
sources distinct until ``_select_wait`` applies their precedence and
``_assemble_candidate_read`` creates the sole durable ``CandidateRead``.

Candidate construction reads the current world and knowledge but does not
execute a trial, apply observations, or commit state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import product
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeAlias, cast

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
from pyrung.core.analysis.pilot.awaited_actions import AwaitedAction
from pyrung.core.analysis.pilot.compass import (
    _evidence_scope_key,
    is_action,
    is_composite_action,
    unique_legal_awaited_action,
)
from pyrung.core.analysis.pilot.constrained_reachability import NavigationEvidence
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    ChannelHeading,
    RouteEdgeContext,
)
from pyrung.core.analysis.pilot.trace import (
    TraceReadConstraints,
    frontier_pairs,
    trace_back,
)
from pyrung.core.analysis.pilot.types import _ActionPair
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.pipeline_graph import StaticPath
    from pyrung.core.analysis.pilot.program_step import ProgramStep
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
    # A program-awaited action (awaited_actions.py): the one operator action the program
    # is dwelling on at the current state of an opaque-loop channel, surfaced when
    # the trace dead-ends and the compass route is the avoided command.  Ordered
    # like a prescribed edge (a recognized bearing), but below route/influence so
    # it is the fallback, never overriding an available route.
    awaited_action_note: str = ""
    # An external input required by the exact program producer selected for an
    # automatic route edge. It is a current-world bearing below an established
    # route/awaited-action evidence and above an unrelated trace action.
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
    def awaited_action_prescribed(self) -> bool:
        return self.source is ActSource.AWAITED_ACTION

    @property
    def program_prescribed(self) -> bool:
        return self.source is ActSource.PROGRAM


def _tree_work_anchors(tree: Any, route: Any) -> tuple[_ActionPair, ...]:
    """Concrete current-trace facts that can identify live work."""

    anchors: list[_ActionPair] = []
    route_condition = getattr(route, "route_condition", None)
    if route_condition is not None:
        anchors.append(route_condition)
        return tuple(anchors)
    for node in tree.iter_nodes():
        if node.relational or node.value is None:
            continue
        pair = (node.tag, node.value)
        if pair not in anchors:
            anchors.append(pair)
    return tuple(anchors)


def _action_allowed(ctx: Any, pair: _ActionPair) -> bool:
    """Whether the current orientation constraints admit this exact action."""

    return pair not in ctx.blocked_actions


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
    if pending is not None and pending.opening.channel_tag in anchor_tags:
        current = frame.snap.get(pending.opening.channel_tag)
        if not _values_match(current, pending.opening.from_value):
            reasons.append(f"pending:{pending.opening.channel_tag}={current!r}")

    committed = tuple(getattr(state, "committed_acts", ()))
    if committed:
        context = committed[-1].context
        before = context.execution.before_snap
        after = context.execution.after_snap
        if getattr(context.policy.motion, "is_coast", False):
            tree_tags = {node.tag for node in frame.tree.iter_nodes()}
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

        earned_work = getattr(state, "earned_work", None)
        components = getattr(earned_work, "components", ()) if earned_work is not None else ()
        if (
            earned_work is not None
            and components
            and any(component.tag in anchor_tags for component in components)
            and earned_work.receipt(before, after).any_forward
        ):
            reasons.append("gauge:advanced")

    return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class WaitPrescription:
    """One valid-by-construction wait bearing."""

    heading: ChannelHeading | None
    reason: str | None = None
    frontier: tuple[_ActionPair, ...] = ()


@dataclass(frozen=True)
class WaitRead:
    """One wait prescription together with every action its read discovered.

    The prescription cannot cross candidate construction on its own.  Its
    completion and exact-producer details travel with it into one ordinary
    admission pass.
    """

    prescription: WaitPrescription | None
    details: tuple[TraceAction, ...] = ()
    declined_reason: str | None = None
    program_step: ProgramStep | None = None
    declined_frontier: tuple[_ActionPair, ...] = ()

    @property
    def reason(self) -> str | None:
        return self.prescription.reason if self.prescription is not None else self.declined_reason

    @property
    def frontier(self) -> tuple[_ActionPair, ...]:
        return (
            self.prescription.frontier if self.prescription is not None else self.declined_frontier
        )

    def without_prescription(self) -> WaitRead:
        """Remove coast authorization without discarding the reading's evidence."""

        if self.prescription is None:
            return self
        return replace(
            self,
            prescription=None,
            declined_reason=self.reason,
            declined_frontier=self.frontier,
        )


@dataclass(frozen=True)
class _TraceAdmission:
    """One application of the candidate pool's ordinary admission rules."""

    active_actions: tuple[_ActionPair, ...]
    actions: tuple[_ActionPair, ...]
    details: tuple[TraceAction, ...]
    detail_by_pair: Mapping[_ActionPair, TraceAction]
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

        step = self.read.program_step
        required_pairs = step.required_pairs if step is not None else frozenset()
        return self.read.prescription is not None and (
            not required_pairs or required_pairs <= self.admitted_pairs
        )

    @property
    def prescription(self) -> WaitPrescription | None:
        return self.candidate_read.prescription

    @property
    def candidate_read(self) -> WaitRead:
        """The evidence-preserving wait result candidate construction may use."""

        if self.viable and not self.admission.establish_pending:
            return self.read
        return self.read.without_prescription()


@dataclass(frozen=True)
class RouteRead:
    """The selected static route and its immediate executable action context."""

    plan: StaticPath
    candidates: tuple[_ActionPair, ...] = ()
    co_actions: tuple[_ActionPair, ...] = ()


@dataclass(frozen=True)
class PrerequisiteRead:
    """Executable prerequisites and convergence state admitted by this read."""

    rungs: tuple[PilotRung, ...] = ()
    held_command_tags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LearnedBatchRead:
    """One learned joint action retained as its own compatibility variant."""

    actions: tuple[_ActionPair, ...]


@dataclass(frozen=True)
class CandidateDiagnosis:
    """Terminal diagnosis owned by candidate construction."""

    reason: str


@dataclass(frozen=True)
class CandidateRead:
    """Owned current-world readings composed for Orientation."""

    trace: _TraceAdmission
    options: tuple[_Candidate, ...]
    wake_cap: int
    route: RouteRead | None = None
    wait: WaitRead | None = None
    prerequisites: PrerequisiteRead = PrerequisiteRead()
    learned_batch: LearnedBatchRead | None = None
    diagnosis: CandidateDiagnosis | None = None


@dataclass(frozen=True)
class _RouteAndCompletionRead:
    """The admitted trace, static route, and charted-completion evidence."""

    trace: _TraceAdmission
    route: RouteRead | None
    charted_completion: WaitRead | None

    @property
    def charted_wait(self) -> WaitRead | None:
        """The charted completion that may participate in wait selection."""

        if self.trace.establish_pending:
            return None
        return self.charted_completion


@dataclass(frozen=True)
class _PrerequisiteSeparation:
    """Trace evidence after executable prerequisites have been separated."""

    trace: _TraceAdmission
    prerequisites: PrerequisiteRead
    instruction_boundary: ChannelHeading | None


@dataclass(frozen=True)
class _LearnedWait:
    """A learned transition whose next step is program-owned motion."""

    read: WaitRead


@dataclass(frozen=True)
class _LearnedAction:
    """A learned transition whose next step is one action."""

    action: _ActionPair


@dataclass(frozen=True)
class _LearnedBatch:
    """A learned transition whose next step is one atomic action batch."""

    read: LearnedBatchRead


_LearnedFallback: TypeAlias = _LearnedWait | _LearnedAction | _LearnedBatch


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
            and _action_allowed(ctx, pair)
            and not _avoid_forces(ctx, (pair,), frame.snap)
            for pair in members
        )
    return wait_edge_nogood(tag, source, destination) not in key_nogoods


def _compass_route_plan(
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair] | None = None,
    unavailable_producer_edges: frozenset[tuple[Any, ...]] = frozenset(),
) -> StaticPath | None:
    if not ctx.compass.graphs:
        return None

    from pyrung.core.analysis.pilot.pipeline_graph import _best_static_path

    nogoods = key_nogoods if key_nogoods is not None else set()
    world_key = getattr(frame, "key", None)
    evidence_scope_key = _evidence_scope_key(world_key, frame.snap.items())

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
            pair_nogoods=nogoods,
            evidence_scope_key=evidence_scope_key,
        )
        return admission.allowed

    plans: list[StaticPath] = []
    for n in frame.tree.iter_nodes():
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
    from pyrung.core.crossing import Cmp, Eq

    if isinstance(boundary, Eq) and len(boundary.values) == 1:
        return ChannelHeading(
            channel_tag=boundary.tag,
            target_value=next(iter(boundary.values)),
            boundary=boundary,
        )
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
    return ChannelHeading(
        channel_tag=boundary.tag,
        target_value=target,
        boundary=boundary,
    )


def _prescribe_wait(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    reason: str | None = None,
) -> WaitRead:
    """Mint a prescribed-wait bearing from one current-world edge read.

    The single owner of "a wait is prescribed" for both mint paths. A route
    completion edge (zoom) must be *grounded* — a wildcard from-value has no
    dwell semantics, so its read has no prescription. An
    influence-path wait passes ``edge=None`` with an explicit ``reason`` —
    always coastable. Automatic sibling edges carry one exact-producer
    ``ProgramStep`` reading in the returned prescription.

    The returned read keeps completion and exact-producer details attached so
    `_build_candidates` can pass them through ordinary trace admission.
    """
    if edge is None:
        return WaitRead(WaitPrescription(None, reason))
    if not _edge_grounded(edge):
        return WaitRead(
            None,
            declined_reason="completion edge has no grounded source value",
        )

    route_reason = (
        f"let-run {edge.role.channel_tag}: {_fmt_from(edge.from_value)}->{edge.to_value!r}"
    )
    route_context = RouteEdgeContext(
        edge.role.channel_tag,
        edge.from_value,
        edge.to_value,
    )

    def _read(
        prescription: WaitPrescription | None,
        *,
        step: Any = None,
        declined_reason: str | None = None,
        declined_frontier: tuple[_ActionPair, ...] = (),
    ) -> WaitRead:
        details = (
            step.inputs_with_lifetime
            if step is not None and prescription is not None
            else step.required_inputs
            if step is not None
            else ()
        )
        return WaitRead(
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
            state.rungs,
            resting=ctx.resting,
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
                WaitPrescription(
                    _route_heading(coast_heading, step.boundary),
                    f"{route_reason}{observation}",
                    frontier=(
                        (
                            boundary_heading.channel_tag,
                            boundary_heading.target_value,
                        ),
                    ),
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
                    WaitPrescription(
                        _route_heading(motion_heading or boundary_heading, boundary),
                        (
                            f"{route_reason}; supply its current input and hand off to "
                            f"{boundary_heading.channel_tag}"
                        ),
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
                WaitPrescription(
                    _route_heading(motion_heading),
                    f"{route_reason}; {step.reason}",
                ),
                step=step,
            )
        return _read(None, step=step, declined_reason=f"{route_reason}; {step.reason}")

    prescription = WaitPrescription(
        _route_heading(),
        route_reason,
    )
    details: tuple[TraceAction, ...] = ()
    frontier: tuple[_ActionPair, ...] = ()
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
        if _action_allowed(ctx, pair)
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
        detail_by_pair=MappingProxyType(detail_by_pair),
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


def _read_route_and_wait(
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _RouteAndCompletionRead:
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
    live_plan = _compass_route_plan(frame, ctx, key_nogoods)
    route_plan = (
        None
        if current_trace_actions
        or banked_trace_work
        or (getattr(state, "pending_departure", None) is not None and admission.active_actions)
        else live_plan
    )
    is_charted_completion = (
        not admission.establish_pending
        and route_plan is not None
        and route_plan.first_edge.action is None
    )
    admitted_completion: _AdmittedWait | None = None
    unavailable_producer_edges: set[tuple[Any, ...]] = set()
    while is_charted_completion:
        assert route_plan is not None
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
    charted_completion: WaitRead | None = None
    if is_charted_completion:
        assert admitted_completion is not None
        charted_completion = admitted_completion.candidate_read
        admission = admitted_completion.admission

    route = (
        RouteRead(route_plan, route_candidates, route_co_actions)
        if route_plan is not None
        else None
    )
    return _RouteAndCompletionRead(admission, route, charted_completion)


def _separate_prerequisites(
    route_and_wait: _RouteAndCompletionRead,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _PrerequisiteSeparation:
    """Separate executable holds without selecting among wait sources."""

    admission = route_and_wait.trace
    is_charted_completion = route_and_wait.charted_wait is not None
    is_coast = any(
        getattr(node, "advance", None) is not None and not node.satisfied
        for node in frame.tree.leaves()
    )
    instruction_boundary: ChannelHeading | None = None
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
                break

    prerequisite_rungs = list(admission.managed_boolean_rungs)
    trace_actions = admission.actions
    active_trace_actions = admission.active_actions
    trace_action_details = admission.details
    if is_charted_completion or is_coast:
        action_or_edge = ctx.compass.action_tags | ctx.edge_tags | ctx.clear_only
        needed = frontier_pairs(frame.tree, frame.snap)
        pulse_tags = {detail.tag for detail in trace_action_details if detail.pulse}
        seen_prereq: set[str] = set()
        for tag, value in trace_actions:
            if tag in seen_prereq or tag in {rung.dest for rung in state.rungs}:
                continue
            detail = admission.detail_by_pair.get((tag, value))
            if detail is None or detail.until is None:
                continue
            scope = _until_unresolved_condition(state.work, detail.until)
            if tag in pulse_tags:
                seen_prereq.add(tag)
                if _action_allowed(ctx, (tag, value)):
                    prerequisite_rungs.extend(_oscillating_rungs(tag, ctx, scope, state.work))
            elif tag not in action_or_edge and not _values_match(frame.snap.get(tag), value):
                if hold_defeats_needed(tag, value, needed, ctx.pdg, ctx.program):
                    continue
                seen_prereq.add(tag)
                if _action_allowed(ctx, (tag, value)) and not _avoid_forces(
                    ctx, [(tag, value)], frame.snap
                ):
                    prerequisite_rungs.append(PilotRung(tag, value, scope))
        prereq_tags = {rung.dest for rung in prerequisite_rungs}
        trace_actions = tuple(pair for pair in trace_actions if pair[0] not in prereq_tags)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair[0] not in prereq_tags
        )

    held_command_tags = frozenset(
        tag
        for tag in ctx.compass.action_tags
        if tag not in {rung.dest for rung in state.rungs}
        and not _values_match(frame.snap.get(tag), ctx.resting.get(tag, False))
    )
    updated_trace = replace(
        admission,
        active_actions=active_trace_actions,
        actions=trace_actions,
        details=trace_action_details,
    )
    return _PrerequisiteSeparation(
        updated_trace,
        PrerequisiteRead(tuple(prerequisite_rungs), held_command_tags),
        instruction_boundary,
    )


def _read_learned_fallback(
    route_and_wait: _RouteAndCompletionRead,
    separated: _PrerequisiteSeparation,
    frame: Any,
    state: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> _LearnedFallback | None:
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
        if not is_action(first_step):
            return _LearnedWait(
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
                return _LearnedBatch(LearnedBatchRead(members))
            continue
        if first_step not in key_nogoods and _action_allowed(ctx, first_step):
            return _LearnedAction(first_step)
    return None


def _read_awaited_action_fallback(frame: Any, ctx: Any) -> AwaitedAction | None:
    """Read the unique program-awaited action without deciding its precedence."""

    return _awaited_action_bearing(frame, ctx)


def _select_wait(
    *,
    charted_completion: WaitRead | None,
    instruction_boundary: ChannelHeading | None,
    learned: _LearnedFallback | None,
    has_candidates: bool,
) -> WaitRead | None:
    """Select one wait from three explicit evidence sources.

    This is the sole chooser among learned motion, charted completion, and an
    instruction-owned boundary, under the established prescription and
    candidate gates.
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

    selected = learned.read if isinstance(learned, _LearnedWait) else None
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
        selected = WaitRead(
            WaitPrescription(
                instruction_boundary,
                reason,
                frontier=(),
            )
        )
    return selected


def _assemble_candidate_read(
    route_and_wait: _RouteAndCompletionRead,
    separated: _PrerequisiteSeparation,
    learned: _LearnedFallback | None,
    awaited_action: AwaitedAction | None,
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> CandidateRead:
    """Compose the final durable candidate read from explicit phase receipts."""

    trace = separated.trace
    route = route_and_wait.route
    route_plan = route.plan if route is not None else None
    route_candidates = route.candidates if route is not None else ()
    trace_actions = trace.actions
    wake_cap = 20
    over_wake_actions: tuple[_ActionPair, ...] = ()
    if len(trace_actions) > 1:
        radii = {
            tag: len(ctx.pdg.downstream_slice(tag, follow_calls=True))
            for tag, _value in trace_actions
        }
        median_radius = sorted(radii.values())[len(radii) // 2] if radii else 0
        wake_cap = max(median_radius * 3, 20)
        over_wake_actions = tuple(
            (tag, value) for tag, value in trace_actions if radii.get(tag, 0) > wake_cap
        )
        trace_actions = tuple(
            (tag, value) for tag, value in trace_actions if radii.get(tag, 0) <= wake_cap
        )

    learned_action = learned.action if isinstance(learned, _LearnedAction) else None
    program_step = (
        route_and_wait.charted_completion.program_step
        if route_and_wait.charted_completion is not None
        else None
    )
    program_pairs = program_step.required_pairs if program_step is not None else frozenset()
    candidates: list[_Candidate] = []
    seen_candidates: set[_ActionPair] = set()
    route_candidate_set = set(route_candidates)

    def _candidate_for(pair: _ActionPair) -> _Candidate:
        detail = trace.detail_by_pair.get(pair)
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
                if pair == learned_action
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

    for pair in route_candidates:
        if _action_allowed(ctx, pair) and pair not in seen_candidates:
            seen_candidates.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in trace_actions:
        if pair not in seen_candidates:
            seen_candidates.add(pair)
            candidates.append(_candidate_for(pair))
    if (
        learned_action is not None
        and _action_allowed(ctx, learned_action)
        and learned_action not in seen_candidates
    ):
        seen_candidates.add(learned_action)
        candidates.append(_candidate_for(learned_action))
    for pair in over_wake_actions:
        if pair not in seen_candidates:
            seen_candidates.add(pair)
            candidates.append(_candidate_for(pair))

    if awaited_action is not None and not candidates:
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
                    bearing_channel_tag=ctx.target.tag,
                    bearing_channel_value=awaited_action.to_state,
                )
            )

    wait = _select_wait(
        charted_completion=route_and_wait.charted_wait,
        instruction_boundary=separated.instruction_boundary,
        learned=learned,
        has_candidates=bool(candidates),
    )
    learned_batch = learned.read if isinstance(learned, _LearnedBatch) else None
    stuck_reason: str | None = None
    if (
        not candidates
        and not separated.prerequisites.rungs
        and (wait is None or wait.prescription is None)
        and learned_batch is None
    ):
        stuck_reason = _diagnose_stuck_reason(frame, ctx)

    final_trace = replace(trace, actions=trace_actions)
    return CandidateRead(
        trace=final_trace,
        options=tuple(candidates),
        wake_cap=wake_cap,
        route=route,
        wait=wait,
        prerequisites=separated.prerequisites,
        learned_batch=learned_batch,
        diagnosis=CandidateDiagnosis(stuck_reason) if stuck_reason is not None else None,
    )


def _build_candidates(
    frame: Any,
    state: Any,
    ctx: Any,
) -> CandidateRead:
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
    awaited_action = _read_awaited_action_fallback(frame, ctx)
    return _assemble_candidate_read(
        route_and_wait,
        separated,
        learned,
        awaited_action,
        frame,
        ctx,
        key_nogoods,
    )


# ---------------------------------------------------------------------------
# Pulse-action helpers
# ---------------------------------------------------------------------------


def _candidate_applied(
    candidate: _Candidate,
    candidates: CandidateRead,
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

    # A convergence-pipeline command (CtrlCmd-style) co-pulses the remaining
    # trace actions so a level prerequisite and the command land together.
    if candidate.tag in ctx.compass.action_tags and candidates.trace.active_actions:
        # A pair rejected as a standalone act remains valid context for a
        # different atomic act.  Fresh orientation therefore keeps it out of
        # the candidate queue while still allowing the joint pulse to be
        # judged under its own Bearing identity.
        for ta in candidates.trace.active_actions:
            if ta[0] not in seen:
                actions.append(ta)
                seen.add(ta[0])

    # Releasing the other held convergence buttons is part of the same command:
    # without it the decoder fires a stuck button instead.  Recorded in the pulse
    # so replay reproduces a fully-specified, unambiguous command surface.
    if candidate.tag in ctx.compass.action_tags:
        for other in sorted(candidates.prerequisites.held_command_tags):
            if other not in seen:
                actions.append((other, ctx.resting.get(other, False)))
                seen.add(other)

    # Prerequisite holds (trace actions split into rungs for coast/zoom)
    # are applied to the fork but were removed from trace_actions — record them
    # so the scan_log faithfully captures everything the fork sees.
    for rung in candidates.prerequisites.rungs:
        tag, value = rung.dest, rung.value
        if tag not in seen:
            actions.append((tag, value))
            seen.add(tag)

    return tuple(actions)
