"""WorkingTheory-specific orientation policy.

This module decides how durable investigation state becomes one next act.
Generic route, world, and bearing construction belongs to orientation_reading.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pyrung.core.analysis.pilot.orientation_reading as _orientation_reading
from pyrung.core.analysis.pilot.avoid import _avoid_forces
from pyrung.core.analysis.pilot.awaited_actions import _button_writes
from pyrung.core.analysis.pilot.candidate_read import CandidateRead
from pyrung.core.analysis.pilot.earned_work import earned_work_is_useful_motion
from pyrung.core.analysis.pilot.execution import (
    MotionKind,
    PulseHorizon,
    ScanEntryConfiguration,
)
from pyrung.core.analysis.pilot.intrascan_schedule import (
    RequirementSchedule,
    compile_scalar_schedule,
    satisfying_values,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Bearing,
    BearingCoast,
    ComposeCorrection,
    ExpectationExemption,
    IntrascanPulse,
    IntrascanTracebackRequest,
    InvestigationSelection,
    LocalProgressKind,
    NavigationConstraints,
    NeedIntrascanBoundaryRealization,
    NeedIntrascanTraceback,
    OrientationRead,
    OrientationWorld,
    ProgramContinuation,
    ProgramScan,
    Pulse,
    TargetSpec,
    _ActionPair,
    _proof_rejection_identity,
    act_identity,
    pulse_identity,
)
from pyrung.core.analysis.pilot.overlay import (
    _target_unresolved_condition,
)
from pyrung.core.analysis.pilot.requirement_admission import (
    actions_preserve_active_requirements,
)
from pyrung.core.analysis.pilot.requirements import OperandAuthority
from pyrung.core.analysis.pilot.temporal_need import iter_temporal_need_branches
from pyrung.core.analysis.pilot.trace_read import TraceChoice
from pyrung.core.analysis.pilot.working_theory import (
    ProgramTransaction,
    TheoryTemporalIntent,
)
from pyrung.core.analysis.pilot.world_key import (
    _physical_world_key,
    _rung_identity,
    _semantic_key,
    _StateKeyConfig,
)
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.context import RungId
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.intrascan_counterfactual import CounterfactualPatch, OccurrenceBoundary


def _act_preserves_requirements(world: OrientationWorld, act: Any) -> bool:
    """Admit only acts whose declared atomic inputs preserve live constraints."""

    if _proof_rejection_identity(
        world.world_key,
        world.snapshot,
        world.key_config,
        world.state.pilot_rungs,
        act,
    ) in getattr(world.state, "proof_rejected_acts", ()):
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
    scope = getattr(view, "investigation_scope", None)
    trigger = getattr(scope, "retry_act_identity", None) or getattr(
        view, "trigger_act_identity", None
    )
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


def _reader_authorized_transaction_pairs(
    world: OrientationWorld,
    base: ActPolicy,
    setup: tuple[_ActionPair, ...],
    current_actions: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    """Return an owned transaction only when fresh readers expose every pair."""

    view = getattr(world.context, "theory_view", None)
    scope = getattr(view, "investigation_scope", None)
    transaction = tuple(getattr(scope, "transaction_act_pairs", ()))
    transaction_identity = getattr(scope, "transaction_act_identity", None)
    if (
        not transaction
        or getattr(scope, "retry_act_identity", None) != transaction_identity
        or pulse_identity(transaction) != transaction_identity
    ):
        return ()
    reader_pairs = (*base.action_pairs, *setup, *current_actions)
    if not all(_pair_matches_any(pair, reader_pairs) for pair in transaction):
        return ()
    return transaction


def _owned_consumer_stop(scope: Any) -> Any:
    """Return one internally consistent receipt-owned consumer stop."""

    if scope is None:
        return None
    stop = getattr(scope, "consumer_stop", None)
    if (
        stop is None
        or not getattr(scope, "transaction_act_pairs", ())
        or getattr(scope, "transaction_attempt_id", None) is None
        or getattr(scope, "consumer_boundary_attempt_id", None) is None
        or getattr(scope, "consumer_boundary", None) is None
    ):
        return None
    return stop


def _reader_authorized_horizon_transaction_pairs(
    world: OrientationWorld,
    base: ActPolicy,
    setup: tuple[_ActionPair, ...],
    current_actions: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    """Rebuild only the transaction named by a valid horizon receipt."""

    scope = getattr(getattr(world.context, "theory_view", None), "investigation_scope", None)
    if _owned_consumer_stop(scope) is None:
        return ()
    transaction = tuple(getattr(scope, "transaction_act_pairs", ()))
    if pulse_identity(transaction) != getattr(scope, "transaction_act_identity", None):
        return ()
    reader_pairs = (*base.action_pairs, *setup, *current_actions)
    if not all(_pair_matches_any(pair, reader_pairs) for pair in transaction):
        return ()
    return transaction


def _owned_consumer_boundary(
    scope: Any,
    actions: tuple[_ActionPair, ...],
) -> Any:
    """Return the boundary only when this pulse continues its owning receipt."""

    stop = _owned_consumer_stop(scope)
    transaction = tuple(getattr(scope, "transaction_act_pairs", ()))
    if stop is None or not all(_pair_matches_any(pair, actions) for pair in transaction):
        return None
    return scope.consumer_boundary


def _theory_temporal_retry_bearing(
    read: OrientationRead,
    target: TargetSpec,
    ordinary: Bearing | None = None,
) -> Bearing | None:
    """Lazily compose a fresh ordinary pulse with its exact temporal need."""

    world = read.world
    candidates = read.candidates
    requirements = tuple(getattr(world.context, "temporal_requirements", ()))
    if not requirements:
        raise ValueError("retry-together theory has no resolved live requirements")
    prescription = candidates.wait.prescription if candidates.wait is not None else None
    temporal_intent = getattr(world.context.theory_view, "temporal_intent", None)
    pending_configuration = _pending_theory_pairs(world)
    configured_overlays = _configured_theory_overlay_pairs(world)
    scope = getattr(world.context.theory_view, "investigation_scope", None)
    configured_corrections = (
        _configured_theory_pairs(world)
        if scope is not None and getattr(scope, "transaction_rearmed", False)
        else pending_configuration
    )
    rearm_retained_transaction = bool(
        temporal_intent is TheoryTemporalIntent.RETRY_THROUGH_DEADLINE
        and pending_configuration
        and scope is not None
        and getattr(scope, "transaction_selected_pairs", ())
        and not getattr(scope, "transaction_rearmed", False)
    )
    if prescription is None or rearm_retained_transaction:
        rearm = _theory_rearm_bearing(read, target)
        if rearm is not None:
            return rearm
    if (
        temporal_intent is TheoryTemporalIntent.SETUP_FIRST
        and ordinary is not None
        and configured_overlays
        and not _pending_overlay_pairs(world)
    ):
        configured_ids = getattr(
            world.context.theory_view,
            "overlay_identities",
            frozenset(),
        )
        correction_requirements = tuple(
            requirement
            for requirement in requirements
            if _uses_ordinary_correction_validation(requirement)
            and getattr(requirement, "corrective_pilot_rungs", ())
            and all(
                _rung_identity(rung) in configured_ids
                for rung in requirement.corrective_pilot_rungs
            )
        )
        if correction_requirements:
            # The PilotRung is already installed and has completed its first
            # observation scan.  Its finite guard may be waiting for the
            # ordinary producer (for example Start -> Starting).  Retry that
            # producer unchanged; do not widen the correction into a second
            # direct assignment merely because its destination is currently
            # false.
            policy = replace(
                ordinary.act.policy,
                source=ActSource.WIDENING,
                note="working theory: activate the exact correction on the fresh steer",
                provenance=(
                    *ordinary.act.policy.provenance,
                    "working-theory exact corrective continuation",
                ),
                local_progress=LocalProgressKind.TEMPORAL_EDGE,
                local_progress_requirements=correction_requirements,
                local_progress_sources=correction_requirements,
            )
            act = replace(ordinary.act, policy=policy)
            if _act_preserves_requirements(world, act):
                return _orientation_reading._bearing(
                    read,
                    act,
                    target=target,
                    rationale="working theory: activate the installed exact correction",
                )
    structural_companions = _temporal_transaction_pairs(world, candidates)

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
            _orientation_reading._pulse_policy(
                option,
                _current_candidate_applied(option, candidates, world),
                world,
            ),
            None,
        )
        for option in candidates.options
        if not _candidate_is_pending_configuration(option, world)
    )
    if prescription is not None:
        # A fresh ProgramStep/AdvanceProfile read may say that unchanged
        # program-owned work is ready to cross the next scan. Lowering the
        # requirement assignment itself is then the physical intervention;
        # the program continuation is evidence for that one scan, not a stored
        # Bearing-coast suffix.
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
        schedule_requirements = tuple(getattr(schedule, "requirements", ()))
        schedule_sources = tuple(
            getattr(schedule, "requirement_sources", ()) or schedule_requirements
        )
        if prescription is not None and pending_configuration:
            # An autonomous program transaction has no pulse pairs to rebuild.
            # Its prior BearingCoast receipt is identity only; executable authority
            # comes from this read's fresh ProgramStep prescription.  Continue
            # only when that newly read BearingCoast is the exact transaction which
            # exposed the temporal need.
            heading = prescription.heading
            route = heading.route if heading is not None else None
            expectation = prescription.expectation
            coast_policy = ActPolicy(
                source=ActSource.ROUTE if route is not None else ActSource.PROGRAM,
                heading=heading,
                motion=MotionKind.COAST_TO_BEARING,
                expectation=expectation,
                expectation_exemption=(
                    ExpectationExemption.UNRESOLVED_EFFECT if expectation is None else None
                ),
                landing_receipt_authority=prescription.landing_receipt_authority,
            )
            coast = BearingCoast(coast_policy)
            trigger_transaction = getattr(
                world.context.theory_view,
                "trigger_program_transaction",
                None,
            )
            if (
                trigger_transaction is not None
                and ProgramTransaction.from_heading(heading, world.snapshot) == trigger_transaction
            ):
                policy = replace(
                    coast_policy,
                    source=ActSource.WIDENING,
                    note="working theory: retry fresh program transaction",
                    provenance=("working-theory configured program continuation",),
                    local_progress=LocalProgressKind.TEMPORAL_EDGE,
                    local_progress_requirements=schedule_requirements,
                    local_progress_sources=schedule_sources,
                    pulse_horizon=PulseHorizon.ASSERTION_SCAN,
                )
                return _orientation_reading._bearing(
                    read,
                    replace(coast, policy=policy),
                    target=target,
                    prerequisites=tuple(schedule.pilot_rungs),
                    rationale="working theory: continue the freshly read program transaction",
                )
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
                # A correction composed in the preceding Compass turn is
                # already persistent configuration.  It authorizes retrying
                # the one current producer, but does not authorize folding a
                # future consumer/sibling steer into that same physical act.
                current_companions = () if configured_corrections else companions
                consumer_boundary = None
                if temporal_intent is TheoryTemporalIntent.RETRY_THROUGH_DEADLINE:
                    retry_authorized = bool(
                        pending_configuration
                        or (scope is not None and getattr(scope, "transaction_rearmed", False))
                    )
                    additions = tuple(
                        pair
                        for pair in (*setup, *current_companions)
                        if pair not in tuple(base.applied)
                        and not _pair_matches_any(pair, configured_corrections)
                    )
                    if pending_configuration:
                        # One retained correction changes the World, then
                        # yields.  The fresh reader must retry the producer
                        # which owns this temporal transaction before another
                        # correction can be installed.
                        if _owned_consumer_stop(scope) is not None:
                            actions = _reader_authorized_horizon_transaction_pairs(
                                world,
                                base,
                                setup,
                                candidates.trace.active_actions,
                            )
                            if not actions:
                                continue
                        else:
                            actions = tuple(base.applied)
                    elif additions:
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
                            return _orientation_reading._bearing(
                                read,
                                act,
                                target=target,
                                rationale=(
                                    "working theory: persist one corrective, then steer again"
                                ),
                            )
                        continue
                    else:
                        actions = tuple(base.applied) if retry_authorized else ()
                    consumer_boundary = _owned_consumer_boundary(scope, actions) or (
                        (
                            getattr(scope, "consumer_boundary", None)
                            or getattr(world.context.theory_view, "trigger_consumer_boundary", None)
                        )
                        if retry_authorized
                        else None
                    )
                else:
                    transaction_retry = _reader_authorized_transaction_pairs(
                        world,
                        base,
                        setup,
                        candidates.trace.active_actions,
                    )
                    merged = _merge_temporal_actions(
                        setup,
                        transaction_retry or tuple(base.applied),
                        current_companions,
                    )
                    actions = tuple(
                        pair
                        for pair in merged or ()
                        if not _pair_matches_any(pair, configured_corrections)
                    )
                    consumer_boundary = _owned_consumer_boundary(scope, actions)
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
                    local_progress_requirements=schedule_requirements,
                    local_progress_sources=schedule_sources,
                    pulse_horizon=(
                        PulseHorizon.CONSUMER_BOUNDARY
                        if consumer_boundary is not None
                        else PulseHorizon.ASSERTION_SCAN
                    ),
                    consumer_boundary=consumer_boundary,
                )
                act = (
                    Pulse(policy, crossing=crossing)
                    if len(actions) == 1
                    else BatchPulse(
                        policy,
                        crossing=crossing,
                    )
                )
                view = world.context.theory_view
                scope = getattr(view, "investigation_scope", None)
                trigger_identity = getattr(scope, "retry_act_identity", None) or getattr(
                    view,
                    "trigger_act_identity",
                    None,
                )
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
                if (
                    temporal_intent is not TheoryTemporalIntent.SETUP_FIRST
                    and candidate_pairs
                    and candidate_pairs <= trigger_pairs
                    and not pending_configuration
                    and not (
                        scope is not None
                        and getattr(scope, "transaction_rearmed", False)
                        and candidate_identity == scope.transaction_act_identity
                    )
                ):
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
                    return _orientation_reading._bearing(
                        read,
                        act,
                        target=target,
                        prerequisites=tuple(schedule.pilot_rungs),
                        rationale=("working theory: retry fresh bearing with exact same-scan need"),
                    )
        if not matched_live_base and setup and configured_corrections:
            continuation_authorized = bool(
                pending_configuration
                or (scope is not None and getattr(scope, "transaction_rearmed", False))
            )
            if ordinary is None or not continuation_authorized:
                return None
            owned_stop = _owned_consumer_stop(scope)
            if owned_stop is not None:
                actions = _reader_authorized_horizon_transaction_pairs(
                    world,
                    ordinary.act.policy,
                    setup,
                    candidates.trace.active_actions,
                )
                if not actions:
                    return None
                consumer_boundary = _owned_consumer_boundary(scope, actions)
                if consumer_boundary is None:
                    return None
                policy = replace(
                    ordinary.act.policy,
                    source=ActSource.WIDENING,
                    action_pairs=actions,
                    applied=actions,
                    note="working theory: retry the receipt-owned transaction",
                    provenance=(
                        *ordinary.act.policy.provenance,
                        "working-theory receipt-owned continuation",
                    ),
                    local_progress=LocalProgressKind.TEMPORAL_EDGE,
                    local_progress_requirements=schedule_requirements,
                    local_progress_sources=schedule_sources,
                    pulse_horizon=PulseHorizon.CONSUMER_BOUNDARY,
                    consumer_boundary=consumer_boundary,
                )
                act = (
                    Pulse(policy, crossing=getattr(ordinary.act, "crossing", None))
                    if len(actions) == 1
                    else BatchPulse(policy, crossing=getattr(ordinary.act, "crossing", None))
                )
                return _orientation_reading._bearing(
                    read,
                    act,
                    target=target,
                    prerequisites=tuple(schedule.pilot_rungs),
                    rationale="working theory: continue the receipt-owned transaction",
                )
            policy = replace(
                ordinary.act.policy,
                source=ActSource.WIDENING,
                note="working theory: continue one fresh act under composed configuration",
                provenance=(
                    *ordinary.act.policy.provenance,
                    "working-theory configured continuation",
                ),
                local_progress=LocalProgressKind.TEMPORAL_EDGE,
                local_progress_requirements=schedule_requirements,
                local_progress_sources=schedule_sources,
                pulse_horizon=PulseHorizon.ASSERTION_SCAN,
            )
            act = replace(ordinary.act, policy=policy)
            return _orientation_reading._bearing(
                read,
                act,
                target=target,
                prerequisites=tuple(schedule.pilot_rungs),
                rationale="working theory: continue after one composed correction",
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
                local_progress_requirements=schedule_requirements,
                local_progress_sources=schedule_sources,
                pulse_horizon=PulseHorizon.ASSERTION_SCAN,
            )
            act = Pulse(policy) if len(setup) == 1 else BatchPulse(policy)
            if _act_preserves_requirements(
                world, act
            ) and not world.context.compass.knowledge.act_is_nogood(
                world.world_key,
                act_identity(act),
            ):
                return _orientation_reading._bearing(
                    read,
                    act,
                    target=target,
                    prerequisites=tuple(schedule.pilot_rungs),
                    rationale="working theory: establish next requirement from current tip",
                )
    return None


def _theory_rearm_bearing(
    read: OrientationRead,
    target: TargetSpec,
) -> Bearing | None:
    """Release spent edge inputs before rereading the temporal retry at its tip."""

    world = read.world
    view = world.context.theory_view
    scope = getattr(view, "investigation_scope", None)
    retained_transaction = bool(
        getattr(view, "temporal_intent", None) is TheoryTemporalIntent.RETRY_THROUGH_DEADLINE
        and _pending_theory_pairs(world)
        and scope is not None
        and getattr(scope, "transaction_act_pairs", ())
        and getattr(scope, "transaction_selected_pairs", ())
        and not getattr(scope, "transaction_rearmed", False)
    )
    identity = (
        scope.transaction_act_identity
        if retained_transaction
        else getattr(scope, "retry_act_identity", None)
        or getattr(view, "trigger_act_identity", None)
    )
    if not identity or len(identity) != 2 or identity[0] != "pulse":
        return None
    trigger_actions = (
        tuple(scope.transaction_act_pairs)
        if retained_transaction and pulse_identity(tuple(scope.transaction_act_pairs)) == identity
        else tuple(identity[1])
    )
    selected_tags = (
        {tag for tag, _value in scope.transaction_selected_pairs} if retained_transaction else set()
    )
    releases = tuple(
        (tag, world.context.resting.get(tag, False))
        for tag, value in trigger_actions
        if (
            tag in selected_tags
            or tag in world.context.edge_tags
            or tag in world.context.clear_only
        )
        and _values_match(world.snapshot.get(tag), value)
        and not _values_match(world.snapshot.get(tag), world.context.resting.get(tag, False))
    )
    if not releases:
        return None
    release_tags = {tag for tag, _value in releases}
    actions = tuple(
        (
            tag,
            world.context.resting.get(tag, False),
        )
        if tag in release_tags
        else (tag, value)
        for tag, value in trigger_actions
    )
    trigger_tags = {tag for tag, _value in trigger_actions}
    trigger_snapshot = {**world.snapshot, **dict(trigger_actions)}
    desired_writes: dict[str, Any] = {}
    if world.context.pdg is not None and world.context.program is not None:
        for tag, value in trigger_actions:
            if _values_match(value, True):
                desired_writes.update(_button_writes(world.context, tag, trigger_snapshot))
    conflicting_releases: list[_ActionPair] = []
    for tag in sorted(world.context.steerable - trigger_tags):
        current = world.snapshot.get(tag)
        resting = world.context.resting.get(tag, False)
        if _values_match(current, resting) or not _values_match(current, True):
            continue
        active_writes = _button_writes(world.context, tag, world.snapshot)
        if any(
            destination in desired_writes
            and not _values_match(active_value, desired_writes[destination])
            for destination, active_value in active_writes.items()
        ):
            conflicting_releases.append((tag, resting))
    actions = (*actions, *conflicting_releases)
    if any(pair in world.context.blocked_actions for pair in actions) or _avoid_forces(
        world.context,
        actions,
        world.snapshot,
    ):
        return None
    policy = ActPolicy(
        source=ActSource.WIDENING,
        action_pairs=actions,
        applied=actions,
        note="working theory: rearm spent edge before temporal retry",
        expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
        provenance=("working-theory temporal rearm",),
        local_progress=LocalProgressKind.REARM,
        pulse_horizon=PulseHorizon.ASSERTION_SCAN,
    )
    act = Pulse(policy) if len(actions) == 1 else BatchPulse(policy)
    if world.context.compass.knowledge.act_is_nogood(world.world_key, act_identity(act)):
        return None
    return _orientation_reading._bearing(
        read,
        act,
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
                    deadline=(
                        atom.guard_atom.deadline
                        if atom.guard_atom is not None
                        else atom.requirement.deadline
                    ),
                    operand_authority=(
                        atom.guard_atom.operand_authority
                        if atom.guard_atom is not None
                        else atom.requirement.operand_authority
                    ),
                ),
            )
            for atom in branch.atoms
        )
        requirement_bindings = _temporal_requirement_bindings(lowered)
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
            yield replace(
                schedule,
                requirement_sources=tuple(sources),
                requirement_bindings=requirement_bindings,
            )


def _temporal_requirement_bindings(
    lowered: tuple[tuple[Any, Any], ...],
) -> tuple[tuple[Any, tuple[Any, ...]], ...]:
    """Group adjustable lowered leaves under their lifecycle parent."""

    selected_by_parent: dict[tuple[Any, ...], list[Any]] = {}
    parents: dict[tuple[Any, ...], Any] = {}
    for atom, requirement in lowered:
        if not requirement.permits_assignment:
            continue
        parent_identity = _semantic_key(atom.requirement.navigation_identity)
        parents[parent_identity] = atom.requirement
        selected_by_parent.setdefault(parent_identity, []).append(requirement)
    return tuple(
        (parents[parent_identity], tuple(selected))
        for parent_identity, selected in selected_by_parent.items()
    )


def _theory_correction_composition(
    read: OrientationRead,
    target: TargetSpec,
    *,
    research_finding_identity: tuple[Any, ...] | None = None,
) -> ComposeCorrection | NeedIntrascanTraceback | None:
    """Choose one persistent correction without executing a program scan."""

    world = read.world
    view = getattr(world.context, "theory_view", None)
    if view is None or view.temporal_intent not in {
        TheoryTemporalIntent.SETUP_FIRST,
        TheoryTemporalIntent.RETRY_TOGETHER,
        TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
    }:
        return None
    requirements = tuple(getattr(world.context, "temporal_requirements", ()))
    if not requirements:
        raise ValueError("temporal retry has no resolved live requirements")
    exact_requirements = tuple(
        requirement
        for requirement in requirements
        if getattr(requirement, "corrective_pilot_rungs", ())
    )
    if exact_requirements:
        exact_rungs_by_identity = {
            _rung_identity(rung): rung
            for requirement in exact_requirements
            for rung in requirement.corrective_pilot_rungs
        }
        exact_rungs = tuple(exact_rungs_by_identity.values())
        installed_rungs = frozenset(getattr(view, "overlay_identities", ()))
        pending_rungs = tuple(
            rung for rung in exact_rungs if _rung_identity(rung) not in installed_rungs
        )
        if pending_rungs and not _avoid_forces(
            world.context,
            tuple((rung.dest, rung.value) for rung in pending_rungs),
            world.snapshot,
        ):
            return ComposeCorrection(
                world_key=world.world_key,
                frontier=_orientation_reading._frontier(read),
                requirements=exact_requirements,
                rationale=(
                    "working theory: compose the exact corrective rung, then read Compass again"
                ),
                pilot_rungs=pending_rungs,
                research_finding_identity=research_finding_identity,
                orientation=read,
            )
        # Exact regression requirements already retain their executable form.
        # Once that form is composed, the next question is whether its pending
        # overlay executes—not whether the same value can also be patched as a
        # scan-entry configuration.
        return None
    installed = frozenset(
        configuration.identity for configuration in getattr(view, "configurations", ())
    )
    for schedule in _iter_temporal_schedules(world, target, requirements):
        for rung in schedule.pilot_rungs:
            configuration = ScanEntryConfiguration(((rung.dest, rung.value),))
            if configuration.identity in installed or _avoid_forces(
                world.context,
                ((rung.dest, rung.value),),
                world.snapshot,
            ):
                continue
            sources = tuple(schedule.requirement_sources or schedule.requirements)
            owned = tuple(
                requirement
                for requirement in sources
                if getattr(getattr(requirement, "condition", None), "tag", None) == rung.dest
            )
            frontier = _orientation_reading._frontier(read)
            if not owned:
                conducted = tuple(
                    (parent, _theory_conducted_occurrence(world.context.compass, view, parent))
                    for parent, selected in schedule.requirement_bindings
                    if any(
                        getattr(getattr(requirement, "condition", None), "tag", None) == rung.dest
                        for requirement in selected
                    )
                )
                exact = tuple(
                    (parent, occurrence)
                    for parent, occurrence in conducted
                    if occurrence is not None and _counterfactual_boundary(occurrence) is not None
                )
                if not exact:
                    continue
                parent, occurrence = exact[0]
                boundary = _counterfactual_boundary(
                    occurrence,
                    getattr(parent, "selected_writer", None),
                )
                assert boundary is not None
                prevented_write = _requirement_obstruction(parent)
                tag = getattr(world.state.work, "_known_tags_by_name", {}).get(rung.dest)
                patch = CounterfactualPatch(
                    dest=rung.dest,
                    value=rung.value,
                    guard=(
                        tag != rung.value
                        if prevented_write is not None and tag is not None
                        else rung.guard
                    ),
                    boundary=boundary,
                )
                request = IntrascanTracebackRequest(
                    patch=patch,
                    requirements=(parent,),
                    consumer_assignments=((rung.dest, rung.value),),
                    research_finding_identity=research_finding_identity,
                    prevented_write=prevented_write,
                )
                if view.has_traceback_result(request.identity):
                    continue
                return NeedIntrascanTraceback(
                    world_key=world.world_key,
                    frontier=frontier,
                    request=request,
                    rationale=(
                        "working theory: test the conducted consumer handoff, then "
                        "trace its preconditions backward"
                    ),
                    orientation=read,
                )
            return ComposeCorrection(
                world_key=world.world_key,
                frontier=frontier,
                configuration=configuration,
                requirements=owned,
                rationale="working theory: compose one correction, then read Compass again",
                research_finding_identity=research_finding_identity,
                orientation=read,
            )
    return None


def _theory_intrascan_bearing(
    read: OrientationRead,
    target: TargetSpec,
) -> Bearing | None:
    """Select only the next ordinary scan proved by a traceback finding.

    The retained finding is evidence, not a stored two-scan plan.  Compass
    rereads the current World, proves that the producer's entry requirements
    still hold, and authorizes one exact program scan.  The later consumer
    assignment remains unselected until the landing World is read afresh.
    """

    world = read.world
    view = getattr(world.context, "theory_view", None)
    if view is None:
        return None
    findings = tuple(getattr(view, "traceback_findings", ()))
    if not findings:
        return None
    physical_key = _physical_world_key(tuple(world.world_key))
    if physical_key != tuple(view.source.world_key):
        return None
    for finding in reversed(findings):
        if (
            finding.theory_id != view.theory_id
            or finding.version_id != view.version_id
            or finding.source != view.source
        ):
            continue
        realization = getattr(finding, "realization", None)
        if realization is None:
            # Completed research may prove that this remains ordinary user
            # configuration. Its receipt suppresses repeated hypothetical
            # work but grants no intrascan act; the temporal reader below
            # chooses the fresh steer.
            continue
        step = finding.witness.traceback_step
        if realization.direct:
            assignments = tuple(realization.consumer_assignments)
            if (
                realization.consumer_write is None
                or not assignments
                or any(tag not in world.context.steerable for tag, _value in assignments)
            ):
                continue
            policy = ActPolicy(
                source=ActSource.WIDENING,
                action_pairs=assignments,
                applied=assignments,
                note="working theory: execute one exact intrascan consumer steer",
                expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
                provenance=("working-theory intrascan direct realization",),
                local_progress=LocalProgressKind.INTRASCAN_DIRECT,
                pulse_horizon=PulseHorizon.ASSERTION_SCAN,
            )
            act = IntrascanPulse(
                policy=policy,
                expected_write=realization.consumer_write,
                evidence_identity=finding.identity,
            )
            if (
                _act_preserves_requirements(world, act)
                and not _avoid_forces(world.context, assignments, world.snapshot)
                and not world.context.compass.knowledge.act_is_nogood(
                    world.world_key,
                    act_identity(act),
                )
            ):
                return _orientation_reading._bearing(
                    read,
                    act,
                    target=target,
                    rationale="working theory: steer one exact intrascan consumer scan",
                )
            continue
        stage_write = realization.stage_write
        if (
            not realization.witnessed
            or step is None
            or stage_write is None
            or realization.stage_scan != view.source.scan_id + 1
            or stage_write.counterfactual
        ):
            continue
        requirements = tuple(getattr(realization, "stage_requirements", ()))
        if not requirements:
            producers = tuple(
                producer for producer in step.producer_traces if producer.write == stage_write
            )
            if len(producers) != 1:
                continue
            requirements = producers[0].enabling_requirements
        if not requirements or any(
            requirement.source_kind != "entry"
            or not _values_match(world.snapshot.get(requirement.tag), requirement.value)
            for requirement in requirements
        ):
            continue
        return _orientation_reading._bearing(
            read,
            ProgramScan(
                expected_write=stage_write,
                evidence_identity=finding.identity,
            ),
            target=target,
            rationale="working theory: execute one exact intrascan staging scan",
        )
    return None


def _theory_intrascan_frontier_bearing(
    world: OrientationWorld,
    target: TargetSpec,
    *,
    orient_read: Any,
) -> Bearing | None:
    """Trace one open occurrence goal through the ordinary current-world reader.

    An open frontier grants no act. It only locks ordinary Trace to the exact
    static writer which can supply the missing value. Compass still derives
    one current action, and the downstream consumer steer stays for a later
    fresh World.
    """

    view = getattr(world.context, "theory_view", None)
    if view is None:
        return None
    current_frontiers = getattr(view, "current_traceback_frontiers", None)
    frontiers = (
        tuple(current_frontiers())
        if current_frontiers is not None
        else tuple(getattr(view, "traceback_frontiers", ()))
    )
    if not frontiers:
        return None
    physical_key = _physical_world_key(tuple(world.world_key))
    if physical_key != tuple(view.source.world_key):
        return None

    ctx = world.context
    constraints = NavigationConstraints(
        blocked_actions=frozenset(getattr(ctx, "blocked_actions", ())),
        avoid_predicate=getattr(ctx, "avoid_pred", None),
        active_requirements=tuple(getattr(ctx, "active_requirements", ())),
    )
    exclusions = frozenset(ctx.compass.knowledge.nogood_identities(world.world_key))
    rejected_actions = _orientation_reading._exact_rejected_actions(exclusions)
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state
    from pyrung.core.analysis.sp_values import writer_value_facts

    for frontier in reversed(frontiers):
        if frontier.theory_id != view.theory_id or frontier.source != view.source:
            continue
        consumer_assignments = frozenset(frontier.consumer_assignments)
        stage_rejected_actions = frozenset((*rejected_actions, *consumer_assignments))
        for goal in frontier.producer_goals:
            if (
                goal.node_index < 0
                or goal.node_index >= len(ctx.pdg.rung_nodes)
                or goal.node_index not in ctx.pdg.writers_of.get(goal.tag, frozenset())
            ):
                continue
            node = ctx.pdg.rung_nodes[goal.node_index]
            if RungId(node.subroutine, node.rung_index) != goal.rung_id or tuple(
                node.branch_path
            ) != tuple(goal.branch_path):
                continue
            occurrence_snapshot = {**world.snapshot, **dict(goal.observed_values)}
            useful = frontier.witness.traceback_step.useful_write
            excluded_writers = frozenset(
                index
                for index, candidate_node in enumerate(ctx.pdg.rung_nodes)
                if RungId(candidate_node.subroutine, candidate_node.rung_index)
                == useful.boundary.rung_id
                and tuple(candidate_node.branch_path) == tuple(useful.boundary.branch_path or ())
            )

            def orient_guard_target(
                concrete_target: TargetSpec,
                route: TraceChoice | None,
                *,
                rejected: frozenset[tuple[str, Any]] = stage_rejected_actions,
                consumer: frozenset[tuple[str, Any]] = consumer_assignments,
                frontier_id: tuple[Any, ...] = frontier.identity,
                goal_id: tuple[Any, ...] = goal.identity,
                goal_evidence: Any = goal,
            ) -> Bearing | None:
                """Let ordinary Compass orient one exact frontier prerequisite."""

                tree = _orientation_reading._trace_for_route(
                    world,
                    concrete_target,
                    constraints,
                    route,
                    rejected,
                )
                key_config = world.key_config
                if key_config is None:
                    tree_tags = {
                        concrete_target.tag,
                        *tree.pivot_tags(),
                        *(
                            leaf.tag
                            for leaf in tree.leaves()
                            if not leaf.is_steerable
                            and not getattr(leaf, "pipeline_internal", False)
                        ),
                    }
                    key_config = _StateKeyConfig(
                        stateful_names=tuple(sorted(tree_tags)),
                        done_specs=(),
                        threshold_vector_specs=(),
                        acc_indices=frozenset(),
                    )
                producer_world = _orientation_reading._assemble_world(
                    replace(world, frame=None, root_route=None),
                    route,
                    tree,
                    key_config,
                )
                ordinary = orient_read(
                    ctx.compass,
                    producer_world,
                    concrete_target,
                    _allow_theory=False,
                )
                if not isinstance(ordinary, Bearing) or ordinary.orientation is None:
                    return None
                applied = frozenset(getattr(ordinary.act.policy, "applied", ()))
                if applied & consumer:
                    return None
                return replace(
                    ordinary,
                    objective=replace(ordinary.objective, target=target),
                    rationale=(
                        "working theory: follow one ordinary bearing toward the "
                        "missing intrascan producer guard"
                    ),
                    investigation_selection=InvestigationSelection(
                        frontier_id=frontier_id,
                        producer_goal_id=goal_id,
                        producer_goal=goal_evidence,
                    ),
                )

            for alternative in goal.guard_alternatives:
                missing = tuple(
                    atom
                    for atom in alternative
                    if _eval_expr_from_state(atom, occurrence_snapshot) is False
                )
                for atom in missing:
                    if atom.tag in ctx.steerable and not atom.operand_is_tag:
                        direct_values: tuple[Any, ...] = ()
                        tag = getattr(world.state.work, "_known_tags_by_name", {}).get(atom.tag)
                        if atom.form in {"xic", "rise", "truthy"}:
                            direct_values = (True,)
                        elif atom.form in {"xio", "fall"}:
                            direct_values = (False,)
                        elif atom.form == "eq":
                            direct_values = (atom.operand,)
                        elif tag is not None and atom.form in {
                            "ne",
                            "lt",
                            "le",
                            "gt",
                            "ge",
                        }:
                            direct_values = satisfying_values(
                                tag,
                                (
                                    Cmp(
                                        atom.tag,
                                        {
                                            "ne": "!=",
                                            "lt": "<",
                                            "le": "<=",
                                            "gt": ">",
                                            "ge": ">=",
                                        }[atom.form],
                                        atom.operand,
                                    ),
                                ),
                                dict(world.snapshot),
                            )
                        for value in direct_values:
                            direct = orient_guard_target(
                                TargetSpec(atom.tag, value),
                                None,
                            )
                            if direct is not None:
                                return direct

                    facts = tuple(
                        sorted(
                            (
                                fact
                                for fact in writer_value_facts(ctx.program, ctx.pdg).get(
                                    atom.tag, ()
                                )
                                if fact.node_index not in excluded_writers
                                and _eval_expr_from_state(
                                    atom,
                                    {
                                        **occurrence_snapshot,
                                        atom.tag: fact.written_value,
                                    },
                                )
                                is True
                            ),
                            key=lambda fact: fact.node_index,
                        )
                    )
                    if not facts or len(facts) > 8:
                        continue
                    for fact in facts:
                        concrete_target = TargetSpec(atom.tag, fact.written_value)
                        route = TraceChoice(
                            id=f"intrascan-guard-writer-{fact.node_index}",
                            label=(
                                f"exact writer for intrascan guard "
                                f"{atom.tag} {atom.form} {atom.operand!r}"
                            ),
                            route=("working-theory intrascan guard writer",),
                            writer_locks=((atom.tag, fact.written_value, fact.node_index),),
                        )
                        ordinary = orient_guard_target(concrete_target, route)
                        if ordinary is not None:
                            return ordinary
    return None


def _theory_intrascan_boundary_realization(
    read: OrientationRead,
) -> NeedIntrascanBoundaryRealization | None:
    """Request one fresh proof after an owned producer advanced the World."""

    world = read.world
    view = getattr(world.context, "theory_view", None)
    reader = getattr(view, "realized_traceback_frontier", None)
    resolved = reader() if reader is not None else None
    if resolved is None:
        return None
    frontier, goal, attempt = resolved
    source = getattr(view, "source", None)
    if source is None:
        return None
    physical_key = _physical_world_key(tuple(world.world_key))
    if physical_key != tuple(source.world_key) or not _values_match(
        world.snapshot.get(goal.tag), goal.value
    ):
        return None
    return NeedIntrascanBoundaryRealization(
        world_key=world.world_key,
        frontier=_orientation_reading._frontier(read),
        traceback_frontier=frontier,
        producer_goal=goal,
        producer_attempt_id=attempt.attempt_id,
        rationale=(
            "working theory: the exact producer stage advanced; reprove its "
            "retained consumer at this fresh World"
        ),
        orientation=read,
    )


def _theory_conducted_occurrence(
    compass: Any,
    view: Any,
    requirement: Any,
) -> Any | None:
    """Return the exact parent consumer occurrence crossed by conductivity."""

    front = compass.conductivity_front(view)
    if front is None:
        return None
    demanding = requirement.demanding_occurrence
    identity = (demanding.kind, demanding.tag, demanding.dynamic_address)
    for flow in front.flows:
        if flow.displacement is None:
            continue
        exact = next(
            (
                read
                for read in flow.consumer_reads
                if (read.kind, read.tag, read.dynamic_address) == identity
            ),
            None,
        )
        if exact is not None:
            return demanding
        if (
            flow.appeared is not None
            and any(
                obligation.tag == demanding.tag and obligation.value in demanding.values
                for obligation in flow.obligations
            )
            and (flow.appeared.scan_id, flow.appeared.ordinal)
            < (demanding.scan_id, demanding.ordinal)
            < (flow.displacement.scan_id, flow.displacement.ordinal)
        ):
            return demanding
    return None


def _theory_requirement_was_conducted(
    compass: Any,
    view: Any,
    requirement: Any,
) -> bool:
    """Compatibility predicate over the exact conducted occurrence reader."""

    return _theory_conducted_occurrence(compass, view, requirement) is not None


def _counterfactual_boundary(
    occurrence: Any,
    static_address: tuple[Any, ...] | None = None,
) -> OccurrenceBoundary | None:
    """Relocate a read to the executor boundary before its condition snapshot."""

    if occurrence.kind != "read":
        return None
    subroutine, rung_index = occurrence.rung
    return OccurrenceBoundary(
        rung_id=RungId(subroutine, rung_index),
        execution_kind=occurrence.execution_kind,
        caller_rung=occurrence.caller_rung,
        call_stack=tuple(occurrence.call_stack),
        depth=occurrence.depth,
        call_invocation=occurrence.call_invocation,
        run_order=getattr(occurrence, "run_order", None),
        branch_path=(
            tuple(occurrence.branch_path)
            if getattr(occurrence, "branch_path", None) is not None
            else tuple(static_address[2])
            if static_address is not None and len(static_address) >= 3
            else None
        ),
    )


def _requirement_obstruction(requirement: Any) -> Any | None:
    """Return the exact harmful write carried by one requirement receipt."""

    direct = getattr(requirement, "obstruction_occurrence", None)
    if direct is not None:
        return direct
    matches = tuple(
        item[1]
        for item in getattr(requirement, "scope", ())
        if isinstance(item, tuple) and len(item) == 2 and item[0] == "overwriter_guard"
    )
    return matches[0] if len(matches) == 1 else None


def _uses_ordinary_correction_validation(requirement: Any) -> bool:
    """Whether a tentative exact rung delegated proof to fresh execution."""

    proof = next(
        (
            item
            for item in getattr(requirement, "scope", ())
            if isinstance(item, tuple) and len(item) >= 3 and item[0] == "tentative-execution"
        ),
        None,
    )
    return bool(
        getattr(requirement, "provenance", "") == "exact-regression-corrective"
        and proof is not None
        and proof[2] == "ordinary Working Theory execution owns validation"
    )


def _theory_setup_bearing(
    read: OrientationRead,
    target: TargetSpec,
) -> Bearing | None:
    """Nominate one direct scalar setup through the ordinary Bearing seam.

    This is the first bounded reader slice: the drive has already resolved the
    detached requirement identities and restored their exact source.  The pure
    compiler chooses compatible scalar representatives but performs no PLC
    execution. Broader producer/availability reads extend this seam.
    """

    world = read.world
    view = getattr(world.context, "theory_view", None)
    if view is None or view.temporal_intent is not TheoryTemporalIntent.SETUP_FIRST:
        return None
    requirements = tuple(getattr(world.context, "temporal_requirements", ()))
    if not requirements:
        raise ValueError("setup-first theory has no resolved live requirements")
    configured_ids = getattr(view, "overlay_identities", frozenset())
    configured_tentative_pairs = tuple(
        (rung.dest, rung.value)
        for requirement in requirements
        if _uses_ordinary_correction_validation(requirement)
        for rung in getattr(requirement, "corrective_pilot_rungs", ())
        if _rung_identity(rung) in configured_ids
    )
    for schedule in _iter_temporal_schedules(world, target, requirements):
        # A composed PilotRung is already the physical intervention.  While
        # its finite guard is dormant, the scalar condition may still look
        # false at this boundary; emitting a second direct assignment would
        # replace the exact correction with a broader generic hold.  Let the
        # ordinary current-world steer activate the installed rung instead.
        actions = tuple(
            pair
            for pair in schedule.assignments
            if not _pair_matches_any(pair, configured_tentative_pairs)
        )
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
            return _orientation_reading._bearing(
                read,
                act,
                target=target,
                prerequisites=tuple(schedule.pilot_rungs),
                rationale="working theory: establish exact temporal setup",
            )
    # Configured, program-owned, incompatible, or unsupported conditions are
    # not direct scalar Bearings. Other current-world readers may still return
    # a coast or upstream act; otherwise the ordinary typed result wins.
    return None


def _theory_setup_traceback(
    read: OrientationRead,
    ordinary: Bearing | None,
) -> NeedIntrascanTraceback | None:
    """Request one exact backward proof for a program-owned setup leaf."""

    world = read.world
    view = getattr(world.context, "theory_view", None)
    if (
        view is None
        or view.temporal_intent is not TheoryTemporalIntent.SETUP_FIRST
        or ordinary is None
    ):
        return None
    consumer_assignments = tuple(ordinary.act.policy.applied)
    if not consumer_assignments:
        return None
    requirements = tuple(getattr(world.context, "temporal_requirements", ()))
    for branch in iter_temporal_need_branches(requirements):
        for atom in branch.atoms:
            condition = atom.condition
            authority = (
                atom.guard_atom.operand_authority
                if atom.guard_atom is not None
                else atom.requirement.operand_authority
            )
            occurrence = (
                atom.guard_atom.deadline
                if atom.guard_atom is not None
                else atom.requirement.demanding_occurrence
            )
            if (
                authority is not OperandAuthority.PROGRAM_WRITTEN
                or not isinstance(condition, Cmp)
                or condition.bound_is_tag
                or occurrence.kind != "read"
                or occurrence.tag != condition.tag
                or len(occurrence.values) != 1
                or constraint_holds(condition, {condition.tag: occurrence.values[0]}) is not False
            ):
                continue
            tag = getattr(world.state.work, "_known_tags_by_name", {}).get(condition.tag)
            if tag is None:
                continue
            values = satisfying_values(tag, (condition,), dict(world.snapshot))
            boundary = _counterfactual_boundary(
                occurrence,
                getattr(atom.requirement, "selected_writer", None),
            )
            patch_guard = _counterfactual_guard(tag, condition)
            if not values or boundary is None or patch_guard is None:
                continue
            request = IntrascanTracebackRequest(
                patch=CounterfactualPatch(
                    dest=condition.tag,
                    value=values[0],
                    guard=patch_guard,
                    boundary=boundary,
                ),
                requirements=(atom.requirement,),
                consumer_assignments=consumer_assignments,
                required_condition=condition,
            )
            if view.has_traceback_result(request.identity):
                continue
            return NeedIntrascanTraceback(
                world_key=world.world_key,
                frontier=_orientation_reading._frontier(read),
                request=request,
                rationale=(
                    "working theory: trace one program-owned setup occurrence "
                    "back to a real earlier writer"
                ),
                orientation=read,
            )
    return None


def _traceback_hop_ancestry(
    view: Any,
    frontier: Any,
) -> frozenset[tuple[Any, ...]] | None:
    """Exact physical hops from one selected frontier back to its root."""

    by_id = {
        candidate.identity: candidate for candidate in getattr(view, "traceback_frontiers", ())
    }
    ancestry: set[tuple[Any, ...]] = set()
    seen: set[tuple[Any, ...]] = set()
    current = frontier
    while current is not None:
        if current.identity in seen or not getattr(current, "hop_identity", None):
            return None
        seen.add(current.identity)
        ancestry.add(current.hop_identity)
        parent_id = getattr(current, "parent_frontier_id", None)
        if parent_id is None:
            break
        current = by_id.get(parent_id)
        if current is None:
            return None
    return frozenset(ancestry)


def _theory_intrascan_continuation_traceback(
    read: OrientationRead,
) -> NeedIntrascanTraceback | None:
    """Extend one retained backward chain through its newly exposed writer.

    The frontier's ordinary bearing has already executed and been rejected.
    Its exact trigger remains the scan-entry action.  When the resulting
    prevention requirement has one complete Boolean branch with exactly one
    false program-owned scalar occurrence, research only that occurrence as
    the next hypothetical hop.  Adjustable or multiply-missing branches stay
    with ordinary composition/navigation.
    """

    world = read.world
    view = getattr(world.context, "theory_view", None)
    if view is None or view.temporal_intent not in {
        TheoryTemporalIntent.SETUP_FIRST,
        TheoryTemporalIntent.RETRY_TOGETHER,
        TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
    }:
        return None
    current_frontiers = getattr(view, "current_traceback_frontiers", None)
    frontiers = (
        tuple(current_frontiers())
        if current_frontiers is not None
        else tuple(
            frontier
            for frontier in getattr(view, "traceback_frontiers", ())
            if frontier.source == view.source
        )
    )
    trigger = getattr(view, "trigger_act_identity", None)
    if (
        not isinstance(trigger, tuple)
        or len(trigger) != 2
        or trigger[0] != "pulse"
        or not isinstance(trigger[1], tuple)
        or not trigger[1]
    ):
        return None
    trigger_actions = tuple(trigger[1])
    trigger_attempt_id = getattr(view, "trigger_attempt_id", None)
    trigger_attempts = tuple(
        attempt
        for attempt in getattr(view, "conductivity_attempts", ())
        if attempt.attempt_id == trigger_attempt_id and attempt.act_identity == trigger
    )
    if len(trigger_attempts) != 1:
        return None
    trigger_attempt = trigger_attempts[0]
    parent_frontier_id = getattr(trigger_attempt, "investigation_frontier_id", None)
    parent_producer_goal_id = getattr(trigger_attempt, "producer_goal_id", None)
    selected_frontiers = tuple(
        frontier for frontier in frontiers if frontier.identity == parent_frontier_id
    )
    if len(selected_frontiers) != 1 or parent_producer_goal_id is None:
        return None
    parent_frontier = selected_frontiers[0]
    selected_goals = tuple(
        goal for goal in parent_frontier.producer_goals if goal.identity == parent_producer_goal_id
    )
    if len(selected_goals) != 1:
        return None
    hop_ancestry = _traceback_hop_ancestry(view, parent_frontier)
    if hop_ancestry is None:
        return None
    requirements = tuple(getattr(world.context, "temporal_trigger_requirements", ()))
    if not requirements:
        return None

    for branch in iter_temporal_need_branches(requirements):
        evaluated: list[tuple[Any, bool]] = []
        for demand in branch.occurrence_demands:
            condition = demand.condition
            occurrence = demand.occurrence
            if (
                not isinstance(condition, Cmp)
                or condition.bound_is_tag
                or occurrence.kind != "read"
                or occurrence.tag != condition.tag
                or len(occurrence.values) != 1
            ):
                evaluated = []
                break
            holds = constraint_holds(
                condition,
                {condition.tag: occurrence.values[0]},
            )
            if holds is None:
                evaluated = []
                break
            evaluated.append((demand, holds))
        missing = tuple(demand for demand, holds in evaluated if holds is False)
        if len(missing) != 1:
            continue
        demand = missing[0]
        condition = demand.condition
        occurrence = demand.occurrence
        if demand.operand_authorities != frozenset((OperandAuthority.PROGRAM_WRITTEN,)):
            continue
        obstruction_occurrences = demand.obstruction_occurrences
        if len(obstruction_occurrences) > 1:
            continue
        tag = getattr(world.state.work, "_known_tags_by_name", {}).get(condition.tag)
        boundary = _counterfactual_boundary(
            occurrence,
            demand.selected_writers[0] if len(demand.selected_writers) == 1 else None,
        )
        patch_guard = _counterfactual_guard(tag, condition) if tag is not None else None
        values = (
            satisfying_values(tag, (condition,), dict(world.snapshot)) if tag is not None else ()
        )
        if boundary is None or patch_guard is None or not values:
            continue
        request = IntrascanTracebackRequest(
            patch=CounterfactualPatch(
                dest=condition.tag,
                value=values[0],
                guard=patch_guard,
                boundary=boundary,
            ),
            requirements=demand.supporting_requirements,
            consumer_assignments=trigger_actions,
            required_condition=condition,
            prevented_write=(obstruction_occurrences[0] if obstruction_occurrences else None),
            parent_frontier_id=parent_frontier.identity,
            parent_producer_goal_id=selected_goals[0].identity,
            parent_attempt_id=trigger_attempt.attempt_id,
        )
        if request.hop_identity in hop_ancestry:
            continue
        if view.has_traceback_result(request.identity):
            continue
        return NeedIntrascanTraceback(
            world_key=world.world_key,
            frontier=_orientation_reading._frontier(read),
            request=request,
            rationale=(
                "working theory: extend the retained intrascan traceback "
                "through one newly exposed program writer"
            ),
            orientation=read,
        )
    return None


def _counterfactual_guard(tag: Any, condition: Cmp) -> Any | None:
    """Rebuild one inert scalar requirement as an analysis-only condition."""

    complements = {
        "==": tag.__ne__,
        "!=": tag.__eq__,
        "<": tag.__ge__,
        "<=": tag.__gt__,
        ">": tag.__le__,
        ">=": tag.__lt__,
    }
    compare = complements.get(condition.op)
    return compare(condition.bound) if compare is not None else None


def _theory_pending_configuration_bearing(
    read: OrientationRead,
    target: TargetSpec,
) -> Bearing | None:
    """Execute one newly composed configuration or PilotRung correction."""

    world = read.world
    view = getattr(world.context, "theory_view", None)
    pending = getattr(view, "pending_configuration_identities", frozenset())
    configurations = tuple(
        configuration
        for configuration in getattr(view, "configurations", ())
        if configuration.identity in pending
    )
    pending_overlay_ids = getattr(view, "pending_overlay_identities", frozenset())
    pilot_rungs = tuple(
        rung for rung in world.state.pilot_rungs if _rung_identity(rung) in pending_overlay_ids
    )
    if not configurations and not pilot_rungs:
        return None
    configured_tags = frozenset(
        {
            *(tag for configuration in configurations for tag, _value in configuration.assignments),
            *(rung.dest for rung in pilot_rungs),
        }
    )
    requirements = tuple(
        requirement
        for requirement in getattr(world.context, "temporal_requirements", ())
        if getattr(getattr(requirement, "condition", None), "tag", None) in configured_tags
        or any(
            _rung_identity(rung) in pending_overlay_ids
            for rung in getattr(requirement, "corrective_pilot_rungs", ())
        )
    )
    if not requirements:
        return None
    act = ProgramContinuation(
        "seek",
        ActPolicy(
            source=ActSource.WIDENING,
            motion=MotionKind.COAST_HOLDING_WORLD,
            note="working theory: observe one newly corrected scan",
            expectation_exemption=ExpectationExemption.UNRESOLVED_EFFECT,
            local_progress=(
                LocalProgressKind.THEORY_CORRECTIVE
                if pilot_rungs
                else LocalProgressKind.TEMPORAL_EDGE
            ),
            local_progress_requirements=requirements,
            local_progress_sources=requirements,
            pulse_horizon=PulseHorizon.ASSERTION_SCAN,
        ),
    )
    if not _act_preserves_requirements(world, act) or world.context.compass.knowledge.act_is_nogood(
        world.world_key,
        act_identity(act),
    ):
        return None
    return _orientation_reading._bearing(
        read,
        act,
        target=target,
        rationale="working theory: execute one correction, then read Compass again",
    )


def _pending_overlay_pairs(world: OrientationWorld) -> tuple[_ActionPair, ...]:
    """Genuine temporary ladder overlays awaiting their first execution."""

    view = getattr(world.context, "theory_view", None)
    pending = getattr(view, "pending_overlay_identities", frozenset())
    return tuple(
        (rung.dest, rung.value)
        for rung in world.state.pilot_rungs
        if _rung_identity(rung) in pending
    )


def _pending_theory_pairs(world: OrientationWorld) -> tuple[_ActionPair, ...]:
    """Theory-owned overlays and configuration awaiting physical execution."""

    view = getattr(world.context, "theory_view", None)
    pending_configuration_ids = getattr(
        view,
        "pending_configuration_identities",
        frozenset(),
    )
    configured = tuple(
        assignment
        for configuration in getattr(view, "configurations", ())
        if configuration.identity in pending_configuration_ids
        for assignment in configuration.assignments
    )
    return (*_pending_overlay_pairs(world), *configured)


def _untried_pending_theory_pairs(
    world: OrientationWorld,
    research: Any,
) -> tuple[_ActionPair, ...]:
    """Pending configuration absent from the exact attempt being researched.

    Composition alone is not physical evidence, so a newly installed
    correction must participate in one execution before Compass researches
    what stopped that execution.  Once the later attempt in the exact
    conductivity comparison names the correction rung, however, ``pending``
    only means that its dedicated setup phase has not completed.  It must not
    hide a different overwrite which that same scan has now exposed.
    """

    view = getattr(world.context, "theory_view", None)
    pending_pairs = _pending_overlay_pairs(world)
    pending_configuration_ids = getattr(
        view,
        "pending_configuration_identities",
        frozenset(),
    )
    pending_configuration_pairs = tuple(
        assignment
        for configuration in getattr(view, "configurations", ())
        if configuration.identity in pending_configuration_ids
        for assignment in configuration.assignments
    )
    if not pending_pairs and not pending_configuration_pairs:
        return ()
    later_attempt_id = getattr(
        getattr(research, "comparison", None),
        "later_attempt_id",
        None,
    )
    attempts = tuple(
        attempt
        for attempt in getattr(view, "conductivity_attempts", ())
        if attempt.attempt_id == later_attempt_id
    )
    if len(attempts) != 1:
        return (*pending_pairs, *pending_configuration_pairs)
    tried = frozenset(getattr(attempts[0], "pilot_rung_identities", ()))
    pending = getattr(view, "pending_overlay_identities", frozenset())
    untried = pending - tried
    overlays = tuple(
        (rung.dest, rung.value)
        for rung in world.state.pilot_rungs
        if _rung_identity(rung) in untried
    )
    tried_configurations = frozenset(
        configuration.identity for configuration in getattr(attempts[0], "configurations", ())
    )
    configured = tuple(
        assignment
        for configuration in getattr(view, "configurations", ())
        if configuration.identity in pending_configuration_ids - tried_configurations
        for assignment in configuration.assignments
    )
    return (*overlays, *configured)


def _configured_theory_pairs(world: OrientationWorld) -> tuple[_ActionPair, ...]:
    """All theory-owned overlays and configuration, pending or observed."""

    view = getattr(world.context, "theory_view", None)
    assignments = tuple(
        assignment
        for configuration in getattr(view, "configurations", ())
        for assignment in configuration.assignments
    )
    return (*_configured_theory_overlay_pairs(world), *assignments)


def _configured_theory_overlay_pairs(world: OrientationWorld) -> tuple[_ActionPair, ...]:
    """Persistent theory-owned PilotRungs, whether pending or observed."""

    view = getattr(world.context, "theory_view", None)
    configured = getattr(view, "overlay_identities", frozenset())
    return tuple(
        (rung.dest, rung.value)
        for rung in world.state.pilot_rungs
        if _rung_identity(rung) in configured
    )


def _pair_matches_any(pair: _ActionPair, others: tuple[_ActionPair, ...]) -> bool:
    return any(pair[0] == other[0] and _values_match(pair[1], other[1]) for other in others)


def _candidate_is_pending_configuration(candidate: Any, world: OrientationWorld) -> bool:
    return _pair_matches_any(candidate.pair, _pending_theory_pairs(world))


def _current_candidate_applied(
    candidate: Any,
    candidates: CandidateRead,
    world: OrientationWorld,
) -> tuple[_ActionPair, ...]:
    """Keep installed corrections in the overlay, not the next act identity."""

    pending = _pending_theory_pairs(world)
    return tuple(
        pair
        for pair in _orientation_reading._candidate_applied(candidate, candidates, world.context)
        if not _pair_matches_any(pair, pending)
    )


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
