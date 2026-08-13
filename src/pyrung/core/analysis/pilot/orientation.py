"""Current-world reading, complete frame assembly, and orientation policy."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.compass import EvidenceScope
from pyrung.core.analysis.pilot.earned_work import earned_work_is_useful_motion
from pyrung.core.analysis.pilot.intrascan_schedule import (
    RequirementSchedule,
    compile_scalar_schedule,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    ChannelHeading,
    Coast,
    ComposeCorrection,
    Dwell,
    ExpectationExemption,
    LocalProgressKind,
    NavigationConstraints,
    NeedProbe,
    NeedResearch,
    ObserveScan,
    OrientationRead,
    OrientationResult,
    OrientationWorld,
    ProbeRequest,
    Pulse,
    PulseHorizon,
    Stuck,
    TargetSpec,
    act_identity,
    pulse_identity,
)
from pyrung.core.analysis.pilot.options import (
    CandidateRead,
    _build_candidates,
    _candidate_applied,
)
from pyrung.core.analysis.pilot.overlay import (
    _pilot_rung_execution_receipt,
    _target_unresolved_condition,
)
from pyrung.core.analysis.pilot.requirement_recovery import (
    actions_preserve_active_requirements,
)
from pyrung.core.analysis.pilot.temporal_need import iter_temporal_need_branches
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
from pyrung.core.analysis.pilot.types import MotionKind, _ActionPair, _IterationFrame
from pyrung.core.analysis.pilot.working_theory import TheoryTemporalIntent
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
    _semantic_key,
    _StateKeyConfig,
    wait_edge_nogood,
)
from pyrung.core.analysis.sp_values import _values_match

_PROBE_BUDGET = 2


def _act_preserves_requirements(world: OrientationWorld, act: Any) -> bool:
    """Admit only acts whose declared atomic inputs preserve live constraints."""

    proof_world_key = (
        _pilot_world_key(
            world.snapshot,
            world.key_config,
            world.state.pilot_rungs,
            (),
        )
        if world.key_config is not None
        else world.world_key
    )
    proof_scope = EvidenceScope.capture(proof_world_key, world.snapshot.items())
    if (proof_scope, act_identity(act)) in getattr(world.state, "proof_rejected_acts", ()):
        return False
    policy = getattr(act, "policy", None)
    if policy is None:
        return True
    return actions_preserve_active_requirements(
        tuple(getattr(world.context, "active_requirements", ())),
        world.snapshot,
        tuple(policy.applied),
    )


def _temporal_transaction_pairs(
    world: OrientationWorld,
    candidates: CandidateRead,
) -> tuple[_ActionPair, ...]:
    """Read exact same-transaction siblings from the current target trace.

    A theory claim names the consumer branch that lost the promised effect.
    Its descendants' current trace actions are the inputs that must accompany
    a retry in that same transaction.  This is the reader-side equivalent of
    the former recovery executor's nested-guard reconstruction, but it neither
    retains nor replays the failed act.
    """

    view = world.context.theory_view
    if view is None:
        return ()
    trace_actions = dict(candidates.trace.active_actions)
    result: list[_ActionPair] = []
    for obligation in view.claim.obligations:
        # The selected handoff receipt already names its consumer-local shape.
        # Prefer those exact steerable siblings directly; branch descendants
        # below are the conservative fallback for older/narrower receipts.
        for tag, value in obligation.required_shape:
            if (
                tag in world.context.steerable
                and tag in trace_actions
                and _values_match(trace_actions[tag], value)
            ):
                pair = (tag, trace_actions[tag])
                if pair not in result:
                    result.append(pair)
        if obligation.consumer is None:
            continue
        consumer_sub, consumer_rung, consumer_branch = obligation.consumer
        for node in world.context.pdg.rung_nodes:
            branch_path = getattr(node, "branch_path", ())
            if (
                getattr(node, "subroutine", object()) != consumer_sub
                or getattr(node, "rung_index", object()) != consumer_rung
                or len(branch_path) <= len(consumer_branch)
                or branch_path[: len(consumer_branch)] != consumer_branch
            ):
                continue
            for tag in node.condition_reads | node.guard_reads:
                if tag in trace_actions and tag in world.context.steerable:
                    pair = (tag, trace_actions[tag])
                    if pair not in result:
                        result.append(pair)
    return tuple(result)


def _merge_temporal_actions(
    *groups: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...] | None:
    """Merge one conjunctive temporal branch, rejecting conflicting values."""

    result: list[_ActionPair] = []
    values: dict[str, Any] = {}
    for group in groups:
        for tag, value in group:
            if tag in values and not _values_match(values[tag], value):
                return None
            if tag not in values:
                values[tag] = value
                result.append((tag, value))
    return tuple(result)


def _iter_temporal_companion_groups(
    world: OrientationWorld,
    candidates: CandidateRead,
    base: ActPolicy,
    structural: tuple[_ActionPair, ...],
) -> Iterator[tuple[_ActionPair, ...]]:
    """Yield the smallest fresh same-transaction widening, then grow lazily.

    Exact consumer descendants, when available, form the first required
    group.  The selected current trace may expose additional siblings which
    the effect walk cannot name (for example an adjacent mode-selection bit).
    Those are already reader-admitted actions in this one route, so widen by
    one deterministic prefix at a time.  No group or cursor survives the
    Compass read.
    """

    base_pairs = tuple(base.applied)
    yielded: list[tuple[_ActionPair, ...]] = []
    initial = _merge_temporal_actions(structural)
    if initial is not None:
        yielded.append(initial)
        yield initial
    siblings = tuple(
        pair
        for pair in candidates.trace.active_actions
        if pair not in base_pairs
        and pair not in structural
        and not _avoid_forces(world.context, (pair,), world.snapshot)
    )
    for width in range(1, len(siblings) + 1):
        group = _merge_temporal_actions(structural, siblings[:width])
        if group is not None and group not in yielded:
            yielded.append(group)
            yield group


def _temporal_base_matches_trigger(
    world: OrientationWorld,
    base: ActPolicy,
    setup: tuple[_ActionPair, ...],
    current_actions: tuple[_ActionPair, ...],
) -> bool:
    """Correlate a fresh base policy with the act that exposed the need.

    The theory retains only the triggering act's canonical identity.  It may
    identify the source producer, but it may never be lowered back into an
    executable act.  Instead, compare that receipt with a policy rebuilt by
    the current readers.  Assignments already present in a later triggering
    batch are supplied by the newly compiled schedule, so remove that
    incidental difference while identifying the producer being refined.
    """

    view = getattr(world.context, "theory_view", None)
    trigger = getattr(view, "trigger_act_identity", None)
    if (
        not isinstance(trigger, tuple)
        or len(trigger) != 2
        or trigger[0] != "pulse"
        or not isinstance(trigger[1], tuple)
    ):
        return False
    trigger_pairs = trigger[1]
    # A later rejected widening may have added current-trace companions which
    # were not requirement assignments.  The retained trigger remains identity
    # only: correlate those pairs only when the fresh reader independently
    # exposes them again.  The companion iterator below is still solely
    # responsible for granting executable authority and composing the next act.
    prior_context = tuple(
        (tag, value)
        for tag, value in (*setup, *current_actions)
        if any(
            trigger_tag == tag and trigger_value == _semantic_key(value)
            for trigger_tag, trigger_value in trigger_pairs
        )
    )
    reconstructed = _merge_temporal_actions(tuple(base.applied), prior_context)
    return reconstructed is not None and pulse_identity(reconstructed) == trigger


def _theory_temporal_retry_bearing(
    world: OrientationWorld,
    candidates: CandidateRead,
    target: TargetSpec,
    ordinary: Bearing | None = None,
) -> Bearing | None:
    """Lazily compose a fresh ordinary pulse with its exact temporal need."""

    requirements = tuple(getattr(world.context, "temporal_requirements", ()))
    if not requirements:
        raise ValueError("retry-together theory has no resolved live requirements")
    prescription = candidates.wait.prescription if candidates.wait is not None else None
    if prescription is None:
        rearm = _theory_rearm_bearing(world, candidates, target)
        if rearm is not None:
            return rearm
    structural_companions = _temporal_transaction_pairs(world, candidates)
    temporal_intent = getattr(world.context.theory_view, "temporal_intent", None)

    # CandidateRead has already applied availability, tide-table, route,
    # Crossings, action-block and prerequisite admission.  Iterate its pulse
    # alternatives afresh; nothing here survives the returned Bearing.
    alternatives: list[tuple[ActPolicy, Any]] = []
    if ordinary is not None:
        ordinary_policy = ordinary.act.policy
        alternatives.append(
            (
                replace(
                    ordinary_policy,
                    motion=MotionKind.INTERVENTION,
                    expectation_exemption=(
                        ExpectationExemption.UNRESOLVED_EFFECT
                        if ordinary_policy.expectation is None
                        else None
                    ),
                    local_progress=None,
                ),
                getattr(ordinary.act, "crossing", None),
            )
        )
    if candidates.learned_batch is not None:
        learned = candidates.learned_batch
        alternatives.append(
            (
                ActPolicy(
                    source=ActSource.LEARNED_BATCH,
                    action_pairs=learned.actions,
                    applied=learned.actions,
                    expectation=learned.expectation,
                    expectation_exemption=(
                        ExpectationExemption.UNRESOLVED_EFFECT
                        if learned.expectation is None
                        else None
                    ),
                ),
                None,
            )
        )
    for crossing in candidates.crossing_batches:
        alternatives.append(
            (
                ActPolicy(
                    source=ActSource.CROSSING,
                    action_pairs=crossing.actions,
                    applied=crossing.actions,
                    note=crossing.reason,
                    expectation=crossing.expectation,
                    expectation_exemption=(
                        ExpectationExemption.UNRESOLVED_EFFECT
                        if crossing.expectation is None
                        else None
                    ),
                ),
                crossing.fidelity,
            )
        )
    alternatives.extend(
        (
            _pulse_policy(
                option,
                _candidate_applied(option, candidates, world.context),
                world,
            ),
            None,
        )
        for option in candidates.options
    )
    if prescription is not None:
        # A fresh ProgramStep/AdvanceProfile read may say that unchanged
        # program-owned work is ready to cross the next scan. Lowering the
        # requirement assignment itself is then the physical intervention;
        # the program continuation is evidence for that one scan, not a stored
        # Coast suffix.
        expectation = prescription.expectation
        alternatives.append(
            (
                ActPolicy(
                    source=ActSource.PROGRAM,
                    heading=prescription.heading,
                    expectation=expectation,
                    expectation_exemption=(
                        ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                    ),
                    landing_receipt_authority=prescription.landing_receipt_authority,
                    note=prescription.reason or "program-owned temporal continuation",
                ),
                None,
            )
        )

    for schedule in _iter_temporal_schedules(world, target, requirements):
        setup = tuple(schedule.assignments)
        matched_live_base = False
        for base, crossing in alternatives:
            if not _temporal_base_matches_trigger(
                world,
                base,
                setup,
                candidates.trace.active_actions,
            ):
                continue
            matched_live_base = True
            for companions in _iter_temporal_companion_groups(
                world,
                candidates,
                base,
                structural_companions,
            ):
                if temporal_intent is TheoryTemporalIntent.RETRY_THROUGH_DEADLINE:
                    additions = tuple(
                        pair
                        for pair in (*setup, *companions)
                        if pair not in tuple(base.applied)
                    )
                    if not additions:
                        continue
                    actions = (additions[0],)
                    policy = ActPolicy(
                        source=ActSource.WIDENING,
                        action_pairs=actions,
                        applied=actions,
                        note="working theory: persist one corrective before fresh steer",
                        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
                        provenance=("working-theory sequential corrective",),
                        local_progress=LocalProgressKind.THEORY_CORRECTIVE,
                        pulse_horizon=PulseHorizon.ASSERTION_SCAN,
                    )
                    act = Pulse(policy)
                    if (
                        _act_preserves_requirements(world, act)
                        and not _avoid_forces(world.context, actions, world.snapshot)
                        and not world.context.compass.knowledge.act_is_nogood(
                            world.world_key,
                            act_identity(act),
                        )
                    ):
                        return _bearing(
                            world,
                            act,
                            candidates,
                            target=target,
                            rationale=("working theory: persist one corrective, then steer again"),
                        )
                    continue
                actions = _merge_temporal_actions(setup, tuple(base.applied), companions)
                if not actions:
                    continue
                policy = replace(
                    base,
                    source=ActSource.WIDENING,
                    action_pairs=actions,
                    applied=actions,
                    note="working theory: retry current bearing with exact temporal need",
                    provenance=(*base.provenance, "working-theory temporal retry"),
                    local_progress=LocalProgressKind.TEMPORAL_EDGE,
                    pulse_horizon=PulseHorizon.ASSERTION_SCAN,
                )
                act = (
                    Pulse(policy, crossing=crossing)
                    if len(actions) == 1
                    else BatchPulse(
                        policy,
                        crossing=crossing,
                    )
                )
                trigger_identity = getattr(world.context.theory_view, "trigger_act_identity", None)
                candidate_identity = act_identity(act)
                trigger_pairs = (
                    frozenset(trigger_identity[1])
                    if isinstance(trigger_identity, tuple)
                    and len(trigger_identity) == 2
                    and trigger_identity[0] == "pulse"
                    and isinstance(trigger_identity[1], tuple)
                    else frozenset()
                )
                candidate_pairs = (
                    frozenset(candidate_identity[1])
                    if isinstance(candidate_identity, tuple)
                    and len(candidate_identity) == 2
                    and candidate_identity[0] == "pulse"
                    and isinstance(candidate_identity[1], tuple)
                    else frozenset()
                )
                if candidate_pairs and candidate_pairs <= trigger_pairs:
                    # Correlation can recover a fresh live base from the prior
                    # batch, but every subset already contained in that trigger
                    # has been tried. Only a newly reader-authorized addition is
                    # a temporal continuation; returning the base is replay.
                    continue
                if (
                    _act_preserves_requirements(world, act)
                    and not _avoid_forces(
                        world.context,
                        actions,
                        world.snapshot,
                    )
                    and not world.context.compass.knowledge.act_is_nogood(
                        world.world_key,
                        act_identity(act),
                    )
                ):
                    return _bearing(
                        world,
                        act,
                        candidates,
                        target=target,
                        prerequisites=tuple(schedule.pilot_rungs),
                        rationale=("working theory: retry fresh bearing with exact same-scan need"),
                    )
        if not matched_live_base and setup:
            # The prior scan may already have advanced the theory tip beyond
            # its producer while a retained look-ahead exposed the next direct
            # requirement. There is then no existing steer to augment at this
            # source: lower the authority-approved assignment as its own setup
            # edge and let the next Compass read continue from that new tip.
            policy = ActPolicy(
                source=ActSource.WIDENING,
                action_pairs=setup,
                applied=setup,
                note="working theory: establish next requirement at productive tip",
                expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
                provenance=("working-theory temporal tip setup",),
                local_progress=LocalProgressKind.TEMPORAL_SETUP,
                pulse_horizon=PulseHorizon.ASSERTION_SCAN,
            )
            act = Pulse(policy) if len(setup) == 1 else BatchPulse(policy)
            if _act_preserves_requirements(
                world, act
            ) and not world.context.compass.knowledge.act_is_nogood(
                world.world_key,
                act_identity(act),
            ):
                return _bearing(
                    world,
                    act,
                    candidates,
                    target=target,
                    prerequisites=tuple(schedule.pilot_rungs),
                    rationale="working theory: establish next requirement from current tip",
                )
    return None


def _theory_rearm_bearing(
    world: OrientationWorld,
    candidates: CandidateRead,
    target: TargetSpec,
) -> Bearing | None:
    """Release spent edge inputs before rereading the temporal retry at its tip."""

    view = world.context.theory_view
    identity = getattr(view, "trigger_act_identity", None)
    if not identity or len(identity) != 2 or identity[0] != "pulse":
        return None
    trigger_actions = tuple(identity[1])
    releases = tuple(
        (tag, world.context.resting.get(tag, False))
        for tag, value in trigger_actions
        if (tag in world.context.edge_tags or tag in world.context.clear_only)
        and _values_match(world.snapshot.get(tag), value)
        and not _values_match(world.snapshot.get(tag), world.context.resting.get(tag, False))
    )
    if not releases:
        return None
    if any(pair in world.context.blocked_actions for pair in releases) or _avoid_forces(
        world.context,
        releases,
        world.snapshot,
    ):
        return None
    policy = ActPolicy(
        source=ActSource.WIDENING,
        action_pairs=releases,
        applied=releases,
        note="working theory: rearm spent edge before temporal retry",
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
        provenance=("working-theory temporal rearm",),
        local_progress=LocalProgressKind.REARM,
        pulse_horizon=PulseHorizon.ASSERTION_SCAN,
    )
    act = Pulse(policy) if len(releases) == 1 else BatchPulse(policy)
    if world.context.compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
        return None
    return _bearing(
        world,
        act,
        candidates,
        target=target,
        rationale="working theory: rearm exact spent edge from provisional tip",
    )


def _iter_temporal_schedules(
    world: OrientationWorld,
    target: TargetSpec,
    requirements: tuple[Any, ...],
) -> Iterator[RequirementSchedule]:
    """Yield compilable complete Boolean branches, one at a time."""

    guard = _target_unresolved_condition(
        world.state.work,
        target.tag,
        target.value,
        target.predicate,
    )
    causal_anchor = getattr(world.context, "temporal_source_anchor", None)
    if causal_anchor is None:
        raise ValueError("temporal read has no exact executable source requirement")
    for branch in iter_temporal_need_branches(requirements):
        lowered = tuple(
            (
                atom,
                replace(
                    atom.requirement,
                    condition=atom.condition,
                    operand_authority=(
                        atom.guard_atom.operand_authority
                        if atom.guard_atom is not None
                        else atom.requirement.operand_authority
                    ),
                ),
            )
            for atom in branch.atoms
        )
        schedule = compile_scalar_schedule(
            tuple(requirement for _atom, requirement in lowered),
            world.state.work,
            guard=guard,
            causal_anchor=causal_anchor,
            allow_deferred_authoritative=True,
        ).schedule
        # A program-owned condition can already hold at the restored source
        # and still be useful causal evidence for RETRY_TOGETHER.  It lowers
        # to no direct assignment; the fresh transaction-sibling reader must
        # contribute the physical addition.  SETUP_FIRST ignores this empty
        # schedule below because it has no standalone act to execute.
        if schedule is not None:
            sources: list[Any] = []
            for atom, requirement in lowered:
                if not any(requirement is item for item in schedule.requirements):
                    continue
                if not any(atom.requirement is source for source in sources):
                    sources.append(atom.requirement)
            yield replace(schedule, requirement_sources=tuple(sources))


def _theory_correction_composition(
    world: OrientationWorld,
    candidates: CandidateRead,
    target: TargetSpec,
) -> ComposeCorrection | None:
    """Choose one persistent correction without executing a program scan."""

    view = getattr(world.context, "theory_view", None)
    if view is None or view.temporal_intent not in {
        TheoryTemporalIntent.RETRY_TOGETHER,
        TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
    }:
        return None
    requirements = tuple(getattr(world.context, "temporal_requirements", ()))
    if not requirements:
        raise ValueError("temporal retry has no resolved live requirements")
    installed = tuple(world.state.pilot_rungs)
    for schedule in _iter_temporal_schedules(world, target, requirements):
        for rung in schedule.pilot_rungs:
            if rung in installed or _avoid_forces(
                world.context,
                ((rung.dest, rung.value),),
                world.snapshot,
            ):
                continue
            sources = tuple(schedule.requirement_sources or schedule.requirements)
            owned = tuple(
                requirement
                for requirement in sources
                if getattr(getattr(requirement, "condition", None), "tag", None)
                == rung.dest
            )
            if not owned:
                continue
            frontier = _frontier(world, candidates)
            orientation_read = OrientationRead(
                world_key=world.world_key,
                world=world,
                candidates=candidates,
                considered_paths=(
                    (candidates.route.plan,) if candidates.route is not None else ()
                ),
                rankings=tuple(candidates.options),
                exclusions=tuple(
                    world.context.compass.knowledge.nogood_identities(world.world_key)
                ),
            )
            return ComposeCorrection(
                world_key=world.world_key,
                frontier=frontier,
                pilot_rung=rung,
                requirements=owned,
                rationale="working theory: compose one correction, then read Compass again",
                orientation=orientation_read,
            )
    return None


def _theory_setup_bearing(
    world: OrientationWorld,
    candidates: CandidateRead,
    target: TargetSpec,
) -> Bearing | None:
    """Nominate one direct scalar setup through the ordinary Bearing seam.

    This is the first bounded reader slice: the drive has already resolved the
    detached requirement identities and restored their exact source.  The pure
    compiler chooses compatible scalar representatives but performs no PLC
    execution. Broader producer/availability reads extend this seam.
    """

    view = getattr(world.context, "theory_view", None)
    if view is None or view.temporal_intent is not TheoryTemporalIntent.SETUP_FIRST:
        return None
    requirements = tuple(getattr(world.context, "temporal_requirements", ()))
    if not requirements:
        raise ValueError("setup-first theory has no resolved live requirements")
    for schedule in _iter_temporal_schedules(world, target, requirements):
        actions = tuple(schedule.assignments)
        if not actions:
            continue
        act_policy = ActPolicy(
            source=ActSource.WIDENING,
            action_pairs=actions,
            applied=actions,
            note="working theory: establish exact temporal setup",
            expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
            local_progress=LocalProgressKind.TEMPORAL_SETUP,
            local_progress_requirements=tuple(schedule.requirements),
            local_progress_sources=tuple(schedule.requirement_sources or schedule.requirements),
            pulse_horizon=PulseHorizon.ASSERTION_SCAN,
        )
        act = Pulse(act_policy) if len(actions) == 1 else BatchPulse(act_policy)
        if _act_preserves_requirements(
            world, act
        ) and not world.context.compass.knowledge.act_is_nogood(
            world.world_key,
            act_identity(act),
        ):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                prerequisites=tuple(schedule.pilot_rungs),
                rationale="working theory: establish exact temporal setup",
            )
    # Configured, program-owned, incompatible, or unsupported conditions are
    # not direct scalar Bearings. Other current-world readers may still return
    # a coast or upstream act; otherwise the ordinary typed result wins.
    return None


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
            and earned_work_is_useful_motion(earned_work.receipt(before, after))
        ):
            reasons.append("earned-work:forward")

    return tuple(dict.fromkeys(reasons))


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
    # ObserveScan exists to acquire the first exact program projection.  Route
    # prerequisites are hypotheses produced by the still-unobserved static
    # reading; installing them would turn observation into an intervention and
    # corrupt the evidence Compass is about to bind.  Every other act executes
    # the current selected route and therefore inherits its prerequisites.
    inherited = () if isinstance(act, ObserveScan) else candidates.prerequisites.pilot_rungs
    merged_prerequisites = tuple(dict.fromkeys((*inherited, *prerequisites)))
    return Bearing(
        world_key=world.world_key,
        act=act,
        objective=BearingObjective(target=target, frontier=_frontier(world, candidates)),
        prerequisites=merged_prerequisites,
        rationale=rationale,
        orientation=orientation_read,
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


def _orient_read(
    compass: Any,
    world: OrientationWorld,
    target: TargetSpec,
    *,
    _allow_theory: bool = True,
    _candidate_read: CandidateRead | None = None,
) -> OrientationResult:
    """Materialize one alternative in act-precedence order.

    A selected wait is considered before learned batches and individual action
    options; widening, diagnosis, and terminal continuation follow. Each exact
    act is checked against the current world's nogoods before it becomes a
    bearing.
    """

    if world.frame is None:
        raise ValueError("single-alternative orientation requires a complete frame")
    candidates = (
        _candidate_read
        if _candidate_read is not None
        else _build_candidates(
            world.frame,
            world.state,
            world.context,
        )
    )

    # Boundary zero has no executed program scan to read yet.  Make that one
    # observation an ordinary Compass-selected bearing; its landing is always
    # reread before any target-relative judgment is made.
    execution_state = getattr(getattr(world.state, "work", None), "state", None)
    if (
        getattr(execution_state, "scan_id", None) == 0
        and getattr(world.state, "bootstrap_execution", None) is None
    ):
        return _bearing(
            world,
            ObserveScan(),
            candidates,
            target=target,
            rationale="observe exactly one entry scan",
        )

    view = getattr(world.context, "theory_view", None)
    if _allow_theory:
        if view is not None and view.temporal_intent in {
            TheoryTemporalIntent.RETRY_TOGETHER,
            TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
        }:
            research = compass.conductivity_research(view)
            if research is not None:
                frontier = _frontier(world, candidates)
                return NeedResearch(
                    world_key=world.world_key,
                    frontier=frontier,
                    request=research,
                    rationale=research.reason,
                    orientation=OrientationRead(
                        world_key=world.world_key,
                        world=world,
                        candidates=candidates,
                        considered_paths=(
                            (candidates.route.plan,) if candidates.route is not None else ()
                        ),
                        rankings=tuple(candidates.options),
                        exclusions=tuple(
                            compass.knowledge.nogood_identities(world.world_key)
                        ),
                    ),
                )
            composition = _theory_correction_composition(world, candidates, target)
            if composition is not None:
                return composition
            ordinary = _orient_read(
                compass,
                world,
                target,
                _allow_theory=False,
                _candidate_read=candidates,
            )
            composed = _theory_temporal_retry_bearing(
                world,
                candidates,
                target,
                ordinary=ordinary if isinstance(ordinary, Bearing) else None,
            )
            if composed is not None:
                return composed
            return _probe_or_stuck(
                compass,
                world,
                candidates,
                "temporal_retry_unresolved",
            )

        setup_first = view is not None and view.temporal_intent is TheoryTemporalIntent.SETUP_FIRST
        prescription = candidates.wait.prescription if candidates.wait is not None else None
        if setup_first and prescription is None:
            rearm = _theory_rearm_bearing(world, candidates, target)
            if rearm is not None:
                return rearm

        theory_setup = _theory_setup_bearing(world, candidates, target)
        if theory_setup is not None:
            return theory_setup
        if setup_first and prescription is None:
            return _probe_or_stuck(
                compass,
                world,
                candidates,
                "temporal_setup_unresolved",
            )

    # A structural awaited-action reading is the program telling us what input
    # it needs in this exact world. It outranks an inferred coast prediction:
    # the coast may be stale by the time its producer is read, while the
    # handshake is executable now and will be verified like every other Pulse.
    for option in candidates.options:
        if option.source is not ActSource.AWAITED_ACTION:
            continue
        applied = _candidate_applied(option, candidates, world.context)
        act = Pulse(_pulse_policy(option, applied, world))
        if _act_preserves_requirements(world, act) and not compass.knowledge.act_is_nogood(
            world.world_key, act_identity(act)
        ):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                rationale=option.awaited_action_note or "program-awaited action",
            )

    prescription = candidates.wait.prescription if candidates.wait is not None else None
    if prescription is not None:
        heading = prescription.heading
        route = heading.route if heading is not None else None
        wait_channel = (
            route.channel_tag
            if route is not None
            else (heading.channel_tag if heading is not None else None)
        )
        wait_nogood = (
            wait_edge_nogood(
                wait_channel,
                route.from_value if route is not None else world.snapshot.get(wait_channel),
                route.target_value
                if route is not None
                else heading.target_value
                if heading is not None
                else None,
            )
            if wait_channel is not None
            else None
        )
        expectation = prescription.expectation
        act = Coast(
            "bearing",
            ActPolicy(
                source=ActSource.ROUTE if route is not None else ActSource.PROGRAM,
                nogood_pair=wait_nogood,
                heading=heading,
                motion=MotionKind.COAST_TO_BEARING,
                expectation=expectation,
                expectation_exemption=(
                    ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                ),
                landing_receipt_authority=prescription.landing_receipt_authority,
            ),
        )
        if _act_preserves_requirements(world, act) and not compass.knowledge.act_is_nogood(
            world.world_key, act_identity(act)
        ):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                rationale=prescription.reason or "charted completion edge",
            )

    if candidates.learned_batch is not None:
        actions = candidates.learned_batch.actions
        expectation = candidates.learned_batch.expectation
        act = BatchPulse(
            ActPolicy(
                source=ActSource.LEARNED_BATCH,
                action_pairs=actions,
                applied=actions,
                expectation=expectation,
                expectation_exemption=(
                    ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                ),
            )
        )
        if _act_preserves_requirements(world, act) and not compass.knowledge.act_is_nogood(
            world.world_key, act_identity(act)
        ):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                rationale="learned joint transition",
            )

    for branch in candidates.crossing_batches:
        expectation = branch.expectation
        policy = ActPolicy(
            source=ActSource.CROSSING,
            action_pairs=branch.actions,
            applied=branch.actions,
            note=branch.reason,
            expectation=expectation,
            expectation_exemption=(
                ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
            ),
        )
        fidelity = branch.fidelity
        act = (
            Pulse(policy, crossing=fidelity)
            if len(branch.actions) == 1
            else BatchPulse(policy, crossing=fidelity)
        )
        if _act_preserves_requirements(world, act) and not compass.knowledge.act_is_nogood(
            world.world_key, act_identity(act)
        ):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                rationale=(
                    branch.reason or "verify crossing proposal"
                    if branch.proposed
                    else "follow grouped reverse crossing"
                ),
            )

    for option in candidates.options:
        applied = _candidate_applied(option, candidates, world.context)
        act = Pulse(_pulse_policy(option, applied, world))
        if not _act_preserves_requirements(world, act) or compass.knowledge.act_is_nogood(
            world.world_key, act_identity(act)
        ):
            continue
        return _bearing(
            world,
            act,
            candidates,
            target=target,
            rationale=(
                option.awaited_action_note
                or getattr(option, "program_note", None)
                or ("static route edge" if option.source is ActSource.ROUTE else "")
                or ("learned transition" if option.source is ActSource.LEARNED_ACTION else "")
                or "ranked trace action"
            ),
        )

    # Widening remains an atomic act, but no sequence of widths survives an
    # observation.  Each rejected width is world-keyed knowledge and the next
    # call recomputes before considering another width.
    active = candidates.trace.active_actions
    for width in range(2, len(active) + 1):
        actions = active[:width]
        expectation = next(
            (
                promised
                for artifact, promised in candidates.widening_expectations
                if artifact == actions
            ),
            None,
        )
        act = BatchPulse(
            ActPolicy(
                source=ActSource.WIDENING,
                action_pairs=actions,
                applied=actions,
                expectation=expectation,
                expectation_exemption=(
                    ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                ),
            )
        )
        if _act_preserves_requirements(world, act) and not compass.knowledge.act_is_nogood(
            world.world_key, act_identity(act)
        ):
            return _bearing(
                world,
                act,
                candidates,
                target=target,
                rationale=f"widen trace context to {width} atomic actions",
            )

    if candidates.diagnosis is not None:
        return _probe_or_stuck(
            compass,
            world,
            candidates,
            candidates.diagnosis.reason,
        )

    terminal: Coast | Dwell
    if compass.knowledge.coast_receipt(world.world_key) is None:
        terminal = Coast(
            "terminal",
            ActPolicy(
                source=ActSource.TERMINAL,
                motion=MotionKind.COAST_HOLDING_WORLD,
                expectation_exemption=ExpectationExemption.AMBIENT_TERMINAL,
            ),
        )
        rationale = "hold the current macro-state and allow program motion"
    else:
        terminal = Dwell(
            ActPolicy(
                source=ActSource.TERMINAL,
                motion=MotionKind.COAST_HOLDING_WORLD,
                expectation_exemption=ExpectationExemption.AMBIENT_TERMINAL,
            )
        )
        rationale = "terminal coast already observed; run one verified dwell"
    if _act_preserves_requirements(world, terminal) and not compass.knowledge.act_is_nogood(
        world.world_key, act_identity(terminal)
    ):
        return _bearing(
            world,
            terminal,
            candidates,
            target=target,
            rationale=rationale,
        )

    return _probe_or_stuck(compass, world, candidates, "all_rejected")


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
    *,
    maintenance_owns: bool = False,
) -> tuple[OrientationResult | None, tuple[OrientationResult, ...]]:
    """Read alternatives once under the caller's work-ownership disposition.

    Alternative order remains the trace reader's deterministic order. There is
    no cross-alternative score and no retained cursor. With
    ``maintenance_owns=True``, an open operation's first bearing wins even when
    it is terminal coast or dwell maintenance. Fresh alternatives instead look
    past maintenance for a concrete bearing and use the first maintenance
    result only as their fallback.
    """

    results: list[OrientationResult] = []
    maintenance: OrientationResult | None = None
    for world in worlds:
        result = _orient_read(compass, world, target)
        results.append(result)
        if isinstance(result, Bearing | ComposeCorrection | NeedResearch):
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
    context_changes = {
        "target": target,
        "blocked_actions": constraints.blocked_actions,
        "avoid_pred": constraints.avoid_predicate,
    }
    # Orientation also serves narrow structural test/navigation contexts.
    # Preserve that protocol while passing requirements through every context
    # which declares the Phase-4 view explicitly.
    if hasattr(world.context, "active_requirements"):
        context_changes["active_requirements"] = constraints.active_requirements
    if hasattr(world.context, "theory_view"):
        context_changes["theory_view"] = constraints.theory_view
    if hasattr(world.context, "temporal_requirements"):
        context_changes["temporal_requirements"] = constraints.temporal_requirements
    if hasattr(world.context, "temporal_source_anchor"):
        context_changes["temporal_source_anchor"] = constraints.temporal_source_anchor
    read_context = replace(world.context, **context_changes)
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
            maintenance_owns=True,
        )
        if selected is not None:
            return selected

    selected, fresh_results = _read_group(
        compass,
        tuple(fresh_worlds),
        target,
    )
    if selected is not None:
        return selected
    return _combined_nonbearing((*open_results, *fresh_results))
