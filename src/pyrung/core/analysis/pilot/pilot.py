"""Public entry points and outer orchestration for PILOT drives.

This module builds static/runtime context, prepares the user-selected trace
constraint, and dispatches ``Bearing | NeedProbe | Stuck`` results from
``Compass``.
It invokes execution, owns verification-time excursion investigation, applies
observations, commits eligible forks, delegates post-commit recovery, and
converts the event stream into public results. It does not synthesize a
navigation decision.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from pyrsistent import pvector

from pyrung.core.analysis.graph import (
    Plan,
    PlanStatus,
    PlanStep,
    RouteAlt,
    RoutePivot,
    RouteTaken,
)
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.advance import build_advance_index, iter_advance_owners
from pyrung.core.analysis.pilot.awaited_actions import sibling_producer_family
from pyrung.core.analysis.pilot.bootstrap import (
    bootstrap_designations,
    observe_bootstrap_effects,
)
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    CoastObservation,
    Compass,
    NavigationCatalog,
    ProbeExhaustedObservation,
)
from pyrung.core.analysis.pilot.correction_candidates import correction_identity
from pyrung.core.analysis.pilot.earned_work import (
    build_earned_work,
    earned_work_is_useful_motion,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    exact_last_landing_write,
    expectation_from_writer,
    fulfilled_expectation_observations,
    obligation_snapshot,
    observe_execution_window,
    occurrence_snapshot,
    promote_certified_prefix_target_observation,
    promote_terminal_target_observation,
    terminal_target_replay_scan_ids,
)
from pyrung.core.analysis.pilot.intrascan import (
    IntrascanQuestion,
    derive_recorded_observations,
)
from pyrung.core.analysis.pilot.investigate import investigate_excursion
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
    ChannelHeading,
    Coast,
    NavigationConstraints,
    NeedProbe,
    OrientationResult,
    OrientationWorld,
    Pulse,
    Stuck,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.overlay import (
    _merged_pilot_rungs,
    _pilot_rungs_from_proposals,
    _target_unresolved_condition,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.pipeline_graph import (
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.program_step import ProgramStepStatus, read_program_step
from pyrung.core.analysis.pilot.progress import (
    _anchor_bearing_receipt,
    _anchor_frame_receipt,
    _install_confirmed_correction,
    _monitor_trend,
    _promote_probationary_corrections,
    _record_pending_landing,
)
from pyrung.core.analysis.pilot.recording import (
    _act_event,
    _build_plan_journal,
    _candidates_built_payload,
    _frontier_clause,
    _iteration_payload,
    _knowledge_payload,
)
from pyrung.core.analysis.pilot.recovery import (
    CompositionBudget,
    Reject,
    Succeed,
    assert_recovery_disposable_state,
    assert_recovery_inactive,
    compose_corrections,
)
from pyrung.core.analysis.pilot.requirement_recovery import (
    RequirementSchedule,
    compile_scalar_schedule,
    guard_alternatives,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    ExpectationReceipt,
    FailedEffectReceipt,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementPhase,
    RequirementStatus,
    bind_guard_operand_authorities,
    classify_bound_operand_authority,
    derive_advance_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
    derive_overwriter_guard_requirement_from_write,
)
from pyrung.core.analysis.pilot.skiff import probe_live_guard_frontiers
from pyrung.core.analysis.pilot.steer import execute
from pyrung.core.analysis.pilot.trace import (
    DomainPrior,
    TraceChoice,
    TraceReadConstraints,
    UnsupportedConstruct,
    _route_forced_names,
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    enumerate_trace_choices,
    frontier_pairs,
    rank_trace_choices,
    target_reached,
    trace_back,
)
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    MotionKind,
    PilotEvent,
    TargetReached,
    WorldView,
    _AcceptedTrial,
    _ActionPair,
    _AttemptResult,
    _BootstrapExecution,
    _CausalCheckpoint,
    _Checkpoint,
    _CommittedAct,
    _ConfirmedCorrection,
    _ContinuationCheckpoint,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _RecoveryContinuation,
    _Step,
    _StepContext,
    _World,
)
from pyrung.core.analysis.pilot.verify import verify_excursion_replay, verify_gates
from pyrung.core.analysis.pilot.working_theory import (
    AbandonTheory,
    AdvanceTheory,
    OpenTheory,
    ProveTheory,
    RecordTheoryAttempt,
    RecordUnattributedEvidence,
    RefineTheory,
    TheoryAttemptDisposition,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryObjectiveSnapshot,
    TheoryObligationSnapshot,
    TheoryRequirementSnapshot,
    TheoryTermination,
    UnattributedTheoryEvidence,
    reduce_theory,
)
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
    _rung_identity,
    _semantic_key,
    _StateKeyConfig,
)
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable
from pyrung.core.context import RungId
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.pipeline_graph import StaticTransitionGraph
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


def _configured_input_names(plc: Any) -> frozenset[str]:
    """Snapshot explicit patch/force ownership without retaining its manager."""

    overrides = getattr(plc, "_input_overrides", None)
    if overrides is None:
        return frozenset()
    return frozenset((*overrides.forces, *overrides.pending_patches))


def _checkpoint_configured_inputs(checkpoint: Any) -> frozenset[str]:
    """Read checkpoint provenance, falling back for lightweight test stubs."""

    configured = getattr(checkpoint, "configured_inputs", None)
    if configured is not None:
        return frozenset(configured)
    source_work = checkpoint.world.work
    return _configured_input_names(source_work)


def _bound_operand_authorities(
    projection: Any,
    checkpoint: _CausalCheckpoint,
    ctx: _PilotContext,
) -> dict[str, OperandAuthority]:
    """Classify exact boundary operands without inventing write permission."""

    source_work = checkpoint.world.work
    source_tags = source_work.state.tags
    known = source_work._known_tags_by_name
    program_written = frozenset(ctx.pdg.writers_of)
    configured = _checkpoint_configured_inputs(checkpoint)
    result: dict[str, OperandAuthority] = {}
    for read in projection.reads:
        tag = read.occurrence.name
        declared = known.get(tag)
        result[tag] = classify_bound_operand_authority(
            tag,
            source_value=source_tags.get(tag, getattr(declared, "default", None)),
            declared_default=getattr(declared, "default", None),
            steerable=ctx.steerable,
            program_written=program_written,
            configured=configured,
        )
    return result


def _bind_guard_derivation_authority(
    derivation: Any,
    checkpoint: _CausalCheckpoint,
    ctx: _PilotContext,
) -> Any:
    """Bind arbitrary guard atoms without completion-parameter heuristics."""

    requirement = bind_guard_operand_authorities(
        derivation.requirement,
        steerable=ctx.steerable,
        program_written=frozenset(ctx.pdg.writers_of),
        configured=_checkpoint_configured_inputs(checkpoint),
    )
    return replace(derivation, requirement=requirement)


def _retain_active_requirement(
    state: _PilotState,
    requirement: ActiveRequirement | None,
) -> bool:
    """Append one exact requirement once without changing executable state."""

    if requirement is None or any(
        current.navigation_identity == requirement.navigation_identity
        for current in state.active_requirements
    ):
        return False
    state.active_requirements.append(requirement)
    return True


def _derive_bootstrap_requirements(
    state: _PilotState,
    ctx: _PilotContext,
    receipt: _BootstrapExecution,
) -> None:
    """Interpret exact appeared bootstrap violations without repairing them."""

    index = build_advance_index(
        ctx.program,
        getattr(receipt.checkpoint.world.work, "_harness", None),
    )
    authorities = _bound_operand_authorities(receipt.projection, receipt.checkpoint, ctx)
    for effect in receipt.appeared_effects:
        derivation = derive_advance_requirement_from_effect(
            index,
            receipt.projection,
            effect.observation,
            operand_authorities=authorities,
            execution_epoch=receipt.execution_epoch,
            execution_owner=receipt.execution_owner,
            selected_writer=effect.designation.producer,
            source_world_key=receipt.checkpoint.key,
            source_checkpoint=receipt.checkpoint,
            provenance="bootstrap",
        )
        if derivation.requirement is None:
            derivation = _bind_guard_derivation_authority(
                derive_overwriter_guard_requirement_from_effect(
                    effect.observation,
                    receipt.projection,
                    execution_epoch=receipt.execution_epoch,
                    execution_owner=receipt.execution_owner,
                    selected_writer=effect.designation.producer,
                    source_world_key=receipt.checkpoint.key,
                    source_checkpoint=receipt.checkpoint,
                    provenance="bootstrap-overwriter",
                ),
                receipt.checkpoint,
                ctx,
            )
        _retain_active_requirement(state, derivation.requirement)


def _release_attempt_projections(attempt: _AttemptResult | None) -> None:
    """Release selected-scan replay evidence after its last consumer."""

    if attempt is not None:
        attempt.release_projections()


def _derive_attempt_requirements(
    attempt: _AttemptResult,
    state: _PilotState,
    ctx: _PilotContext,
    checkpoint: _CausalCheckpoint | None,
) -> None:
    """Retain exact failed-effect requirements from a disposable steer."""

    # An accepted act is locally successful.  Its exact expectation receipt is
    # retained after adoption for any later causal regression; immediate
    # failure inversion belongs only to a rejected disposable attempt.
    if checkpoint is None or attempt.trial is not None:
        return
    executed = attempt.executed_attempt
    if executed is None:
        return
    fallback_scan = executed.assertion_scan
    question = IntrascanQuestion(
        expectation=executed.bearing.expectation,
        execution=executed.pulse.fork,
        assertion_scan=fallback_scan,
        source_checkpoint=checkpoint,
        advance_index=None,
        operand_authorities={},
        steerable=ctx.steerable,
        program_written=frozenset(ctx.pdg.writers_of),
        configured_inputs=_checkpoint_configured_inputs(checkpoint),
        advance_index_factory=lambda: build_advance_index(
            ctx.program,
            getattr(checkpoint.world.work, "_harness", None),
        ),
        operand_authorities_at=lambda projection: _bound_operand_authorities(
            projection,
            checkpoint,
            ctx,
        ),
        projection_at=executed.projection_at,
    )
    report = derive_recorded_observations(
        question,
        executed.effect_observations,
        fallback_scan=fallback_scan,
    )
    for finding in report.findings:
        observation = finding.observation
        derivation = finding.derivation
        diagnostic = finding.diagnostic_snapshot()
        failed = FailedEffectReceipt(
            explanation=diagnostic.explanation,
            observation=diagnostic.observation,
            selected_writer=diagnostic.selected_writer,
            source_world_key=diagnostic.source_world_key,
            checkpoint_owner=checkpoint.owner,
            execution_epoch=observation.execution_epoch,
            execution_owner=observation.execution_owner,
            source_checkpoint=checkpoint,
            act_identity=act_identity(executed.bearing.act),
            local_act=executed.bearing.act,
            local_bearing=executed.bearing,
            expectation=executed.bearing.expectation,
        )
        if not any(current.identity == failed.identity for current in state.failed_effect_receipts):
            state.failed_effect_receipts.append(failed)
        _retain_active_requirement(state, derivation.requirement)


def _retain_expectation_receipt(
    trial: _AcceptedTrial,
    act: Any,
    state: _PilotState,
    checkpoint: _CausalCheckpoint | None,
) -> None:
    """Journal an accepted whole-shape expectation with exact occurrences."""

    if checkpoint is None:
        return
    expectation = trial.attempt.bearing.expectation
    if expectation is None:
        return
    observations = fulfilled_expectation_observations(
        expectation,
        trial.attempt.effect_observations,
    )
    if len(observations) != len(expectation.obligations):
        return
    epochs = {id(item.execution_epoch) for item in observations}
    owners = {id(item.execution_owner) for item in observations}
    if len(epochs) != 1 or len(owners) != 1:
        return
    first = observations[0]
    if first.execution_epoch is None or first.execution_owner is None:
        return
    producers = tuple(
        occurrence_snapshot(item.appeared) for item in observations if item.appeared is not None
    )
    consumers = tuple(
        occurrence_snapshot(item.consumer_read)
        for item in observations
        if item.consumer_read is not None
    )
    producer_scans = tuple(
        item.appeared.scan_id for item in observations if item.appeared is not None
    )
    if not producer_scans:
        return
    # Bind the receipt to the adopted live lineage, not the disposable pulse
    # fork.  Adoption may rebuild the runner overlay while preserving the
    # exact history; later regression queries fork from ``state.work`` and
    # therefore share these retained epoch/query objects.
    sealed = state.work._causal_lineage.seal_through(state.work.state.scan_id)
    receipt_owner = next(
        (
            (epoch, owner)
            for epoch, owner in sealed
            if all(epoch.first_scan <= scan <= epoch.last_scan for scan in producer_scans)
        ),
        None,
    )
    if receipt_owner is None:
        return
    receipt_epoch, receipt_query = receipt_owner
    receipt = ExpectationReceipt(
        source_world_key=checkpoint.key,
        checkpoint_owner=checkpoint.owner,
        act_identity=act_identity(act),
        active_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        obligations=tuple(obligation_snapshot(item.obligation) for item in observations),
        producer_occurrences=producers,
        consumer_occurrences=consumers,
        execution_epoch=receipt_epoch,
        execution_owner=receipt_query,
        source_checkpoint=checkpoint,
        local_act=act,
        local_bearing=trial.attempt.bearing,
        expectation=trial.attempt.bearing.expectation,
    )
    if not any(current.identity == receipt.identity for current in state.expectation_receipts):
        state.expectation_receipts.append(receipt)


@dataclass(frozen=True)
class _RequirementRepairResult:
    """One bounded local-repair handoff to the outer fresh-read loop."""

    attempted: bool = False
    repaired: bool = False
    knowledge_changed: bool = False
    declined: bool = False
    requirement: ActiveRequirement | None = None
    assignments: tuple[tuple[str, Any], ...] = ()
    detail: str = ""


def _copy_repair_knowledge(source: _PilotState, target: _PilotState) -> bool:
    """Merge exact receipts and act admissibility from a disposable retry."""

    changed = False
    expectation_slots = {
        (item.act_identity, item.obligations): index
        for index, item in enumerate(target.expectation_receipts)
    }
    for item in source.expectation_receipts:
        slot = expectation_slots.get((item.act_identity, item.obligations))
        if slot is not None and target.expectation_receipts[slot].identity != item.identity:
            # A successful local retry supersedes the invalidated future's
            # receipt for the same whole-shape act.  Keep one journal slot while
            # updating its exact epoch/source proof to the corrected execution.
            target.expectation_receipts[slot] = item
            changed = True
    for attr, identity in (
        ("active_requirements", "navigation_identity"),
        ("failed_effect_receipts", "identity"),
    ):
        destination = getattr(target, attr)
        known = {getattr(item, identity) for item in destination}
        for item in getattr(source, attr):
            key = getattr(item, identity)
            if key not in known:
                destination.append(item)
                known.add(key)
                changed = True
    rejected_delta = source.proof_rejected_acts - target.proof_rejected_acts
    if rejected_delta:
        target.proof_rejected_acts.update(rejected_delta)
        changed = True
    return changed


def _disposable_requirement_state(
    state: _PilotState,
    checkpoint: _CausalCheckpoint,
) -> _PilotState:
    """Clone one exact causal world without sharing Phase-5 knowledge lists."""

    clone = _PilotState(
        world=checkpoint.world,
        key_config=state.key_config,
        seen_keys=set(state.seen_keys),
        checkpoints=[],
        watch_tags=list(state.watch_tags),
        invocation_checkpoint=state.invocation_checkpoint,
        bootstrap_execution=state.bootstrap_execution,
        active_requirements=list(state.active_requirements),
        expectation_receipts=list(state.expectation_receipts),
        failed_effect_receipts=list(state.failed_effect_receipts),
        theory_state=state.theory_state,
        requirement_repair_attempts=set(state.requirement_repair_attempts),
        recovery_continuation=state.recovery_continuation,
        proof_rejected_acts=set(state.proof_rejected_acts),
        search_start_scan=state.search_start_scan,
        earned_work=state.earned_work,
    )
    clone.load_world(checkpoint.world)
    return clone


def _exact_failed_source(
    requirement: ActiveRequirement,
    state: _PilotState,
) -> FailedEffectReceipt | None:
    """Match one requirement to its exact failed local transaction."""

    matches = tuple(
        receipt
        for receipt in state.failed_effect_receipts
        if receipt.checkpoint_owner is requirement.checkpoint_owner
        and receipt.source_world_key == requirement.source_world_key
        and receipt.selected_writer == requirement.selected_writer
        and receipt.execution_epoch is requirement.execution_epoch
        and receipt.execution_owner is requirement.execution_owner
        and requirement.deadline in receipt.explanation.supporting_occurrences
        and receipt.local_act is not None
        and receipt.local_bearing is not None
        and receipt.expectation is receipt.local_act.policy.expectation
        and receipt.act_identity == act_identity(receipt.local_act)
    )
    return matches[0] if len(matches) == 1 else None


def _continuation_origin_receipt(
    requirement: ActiveRequirement,
    state: _PilotState,
) -> ExpectationReceipt | None:
    """Resolve the one accepted source transaction behind a later coast loss."""

    matches = tuple(
        receipt
        for receipt in state.expectation_receipts
        if receipt.checkpoint_owner is requirement.checkpoint_owner
        and receipt.source_world_key == requirement.source_world_key
        and receipt.local_act is not None
        and receipt.local_bearing is not None
        and receipt.expectation is receipt.local_act.policy.expectation
        and not isinstance(receipt.local_act, Coast)
    )
    return matches[0] if len(matches) == 1 else None


def _is_exact_bootstrap_source(
    requirement: ActiveRequirement,
    state: _PilotState,
) -> bool:
    receipt = state.bootstrap_execution
    if (
        receipt is None
        or not requirement.provenance.startswith("bootstrap")
        or requirement.source_checkpoint is not receipt.checkpoint
        or requirement.checkpoint_owner is not receipt.checkpoint.owner
        or requirement.execution_epoch is not receipt.execution_epoch
        or requirement.execution_owner is not receipt.execution_owner
    ):
        return False
    matches = []
    for effect in receipt.appeared_effects:
        if effect.designation.producer != requirement.selected_writer:
            continue
        occurrences = (
            *effect.observation.observed_reads,
            effect.observation.consumer_read,
            effect.observation.displaced_read,
        )
        if any(
            occurrence is not None
            and occurrence_snapshot(occurrence) == requirement.demanding_occurrence
            for occurrence in occurrences
        ):
            matches.append(effect)
    return len(matches) == 1


def _bootstrap_designation_for_requirement(
    requirement: ActiveRequirement,
    state: _PilotState,
) -> Any | None:
    receipt = state.bootstrap_execution
    if receipt is None:
        return None
    matches = []
    for effect in receipt.appeared_effects:
        occurrences = (
            *effect.observation.observed_reads,
            effect.observation.consumer_read,
            effect.observation.displaced_read,
        )
        if effect.designation.producer == requirement.selected_writer and any(
            occurrence is not None
            and occurrence_snapshot(occurrence) == requirement.demanding_occurrence
            for occurrence in occurrences
        ):
            matches.append(effect.designation)
    return matches[0] if len(matches) == 1 else None


def _whole_expectation_survived(executed: Any, expectation: EffectExpectation) -> bool:
    observations = executed.effect_observations
    return len(fulfilled_expectation_observations(expectation, observations)) == len(
        expectation.obligations
    )


def _bootstrap_local_designation_survived(
    observations: tuple[Any, ...],
    designation: Any,
) -> bool:
    """Whether one repaired bootstrap scan met its exact local designation."""

    return (
        len(observations) == 1
        and observations[0].designation is designation
        and observations[0].observation.disposition == "SURVIVED"
    )


def _source_requirements(
    requirement: ActiveRequirement,
    state: _PilotState,
) -> tuple[ActiveRequirement, ...]:
    return tuple(
        current
        for current in state.active_requirements
        if current.status is RequirementStatus.ACTIVE
        and current.phase is RequirementPhase.STEADY
        and current.checkpoint_owner is requirement.checkpoint_owner
        and current.source_world_key == requirement.source_world_key
    )


def _constraint_target_value(plc: Any, condition: Cmp) -> Any | None:
    """Choose one declared-domain value which satisfies a scalar condition."""

    tag = plc._known_tags_by_name.get(condition.tag)
    if tag is None or condition.bound_is_tag:
        return None
    current = plc.state.tags.get(condition.tag, tag.default)
    candidates: list[Any] = [current, tag.default]
    if tag.choices:
        candidates.extend(tag.choices)
    bound = condition.bound
    if condition.op == "==":
        candidates.append(bound)
    elif condition.op == "!=":
        if isinstance(bound, bool):
            candidates.append(not bound)
        elif isinstance(bound, int | float) and not isinstance(bound, bool):
            candidates.extend((bound - 1, bound + 1))
    elif isinstance(bound, int | float) and not isinstance(bound, bool):
        candidates.extend(
            {
                ">": (bound + 1,),
                ">=": (bound,),
                "<": (bound - 1,),
                "<=": (bound,),
            }.get(condition.op, ())
        )
    for candidate in candidates:
        if (
            constraint_holds(
                condition,
                {**dict(plc.state.tags), condition.tag: candidate},
            )
            is True
        ):
            return candidate
    return None


def _nested_guard_act(
    source_state: _PilotState,
    ctx: _PilotContext,
    local_bearing: Bearing,
    local_act: Any,
    requirements: tuple[ActiveRequirement, ...],
) -> Any | None:
    """Nest only exact prerequisites due in the selected local transaction."""

    guard_requirements = tuple(
        requirement
        for requirement in requirements
        if isinstance(requirement.condition, GuardRequirementAtom | GuardRequirementExpr)
    )
    transaction_pairs: list[_ActionPair] = []
    expectation = local_act.policy.expectation
    selected_trace = getattr(
        getattr(local_bearing.orientation, "candidates", None),
        "trace",
        None,
    )
    trace_actions = dict(getattr(selected_trace, "active_actions", ()))
    if expectation is not None:
        for obligation in expectation.obligations:
            if obligation.consumer is None:
                continue
            consumer_sub, consumer_rung, consumer_branch = obligation.consumer
            for node in ctx.pdg.rung_nodes:
                branch_path = getattr(node, "branch_path", ())
                if (
                    getattr(node, "subroutine", object()) != consumer_sub
                    or getattr(node, "rung_index", object()) != consumer_rung
                    or len(branch_path) <= len(consumer_branch)
                    or branch_path[: len(consumer_branch)] != consumer_branch
                ):
                    continue
                reads = node.condition_reads | node.guard_reads
                for tag in reads:
                    if tag not in trace_actions or tag not in ctx.steerable:
                        continue
                    pair = (tag, trace_actions[tag])
                    if (
                        constraint_holds(
                            Cmp(tag, "==", trace_actions[tag]),
                            source_state.work.state.tags,
                        )
                        is not True
                        and pair not in transaction_pairs
                    ):
                        transaction_pairs.append(pair)

    transaction_acts: list[Any] = []
    for tag, value in transaction_pairs:
        raw_world = OrientationWorld(
            world_key=(),
            snapshot=dict(source_state.work.state.tags),
            frame=None,
            state=source_state,
            context=ctx,
            key_config=source_state.key_config,
        )
        oriented = ctx.compass.orient(
            raw_world,
            TargetSpec(tag, value),
            NavigationConstraints(),
        )
        if not isinstance(oriented, Bearing) or not oriented.act.policy.applied:
            return None
        transaction_acts.append(oriented.act)

    if not guard_requirements and not transaction_acts:
        return local_act

    nested_acts: list[tuple[Any, ActiveRequirement, GuardRequirementAtom]] = []
    snap = dict(source_state.work.state.tags)
    for requirement in guard_requirements:
        choices: list[tuple[tuple[Any, ...], tuple[Any, ...]]] = []
        condition_tree = cast(GuardRequirementCondition, requirement.condition)
        for alternative in guard_alternatives(condition_tree):
            acts: list[tuple[Any, GuardRequirementAtom]] = []
            exact = True
            for atom in alternative:
                if constraint_holds(atom.condition, snap) is True:
                    continue
                if not atom.permits_assignment:
                    exact = False
                    break
                condition = atom.condition
                if not isinstance(condition, Cmp):
                    exact = False
                    break
                target_value = _constraint_target_value(source_state.work, condition)
                if target_value is None:
                    exact = False
                    break
                target = TargetSpec(condition.tag, target_value)
                raw_world = OrientationWorld(
                    world_key=(),
                    snapshot=dict(snap),
                    frame=None,
                    state=source_state,
                    context=ctx,
                    key_config=source_state.key_config,
                )
                oriented = ctx.compass.orient(raw_world, target, NavigationConstraints())
                if not isinstance(oriented, Bearing) or not oriented.act.policy.applied:
                    exact = False
                    break
                acts.append((oriented.act, atom))
            if exact:
                choices.append(
                    (
                        (
                            sum(
                                1
                                for _act, atom in acts
                                if getattr(atom.condition, "tag", None) not in ctx.steerable
                            ),
                            len(acts),
                            tuple(act_identity(act) for act, _atom in acts),
                        ),
                        tuple(acts),
                    )
                )
        selected = min(choices, key=lambda choice: choice[0])[1] if choices else None
        if selected is None:
            return None
        nested_acts.extend((act, requirement, atom) for act, atom in selected)

    ordered_pairs: list[_ActionPair] = []
    by_tag: dict[str, Any] = {}
    for act in (
        local_act,
        *transaction_acts,
        *(act for act, _requirement, _atom in nested_acts),
    ):
        for tag, value in act.policy.applied:
            if tag in by_tag and not _values_match(by_tag[tag], value):
                return None
            if tag not in by_tag:
                by_tag[tag] = value
                ordered_pairs.append((tag, value))

    original_obligations = (
        local_act.policy.expectation.obligations if local_act.policy.expectation is not None else ()
    )
    nested_obligations = [
        obligation
        for nested in transaction_acts
        if nested.policy.expectation is not None
        for obligation in nested.policy.expectation.obligations
    ]
    for nested, requirement, atom in nested_acts:
        if nested.policy.expectation is None:
            continue
        deadline = requirement.demanding_occurrence
        if deadline.execution_kind != "rung":
            return None
        consumer_nodes = tuple(
            node
            for node in ctx.pdg.rung_nodes
            if resolve_rung(ctx.program, node) is atom.demanding_rung
        )
        consumer_node = consumer_nodes[0] if len(consumer_nodes) == 1 else None
        consumer_address = (
            (
                consumer_node.subroutine,
                consumer_node.rung_index,
                consumer_node.branch_path,
            )
            if consumer_node is not None
            else None
        )
        consumer_rung = (
            resolve_rung(ctx.program, consumer_node) if consumer_node is not None else None
        )
        if consumer_rung is None or consumer_address is None:
            return None
        for obligation in nested.policy.expectation.obligations:
            if obligation.consumer is None:
                obligation = replace(
                    obligation,
                    consumer=consumer_address,
                    consumer_rung=consumer_rung,
                    required_shape=((obligation.tag, obligation.value),),
                )
            nested_obligations.append(obligation)
    obligations = (*nested_obligations, *original_obligations)
    expectation = EffectExpectation(obligations) if obligations else local_act.policy.expectation
    policy = replace(
        local_act.policy,
        source=ActSource.LEARNED_BATCH,
        action_pairs=tuple(ordered_pairs),
        applied=tuple(ordered_pairs),
        expectation=expectation,
        expectation_exemption=None,
        motion=MotionKind.INTERVENTION,
        provenance=(*local_act.policy.provenance, "active guard requirement"),
    )
    if len(ordered_pairs) == 1:
        return Pulse(policy)
    return BatchPulse(policy)


def _mandatory_guard_blocker(
    requirements: tuple[ActiveRequirement, ...],
    snapshot: Mapping[str, Any],
) -> GuardRequirementAtom | None:
    """Name one exact false program-owned guard for a proved landing overwrite."""

    def exhaustive(condition: GuardRequirementCondition) -> bool:
        if isinstance(condition, GuardRequirementAtom):
            return True
        return condition.exhaustive and all(exhaustive(term) for term in condition.terms)

    for requirement in requirements:
        condition = requirement.condition
        if not isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
            continue
        if not any(item and item[0] == "overwriter_guard" for item in requirement.scope):
            # A producer guard can become true through a still-untried sibling
            # action (for example a crossing DNF branch).  Only an observed
            # final landing overwrite proves the local act has reached this
            # mandatory condition and may safely decline here.
            continue
        if not exhaustive(condition):
            continue
        blockers: list[GuardRequirementAtom] = []
        for alternative in guard_alternatives(condition):
            unsatisfied = tuple(
                atom
                for atom in alternative
                if constraint_holds(atom.condition, snapshot) is not True
            )
            if not unsatisfied:
                break
            if any(atom.permits_assignment for atom in unsatisfied):
                break
            blockers.append(unsatisfied[0])
        else:
            if blockers:
                return blockers[0]
    return None


def _mandatory_guard_decline_reason(
    blocker: GuardRequirementAtom,
    snapshot: Mapping[str, Any],
    target: TargetSpec,
) -> str:
    """Describe an exact mandatory guard solely in terms of the machine."""

    condition = blocker.condition
    if isinstance(condition, Cmp):
        observed = (
            blocker.deadline.values[-1]
            if blocker.deadline.tag == condition.tag and blocker.deadline.values
            else snapshot.get(condition.tag)
        )
        bound = condition.bound if not condition.bound_is_tag else snapshot.get(condition.bound)
        needed = (
            f"{condition.tag} {condition.op} {condition.bound}={bound!r}"
            if condition.bound_is_tag
            else f"{condition.tag} {condition.op} {bound!r}"
        )
        return (
            f"The machine has {condition.tag}={observed!r}, but "
            f"{target.tag}={target.value!r} requires {needed}; "
            f"{condition.tag} is controlled by the program."
        )
    return (
        f"The machine cannot preserve {target.tag}={target.value!r} because its "
        "required program-controlled condition is false."
    )


def _rebound_bearing(
    source_state: _PilotState,
    ctx: _PilotContext,
    original: Bearing,
    act: Any,
) -> Bearing | None:
    """Bind the exact retained act to a fresh read of its causal source."""

    raw_world = OrientationWorld(
        world_key=(),
        snapshot=dict(source_state.work.state.tags),
        frame=None,
        state=source_state,
        context=ctx,
        key_config=source_state.key_config,
    )
    reading = ctx.compass.orient(
        raw_world,
        original.objective.target,
        NavigationConstraints(active_requirements=tuple(source_state.active_requirements)),
    )
    if reading.orientation is None:
        return None
    return Bearing(
        world_key=reading.orientation.world_key,
        act=act,
        objective=original.objective,
        prerequisites=original.prerequisites,
        rationale=f"causal local repair: {original.rationale}",
        orientation=reading.orientation,
    )


def _compile_source_schedule(
    requirements: tuple[ActiveRequirement, ...],
    source_state: _PilotState,
    ctx: _PilotContext,
) -> tuple[RequirementSchedule | None, str]:
    scalar = tuple(
        requirement for requirement in requirements if isinstance(requirement.condition, Cmp)
    )
    if not scalar:
        return None, ""
    guard = _target_unresolved_condition(
        source_state.work,
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    )
    compilation = compile_scalar_schedule(scalar, source_state.work, guard=guard)
    return compilation.schedule, compilation.detail


def _record_requirement_correction(
    state: _PilotState,
    schedule: RequirementSchedule,
    *,
    scan: int,
) -> None:
    if not schedule.pilot_rungs:
        return
    correction = _ConfirmedCorrection(
        identity=correction_identity(schedule.pilot_rungs),
        pilot_rungs=schedule.pilot_rungs,
        sources=tuple(tag for tag, _value in schedule.assignments),
        justification="active requirement schedule locally repaired its exact expectation",
    )
    _install_confirmed_correction(
        state,
        correction,
        origin_key=schedule.source_world_key,
        scan=scan,
        source="requirement",
        adopt_existing=True,
    )


def _compile_program_guard_schedule(
    requirements: tuple[ActiveRequirement, ...],
    source: _PilotState,
    ctx: _PilotContext,
) -> tuple[RequirementSchedule | None, str]:
    guards = tuple(
        requirement
        for requirement in requirements
        if isinstance(requirement.condition, GuardRequirementAtom | GuardRequirementExpr)
    )
    if not guards:
        return None, ""
    snapshot = dict(source.work.state.tags)
    proposals: list[_ActionPair] = []
    known: dict[str, Any] = {}
    for requirement in guards:
        choices: list[tuple[tuple[Any, ...], list[_ActionPair]]] = []
        condition_tree = cast(GuardRequirementCondition, requirement.condition)
        for alternative in guard_alternatives(condition_tree):
            candidate: list[_ActionPair] = []
            exact = True
            for atom in alternative:
                if constraint_holds(atom.condition, snapshot) is True:
                    continue
                if not atom.permits_assignment:
                    exact = False
                    break
                condition = atom.condition
                if not isinstance(condition, Cmp):
                    exact = False
                    break
                target_value = _constraint_target_value(source.work, condition)
                if target_value is None:
                    exact = False
                    break
                target = TargetSpec(condition.tag, target_value)
                oriented = ctx.compass.orient(
                    OrientationWorld(
                        world_key=(),
                        snapshot=dict(snapshot),
                        frame=None,
                        state=source,
                        context=ctx,
                        key_config=source.key_config,
                    ),
                    target,
                    NavigationConstraints(),
                )
                if not isinstance(oriented, Bearing) or not oriented.act.policy.applied:
                    exact = False
                    break
                candidate.extend(oriented.act.policy.applied)
            if exact:
                choices.append(
                    (
                        (
                            sum(
                                1
                                for atom in alternative
                                if getattr(atom.condition, "tag", None) not in ctx.steerable
                            ),
                            len(candidate),
                            tuple((tag, repr(value)) for tag, value in candidate),
                        ),
                        candidate,
                    )
                )
        selected = min(choices, key=lambda choice: choice[0])[1] if choices else None
        if selected is None:
            return None, "bootstrap overwriter guard is not exactly steerable"
        for tag, value in selected:
            if tag in known and not _values_match(known[tag], value):
                return None, "bootstrap guard requirements are incompatible"
            if tag not in known:
                known[tag] = value
                proposals.append((tag, value))
    scope = _target_unresolved_condition(
        source.work,
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    )
    rungs = tuple(_pilot_rungs_from_proposals(proposals, scope))
    first = guards[0]
    return (
        RequirementSchedule(
            requirements=guards,
            assignments=tuple(proposals),
            pilot_rungs=rungs,
            checkpoint_owner=first.checkpoint_owner,
            source_world_key=first.source_world_key,
            phase=first.phase,
        ),
        "",
    )


def _repair_bootstrap_requirement(
    requirement: ActiveRequirement,
    state: _PilotState,
    ctx: _PilotContext,
) -> _RequirementRepairResult:
    if not _is_exact_bootstrap_source(requirement, state):
        return _RequirementRepairResult()
    requirements = _source_requirements(requirement, state)
    source = _disposable_requirement_state(state, requirement.source_checkpoint)
    schedule, detail = _compile_source_schedule(requirements, source, ctx)
    guard_schedule, guard_detail = _compile_program_guard_schedule(
        requirements,
        source,
        ctx,
    )
    if detail or guard_detail:
        return _RequirementRepairResult(
            requirement=requirement,
            detail=detail or guard_detail,
        )
    if schedule is None and guard_schedule is None:
        return _RequirementRepairResult(requirement=requirement, detail=detail)
    if schedule is None:
        assert guard_schedule is not None
        schedule = guard_schedule
    elif guard_schedule is not None:
        schedule = replace(
            schedule,
            requirements=(*schedule.requirements, *guard_schedule.requirements),
            assignments=(*schedule.assignments, *guard_schedule.assignments),
            pilot_rungs=(*schedule.pilot_rungs, *guard_schedule.pilot_rungs),
        )
    designation = _bootstrap_designation_for_requirement(requirement, state)
    if designation is None:
        return _RequirementRepairResult(
            requirement=requirement,
            detail="bootstrap designation is ambiguous",
        )
    source.pilot_rungs = _merged_pilot_rungs(schedule.pilot_rungs, source.pilot_rungs)
    attempt_identity = ("bootstrap", schedule.identity)
    if attempt_identity in state.requirement_repair_attempts:
        return _RequirementRepairResult(requirement=requirement, detail="repair already attempted")
    state.requirement_repair_attempts.add(attempt_identity)

    def attempt(candidate: _PilotState, transaction: Any) -> Any:
        transaction.register_disposable_state(candidate)
        candidate.work.step()
        projection = candidate.work._replay_rung_write_projection_at(candidate.work.state.scan_id)
        observations = (
            observe_bootstrap_effects((designation,), projection) if projection is not None else ()
        )
        if _bootstrap_local_designation_survived(observations, designation):
            return Succeed(candidate)
        return Reject(candidate)

    composition = compose_corrections(
        source,
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
        initial_identity=attempt_identity,
        protected_states=(state,),
    )
    repaired = composition.termination.value == "success"
    if repaired:
        _record_requirement_correction(
            composition.value,
            schedule,
            scan=requirement.source_checkpoint.world.work.state.scan_id,
        )
        state.world = composition.value.world
        state.hold_log.extend(composition.value.hold_log)
        state.correction_receipts.extend(composition.value.correction_receipts)
        state.checkpoints.clear()
        state.pending_departure = None
    return _RequirementRepairResult(
        attempted=True,
        repaired=repaired,
        requirement=requirement,
        assignments=schedule.assignments,
        detail="bootstrap local transaction repaired" if repaired else "bootstrap repair rejected",
    )


def _program_guard_rebase_surfaces(
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[tuple[_CausalCheckpoint, Any, Any], ...]:
    """Join retained executable boundaries to their exact execution histories."""

    surfaces: list[tuple[_CausalCheckpoint, Any, Any]] = []
    checkpoints: list[_CausalCheckpoint] = [
        _CausalCheckpoint(
            key=checkpoint.key,
            world=checkpoint.world,
            objective=checkpoint.objective,
            configured_inputs=ctx.configured_inputs
            | _configured_input_names(checkpoint.world.work),
            owner=checkpoint.owner,
        )
        for checkpoint in state.checkpoints
    ]
    if state.invocation_checkpoint is not None:
        checkpoints.append(state.invocation_checkpoint)
    bootstrap = state.bootstrap_execution
    if bootstrap is not None:
        checkpoints.append(bootstrap.checkpoint)
        surfaces.append(
            (bootstrap.checkpoint, bootstrap.execution_epoch, bootstrap.execution_owner)
        )
    for receipt in (*state.expectation_receipts, *state.failed_effect_receipts):
        checkpoints.append(receipt.source_checkpoint)
        surfaces.append(
            (receipt.source_checkpoint, receipt.execution_epoch, receipt.execution_owner)
        )
    for requirement in state.active_requirements:
        checkpoints.append(requirement.source_checkpoint)
        surfaces.append(
            (
                requirement.source_checkpoint,
                requirement.execution_epoch,
                requirement.execution_owner,
            )
        )

    # The live lineage owns accepted program motion even when that motion had
    # no expectation receipt. Ordinary progress checkpoints retain the exact
    # executable boundaries on that same lineage.
    lineage = state.work._causal_lineage
    for epoch, owner in lineage.seal_through(state.work.state.scan_id):
        for checkpoint in checkpoints:
            if checkpoint.world.work.state.scan_id < epoch.first_scan:
                surfaces.append((checkpoint, epoch, owner))

    unique: list[tuple[_CausalCheckpoint, Any, Any]] = []
    identities: set[tuple[int, int, int]] = set()
    for checkpoint, epoch, owner in surfaces:
        identity = (id(checkpoint), id(epoch), id(owner))
        if identity in identities or getattr(owner, "epoch", None) is not epoch:
            continue
        identities.add(identity)
        unique.append((checkpoint, epoch, owner))
    return tuple(unique)


def _program_guard_transition_candidates(
    owner: Any,
    rung_ids: frozenset[RungId],
    tag: str,
    *,
    before_scan: int,
) -> tuple[int, ...]:
    """Use compressed firing columns to rank possible transitions newest first."""

    main = frozenset(rung.rung_index for rung in rung_ids if rung.subroutine is None)
    nested = frozenset(rung for rung in rung_ids if rung.subroutine is not None)
    candidates: set[int] = set()
    if main:
        candidates.update(
            owner.rung_firing_timelines.tag_transition_candidate_scans_before(
                main,
                tag,
                before_scan,
            )
        )
    if nested:
        candidates.update(
            owner.node_firing_timelines.tag_transition_candidate_scans_before(
                nested,
                tag,
                before_scan,
            )
        )
    return tuple(sorted(candidates, reverse=True))


def _preinvocation_program_guard_surfaces(
    state: _PilotState,
    ctx: _PilotContext,
    rung_ids: frozenset[RungId],
    tag: str,
    *,
    before_scan: int,
) -> tuple[tuple[_CausalCheckpoint, Any, Any], ...]:
    """Reconstruct exact retained boundaries for pre-drive transition candidates."""

    invocation = state.invocation_checkpoint
    if invocation is None:
        return ()
    invocation_work = invocation.world.work
    invocation_scan = invocation_work.state.scan_id
    surfaces: list[tuple[_CausalCheckpoint, Any, Any]] = []
    for epoch, owner in invocation_work._causal_lineage.seal_through(invocation_scan):
        bounded_before = min(before_scan, invocation_scan + 1, epoch.last_scan + 1)
        for candidate_scan in _program_guard_transition_candidates(
            owner,
            rung_ids,
            tag,
            before_scan=bounded_before,
        ):
            source_scan = candidate_scan - 1
            if source_scan < 0:
                continue
            try:
                source_work = fork_with_pilot_rungs(
                    invocation_work,
                    tuple(invocation.world.pilot_rungs),
                    scan_id=source_scan,
                )
            except KeyError:
                continue
            source_key = (
                _pilot_world_key(
                    dict(source_work.state.tags),
                    state.key_config,
                    invocation.world.pilot_rungs,
                    (),
                )
                if state.key_config is not None
                else None
            )
            surfaces.append(
                (
                    _CausalCheckpoint(
                        key=source_key,
                        world=invocation.world.set(work=source_work),
                        objective=invocation.objective,
                        configured_inputs=invocation.configured_inputs,
                    ),
                    epoch,
                    owner,
                )
            )
    return tuple(surfaces)


def _program_guard_rebase_requirement(
    blocker: GuardRequirementAtom,
    parent: ActiveRequirement,
    state: _PilotState,
    ctx: _PilotContext,
) -> ActiveRequirement | None:
    """Trace one false program guard back to the nearest exact harmful writer.

    Range-compressed firing timelines only rank candidates, nearest first.
    Each candidate still needs an owner-bound scan projection, a unique
    good-to-bad write, and an executable retained checkpoint before its scan.
    """

    condition = blocker.condition
    if not isinstance(condition, Cmp):
        return None
    failed_snapshot = dict(parent.source_checkpoint.world.work.state.tags)
    if (
        constraint_holds(condition, failed_snapshot) is not False
        or condition.tag not in failed_snapshot
    ):
        return None

    writer_nodes = tuple(
        ctx.pdg.rung_nodes[index] for index in ctx.pdg.writers_of.get(condition.tag, ())
    )
    rung_ids = frozenset(RungId(node.subroutine, node.rung_index) for node in writer_nodes)
    if not rung_ids:
        return None

    failed_scan = parent.source_checkpoint.world.work.state.scan_id
    ranked: list[tuple[int, int, _CausalCheckpoint, Any, Any]] = []
    surfaces = (
        *_program_guard_rebase_surfaces(state, ctx),
        *_preinvocation_program_guard_surfaces(
            state,
            ctx,
            rung_ids,
            condition.tag,
            before_scan=failed_scan + 1,
        ),
    )
    for checkpoint, epoch, owner in surfaces:
        checkpoint_scan = checkpoint.world.work.state.scan_id
        if (
            checkpoint_scan >= failed_scan
            or constraint_holds(condition, checkpoint.world.work.state.tags) is not True
        ):
            continue
        before_scan = min(failed_scan + 1, epoch.last_scan + 1)
        for candidate_scan in _program_guard_transition_candidates(
            owner,
            rung_ids,
            condition.tag,
            before_scan=before_scan,
        ):
            if checkpoint_scan < candidate_scan:
                ranked.append((candidate_scan, checkpoint_scan, checkpoint, epoch, owner))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    seen_candidates: set[tuple[int, int, int]] = set()
    for candidate_scan, _checkpoint_scan, checkpoint, epoch, owner in ranked:
        candidate_identity = (candidate_scan, id(epoch), id(checkpoint))
        if candidate_identity in seen_candidates:
            continue
        seen_candidates.add(candidate_identity)
        projection = owner._runner()._replay_rung_write_projection_at(candidate_scan)
        if projection is None or projection.scan_id != candidate_scan:
            continue
        crossings = []
        for write in projection.writes:
            if write.transition.tag_name != condition.tag:
                continue
            before = dict(projection.entry_tags)
            before[condition.tag] = write.transition.from_value
            after = dict(before)
            after[condition.tag] = write.transition.to_value
            if (
                constraint_holds(condition, before) is True
                and constraint_holds(condition, after) is False
            ):
                crossings.append(write)
        if len(crossings) != 1:
            continue
        displacement = crossings[0]
        exact_nodes = tuple(
            node
            for node in writer_nodes
            if RungId(node.subroutine, node.rung_index) == displacement.rung_id
            and resolve_rung(ctx.program, node) is displacement.run.rung
        )
        if len(exact_nodes) != 1:
            continue
        node = exact_nodes[0]
        selected_writer = (node.subroutine, node.rung_index, node.branch_path)
        derivation = _bind_guard_derivation_authority(
            derive_overwriter_guard_requirement_from_write(
                displacement,
                projection,
                execution_epoch=epoch,
                execution_owner=owner,
                selected_writer=selected_writer,
                source_world_key=checkpoint.key,
                source_checkpoint=checkpoint,
                provenance="program-guard-rebase",
                scope=(("program_guard_rebase", condition),),
            ),
            checkpoint,
            ctx,
        )
        if derivation.requirement is not None:
            return derivation.requirement
    return None


def _repair_program_guard_from_history(
    blocker: GuardRequirementAtom,
    parent: ActiveRequirement,
    state: _PilotState,
    ctx: _PilotContext,
) -> _RequirementRepairResult:
    """Restore the nearest causal checkpoint and steer one ordinary bearing."""

    rebased = _program_guard_rebase_requirement(blocker, parent, state, ctx)
    if rebased is None:
        return _RequirementRepairResult()
    _retain_active_requirement(state, rebased)
    source = _disposable_requirement_state(state, rebased.source_checkpoint)
    schedule, detail = _compile_program_guard_schedule((rebased,), source, ctx)
    if schedule is None:
        return _RequirementRepairResult(requirement=rebased, detail=detail)
    if len(schedule.assignments) != 1:
        return _RequirementRepairResult(
            requirement=rebased,
            detail="program guard requires a compound bearing",
        )

    carried_rungs = tuple(
        rung
        for receipt in state.correction_receipts
        if receipt.status.effective
        for rung in receipt.pilot_rungs
    )
    source.pilot_rungs = _merged_pilot_rungs(carried_rungs, source.pilot_rungs)
    local_target = TargetSpec(*schedule.assignments[0])
    local_ctx = replace(ctx, target=local_target, compass=ctx.compass)
    oriented = local_ctx.compass.orient(
        OrientationWorld(
            world_key=(),
            snapshot=dict(source.work.state.tags),
            frame=None,
            state=source,
            context=local_ctx,
            key_config=source.key_config,
        ),
        local_target,
        NavigationConstraints(active_requirements=tuple(source.active_requirements)),
    )
    if not isinstance(oriented, Bearing):
        return _RequirementRepairResult(
            requirement=rebased,
            detail="program guard target has no current-world bearing",
        )
    attempt_identity = (
        "program-guard-history-rebase",
        rebased.navigation_identity,
        act_identity(oriented.act),
        schedule.identity,
    )
    if attempt_identity in state.requirement_repair_attempts:
        return _RequirementRepairResult(requirement=rebased, detail="repair already attempted")
    state.requirement_repair_attempts.add(attempt_identity)

    def attempt(candidate: _PilotState, transaction: Any) -> Any:
        transaction.register_disposable_state(candidate)
        transition = _transition_once(
            candidate,
            local_ctx,
            local_target,
            NavigationConstraints(active_requirements=tuple(candidate.active_requirements)),
            oriented=oriented,
            resolve_excursion=False,
            derive_requirements=True,
            derivation_checkpoint=rebased.source_checkpoint,
        )
        try:
            if (
                transition.trial is not None
                and constraint_holds(blocker.condition, candidate.work.state.tags) is True
            ):
                return Succeed(candidate)
            return Reject(candidate)
        finally:
            _release_attempt_projections(transition.attempt)

    composition = compose_corrections(
        source,
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
        initial_identity=attempt_identity,
        protected_states=(state,),
    )
    repaired = composition.termination.value == "success"
    if repaired:
        disposable = composition.value
        _copy_repair_knowledge(disposable, state)
        state.world = disposable.world
        state.seen_keys.update(disposable.seen_keys)
        state.journey.extend(disposable.journey)
        state.hold_log.extend(disposable.hold_log)
        state.correction_receipts.extend(disposable.correction_receipts)
        ctx.compass = local_ctx.compass
        state.checkpoints.clear()
        state.pending_departure = None
        state.recovery_continuation = None
    return _RequirementRepairResult(
        attempted=True,
        repaired=repaired,
        requirement=rebased,
        assignments=schedule.assignments,
        detail=(
            "program-owned guard rebased through retained history"
            if repaired
            else "program-guard history rebase rejected"
        ),
    )


def _repair_any_program_guard_from_history(
    requirement: ActiveRequirement,
    state: _PilotState,
    ctx: _PilotContext,
) -> _RequirementRepairResult:
    """Try every exact program-owned atom against nearest retained crossings."""

    condition = requirement.condition
    if not isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
        return _RequirementRepairResult()
    atoms = tuple(
        dict.fromkeys(atom for alternative in guard_alternatives(condition) for atom in alternative)
    )
    blocked: _RequirementRepairResult | None = None
    for atom in atoms:
        if atom.operand_authority is not OperandAuthority.PROGRAM_WRITTEN:
            continue
        repaired = _repair_program_guard_from_history(atom, requirement, state, ctx)
        if repaired.attempted:
            return repaired
        if repaired.detail and blocked is None:
            blocked = repaired
    return blocked or _RequirementRepairResult()


def _repaired_program_continuation(
    candidate: _PilotState,
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    expectation: EffectExpectation,
    *,
    execution_work: Any | None = None,
) -> int | None:
    """Prove that a corrected consumer handoff folded only target-path work.

    A pulse may satisfy its exact consumer and then ride several program-owned
    state writes before the executor returns.  In that case the trial's channel
    verification still describes a departure from the pulse's requested value,
    even though the corrected suffix is useful autonomous progress.  Re-read
    the target path at the exact consumer scan and ask ``ProgramStep`` whether
    its selected landing writer is reached by an unchanged projection.

    The landing writer must be the unique program-owned writer already selected
    on that fresh target trace.  A same-tag automatic writer outside the target
    path therefore cannot turn arbitrary drift into an accepted repair.
    """

    channel = trial.execution.channel_motion.channel_tag
    if channel is None:
        return None
    work = candidate.work if execution_work is None else execution_work
    observations = fulfilled_expectation_observations(
        expectation,
        trial.attempt.effect_observations,
    )
    handoffs = tuple(
        item
        for item in observations
        if item.obligation.tag == channel and item.appeared is not None
    )
    if len(handoffs) != 1:
        return None
    handoff = handoffs[0]
    boundary = handoff.consumer_read or handoff.appeared
    boundary_projection = handoff.execution_projection
    assert boundary is not None
    if boundary_projection is None or boundary_projection.scan_id != boundary.scan_id:
        return None
    boundary_scan = boundary.scan_id
    exact_scan_ids = trial.attempt.pulse.kernel_scan_ids
    if boundary_scan not in exact_scan_ids:
        return None

    same_scan_suffix = tuple(
        write
        for write in boundary_projection.writes
        if write.ordinal > boundary.ordinal
        and write.transition.tag_name == channel
        and write.run.enabled
    )
    boundary_operation_runs = boundary.run.rung_occurrences
    if any(
        all(write.run is not operation_run for operation_run in boundary_operation_runs)
        for write in same_scan_suffix
    ):
        # The scan-exit snapshot is a faithful consumer boundary only when any
        # later channel write belongs to that same dynamic consumer operation,
        # including its exact nested branch runs. A sibling/outer writer would
        # already have displaced the handoff before the historical fork on
        # which ProgramStep projects.
        return None

    try:
        handoff_work = fork_with_pilot_rungs(
            work,
            candidate.pilot_rungs,
            scan_id=boundary_scan,
        )
    except KeyError:
        return None

    handoff_snap = dict(handoff_work.state.tags)
    landing_value = work.state.tags.get(channel)
    if _values_match(handoff_snap.get(channel), landing_value):
        return None

    probe = _disposable_requirement_state(
        candidate,
        _CausalCheckpoint(
            key=None,
            world=candidate.world.set(work=handoff_work),
            objective=trial.attempt.bearing.objective,
            configured_inputs=ctx.configured_inputs,
        ),
    )
    reading = ctx.compass.orient(
        OrientationWorld(
            world_key=(),
            snapshot=handoff_snap,
            frame=None,
            state=probe,
            context=ctx,
            key_config=probe.key_config,
        ),
        ctx.target,
        NavigationConstraints(active_requirements=tuple(probe.active_requirements)),
    )
    orientation = reading.orientation
    if orientation is None or orientation.world.frame is None:
        return None
    landing_writers = {
        node.writer_rung
        for node in orientation.world.frame.tree.iter_nodes()
        if node.tag == channel
        and _values_match(node.value, landing_value)
        and node.writer_rung is not None
    }
    if len(landing_writers) != 1:
        return None
    landing_writer = next(iter(landing_writers))
    selected_rung = resolve_rung(ctx.program, ctx.pdg.rung_nodes[landing_writer])
    if selected_rung is None:
        return None

    later_writes = list(same_scan_suffix)
    relevant_projections = [boundary_projection]
    for scan_id in exact_scan_ids:
        if scan_id <= boundary_scan or scan_id > work.state.scan_id:
            continue
        projection = work._replay_rung_write_projection_at(scan_id)
        if projection is None:
            return None
        relevant_projections.append(projection)
        later_writes.extend(
            write
            for write in projection.writes
            if write.transition.tag_name == channel and write.run.enabled
        )
    landing_occurrences = tuple(
        write for write in later_writes if _values_match(write.transition.to_value, landing_value)
    )
    if not landing_occurrences:
        return None

    # The whole observed suffix must belong to one retained causal epoch. Scan
    # numbers alone are not occurrence ownership: a fork can reuse them under
    # another execution epoch.
    suffix_owner = work._causal_lineage.owner_at(boundary_scan)
    if suffix_owner is None or any(
        work._causal_lineage.owner_at(projection.scan_id) is not suffix_owner
        for projection in relevant_projections
    ):
        return None

    def dynamic_invocations(projection: Any) -> frozenset[int | None]:
        return frozenset(
            occurrence.call_invocation
            for occurrence in (*projection.reads, *projection.writes)
            if occurrence.run.rung is selected_rung
        )

    if any(len(dynamic_invocations(projection)) > 1 for projection in relevant_projections):
        return None

    world = WorldView(
        snapshot=handoff_snap,
        pdg=ctx.pdg,
        program=ctx.program,
        steerable=ctx.steerable,
        opaque_loop=ctx.opaque_loop,
        prior=ctx.domain_prior,
        clear_only=ctx.clear_only,
        pipeline_internal_tags=ctx.pipeline_internal_tags,
        pipeline_roles=ctx.pipeline_roles,
        avoid_pred=ctx.avoid_pred,
        harness=getattr(handoff_work, "_harness", None),
    )
    family = sibling_producer_family(world, channel, landing_value)
    producers = (
        tuple(
            producer for producer in family.program_owned if producer.rung_index == landing_writer
        )
        if family is not None
        else ()
    )
    if len(producers) != 1:
        return None
    step = read_program_step(
        world,
        producers[0],
        handoff_work,
        candidate.pilot_rungs,
        resting=ctx.resting,
        projection_scans=4,
    )
    motion = step.observable_motion(channel)
    if not (
        step.status is ProgramStepStatus.KEEP_RUNNING
        and motion is not None
        and _values_match(motion.before_value, handoff_snap.get(channel))
        and _values_match(motion.target_value, landing_value)
    ):
        return None

    # Reproduce ProgramStep's bounded unchanged projection and join its one
    # selected producer occurrence to the historical landing winner by full
    # dynamic address. Static rung identity alone aliases repeated subroutine
    # calls and loop iterations.
    projected_work = fork_with_pilot_rungs(handoff_work, candidate.pilot_rungs)
    projected_occurrences = []
    for _ in range(4):
        projected_work.step()
        projection = projected_work._replay_rung_write_projection_at(projected_work.state.scan_id)
        if projection is None or len(dynamic_invocations(projection)) > 1:
            return None
        projected_occurrences.extend(
            write
            for write in projection.writes
            if write.run.rung is selected_rung
            and write.transition.tag_name == channel
            and _values_match(write.transition.to_value, landing_value)
            and write.run.enabled
        )
        if _values_match(projected_work.state.tags.get(channel), landing_value):
            break
    if len(projected_occurrences) != 1:
        return None
    projected_occurrence = projected_occurrences[0]

    def dynamic_address(write: Any) -> tuple[Any, ...]:
        return (
            write.scan_id,
            write.ordinal,
            write.run_order,
            write.call_invocation,
            write.rung_id,
            write.run.kind,
            write.run.caller_rung,
            write.run.call_stack,
        )

    historical_matches = tuple(
        occurrence
        for occurrence in landing_occurrences
        if dynamic_address(occurrence) == dynamic_address(projected_occurrence)
    )
    if len(historical_matches) != 1:
        return None
    landing_occurrence = historical_matches[0]
    selected_node = ctx.pdg.rung_nodes[landing_writer]
    capture_indices = ctx.pdg.timeline_capture_indices_for_node(landing_writer)
    if selected_node.subroutine is not None:
        if len(capture_indices) != 1:
            return None
        if landing_occurrence.run.caller_rung != next(iter(capture_indices)):
            return None
    if any(
        write.scan_id == landing_occurrence.scan_id
        and write.ordinal > landing_occurrence.ordinal
        and write.transition.tag_name == channel
        and write.run.enabled
        for write in later_writes
    ):
        # The suffix boundary is a historical scan exit. A later same-scan
        # channel write would make that boundary unobservable.
        return None
    return landing_occurrence.scan_id


def _promoted_target_suffix_observation(
    expectation: EffectExpectation,
    pulse: Any,
    checkpoint_scan: int,
) -> Any:
    """Promote an exact zero-net target loss after one proven checkpoint."""

    exact_suffix = tuple(
        scan_id
        for scan_id in pulse.kernel_scan_ids
        if checkpoint_scan < scan_id <= pulse.fork.state.scan_id
    )
    if not exact_suffix:
        return None
    boundary_projection = pulse.projection_at(exact_suffix[0])
    if boundary_projection is None:
        return None
    terminal = expectation.obligations[0]
    observations = observe_execution_window(
        expectation,
        pulse.fork,
        scan_before=checkpoint_scan,
        action_scan=None,
        coast_receipt=pulse.coast_receipt,
        kernel_scan_ids=exact_suffix,
        projection_at=pulse.projection_at,
    )
    return promote_terminal_target_observation(
        observations,
        window_entry_value=boundary_projection.entry_tags.get(terminal.tag),
        final_landing_value=pulse.fork.state.tags.get(terminal.tag),
    )


def _continuation_source_checkpoint(
    state: _PilotState,
    continuation: _RecoveryContinuation,
) -> _CausalCheckpoint | None:
    """Resolve one retained causal source without storing it in the stream."""

    matches = tuple(
        requirement.source_checkpoint
        for requirement in state.active_requirements
        if requirement.checkpoint_owner is continuation.checkpoint_owner
        and requirement.source_world_key == continuation.source_world_key
    )
    unique = {id(checkpoint): checkpoint for checkpoint in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _adjacent_continuation_source(
    state: _PilotState,
    pulse: Any,
    prefix_proof: _ContinuationCheckpoint | None = None,
) -> _CausalCheckpoint | None:
    """Return source authority only for one contiguous certified exact window."""

    continuation = state.recovery_continuation
    if continuation is None:
        return None
    tip = continuation.tip
    assert state.key_config is not None
    current_key = _pilot_world_key(
        dict(state.work.state.tags),
        state.key_config,
        state.pilot_rungs,
        state.active_requirements,
    )
    ephemeral_prefix = bool(
        prefix_proof is not None
        and prefix_proof.kind == "target_prefix"
        and prefix_proof.scan_id == tip.scan_id
        and prefix_proof.world_key == tip.world_key
        and prefix_proof.execution_epoch is tip.execution_epoch
        and prefix_proof.execution_owner is tip.execution_owner
        and prefix_proof.landing_occurrence is not None
    )
    pulse_epoch_owner = _execution_epoch_owner(pulse.fork, pulse.scan_before)
    exact_scan_ids = tuple(range(pulse.scan_before + 1, pulse.fork.state.scan_id + 1))
    if (
        not (tip.program_step_certified or ephemeral_prefix)
        or tip.scan_id != pulse.scan_before
        or tip.world_key != current_key
        or pulse_epoch_owner is None
        or pulse_epoch_owner[0] is not tip.execution_epoch
        or pulse_epoch_owner[1] is not tip.execution_owner
        or pulse.kernel_scan_ids != exact_scan_ids
        or any(pulse.projection_at(scan_id) is None for scan_id in exact_scan_ids)
    ):
        return None
    receipt = pulse.coast_receipt
    if receipt is not None and (
        receipt.macro_folds
        or receipt.advances
        or receipt.timer_quanta_replayed
        or receipt.skipped_scans
    ):
        return None
    return _continuation_source_checkpoint(state, continuation)


def _exact_local_repair_window(
    checkpoint: _CausalCheckpoint | None,
    pulse: Any,
) -> bool:
    """Whether one rejected retry retains its whole exact source window."""

    if (
        checkpoint is None
        or checkpoint.world.work.state.scan_id != pulse.scan_before
        or pulse.kernel_scan_ids
        != tuple(range(pulse.scan_before + 1, pulse.fork.state.scan_id + 1))
        or any(pulse.projection_at(scan_id) is None for scan_id in pulse.kernel_scan_ids)
    ):
        return False
    receipt = pulse.coast_receipt
    return receipt is None or not (
        receipt.macro_folds
        or receipt.advances
        or receipt.timer_quanta_replayed
        or receipt.skipped_scans
    )


def _selected_program_step(trial: _AcceptedTrial) -> Any | None:
    orientation = trial.attempt.bearing.orientation
    candidates = orientation.candidates if orientation is not None else None
    wait = candidates.wait if candidates is not None else None
    return wait.program_step if wait is not None else None


def _recovery_anchor_program_step(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    target: TargetSpec,
) -> Any | None:
    """Freshly prove that a repaired anchor should keep running unchanged."""

    continuation = state.recovery_continuation
    if continuation is None or continuation.tip.scan_id != state.work.state.scan_id:
        return None
    if continuation.tip.world_key != frame.key:
        return None
    owned = _execution_epoch_owner(state.work, state.work.state.scan_id)
    if (
        owned is None
        or owned[0] is not continuation.tip.execution_epoch
        or owned[1] is not continuation.tip.execution_owner
    ):
        return None
    expectation = _selected_terminal_target_expectation(frame, target, ctx)
    if expectation is None:
        return None
    terminal = expectation.obligations[0]
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
    family = sibling_producer_family(world, terminal.tag, terminal.value)

    def producer_address(producer: Any) -> tuple[Any, ...]:
        node = ctx.pdg.rung_nodes[producer.rung_index]
        return (node.subroutine, node.rung_index, node.branch_path)

    producers = (
        tuple(
            producer
            for producer in family.program_owned
            if producer_address(producer) == terminal.producer
        )
        if family is not None
        else ()
    )
    if len(producers) != 1:
        return None
    step = read_program_step(
        world,
        producers[0],
        state.work,
        state.pilot_rungs,
        resting=ctx.resting,
    )
    if (
        step.status is not ProgramStepStatus.KEEP_RUNNING
        or step.required_inputs
        or step.context_actions
        or step.observable_motion() is None
    ):
        return None
    return step


def _preempt_recovery_action_with_program_coast(
    result: OrientationResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    target: TargetSpec,
) -> tuple[OrientationResult, Any | None]:
    """Prefer a proved unchanged program hop over a harmful fresh action."""

    if not isinstance(result, Bearing):
        return result, None
    step = _recovery_anchor_program_step(frame, state, ctx, target)
    if step is None or isinstance(result.act, Coast):
        return result, step
    expectation = _selected_terminal_target_expectation(frame, target, ctx)
    assert expectation is not None
    motion = step.observable_motion()
    assert motion is not None
    act = Coast(
        "bearing",
        replace(
            result.act.policy,
            source=ActSource.PROGRAM,
            action_pairs=(),
            applied=(),
            nogood_pair=None,
            heading=ChannelHeading(
                motion.channel_tag,
                motion.target_value,
                boundary=step.boundary,
            ),
            motion=MotionKind.COAST_TO_BEARING,
            expectation=expectation,
            expectation_exemption=None,
            provenance=(*result.act.policy.provenance, "recovery ProgramStep keep-running"),
        ),
    )
    orientation = (
        replace(result.orientation, selected_bearing_id=repr(act_identity(act)))
        if result.orientation is not None
        else None
    )
    return (
        replace(
            result,
            act=act,
            rationale=step.reason,
            orientation=orientation,
        ),
        step,
    )


def _execution_epoch_owner(work: Any, scan_id: int) -> tuple[Any, Any] | None:
    matches = tuple(
        (epoch, owner)
        for epoch, owner in work._causal_lineage.seal_through(scan_id)
        if epoch.first_scan <= scan_id <= epoch.last_scan
    )
    return matches[0] if len(matches) == 1 else None


def _advance_recovery_continuation(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    program_step: Any | None = None,
) -> bool:
    """Append one freshly certified checkpoint for the committed recovery hop."""

    continuation = state.recovery_continuation
    if continuation is None:
        return False
    pulse = trial.attempt.pulse
    exact_window = (
        continuation.tip.scan_id == pulse.scan_before
        and continuation.tip.world_key == frame.key
        and pulse.kernel_scan_ids
        == tuple(range(pulse.scan_before + 1, pulse.fork.state.scan_id + 1))
    )
    receipt = pulse.coast_receipt
    exact_window = exact_window and not (
        receipt is not None
        and (
            receipt.macro_folds
            or receipt.advances
            or receipt.timer_quanta_replayed
            or receipt.skipped_scans
        )
    )
    certified = False
    motion = None
    if exact_window and isinstance(trial.attempt.bearing.act, Coast):
        step = program_step or _selected_program_step(trial)
        physical_channel = trial.execution.channel_motion.channel_tag
        motion = step.observable_motion(physical_channel) if step is not None else None
        certified = bool(
            step is not None
            and step.status is ProgramStepStatus.KEEP_RUNNING
            and motion is not None
            and _values_match(
                motion.before_value,
                trial.execution.before_snap.get(motion.channel_tag),
            )
            and _values_match(
                motion.target_value,
                trial.execution.after_snap.get(motion.channel_tag),
            )
        )
    # Program-input handoffs need the typed NEEDS_INPUT/action/boundary join;
    # a resting edge and a survived expectation alone are not authority.
    selected_release = False
    if not certified and not selected_release:
        state.recovery_continuation = None
        return False
    epoch_owner = _execution_epoch_owner(pulse.fork, state.work.state.scan_id)
    projections = tuple(pulse.projection_at(scan_id) for scan_id in pulse.kernel_scan_ids)
    if epoch_owner is None or any(projection is None for projection in projections):
        state.recovery_continuation = None
        return False
    landing_occurrence = None
    if certified and motion is not None:
        exact_projections = tuple(
            projection for projection in projections if projection is not None
        )
        landing = exact_last_landing_write(
            exact_projections,
            after=None,
            tag=motion.channel_tag,
            target_value=motion.before_value,
            landing_value=motion.target_value,
        )
        if landing is None:
            state.recovery_continuation = None
            return False
        landing_occurrence = occurrence_snapshot(landing[1])
    assert state.key_config is not None
    key = _pilot_world_key(
        dict(state.work.state.tags),
        state.key_config,
        state.pilot_rungs,
        state.active_requirements,
    )
    state.recovery_continuation = replace(
        continuation,
        checkpoints=(
            *continuation.checkpoints,
            _ContinuationCheckpoint(
                scan_id=state.work.state.scan_id,
                world_key=key,
                kind="unchanged_coast" if certified else "program_input_handoff",
                execution_epoch=epoch_owner[0],
                execution_owner=epoch_owner[1],
                landing_occurrence=landing_occurrence,
            ),
        ),
    )
    return True


def _repair_failed_requirement(
    requirement: ActiveRequirement,
    state: _PilotState,
    ctx: _PilotContext,
) -> _RequirementRepairResult:
    receipt = _exact_failed_source(requirement, state)
    if receipt is None:
        return _RequirementRepairResult()
    requirements = _source_requirements(requirement, state)
    source = _disposable_requirement_state(state, requirement.source_checkpoint)
    schedule, detail = _compile_source_schedule(requirements, source, ctx)
    if detail:
        return _RequirementRepairResult(requirement=requirement, detail=detail)
    if schedule is not None:
        source.pilot_rungs = _merged_pilot_rungs(schedule.pilot_rungs, source.pilot_rungs)
    history_rebase = _repair_any_program_guard_from_history(requirement, state, ctx)
    if history_rebase.attempted or history_rebase.detail:
        return history_rebase
    local_bearing = receipt.local_bearing
    local_act = receipt.local_act
    if isinstance(
        requirement.condition, GuardRequirementAtom | GuardRequirementExpr
    ) and isinstance(local_act, Coast):
        origin = _continuation_origin_receipt(requirement, state)
        if origin is None or receipt.expectation is None:
            return _RequirementRepairResult(
                requirement=requirement,
                detail="guard continuation has no unique accepted source transaction",
            )
        origin_obligations = (
            origin.expectation.obligations if origin.expectation is not None else ()
        )
        obligations = (*origin_obligations, *receipt.expectation.obligations)
        expectation = EffectExpectation(tuple(dict.fromkeys(obligations)))
        local_act = replace(
            origin.local_act,
            policy=replace(
                origin.local_act.policy,
                expectation=expectation,
                expectation_exemption=None,
            ),
        )
        local_bearing = replace(origin.local_bearing, act=local_act)
    local_act = _nested_guard_act(
        source,
        ctx,
        local_bearing,
        local_act,
        requirements,
    )
    if local_act is None:
        blocker = _mandatory_guard_blocker(requirements, source.work.state.tags)
        if blocker is not None:
            rebased = _repair_program_guard_from_history(
                blocker,
                requirement,
                state,
                ctx,
            )
            if rebased.attempted or rebased.detail:
                return rebased
            return _RequirementRepairResult(
                declined=True,
                requirement=requirement,
                detail=_mandatory_guard_decline_reason(
                    blocker,
                    source.work.state.tags,
                    ctx.target,
                ),
            )
        return _RequirementRepairResult(requirement=requirement, detail="guard repair is ambiguous")
    bearing = _rebound_bearing(source, ctx, local_bearing, local_act)
    if bearing is None:
        return _RequirementRepairResult(
            requirement=requirement, detail="causal source cannot be reread"
        )
    assignment_identity = schedule.identity if schedule is not None else ()
    attempt_identity = (
        "failed-effect",
        requirement.source_world_key,
        requirement.checkpoint_owner,
        receipt.act_identity,
        act_identity(local_act),
        assignment_identity,
        tuple(current.identity for current in requirements),
    )
    if attempt_identity in state.requirement_repair_attempts:
        return _RequirementRepairResult(requirement=requirement, detail="repair already attempted")
    state.requirement_repair_attempts.add(attempt_identity)
    disposable_ctx = replace(ctx, compass=ctx.compass)
    rejection_reasons: list[str] = []

    def attempt(candidate: _PilotState, transaction: Any) -> Any:
        transaction.register_disposable_state(candidate)
        transition = _transition_once(
            candidate,
            disposable_ctx,
            ctx.target,
            NavigationConstraints(active_requirements=tuple(candidate.active_requirements)),
            oriented=bearing,
            resolve_excursion=False,
            derive_requirements=True,
            derivation_checkpoint=requirement.source_checkpoint,
        )
        try:
            executed = (
                transition.attempt.executed_attempt if transition.attempt is not None else None
            )
            expectation = local_act.policy.expectation
            target_reached = transition.trial is not None and isinstance(
                transition.trial.verification, TargetReached
            )
            if (
                transition.trial is None
                or executed is None
                or (expectation is None and not target_reached)
                or (
                    expectation is not None
                    and not _whole_expectation_survived(executed, expectation)
                )
            ):
                rejection_reasons.append("local expectation did not survive")
                return Reject(candidate)
            requirements_before_monitor = tuple(
                current.identity for current in candidate.active_requirements
            )
            failed_before_monitor = tuple(
                current.identity for current in candidate.failed_effect_receipts
            )
            landing_scan = candidate.work.state.scan_id
            landing_snap = dict(candidate.work.state.tags)
            # A locally repaired obligation is only the first acceptance boundary.
            # An exact target-path ProgramStep may prove that the executor merely
            # folded autonomous continuation after that boundary. Otherwise feed
            # the landing through ordinary post-commit trend/departure ownership: a
            # successive hazard can then restore this source and add a stronger
            # requirement, while unresolved departures remain unadopted.
            if not target_reached:
                assert expectation is not None
                autonomous_checkpoint = _repaired_program_continuation(
                    candidate,
                    disposable_ctx,
                    transition.trial,
                    expectation,
                )
                if autonomous_checkpoint is None:
                    tuple(
                        _monitor_trend(
                            transition.trial,
                            transition.frame,
                            candidate,
                            disposable_ctx,
                        )
                    )
            learned_requirement = requirements_before_monitor != tuple(
                current.identity for current in candidate.active_requirements
            ) or failed_before_monitor != tuple(
                current.identity for current in candidate.failed_effect_receipts
            )
            if learned_requirement or candidate.pending_departure is not None:
                rejection_reasons.append(
                    "monitor learned another requirement"
                    if learned_requirement
                    else "monitor retained an unresolved departure"
                )
                return Reject(candidate)
            if (
                candidate.work.state.scan_id != landing_scan
                or dict(candidate.work.state.tags) != landing_snap
            ):
                rejection_reasons.append(
                    "monitor changed the accepted landing "
                    f"to {ctx.target.tag}={candidate.work.state.tags.get(ctx.target.tag)!r}"
                )
                return Reject(candidate)
            assert candidate.key_config is not None
            epoch_owner = _execution_epoch_owner(
                candidate.work,
                candidate.work.state.scan_id,
            )
            if epoch_owner is None:
                rejection_reasons.append("locally repaired checkpoint has no exact execution owner")
                return Reject(candidate)
            seed = _ContinuationCheckpoint(
                scan_id=candidate.work.state.scan_id,
                world_key=_pilot_world_key(
                    dict(candidate.work.state.tags),
                    candidate.key_config,
                    candidate.pilot_rungs,
                    candidate.active_requirements,
                ),
                kind="local_repair",
                execution_epoch=epoch_owner[0],
                execution_owner=epoch_owner[1],
            )
            candidate.recovery_continuation = _RecoveryContinuation(
                checkpoint_owner=requirement.checkpoint_owner,
                source_world_key=requirement.source_world_key,
                checkpoints=(seed,),
            )
            return Succeed(candidate)
        finally:
            _release_attempt_projections(transition.attempt)

    composition = compose_corrections(
        source,
        budget=CompositionBudget(1),
        attempt=attempt,
        budget_exhausted=lambda candidate: candidate,
        initial_identity=attempt_identity,
        protected_states=(state,),
    )
    disposable = composition.value
    changed = _copy_repair_knowledge(disposable, state)
    repaired = composition.termination.value == "success"
    if repaired:
        if schedule is not None:
            _record_requirement_correction(
                disposable,
                schedule,
                scan=requirement.source_checkpoint.world.work.state.scan_id,
            )
        state.world = disposable.world
        state.seen_keys.update(disposable.seen_keys)
        state.journey.extend(disposable.journey)
        state.hold_log.extend(disposable.hold_log)
        state.correction_receipts.extend(disposable.correction_receipts)
        state.requirement_repair_attempts.update(disposable.requirement_repair_attempts)
        state.recovery_continuation = disposable.recovery_continuation
        state.checkpoints.clear()
        state.pending_departure = None
    return _RequirementRepairResult(
        attempted=True,
        repaired=repaired,
        knowledge_changed=changed,
        requirement=requirement,
        assignments=schedule.assignments if schedule is not None else (),
        detail=(
            "local transaction repaired"
            if repaired
            else (
                f"local transaction rejected: {rejection_reasons[-1]}"
                if rejection_reasons
                else "local transaction rejected"
            )
        ),
    )


def _repair_one_active_requirement(
    state: _PilotState,
    ctx: _PilotContext,
) -> _RequirementRepairResult:
    """Attempt one newest exact schedule, then return to the outer fresh read."""

    blocked: _RequirementRepairResult | None = None
    ordered = sorted(
        enumerate(state.active_requirements),
        key=lambda item: (
            item[1].deadline.scan_id,
            item[1].deadline.ordinal,
            item[0],
        ),
        reverse=True,
    )
    for _index, requirement in ordered:
        if (
            requirement.status is not RequirementStatus.ACTIVE
            or requirement.phase is not RequirementPhase.STEADY
        ):
            continue
        bootstrap = _repair_bootstrap_requirement(requirement, state, ctx)
        if bootstrap.attempted:
            return bootstrap
        if bootstrap.detail and blocked is None:
            blocked = bootstrap
        failed = _repair_failed_requirement(requirement, state, ctx)
        if failed.attempted:
            return failed
        if failed.detail and blocked is None:
            blocked = failed
    # A newest authoritative/ambiguous guard is mandatory but not executable.
    # Keep its diagnostic while allowing an earlier exact adjustable
    # requirement to run; otherwise the unrepairable newest member starves the
    # correction which can make the whole same-source schedule progress.
    return blocked or _RequirementRepairResult()


def _execute_bootstrap_scan(state: _PilotState, ctx: _PilotContext) -> _BootstrapExecution | None:
    """Retain boundary 0 and execute one observed program-owned scan.

    The old cold-start settle advanced the live runner directly.  This adapter
    deliberately preserves that landing and search-budget behavior while
    making the execution truth addressable.  Its pre-scan trace supplies only
    conservative designations; missing designations are not failed promises,
    and factual appeared-effect classification does not alter orchestration.
    """

    if state.work.state.scan_id != 0:
        return None

    before_snap = dict(state.work.state.tags)
    checkpoint = state.invocation_checkpoint
    if checkpoint is None:
        raise RuntimeError("bootstrap scan has no retained invocation checkpoint")
    scan_before = state.work.state.scan_id

    # Designation is derived before execution from the retained source world.
    # It is best-effort evidence: an unsupported or ambiguous read must preserve
    # the established scan-1 landing and leave ordinary Orientation to name its
    # frontier.
    designations = ()
    if ctx.target.predicate is None:
        try:
            read = TraceReadConstraints.from_context(
                ctx,
                state.work,
                route=ctx.route,
                avoid_pred=ctx.avoid_pred,
            )
            tree = trace_back(
                ctx.target.tag,
                ctx.target.value,
                before_snap,
                ctx.pdg,
                ctx.program,
                ctx.steerable,
                constraints=read,
            )
            channel_tags = frozenset(ctx.opaque_loop) | frozenset(
                role.channel_tag for role in ctx.pipeline_roles
            )
            designations = bootstrap_designations(
                tree,
                ctx.pdg,
                ctx.program,
                steerable=ctx.steerable,
                channel_tags=channel_tags,
            )
        except Exception:  # noqa: BLE001 - designation is conservative evidence only
            logger.debug("pilot: bootstrap designation failed closed", exc_info=True)

    # Exactly the normal program scan previously used for the hidden settle:
    # no Pulse, Coast, temporary rung, or operator patch participates.
    state.work.step()
    scan_after = state.work.state.scan_id
    projection = state.work._replay_rung_write_projection_at(scan_after)
    if projection is None:
        raise RuntimeError("bootstrap scan has no exact execution projection")
    execution_epoch_pair = next(
        (
            (epoch, owner)
            for epoch, owner in state.work._causal_lineage.seal_through(scan_after)
            if epoch.first_scan <= scan_after <= epoch.last_scan
        ),
        None,
    )
    if execution_epoch_pair is None:
        raise RuntimeError("bootstrap scan has no retained execution epoch")
    execution_epoch, execution_owner = execution_epoch_pair
    appeared_effects = observe_bootstrap_effects(designations, projection)

    receipt = _BootstrapExecution(
        checkpoint=checkpoint,
        scan_before=scan_before,
        scan_after=scan_after,
        projection=projection,
        landing=projection.exit_tags,
        designations=designations,
        appeared_effects=appeared_effects,
        execution_epoch=execution_epoch,
        execution_owner=execution_owner,
    )
    state.bootstrap_execution = receipt
    _derive_bootstrap_requirements(state, ctx, receipt)
    return receipt


@dataclass(frozen=True)
class _DriveSetup:
    """Static/runtime preparation shared by every target driven on one PLC."""

    work: PLC
    program: Any
    pdg: ProgramGraph
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    anchor_scan: int
    diag_snapshot: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    stateful_domains: dict[str, tuple[Any, ...]] | None
    key_config: _StateKeyConfig | None
    evidence: TransitionEvidence | None
    compass: Compass
    opaque_loop: frozenset[str]
    configured_inputs: frozenset[str]


@dataclass(frozen=True)
class _DriveOutcome:
    """Named result assembled from the terminal event of one drive loop."""

    reached: bool
    work: PLC
    journal: tuple[PlanStep, ...]
    journey: tuple[_Step, ...]
    reason: str | None
    knowledge: dict[str, Any]
    root_route: TraceChoice | None


@dataclass(frozen=True)
class _IterationTransition:
    """One current-world orientation and its locally adopted trial result.

    The supplied state/context are the transaction boundary: a live caller
    keeps the effects, while a bounded investigation passes disposable clones.
    Post-commit progress policy, probing, event emission, and repetition remain
    outside this non-looping seam.
    """

    result: OrientationResult
    frame: _IterationFrame
    attempt: _AttemptResult | None = None
    trial: _AcceptedTrial | None = None
    continuation_hop: bool = False
    shadow_observation: _ShadowTheoryTransition | None = None


@dataclass(frozen=True)
class _ShadowTheoryTransition:
    """Detached shadow evidence returned without reducing lifecycle state."""

    claim: TheoryClaim
    source: TheoryBoundaryIdentity
    execution_owner_token: tuple[Any, ...]
    occurrence_evidence: tuple[Any, ...]
    act_identity: tuple[Any, ...]
    pilot_rung_identities: tuple[tuple[Any, ...], ...]
    disposition: TheoryAttemptDisposition
    evidence: tuple[Any, ...]
    requirements: tuple[TheoryRequirementSnapshot, ...]
    adopted_boundary: TheoryBoundaryIdentity | None = None

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            "observed-transition",
            self.claim.identity,
            self.source,
            self.execution_owner_token,
            self.occurrence_evidence,
            self.act_identity,
            self.pilot_rung_identities,
            self.disposition,
            self.evidence,
        )


def _theory_occurrence_identity(occurrence: Any) -> tuple[Any, ...]:
    return (
        occurrence.kind,
        occurrence.tag,
        occurrence.scan_id,
        occurrence.dynamic_address,
        _semantic_key(occurrence.values),
        occurrence.enabled,
    )


def _theory_boundary_from_checkpoint(checkpoint: _CausalCheckpoint) -> TheoryBoundaryIdentity:
    if checkpoint.key is None:
        raise ValueError("shadow theory requires an exact checkpoint world key")
    scan_id = checkpoint.world.work.state.scan_id
    epoch_owner = _execution_epoch_owner(checkpoint.world.work, scan_id)
    world_key = tuple(checkpoint.key)
    if epoch_owner is None:
        # Boundary zero precedes the first execution epoch. Its retained
        # checkpoint owner is the exact source identity; the subsequent attempt
        # separately carries the owner of scan 1.
        return TheoryBoundaryIdentity(
            world_key=world_key,
            scan_id=scan_id,
            checkpoint_token=("checkpoint-owner", id(checkpoint.owner), world_key, scan_id),
        )
    owner_token = ("execution-owner", id(epoch_owner[0]), id(epoch_owner[1]))
    return TheoryBoundaryIdentity(
        world_key=world_key,
        scan_id=scan_id,
        checkpoint_token=("execution-boundary", world_key, scan_id, owner_token),
        execution_owner_token=owner_token,
    )


def _theory_live_boundary(state: _PilotState) -> TheoryBoundaryIdentity:
    snapshot = dict(state.work.state.tags)
    key = (
        _pilot_world_key(
            snapshot,
            state.key_config,
            state.pilot_rungs,
            state.active_requirements,
        )
        if state.key_config is not None
        else ()
    )
    scan_id = state.work.state.scan_id
    epoch_owner = _execution_epoch_owner(state.work, scan_id)
    if epoch_owner is None:
        raise ValueError("shadow theory requires one exact live execution owner")
    owner_token = ("execution-owner", id(epoch_owner[0]), id(epoch_owner[1]))
    world_key = tuple(key)
    return TheoryBoundaryIdentity(
        world_key=world_key,
        scan_id=scan_id,
        checkpoint_token=(
            "execution-boundary",
            world_key,
            scan_id,
            owner_token,
        ),
        execution_owner_token=owner_token,
    )


def _theory_objective_snapshot(objective: BearingObjective) -> TheoryObjectiveSnapshot:
    target = objective.target
    predicate = _semantic_key(target.predicate) if target.predicate is not None else None
    return TheoryObjectiveSnapshot(
        target_tag=target.tag,
        target_value=_semantic_key(target.value),
        predicate_identity=(predicate if isinstance(predicate, tuple) else (predicate,))
        if predicate is not None
        else None,
        frontier=tuple((tag, _semantic_key(value)) for tag, value in objective.frontier),
    )


def _theory_obligation_snapshot(obligation: Any) -> TheoryObligationSnapshot:
    selector = _semantic_key(getattr(obligation, "occurrence_selector", None))
    return TheoryObligationSnapshot(
        tag=obligation.tag,
        value=_semantic_key(obligation.value),
        producer=tuple(obligation.producer),
        consumer=tuple(obligation.consumer) if obligation.consumer is not None else None,
        required_shape=tuple(
            (tag, _semantic_key(value)) for tag, value in obligation.required_shape
        ),
        boundary=(
            (obligation.boundary[0], _semantic_key(obligation.boundary[1]))
            if obligation.boundary is not None
            else None
        ),
        terminal_target=obligation.terminal_target,
        polarity=str(getattr(obligation, "polarity", "produce")),
        occurrence_selector=(selector if isinstance(selector, tuple) else (selector,))
        if getattr(obligation, "occurrence_selector", None) is not None
        else None,
    )


def _theory_requirement_snapshot(requirement: ActiveRequirement) -> TheoryRequirementSnapshot:
    diagnostic = requirement.diagnostic_snapshot()
    condition = _semantic_key(diagnostic.condition)
    selected_writer = _semantic_key(diagnostic.selected_writer)
    scope = _semantic_key(diagnostic.scope)
    condition_identity = condition if isinstance(condition, tuple) else (condition,)
    selected_writer_identity = (
        selected_writer if isinstance(selected_writer, tuple) else (selected_writer,)
    )
    scope_identity = scope if isinstance(scope, tuple) else (scope,)
    source_world_key = _semantic_key(diagnostic.source_world_key)
    source_world_identity = (
        source_world_key if isinstance(source_world_key, tuple) else (source_world_key,)
    )
    semantic_identity = (
        "requirement",
        condition_identity,
        _theory_occurrence_identity(diagnostic.demanding_occurrence),
        _theory_occurrence_identity(diagnostic.deadline),
        selected_writer_identity,
        diagnostic.operand_authority.value,
        source_world_identity,
        diagnostic.causal_identity,
        diagnostic.phase.value,
        diagnostic.status.value,
        diagnostic.provenance,
        scope_identity,
    )
    return TheoryRequirementSnapshot(
        semantic_identity=semantic_identity,
        condition_identity=condition_identity,
        demanding_occurrence=_theory_occurrence_identity(diagnostic.demanding_occurrence),
        deadline_occurrence=_theory_occurrence_identity(diagnostic.deadline),
        selected_writer=selected_writer_identity,
        operand_authority=diagnostic.operand_authority.value,
        source_world_key=source_world_identity,
        source_scan=diagnostic.source_scan,
        checkpoint_token=("checkpoint-owner", diagnostic.causal_identity[2]),
        execution_owner_token=(
            "execution-owner",
            diagnostic.causal_identity[0],
            diagnostic.causal_identity[1],
        ),
        phase=diagnostic.phase.value,
        status=diagnostic.status.value,
        provenance=diagnostic.provenance,
        scope=scope_identity,
    )


def _shadow_claim(
    expectation: EffectExpectation,
    objective: BearingObjective,
    source: TheoryBoundaryIdentity,
) -> TheoryClaim:
    obligations = tuple(
        _theory_obligation_snapshot(obligation) for obligation in expectation.obligations
    )
    return TheoryClaim(
        source=source,
        objective=_theory_objective_snapshot(objective),
        obligations=obligations,
        selected_boundary=replace(
            source,
            occurrence_identity=(
                "selected-boundary",
                tuple(
                    (
                        item.producer,
                        item.consumer,
                        item.boundary,
                        item.polarity,
                        item.occurrence_selector,
                    )
                    for item in obligations
                ),
            ),
        ),
    )


def _shadow_execution_evidence(execution: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Detach one exact attempt owner and its dynamic occurrence evidence."""

    observations = execution.effect_observations
    owner_pairs = {
        (id(observation.execution_epoch), id(observation.execution_owner))
        for observation in observations
        if observation.execution_epoch is not None and observation.execution_owner is not None
    }
    if len(observations) == 0 or len(owner_pairs) != 1:
        raise ValueError("shadow attempt requires one exact execution owner")
    epoch_id, owner_id = next(iter(owner_pairs))
    occurrence_evidence = tuple(
        _semantic_key(observation.diagnostic_snapshot()) for observation in observations
    )
    return ("execution-owner", epoch_id, owner_id), occurrence_evidence


def _shadow_transition_from_attempt(
    state: _PilotState,
    attempt: _AttemptResult,
    bearing: Bearing,
    checkpoint: _CausalCheckpoint | None,
    *,
    prior_requirement_identities: frozenset[tuple[Any, ...]],
) -> _ShadowTheoryTransition | None:
    """Detach shadow evidence from the already-executed ordinary steer.

    The execution's effect observations already own their exact ordered
    projections. This adapter never forks, replays, or rebuilds a projection;
    unavailable shared evidence simply leaves the shadow observation absent.
    """

    execution = attempt.executed_attempt
    if execution is None or execution.bearing.expectation is None or checkpoint is None:
        return None
    source = _theory_boundary_from_checkpoint(checkpoint)
    execution_owner, effects = _shadow_execution_evidence(execution)
    return _ShadowTheoryTransition(
        claim=_shadow_claim(
            execution.bearing.expectation,
            bearing.objective,
            source,
        ),
        source=source,
        execution_owner_token=execution_owner,
        occurrence_evidence=effects,
        act_identity=act_identity(bearing.act),
        pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        disposition=(
            TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
            if attempt.trial is not None
            else TheoryAttemptDisposition.REJECTED_EXACT
            if attempt.proof_rejection
            else TheoryAttemptDisposition.REJECTED_EMPIRICAL
        ),
        evidence=(
            ("effects", effects),
            ("gates", _semantic_key(attempt.gate_events)),
        ),
        requirements=tuple(
            _theory_requirement_snapshot(requirement)
            for requirement in state.active_requirements
            if requirement.identity not in prior_requirement_identities
        ),
    )


def _shadow_bootstrap_transition(
    state: _PilotState,
    ctx: _PilotContext,
    receipt: _BootstrapExecution,
) -> _ShadowTheoryTransition | None:
    """Detach the exact cold-start execution when it selected a factual claim."""

    if not receipt.appeared_effects:
        return None
    source = _theory_boundary_from_checkpoint(receipt.checkpoint)
    effects = tuple(
        _semantic_key(effect.diagnostic_snapshot()) for effect in receipt.appeared_effects
    )
    obligations = tuple(
        TheoryObligationSnapshot(
            tag=effect.designation.tag,
            value=_semantic_key(effect.designation.value),
            producer=tuple(effect.designation.producer),
            consumer=(
                tuple(effect.designation.consumer)
                if effect.designation.consumer is not None
                else None
            ),
            required_shape=tuple(
                (tag, _semantic_key(value)) for tag, value in effect.designation.required_shape
            ),
            boundary=(ctx.target.tag, _semantic_key(ctx.target.value)),
            terminal_target=effect.designation.tag == ctx.target.tag,
            polarity="produce",
            occurrence_selector=None,
        )
        for effect in receipt.appeared_effects
    )
    selected_boundary = replace(
        source,
        occurrence_identity=(
            "selected-bootstrap-boundary",
            receipt.scan_after,
            tuple((item.producer, item.consumer, item.boundary) for item in obligations),
        ),
    )
    claim = TheoryClaim(
        source=source,
        objective=_theory_objective_snapshot(receipt.checkpoint.objective),
        obligations=obligations,
        selected_boundary=selected_boundary,
    )
    requirements = tuple(
        _theory_requirement_snapshot(requirement)
        for requirement in state.active_requirements
        if requirement.source_checkpoint is receipt.checkpoint
    )
    reached = target_reached(
        dict(state.work.state.tags),
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    )
    return _ShadowTheoryTransition(
        claim=claim,
        source=source,
        execution_owner_token=(
            "execution-owner",
            id(receipt.execution_epoch),
            id(receipt.execution_owner),
        ),
        occurrence_evidence=("bootstrap-scan", receipt.scan_after, effects),
        act_identity=("executed-program-scan", receipt.scan_before, receipt.scan_after),
        pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        disposition=(
            TheoryAttemptDisposition.WITNESS
            if reached and not requirements
            else TheoryAttemptDisposition.REJECTED_EXACT
            if requirements
            else TheoryAttemptDisposition.INCOMPLETE
        ),
        evidence=(("effects", effects),),
        requirements=requirements,
    )


def _record_shadow_theory_fact(state: _PilotState, fact: Any) -> None:
    """Single monkeypatchable shadow seam; the reducer cannot affect control."""

    try:
        state.theory_state = reduce_theory(state.theory_state, fact)
    except Exception:  # noqa: BLE001 - shadow diagnostics cannot change the drive
        logger.debug("pilot: shadow theory reduction failed", exc_info=True)


def _run_shadow_hook(hook: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    """Keep every shadow conversion outside production control flow."""

    try:
        hook(*args, **kwargs)
    except Exception:  # noqa: BLE001 - shadow diagnostics cannot change the drive
        logger.debug("pilot: shadow theory hook failed", exc_info=True)


def _shadow_requirement_identities(state: Any) -> frozenset[tuple[Any, ...]]:
    """Snapshot shadow comparison keys without exposing the drive to failure."""

    try:
        return frozenset(
            requirement.identity for requirement in getattr(state, "active_requirements", ())
        )
    except Exception:  # noqa: BLE001 - shadow diagnostics cannot change the drive
        logger.debug("pilot: shadow requirement snapshot failed", exc_info=True)
        return frozenset()


def _active_shadow_theory(state: _PilotState) -> Any:
    theory_id = state.theory_state.active_theory_id
    return state.theory_state.ledger.theories.get(theory_id) if theory_id is not None else None


def _shadow_claim_correlates(state: _PilotState, claim: TheoryClaim) -> bool:
    theory = _active_shadow_theory(state)
    if theory is None:
        return True
    active_claim = state.theory_state.ledger.claims[theory.claim_id]
    progress = state.theory_state.ledger.progress[theory.current_progress_id]
    version = state.theory_state.ledger.versions[theory.current_version_id]
    return (
        claim.objective == active_claim.objective
        and claim.obligations == active_claim.obligations
        and claim.source
        in (
            active_claim.source,
            progress.provisional_tip,
            version.source,
        )
    )


def _shadow_attempt_identity(
    theory_id: tuple[Any, ...],
    observation: _ShadowTheoryTransition,
) -> tuple[Any, ...]:
    return (
        "shadow-attempt",
        theory_id,
        observation.identity,
        observation.execution_owner_token,
        observation.occurrence_evidence,
    )


def _record_shadow_transition(
    state: _PilotState,
    observation: _ShadowTheoryTransition | None,
    *,
    remaining_budget: int,
) -> None:
    if observation is None:
        return
    if state.theory_state.active_theory_id is None:
        _record_shadow_theory_fact(
            state,
            OpenTheory(
                claim=observation.claim,
                opening_identity=("shadow-open", observation.claim.identity),
                remaining_budget=max(0, remaining_budget),
            ),
        )
    elif not _shadow_claim_correlates(state, observation.claim):
        _record_shadow_theory_fact(
            state,
            RecordUnattributedEvidence(
                UnattributedTheoryEvidence(
                    observation_id=("unattributed", observation.identity),
                    boundary=observation.source,
                    evidence=(observation.claim.identity, observation.evidence),
                )
            ),
        )
        return

    theory = _active_shadow_theory(state)
    if theory is None:
        return
    _record_shadow_theory_fact(
        state,
        RecordTheoryAttempt(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            attempt_identity=_shadow_attempt_identity(theory.theory_id, observation),
            source=observation.source,
            execution_owner_token=observation.execution_owner_token,
            occurrence_evidence=observation.occurrence_evidence,
            act_identity=observation.act_identity,
            pilot_rung_identities=observation.pilot_rung_identities,
            disposition=observation.disposition,
            evidence=observation.evidence,
        ),
    )
    if observation.requirements:
        theory = _active_shadow_theory(state)
        assert theory is not None
        _record_shadow_theory_fact(
            state,
            RefineTheory(
                theory_id=theory.theory_id,
                parent_version_id=theory.current_version_id,
                source=observation.source,
                refined_source=_theory_live_boundary(state),
                requirements=observation.requirements,
                refinement_identity=(
                    "shadow-refine",
                    observation.identity,
                    tuple(item.semantic_identity for item in observation.requirements),
                ),
            ),
        )


def _record_shadow_bootstrap(
    state: _PilotState,
    ctx: _PilotContext,
    receipt: _BootstrapExecution | None,
    *,
    remaining_budget: int,
) -> None:
    if receipt is None:
        return
    if receipt.checkpoint.key is None:
        boundary = TheoryBoundaryIdentity(
            world_key=("unavailable-world-key", receipt.scan_before),
            scan_id=receipt.scan_before,
            checkpoint_token=(
                "checkpoint-owner",
                id(receipt.checkpoint.owner),
                "world-key-unavailable",
            ),
        )
        _record_shadow_theory_fact(
            state,
            RecordUnattributedEvidence(
                UnattributedTheoryEvidence(
                    observation_id=(
                        "shadow-bootstrap-incomplete",
                        id(receipt.checkpoint.owner),
                        receipt.scan_before,
                        receipt.scan_after,
                    ),
                    boundary=boundary,
                    evidence=(
                        "exact-source-world-key-unavailable",
                        _semantic_key(receipt.diagnostic_snapshot()),
                    ),
                )
            ),
        )
        return
    _record_shadow_transition(
        state,
        _shadow_bootstrap_transition(state, ctx, receipt),
        remaining_budget=remaining_budget,
    )


def _record_shadow_requirement_delta(
    state: _PilotState,
    before: frozenset[tuple[Any, ...]],
    *,
    identity: tuple[Any, ...],
) -> None:
    theory = _active_shadow_theory(state)
    if theory is None:
        return
    novel = tuple(
        _theory_requirement_snapshot(requirement)
        for requirement in state.active_requirements
        if requirement.identity not in before
    )
    if not novel:
        return
    _record_shadow_theory_fact(
        state,
        RefineTheory(
            theory_id=theory.theory_id,
            parent_version_id=theory.current_version_id,
            source=state.theory_state.ledger.progress[theory.current_progress_id].provisional_tip,
            refined_source=_theory_live_boundary(state),
            requirements=novel,
            refinement_identity=("shadow-refine", identity),
        ),
    )


def _record_shadow_advance(
    state: _PilotState,
    observation: _ShadowTheoryTransition | None,
    *,
    remaining_budget: int,
    phase_receipts: tuple[tuple[Any, ...], ...] = (),
) -> None:
    if observation is None or observation.adopted_boundary is None:
        return
    theory = _active_shadow_theory(state)
    if theory is None:
        return
    boundary = _theory_live_boundary(state)
    if boundary.scan_id < observation.adopted_boundary.scan_id or (
        boundary.scan_id == observation.source.scan_id
        and boundary.world_key == observation.source.world_key
    ):
        return
    parent = state.theory_state.ledger.progress[theory.current_progress_id]
    _record_shadow_theory_fact(
        state,
        AdvanceTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            accepted_attempt_id=_shadow_attempt_identity(theory.theory_id, observation),
            source=observation.source,
            boundary=boundary,
            advance_identity=("shadow-advance", observation.identity, boundary),
            phase_receipts=phase_receipts,
            remaining_budget=min(parent.remaining_budget, max(0, remaining_budget)),
        ),
    )


def _record_shadow_repair_result(
    state: _PilotState,
    *,
    requirement: ActiveRequirement | None,
    assignments: Any,
    remaining_budget: int,
) -> None:
    theory = _active_shadow_theory(state)
    if theory is None:
        return
    boundary = _theory_live_boundary(state)
    assignment_identity = _semantic_key(assignments)
    requirement_identity = (
        _theory_requirement_snapshot(requirement).semantic_identity
        if requirement is not None
        else ()
    )
    _record_shadow_theory_fact(
        state,
        RecordUnattributedEvidence(
            UnattributedTheoryEvidence(
                observation_id=(
                    "shadow-local-repair",
                    theory.theory_id,
                    theory.current_version_id,
                    boundary,
                    requirement_identity,
                    assignment_identity,
                ),
                boundary=boundary,
                evidence=(
                    "local-repair-executed-without-detached-attempt-owner",
                    requirement_identity,
                    assignment_identity,
                    max(0, remaining_budget),
                ),
            )
        ),
    )


def _record_shadow_proved(state: _PilotState) -> None:
    theory = _active_shadow_theory(state)
    if theory is None:
        return
    boundary = _theory_live_boundary(state)
    if state.active_requirements:
        _record_shadow_theory_fact(
            state,
            RecordUnattributedEvidence(
                UnattributedTheoryEvidence(
                    observation_id=(
                        "shadow-target-reached-with-active-requirements",
                        theory.theory_id,
                        theory.current_version_id,
                        boundary,
                    ),
                    boundary=boundary,
                    evidence=(
                        "proof-withheld",
                        tuple(
                            _theory_requirement_snapshot(requirement).semantic_identity
                            for requirement in state.active_requirements
                        ),
                    ),
                )
            ),
        )
        return
    attempts = tuple(
        state.theory_state.ledger.attempts[attempt_id]
        for attempt_id in theory.attempt_ids
        if state.theory_state.ledger.attempts[attempt_id].disposition
        in (
            TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
            TheoryAttemptDisposition.WITNESS,
        )
    )
    if not attempts:
        return
    fulfilled = attempts[-1].occurrence_evidence
    _record_shadow_theory_fact(
        state,
        ProveTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            promoted_landing=boundary,
            proof_identity=("shadow-proved", theory.theory_id, boundary),
            fulfilled_obligations=fulfilled,
            requirement_observations=(),
            retained_pilot_rung_identities=tuple(
                _rung_identity(rung) for rung in state.pilot_rungs
            ),
        ),
    )


def _record_shadow_abandoned(
    state: _PilotState,
    termination: TheoryTermination,
) -> None:
    theory = _active_shadow_theory(state)
    if theory is None:
        return
    _record_shadow_theory_fact(
        state,
        AbandonTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            termination=termination,
            abandonment_identity=(
                "shadow-abandoned",
                theory.theory_id,
                theory.current_version_id,
                termination,
                _theory_live_boundary(state),
            ),
        ),
    )


@dataclass(frozen=True)
class _ProverContext:
    """Best-effort static evidence shared by drive setup and target context."""

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    stateful_domains: dict[str, tuple[Any, ...]] | None = None
    key_config: _StateKeyConfig | None = None
    evidence: TransitionEvidence | None = None


# ---------------------------------------------------------------------------
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


def _commit_step(
    fork: PLC,
    inputs: dict[str, Any],
    scan_before: int,
    resting: dict[str, Any],
    edge_tags: set[str],
) -> tuple[PLC, tuple[_Step, ...]]:
    """Record a step (or release+pulse pair) and swap the work fork.

    ``inputs`` is the policy's full ``ActPolicy.applied`` set, not only its
    primary candidate. A ``rise()``/``fall()`` gate needs an edge — a transition
    — but a recorded ``_Step`` holds its ``inputs`` constant across the step's
    scans and the patch persists into the next step, so the naive replay
    (``patch(inputs); step``) cannot recreate the transition once the edge is
    already at the pulsed level (the consecutive-command case).  PILOT's live
    pulse drops the edge to resting for one scan before raising it
    (``_apply_actions``); mirror that here by recording an explicit 1-scan release
    step whenever the inputs drive an edge tag *off* resting, so the replay
    reproduces the same edge.
    """
    edge_release = {
        t: resting.get(t, False)
        for t in inputs
        if t in edge_tags and not _values_match(inputs[t], resting.get(t, False))
    }
    if edge_release:
        steps = (
            _Step(inputs=edge_release, scan_before=scan_before, scan_after=scan_before + 1),
            _Step(
                inputs=dict(inputs),
                scan_before=scan_before + 1,
                scan_after=fork.state.scan_id,
            ),
        )
    else:
        steps = (
            _Step(
                inputs=dict(inputs),
                scan_before=scan_before,
                scan_after=fork.state.scan_id,
            ),
        )
    return fork, steps


def _make_pilot_context(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    edge_tags: set[str],
    resting: dict[str, Any],
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None,
    stateful_domains: dict[str, tuple[Any, ...]] | None,
    evidence: TransitionEvidence | None,
    key_config: _StateKeyConfig | None,
    compass: Compass | None,
    opaque_loop: frozenset[str],
    route: TraceChoice | None,
    max_scans: int,
    avoid_pred: Any = None,
    target_predicate: Any = None,
    configured_inputs: frozenset[str] = frozenset(),
) -> _PilotContext:
    pipeline_roles = _infer_pipeline_roles_for_context(
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )
    pipeline_internal_tags = frozenset(
        tag for role in pipeline_roles for tag in role.trace_internal_tags
    )
    prior_compass = compass or Compass()
    compass = Compass(
        catalog=NavigationCatalog(
            slices=prior_compass.catalog.slices,
            graphs=_build_static_transition_graphs_for_context(
                pipeline_roles,
                pdg,
                program,
                steerable,
                opaque_loop,
                evidence,
            ),
        ),
        knowledge=prior_compass.knowledge,
    )
    # Domain prior for trace's inequality resolution: nondeterministic domains
    # for free inputs, stateful domains for program-owned tags, and affine
    # func-deps for derived tags. All are receipts from the same ExploreContext.
    domain_prior = DomainPrior(
        nd_domains=nd_domains,
        stateful_domains=stateful_domains,
        func_deps=evidence.affine_projections() if evidence is not None else None,
    )
    # Clear-only (ack-cleared momentary) command tags: a subset of ``steerable``
    # kept off prerequisite holds and off preferred init/reset writer selection.
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, program)
    return _PilotContext(
        target=TargetSpec(target_tag, target_value, target_predicate),
        pdg=pdg,
        program=program,
        steerable=steerable,
        edge_tags=edge_tags,
        clear_only=clear_only,
        resting=resting,
        nd_domains=nd_domains,
        domain_prior=domain_prior,
        evidence=evidence,
        key_config=key_config,
        compass=compass,
        opaque_loop=opaque_loop,
        pipeline_roles=pipeline_roles,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        blocked_actions=frozenset(),
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        configured_inputs=configured_inputs,
    )


def _prepare_drive(
    plc: PLC,
    *,
    unlink: list[str] | None,
) -> _DriveSetup:
    """Build the shared program/runtime analysis for one public drive."""

    from pyrung.core.analysis.pdg import build_program_graph

    configured_inputs = _configured_input_names(plc)
    work = fork_with_pilot_rungs(plc, (), history_budget=math.inf)
    program = plc._program
    pdg = build_program_graph(program)
    harness_fb = install_harness(work, unlink=unlink)
    ref_consts = compute_reference_constants(pdg, program, work._known_tags_by_name)
    steerable = compute_steerable(pdg, work._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, work._known_tags_by_name, pdg, program)
    diag_snapshot = dict(work.state.tags)
    prover = _build_prover_context(
        program,
        diag_snapshot,
    )
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    return _DriveSetup(
        work=work,
        program=program,
        pdg=pdg,
        steerable=steerable,
        edge_tags=edge_tags,
        resting=resting,
        anchor_scan=work.state.scan_id,
        diag_snapshot=diag_snapshot,
        nd_domains=prover.nd_domains,
        stateful_domains=prover.stateful_domains,
        key_config=prover.key_config,
        evidence=prover.evidence,
        compass=Compass(NavigationCatalog(slices=tuple(opaque_slices))),
        opaque_loop=detect_opaque_loop(pdg, program),
        configured_inputs=configured_inputs,
    )


def _prepare_target_context(
    setup: _DriveSetup,
    target_tag: str,
    target_value: Any,
    target_predicate: Any,
    *,
    max_scans: int,
    avoid_pred: Any,
    compass: Compass | None = None,
    work: PLC | None = None,
) -> tuple[_PilotContext, RouteTaken | None]:
    """Bind one target and its initial route report to a prepared drive."""

    target_work = setup.work if work is None else work
    route_taken = _prepare_route(
        target_work,
        target_tag,
        target_value,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        target_predicate=target_predicate,
        avoid_pred=avoid_pred,
    )
    ctx = _make_pilot_context(
        target_work,
        target_tag,
        target_value,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.edge_tags,
        setup.resting,
        nd_domains=setup.nd_domains,
        stateful_domains=setup.stateful_domains,
        evidence=setup.evidence,
        key_config=setup.key_config,
        compass=compass or setup.compass,
        opaque_loop=setup.opaque_loop,
        route=None,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        target_predicate=target_predicate,
        configured_inputs=setup.configured_inputs,
    )
    return ctx, route_taken


def _infer_pipeline_roles_for_context(
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[PipelineRoles, ...]:
    if not opaque_loop:
        return ()

    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles

    roles: list[PipelineRoles] = []
    for tag in sorted(opaque_loop):
        if evidence is not None and not evidence.is_stepping(tag):
            continue
        role = infer_pipeline_roles(tag, pdg, program, steerable, opaque_loop, evidence)
        if role.request_tags:
            roles.append(role)
    return tuple(roles)


def _build_static_transition_graphs_for_context(
    pipeline_roles: tuple[PipelineRoles, ...],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[StaticTransitionGraph, ...]:
    if not pipeline_roles:
        return ()
    from pyrung.core.analysis.pilot.pipeline_graph import build_static_transition_graphs

    return build_static_transition_graphs(
        pipeline_roles,
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )


def _with_avoid_reason(
    base: str,
    state: _PilotState,
    ctx: _PilotContext,
    frame: _IterationFrame | None = None,
) -> str:
    """Append the violated ``avoid=`` condition(s) to a terminal reason.

    Keeps the decline legible — (concrete frontier tag, target, outcome class) —
    so ``how(..., avoid=X)`` that excludes every path names ``X`` rather than
    surfacing a bare ``stuck``.
    """
    if getattr(ctx, "avoid_pred", None) is None:
        return base
    named = set(getattr(state, "avoid_names", set()) or ())
    if not named and frame is not None:
        # No candidate was ever action-gated (the route gate pruned every route
        # to the target silently).  Re-derive which avoid conditions forced those
        # routes so the decline still names them.
        named.update(_avoid_route_names(frame, ctx))
    if not named and frame is not None:
        # Opaque writer cuts can prevent route enumeration from reconstructing
        # the pruned Or arms.  In that case, name only avoid conditions that are
        # structurally upstream of the outstanding frontier.
        related: set[str] = set()
        for tag, _value in frontier_pairs(frame.tree, frame.snap):
            related.update(ctx.pdg.upstream_slice(tag, follow_calls=True))
        named.update(set(getattr(ctx.avoid_pred, "names", ())) & related)
    names = sorted(named)
    if not names:
        return base
    if frame is not None:
        fr = frontier_pairs(frame.tree, frame.snap)
        frontier = fr[0][0] if fr else ctx.target.tag
    else:
        frontier = ctx.target.tag
    return (
        f"{base}: avoid excludes {', '.join(names)} (frontier {frontier}, target {ctx.target.tag})"
    )


def _stopped_reason() -> str:
    """Translate internal orientation taxonomy into an honest public stop."""
    return "No productive next action was found"


def _avoid_route_names(frame: _IterationFrame, ctx: _PilotContext) -> tuple[str, ...]:
    """Avoid-condition names that forced *every* route to the value target.

    Enumerates the same routes as ``_prepare_route`` from the current frame and,
    when they are all avoid-forced (no survivor), returns the union of the
    violated member names.  ``()`` when the target isn't a value-route target or
    any route survives.
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    snap = frame.snap
    if not (
        _target_is_value_route(ctx.target.predicate)
        and not _values_match(snap.get(ctx.target.tag), ctx.target.value)
    ):
        return ()
    choices = enumerate_trace_choices(
        ctx.target.tag,
        ctx.target.value,
        snap,
        ctx.pdg,
        ctx.program,
        steerable=ctx.steerable,
        clear_only=ctx.clear_only,
    )
    read = TraceReadConstraints(
        clear_only=ctx.clear_only,
        opaque_loop=ctx.opaque_loop,
    )
    names: set[str] = set()
    survivor = False
    forced_any = False
    for ch in choices:
        tree = trace_back(
            ctx.target.tag,
            ctx.target.value,
            snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            constraints=replace(read, route=ch),
        )
        forced = _route_forced_names([tree], snap, avoid)
        if forced:
            names.update(forced)
            forced_any = True
        else:
            survivor = True
    if survivor or not forced_any:
        return ()
    # Every route to the target was avoid-forced.  Arm collapse (one Or-arm per
    # traced route) can hide members, so report the full avoid set that blocked
    # it, falling back to the observed names for a bare-callable avoid.
    return tuple(getattr(avoid, "names", ()) or sorted(names))


def _record_attempt(
    attempt: Any,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    objective: BearingObjective,
    act: Any = None,
) -> None:
    """Commit knowledge from an attempt, whether accepted or rejected.

    Runs after each execution/verification wrapper and before any accepted world
    is assessed. Compass observations, excursion holds, and nogoods commit even
    when the trial is rejected so negative knowledge survives world reverts.
    """
    # The commit point: apply() returns the next compass value; this single
    # assignment replaces the context's compass (a value, never a shared
    # mutable advanced behind readers' backs).
    knowledge_observations = [
        *attempt.observations,
        *(ActionNogoodObservation(frame.key, ("pair", pair)) for pair in attempt.nogood_pairs),
    ]
    ctx.compass, _ = ctx.compass.apply(knowledge_observations)
    if attempt.confirmed_correction is not None:
        _anchor_frame_receipt(frame, state, objective)
        _install_confirmed_correction(
            state,
            attempt.confirmed_correction,
            origin_key=frame.key,
            scan=state.work.state.scan_id,
            source="excursion",
        )
    if attempt.avoid_names:
        # Knowledge: which avoid conditions excluded a path, for a naming decline.
        state.avoid_names.update(attempt.avoid_names)


def _resolve_excursion(
    attempt: _AttemptResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Investigate one reported excursion and continue verification on its replay."""
    executed = attempt.excursion_attempt
    if executed is None:
        return attempt
    executed.pulse.release_projections()
    executed = replace(executed, effect_observations=())
    attempt = replace(attempt, excursion_attempt=executed)

    key_config = state.key_config
    assert key_config is not None
    pulse = executed.pulse
    try:
        result = investigate_excursion(
            state.work,
            pulse.fork,
            frame.snap,
            pulse.post_pulse_snap,
            frame.key,
            executed.bearing.act.policy.applied,
            cfg=key_config,
            steerable=ctx.steerable,
            pilot_rungs=state.pilot_rungs,
            resting=ctx.resting,
            edge_tags=ctx.edge_tags,
            scan_budget=state.remaining_search_scans(ctx.max_scans),
            pdg=ctx.pdg,
            program=ctx.program,
            ctx=ctx,
        )
        return verify_excursion_replay(attempt, result, frame, state, ctx)
    finally:
        # The returned AttemptResult owns the replay pulse (if any). The
        # superseded excursion pulse is no longer reachable by the outer
        # transition finalizer, so release it here on every replay outcome.
        pulse.release_projections()


def _step_context(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
) -> _StepContext:
    """Build the context owned by one committed operation.

    Commit adds only unresolved frontier tags and exact executable pilot
    rungs; every other view derives from the policy and execution-evidence
    owners already inside the trial.
    """
    bearing = trial.attempt.bearing
    policy = bearing.act.policy
    is_coast = policy.motion.is_coast

    frontier_tags: tuple[str, ...] = ()
    pilot_rungs: tuple[Any, ...] = ()

    if is_coast:
        seen: set[str] = set()
        frontier: list[str] = []
        for n in frame.tree.leaves():
            if (
                not n.satisfied
                and not n.is_steerable
                and not getattr(n, "pipeline_internal", False)
                and n.tag not in seen
            ):
                seen.add(n.tag)
                frontier.append(n.tag)
        frontier_tags = tuple(frontier)
        pilot_rungs = tuple(state.pilot_rungs)

    return _StepContext(
        policy=policy,
        execution=trial.execution,
        frontier_tags=frontier_tags,
        pilot_rungs=pilot_rungs,
    )


def _adopt_trial(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AcceptedTrial:
    """Adopt one gate-approved trial without applying post-commit policy.

    Verification already ran inside the steering wrapper and
    ``_record_attempt`` already committed its knowledge.  This is the shared
    local commit used by the live loop and disposable composition; only the
    live caller may subsequently invoke ``_monitor_trend``.
    """
    # Capture a satisfied bearing's launch world before commit. Its landing
    # remains pending until ordinary progress is banked; an Alarm ejection must
    # replays from this exact source with its PilotRungs, not an older trend CP.
    _anchor_bearing_receipt(trial, frame, state)

    # Knowledge handling may have installed an excursion correction after verification built the
    # trial.  The accepted world key must describe that effective rung overlay,
    # not the pre-correction one used by the diagnostic fork.
    verified = trial.verification
    execution = trial.execution
    if isinstance(verified, AssessedMotion):
        assert state.key_config is not None
        trial = replace(
            trial,
            verification=replace(
                verified,
                new_key=_pilot_world_key(
                    dict(execution.after_snap),
                    state.key_config,
                    state.pilot_rungs,
                    state.active_requirements,
                ),
            ),
        )
    _commit_trial(trial, frame, state, ctx)
    return trial


def _monitor_committed_trial(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    continuation_hop: bool = False,
) -> Iterator[PilotEvent]:
    """Emit one adopted trial and apply outer-loop progress policy."""

    assert_recovery_inactive("monitor a committed trial")
    policy = trial.attempt.bearing.act.policy
    yield PilotEvent(
        "trial_committed",
        state.work.state.scan_id,
        {
            "candidate": dict(policy.action_pairs),
            "applied": policy.applied,
            "steps": tuple(state.steps),
            "snapshot": dict(state.work.state.tags),
        },
    )
    if not continuation_hop:
        yield from _monitor_trend(trial, frame, state, ctx)


def _commit_trial(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> None:
    assert_recovery_disposable_state(state, "commit")
    attempt = trial.attempt
    pulse = attempt.pulse
    bearing = attempt.bearing
    policy = bearing.act.policy
    execution = trial.execution
    verified = trial.verification
    key_was_seen = isinstance(verified, AssessedMotion) and verified.new_key in state.seen_keys
    if isinstance(verified, AssessedMotion):
        state.seen_keys.add(verified.new_key)
    # Record what was physically applied — the candidate plus its co-actions (the
    # command button and its one-shot ``rise(CmdChgRequest)`` edge gate) — not the
    # policy's narrow primary candidate. Replay and live apply must reproduce every input
    # that drove the transition.  ``applied`` is the full set and is empty exactly
    # for bearing/let-run coasts, where an empty action means "coast, no input".
    # A terminal let-run animates conditional holds during its coast; record them
    # on the step so the path is self-describing.  ``pilot_rungs`` is the live
    # round-by-round accumulator — snapshot the conditional ones active now.  A
    # pulse/bearing-coast step animates nothing, so it carries no reactive holds.
    #
    # The *steady* holds active during the coast (e.g. the Enable that drives a
    # harness sensor's ramp) are the input that makes the coast advance — fold
    # them into the recorded inputs so replay re-establishes them.  ``applied``
    # is empty for a let-run, so this is the only place the driver is recorded.
    step_inputs = dict(policy.applied)
    work, steps = _commit_step(
        pulse.fork,
        step_inputs,
        pulse.scan_before,
        ctx.resting,
        ctx.edge_tags,
    )
    act = _CommittedAct(steps=steps, context=_step_context(trial, frame, state))
    # Adopt the physical fork and its replay evidence in one persistent-world
    # update. No consumer can observe steps detached from their operation owner.
    state.world = state.world.set(
        work=work,
        committed_acts=state.committed_acts.append(act),
    )
    if isinstance(verified, AssessedMotion):
        # Revisit novelty is invocation knowledge. Consume every credential
        # only after adopting the accepted execution, and never roll it back
        # with _World.
        state.consumed_revisits.update(verified.revisit_credentials)
    # The world record reverts; the flattened journey is the append-only public
    # history of every physical step, including later-reverted operations.
    state.journey.extend(steps)
    # Waiting is not searching: an accepted coast's span is dwell — the machine
    # advancing itself while the pilot holds heading — so it must not drain the
    # invocation's search budget. A revert rewinds this credit with the world.
    # The credit is earned only when the machine actually moved its own work —
    # the coast reached its channel target or advanced earned work; a
    # coast that parks with nothing moving is the *search* failing. Sterile laps
    # must still drain the budget so a parked machine has a terminating force.
    if policy.motion.is_coast:
        productive = (
            not key_was_seen
            or execution.channel_motion.reached
            or earned_work_is_useful_motion(trial.earned_work_receipt)
        )
        if productive:
            state.dwell_scans += state.work.state.scan_id - pulse.scan_before


def _prepare_oriented_result(
    state: _PilotState,
    result: OrientationResult,
    world: OrientationWorld,
    frame: _IterationFrame,
) -> None:
    """Install the minimal current-world bookkeeping needed before execution."""

    if state.key_config is None:
        state.key_config = world.key_config
    if state.best_trend is None:
        state.best_trend = frame.distance_before
        state.seen_keys.add(frame.key)
    if not state.checkpoints and isinstance(result, Bearing):
        state.checkpoints.append(
            _Checkpoint(
                key=frame.key,
                world=state.snapshot_world(),
                trend=frame.distance_before,
                objective=result.objective,
            )
        )
    if isinstance(result, Bearing):
        state.recorded_root_route = world.root_route


def _certify_current_target_prefix(
    attempt: _AttemptResult,
    adoption_scan: int,
    target_expectation: EffectExpectation | None,
    state: _PilotState,
    ctx: _PilotContext,
) -> _ContinuationCheckpoint | None:
    """Ephemerally join a fresh ProgramStep to this Pulse's target occurrence."""

    executed = attempt.executed_attempt
    if (
        executed is None
        or target_expectation is None
        or not isinstance(executed.bearing.act, Coast)
    ):
        return None
    pulse = executed.pulse
    if pulse.kernel_scan_ids != tuple(range(pulse.scan_before + 1, pulse.fork.state.scan_id + 1)):
        return None
    observations = observe_execution_window(
        target_expectation,
        pulse.fork,
        scan_before=adoption_scan,
        action_scan=None,
        coast_receipt=pulse.coast_receipt,
        kernel_scan_ids=tuple(
            scan_id for scan_id in pulse.kernel_scan_ids if scan_id > adoption_scan
        ),
        projection_at=pulse.projection_at,
    )
    appeared = tuple(
        observation
        for observation in observations
        if observation.appeared is not None and observation.obligation.terminal_target
    )
    if len(appeared) != 1:
        return None
    historical = appeared[0].appeared
    assert historical is not None
    if historical.scan_id != adoption_scan + 1:
        return None
    try:
        boundary_work = fork_with_pilot_rungs(
            pulse.fork,
            state.pilot_rungs,
            scan_id=adoption_scan,
        )
    except KeyError:
        return None
    boundary_snap = dict(boundary_work.state.tags)
    world = WorldView(
        snapshot=boundary_snap,
        pdg=ctx.pdg,
        program=ctx.program,
        steerable=ctx.steerable,
        opaque_loop=ctx.opaque_loop,
        prior=ctx.domain_prior,
        clear_only=ctx.clear_only,
        pipeline_internal_tags=ctx.pipeline_internal_tags,
        pipeline_roles=ctx.pipeline_roles,
        avoid_pred=ctx.avoid_pred,
        harness=getattr(boundary_work, "_harness", None),
    )
    terminal = target_expectation.obligations[0]
    family = sibling_producer_family(world, terminal.tag, terminal.value)

    def producer_address(producer: Any) -> tuple[Any, ...]:
        node = ctx.pdg.rung_nodes[producer.rung_index]
        return (node.subroutine, node.rung_index, node.branch_path)

    producers = (
        tuple(
            producer
            for producer in family.program_owned
            if producer_address(producer) == terminal.producer
        )
        if family is not None
        else ()
    )
    if len(producers) != 1:
        return None
    step = read_program_step(
        world,
        producers[0],
        boundary_work,
        state.pilot_rungs,
        resting=ctx.resting,
        projection_scans=1,
    )
    if not step.producer_observed:
        return None
    selected_rung = resolve_rung(ctx.program, ctx.pdg.rung_nodes[producers[0].rung_index])
    if selected_rung is None:
        return None
    projected = fork_with_pilot_rungs(boundary_work, state.pilot_rungs)
    projected.step()
    projection = projected._replay_rung_write_projection_at(projected.state.scan_id)
    if projection is None:
        return None
    projected_occurrences = tuple(
        write
        for write in projection.writes
        if write.run.rung is selected_rung
        and write.run.enabled
        and write.transition.tag_name == terminal.tag
        and _values_match(write.transition.to_value, terminal.value)
    )
    if len(projected_occurrences) != 1:
        return None

    def address(write: Any) -> tuple[Any, ...]:
        return (
            write.scan_id,
            write.ordinal,
            write.run_order,
            write.call_invocation,
            write.rung_id,
            write.run.kind,
            write.run.caller_rung,
            write.run.call_stack,
        )

    if address(projected_occurrences[0]) != address(historical):
        return None
    epoch_owner = _execution_epoch_owner(pulse.fork, adoption_scan)
    if epoch_owner is None:
        return None
    assert state.key_config is not None
    boundary_key = _pilot_world_key(
        boundary_snap,
        state.key_config,
        state.pilot_rungs,
        state.active_requirements,
    )
    return _ContinuationCheckpoint(
        scan_id=adoption_scan,
        world_key=boundary_key,
        kind="target_prefix",
        execution_epoch=epoch_owner[0],
        execution_owner=epoch_owner[1],
        landing_occurrence=occurrence_snapshot(historical),
    )


def _transition_once(
    state: _PilotState,
    ctx: _PilotContext,
    target: TargetSpec,
    constraints: NavigationConstraints,
    *,
    oriented: OrientationResult | None = None,
    resolve_excursion: bool = True,
    derive_requirements: bool = True,
    derivation_checkpoint: _CausalCheckpoint | None = None,
) -> _IterationTransition:
    """Orient and locally settle exactly one current-world result.

    A Bearing passes through the ordinary executor, excursion resolver,
    observation/nogood application, verification, and local commit.  A
    NeedProbe or Stuck result is returned without acting.  The function never
    probes, monitors post-commit progress, emits events, or repeats.

    Mutations are scoped entirely by ``state`` and ``ctx``.  The outer loop
    passes its live objects; bounded investigation passes disposable clones and
    may roll them back without leaking Compass knowledge.
    """

    assert_recovery_disposable_state(state, "execute a transition")
    result = oriented
    if result is None:
        raw_world = OrientationWorld(
            world_key=(),
            snapshot=dict(state.work.state.tags),
            frame=None,
            state=state,
            context=ctx,
            key_config=state.key_config,
        )
        result = ctx.compass.orient(raw_world, target, constraints)

    orientation_read = result.orientation
    if orientation_read is None:
        raise RuntimeError("Compass orientation omitted its current-world reading")
    orientation_world = replace(
        orientation_read.world,
        state=state,
        context=ctx,
        key_config=state.key_config or orientation_read.world.key_config,
    )
    frame = orientation_world.frame
    _prepare_oriented_result(state, result, orientation_world, frame)
    result, recovery_program_step = _preempt_recovery_action_with_program_coast(
        result,
        frame,
        state,
        ctx,
        target,
    )
    if not isinstance(result, Bearing):
        return _IterationTransition(result=result, frame=frame)

    terminal_target_expectation = _selected_terminal_target_expectation(
        frame,
        target,
        ctx,
    )
    act = result.act
    expectation_checkpoint = (
        _CausalCheckpoint(
            key=frame.key,
            world=state.snapshot_world(),
            objective=result.objective,
            configured_inputs=ctx.configured_inputs | _configured_input_names(state.work),
        )
        if result.expectation is not None or terminal_target_expectation is not None
        else None
    )
    shadow_requirements_before = _shadow_requirement_identities(state)
    attempt = execute(result, orientation_world)
    if resolve_excursion and attempt.excursion_attempt is not None:
        attempt = _resolve_excursion(attempt, frame, state, ctx)
    prefix_proof = None
    prefix_execution = attempt.executed_attempt
    if terminal_target_expectation is not None and prefix_execution is not None:
        prefix_proof = _certify_current_target_prefix(
            attempt,
            prefix_execution.pulse.scan_before,
            terminal_target_expectation,
            state,
            ctx,
        )
    if terminal_target_expectation is not None:
        result, attempt = _promote_transient_target_failure(
            result,
            attempt,
            terminal_target_expectation,
            frame,
            state,
            ctx,
            prefix_proof,
            local_repair_checkpoint=derivation_checkpoint,
        )
        act = result.act
    continuation_checkpoint = None
    executed_for_derivation = attempt.executed_attempt
    if terminal_target_expectation is not None and executed_for_derivation is not None:
        continuation_checkpoint = _adjacent_continuation_source(
            state,
            executed_for_derivation.pulse,
            prefix_proof,
        )
    if derive_requirements:
        _derive_attempt_requirements(
            attempt,
            state,
            ctx,
            continuation_checkpoint or derivation_checkpoint or expectation_checkpoint,
        )
    shadow_observation = None
    try:
        shadow_observation = _shadow_transition_from_attempt(
            state,
            attempt,
            result,
            derivation_checkpoint or expectation_checkpoint,
            prior_requirement_identities=shadow_requirements_before,
        )
    except Exception:  # noqa: BLE001 - shadow conversion cannot change the drive
        logger.debug("pilot: shadow theory observation failed", exc_info=True)
    _record_attempt(attempt, frame, state, ctx, result.objective, act)

    if isinstance(act, Coast) and act.mode == "terminal":
        stop_reason = (
            attempt.stall_receipt.stop_reason
            if attempt.stall_receipt is not None
            else (
                attempt.trial.execution.coast_receipt.stop_reason
                if (attempt.trial is not None and attempt.trial.execution.coast_receipt is not None)
                else "terminal-coast"
            )
        )
        ctx.compass, _ = ctx.compass.apply((CoastObservation(frame.key, stop_reason),))

    if attempt.trial is None:
        if attempt.proof_rejection:
            proof_world_key = (
                _pilot_world_key(
                    frame.snap,
                    state.key_config,
                    state.pilot_rungs,
                    (),
                )
                if state.key_config is not None
                else frame.key
            )
            state.proof_rejected_acts.add((proof_world_key, act_identity(act)))
        else:
            rejection_key = (
                _pilot_world_key(
                    frame.snap,
                    state.key_config,
                    state.pilot_rungs,
                    state.active_requirements,
                )
                if state.key_config is not None
                else frame.key
            )
            ctx.compass, _ = ctx.compass.apply(
                (ActionNogoodObservation(rejection_key, act_identity(act)),)
            )
        return _IterationTransition(
            result=result,
            frame=frame,
            attempt=attempt,
            shadow_observation=shadow_observation,
        )

    trial = _adopt_trial(attempt.trial, frame, state, ctx)
    continuation_hop = _advance_recovery_continuation(
        trial,
        frame,
        state,
        ctx,
        recovery_program_step,
    )
    _retain_expectation_receipt(
        trial,
        act,
        state,
        derivation_checkpoint or expectation_checkpoint,
    )
    if shadow_observation is not None:
        try:
            shadow_observation = replace(
                shadow_observation,
                adopted_boundary=_theory_live_boundary(state),
            )
        except Exception:  # noqa: BLE001 - shadow diagnostics cannot change the drive
            logger.debug("pilot: shadow adoption snapshot failed", exc_info=True)
    return _IterationTransition(
        result=result,
        frame=frame,
        attempt=attempt,
        trial=trial,
        continuation_hop=continuation_hop,
        shadow_observation=shadow_observation,
    )


def _selected_terminal_target_expectation(
    frame: _IterationFrame,
    target: TargetSpec,
    ctx: _PilotContext,
) -> EffectExpectation | None:
    """Name the exact selected root writer for an equality target.

    This is only a designation until execution proves the writer appeared.
    Relational targets and unresolved/ambiguous root writers fail closed.
    """

    if target.predicate is not None:
        return None
    root = frame.tree
    if (
        root.writer_rung is None
        or root.tag != target.tag
        or not _values_match(root.value, target.value)
    ):
        return None
    return expectation_from_writer(
        ctx.pdg,
        ctx.program,
        writer_node=root.writer_rung,
        tag=target.tag,
        value=target.value,
        boundary=(target.tag, target.value),
        terminal_target=True,
    )


def _promote_transient_target_failure(
    result: Bearing,
    attempt: _AttemptResult,
    target_expectation: EffectExpectation,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    prefix_proof: _ContinuationCheckpoint | None = None,
    *,
    local_repair_checkpoint: _CausalCheckpoint | None = None,
) -> tuple[Bearing, _AttemptResult]:
    """Re-verify an act only after its selected target appeared and was lost."""

    executed = attempt.executed_attempt
    if executed is None:
        return result, attempt
    pulse = executed.pulse
    terminal_obligation = target_expectation.obligations[0]
    window_entry = pulse.source_snap if pulse.source_snap is not None else pulse.action_snap
    window_entry_value = window_entry.get(terminal_obligation.tag)
    final_landing_value = pulse.fork.state.tags.get(terminal_obligation.tag)
    exact_scans = tuple(
        scan_id
        for scan_id in pulse.kernel_scan_ids
        if pulse.scan_before < scan_id <= pulse.fork.state.scan_id
    )
    candidate_scans = terminal_target_replay_scan_ids(
        target_expectation,
        pulse.fork,
        exact_scans,
    )
    if not candidate_scans:
        return result, attempt
    target_observations = observe_execution_window(
        target_expectation,
        pulse.fork,
        scan_before=pulse.scan_before,
        # This observer certifies the selected program-owned terminal writer,
        # not the intervention assertion.  The act's ordinary expectation owns
        # assertion-scan evidence separately; requiring that scan here would
        # defeat sparse target-writer nomination for a later autonomous scan.
        action_scan=None,
        coast_receipt=pulse.coast_receipt,
        kernel_scan_ids=candidate_scans,
        projection_at=pulse.projection_at,
    )
    promoted = promote_terminal_target_observation(
        target_observations,
        window_entry_value=window_entry_value,
        final_landing_value=final_landing_value,
    )
    existing = executed.bearing.expectation
    if promoted is None and attempt.trial is not None and existing is not None:
        checkpoint_scan = _repaired_program_continuation(
            state,
            ctx,
            attempt.trial,
            existing,
            execution_work=pulse.fork,
        )
        if checkpoint_scan is not None:
            promoted = _promoted_target_suffix_observation(
                target_expectation,
                pulse,
                checkpoint_scan,
            )
        if promoted is None and _exact_local_repair_window(
            local_repair_checkpoint,
            pulse,
        ):
            # A repaired local transaction may carry useful program-owned
            # motion all the way to a non-zero target displacement before any
            # corrected landing is adopted.  The retry's accepted original
            # expectation and exact source/window grant observation authority;
            # the target adapter still requires one exact selected occurrence
            # and its final landing writer.  The supplied checkpoint remains
            # the causal source for the next requirement.
            promoted = promote_certified_prefix_target_observation(
                target_observations,
                final_landing_value=pulse.fork.state.tags.get(terminal_obligation.tag),
            )
    if promoted is None and _adjacent_continuation_source(state, pulse, prefix_proof) is not None:
        promoted = promote_certified_prefix_target_observation(
            target_observations,
            final_landing_value=pulse.fork.state.tags.get(terminal_obligation.tag),
        )
    if promoted is None:
        return result, attempt

    if existing is not None:
        matching = tuple(
            obligation
            for obligation in existing.obligations
            if obligation.tag == terminal_obligation.tag
            and _values_match(obligation.value, terminal_obligation.value)
            and obligation.producer == terminal_obligation.producer
        )
        # A consumer-owned target handoff already has the established delayed
        # recovery semantics. Do not mint a parallel terminal obligation.
        if any(obligation.consumer is not None for obligation in matching):
            return result, attempt
        obligations = (
            *(obligation for obligation in existing.obligations if obligation not in matching),
            terminal_obligation,
        )
        retained_observations = tuple(
            observation
            for observation in executed.effect_observations
            if observation.obligation not in matching
        )
    else:
        obligations = target_expectation.obligations
        retained_observations = executed.effect_observations
    expectation = EffectExpectation(obligations)
    rebound_policy = replace(
        result.act.policy,
        expectation=expectation,
        expectation_exemption=None,
    )
    rebound_act = replace(result.act, policy=rebound_policy)
    rebound = replace(result, act=rebound_act)
    rebound_executed = replace(
        executed,
        bearing=rebound,
        effect_observations=(*retained_observations, promoted),
    )
    verified = verify_gates(rebound_executed, frame, state, ctx)
    return rebound, replace(
        verified,
        observations=attempt.observations,
        confirmed_correction=attempt.confirmed_correction,
    )


def _finished_event(
    state: _PilotState,
    ctx: _PilotContext,
    journal_channel_tags: frozenset[str],
    journal_acc_names: frozenset[str],
    *,
    reached: bool,
    reason: str,
) -> PilotEvent:
    """Build the single terminal recording shape for every loop exit."""

    return PilotEvent(
        "finished",
        state.work.state.scan_id,
        {
            "reached": reached,
            "steps": tuple(state.steps),
            "journey": tuple(state.journey),
            "knowledge": _knowledge_payload(state, ctx.compass),
            "root_route": ctx.route or state.recorded_root_route,
            "work": state.work,
            "reason": reason,
            "plan_journal": _build_plan_journal(
                state,
                state.work,
                journal_channel_tags,
                journal_acc_names,
            ),
        },
    )


def _stuck_event(
    state: _PilotState,
    ctx: _PilotContext,
    frame: _IterationFrame | None,
    reason: str,
    *,
    candidate_count: int,
    diagnosis: Stuck | None = None,
) -> PilotEvent:
    """Build the common terminal-stuck diagnostic shape."""

    data: dict[str, Any] = {
        "reason": reason,
        "distance": frame.distance_before if frame is not None else None,
        "candidate_count": candidate_count,
        "nogoods_at_key": (
            len(ctx.compass.knowledge.nogood_identities(frame.key)) if frame is not None else 0
        ),
        "terminal": True,
    }
    if diagnosis is not None:
        data["diagnosis"] = diagnosis
    return PilotEvent("stuck", state.work.state.scan_id, data)


def _stopped_events(
    state: _PilotState,
    ctx: _PilotContext,
    frame: _IterationFrame | None,
    reason: str,
    journal_channel_tags: frozenset[str],
    journal_acc_names: frozenset[str],
    *,
    candidate_count: int,
    diagnosis: Stuck | None = None,
) -> Iterator[PilotEvent]:
    """Emit one failed terminal sequence and restore its checkpoint world."""

    yield _stuck_event(
        state,
        ctx,
        frame,
        reason,
        candidate_count=candidate_count,
        diagnosis=diagnosis,
    )
    if state.checkpoints:
        state.load_world(state.checkpoints[-1].world)
    yield _finished_event(
        state,
        ctx,
        journal_channel_tags,
        journal_acc_names,
        reached=False,
        reason=reason,
    )


def _pilot_loop_events(
    plc: PLC,
    ctx: _PilotContext,
) -> Iterator[PilotEvent]:
    """Run the PILOT loop as a structured event stream."""

    assert_recovery_inactive("invoke the drive loop")
    # Semantic sets for the plan journal (see ``_build_plan_journal``): the
    # channel registers (opaque-loop tags + each pipeline role's
    # ``channel_tag``) pick the transition label; the accumulator registers
    # (from every accumulating instruction's profile, incl. harness couplings)
    # split accelerator patches from command inputs.  Both are static for the
    # life of this loop, so computed once here rather than per journal build.
    journal_channel_tags = frozenset(ctx.opaque_loop) | frozenset(
        role.channel_tag for role in ctx.pipeline_roles
    )
    journal_acc_names = frozenset(
        owner.profile.accumulator.name
        for owner in iter_advance_owners(ctx.program, harness=getattr(plc, "_harness", None))
        if owner.profile.accumulator is not None
    )
    state = _PilotState(
        world=_World(
            work=plc,
            committed_acts=pvector([]),
            best_trend=None,
            pilot_rungs=pvector([]),
            dwell_scans=0,
        ),
        key_config=ctx.key_config,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
        search_start_scan=plc.state.scan_id,
    )
    invocation_snapshot = dict(state.work.state.tags)
    state.invocation_checkpoint = _CausalCheckpoint(
        key=(
            _pilot_world_key(
                invocation_snapshot,
                state.key_config,
                state.pilot_rungs,
                state.active_requirements,
            )
            if state.key_config is not None
            else None
        ),
        world=state.snapshot_world(),
        objective=BearingObjective(ctx.target),
        configured_inputs=ctx.configured_inputs | _configured_input_names(state.work),
    )
    # The target-relative earned-work model (earned_work.py): event-earned
    # ordinals the threshold-masked search key deliberately aliases.  Static
    # for the loop's life; knowledge side (never reverted). Best-effort: an
    # empty model leaves target-relative coordinates uncredited.
    try:
        state.earned_work = build_earned_work(
            ctx.pdg,
            ctx.program,
            ctx.target.tag,
            ctx.key_config,
            steerable=ctx.steerable,
            clear_only=ctx.clear_only,
            edge_tags=frozenset(ctx.edge_tags),
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            channel_tags=frozenset(role.channel_tag for role in ctx.pipeline_roles),
            harness=getattr(plc, "_harness", None),
        )
    except Exception:  # noqa: BLE001 — diagnostics must not break the drive
        logger.debug("pilot: earned-work build failed", exc_info=True)

    # At scan 0, calc-computed intermediates are still at defaults and may
    # trivially satisfy conditions that fail once rungs execute (for example,
    # PV >= Lower where Lower is calculated from SetPoint). Preserve the
    # established one-scan landing, but retain its causal source and exact
    # ordered execution truth instead of hiding the step.
    bootstrap_execution = _execute_bootstrap_scan(state, ctx)
    _run_shadow_hook(
        _record_shadow_bootstrap,
        state,
        ctx,
        bootstrap_execution,
        remaining_budget=state.remaining_search_scans(ctx.max_scans),
    )

    yield PilotEvent(
        "started",
        state.work.state.scan_id,
        {
            "target": (ctx.target.tag, ctx.target.value),
            "steerable_count": len(ctx.steerable),
            "opaque_loop": ctx.opaque_loop,
            "pipeline_roles": ctx.pipeline_roles,
            "pipeline_internal_tags": ctx.pipeline_internal_tags,
            "route": ctx.route,
            "bootstrap_execution": (
                bootstrap_execution.diagnostic_snapshot()
                if bootstrap_execution is not None
                else None
            ),
            "active_requirements": tuple(
                requirement.diagnostic_snapshot() for requirement in state.active_requirements
            ),
        },
    )
    for requirement in state.active_requirements:
        yield PilotEvent(
            "requirement_activated",
            requirement.deadline.scan_id,
            {"requirement": requirement.diagnostic_snapshot()},
        )

    # Each turn reads the current world and builds candidate modes. Every mode
    # executes and verifies on a fork inside steer.py, after which the loop
    # applies its observations. A gate-approved fork is then committed and sent
    # to progress.py, which may checkpoint it, keep a departure pending, or
    # investigate and revert it. Rejected modes fall through to the next mode in
    # the same turn.
    # ``max_scans`` counts new search scans from this invocation's start.
    # Accepted productive coasts credit their dwell back (see
    # ``_World.dwell_scans``); tentative fork scans still count until their
    # operation is accepted. An armed self-advancing dwell — a 39k-scan dry
    # timer the coast rides — is the machine doing its own work, not the pilot
    # spending effort.
    last_frame: _IterationFrame | None = None
    last_frontier: tuple[_ActionPair, ...] = ()
    while state.search_scans < ctx.max_scans:
        snap = dict(state.work.state.tags)
        if target_reached(snap, ctx.target.tag, ctx.target.value, ctx.target.predicate):
            _run_shadow_hook(_record_shadow_proved, state)
            _promote_probationary_corrections(state)
            if state.steps:
                # The terminal let-run's span extends to the actual finish scan;
                # rewrite the last step (and its journey twin, the same object) so
                # both the clean path and the journey carry the true coast length.
                state.extend_last_step(state.work.state.scan_id)
            yield _finished_event(
                state,
                ctx,
                journal_channel_tags,
                journal_acc_names,
                reached=True,
                reason="target reached",
            )
            return

        requirements_before_repair = len(state.active_requirements)
        shadow_requirements_before_repair = _shadow_requirement_identities(state)
        receipts_before_repair = len(state.expectation_receipts)
        failures_before_repair = len(state.failed_effect_receipts)
        repair = _repair_one_active_requirement(state, ctx)
        for active in state.active_requirements[requirements_before_repair:]:
            yield PilotEvent(
                "requirement_activated",
                active.deadline.scan_id,
                {"requirement": active.diagnostic_snapshot()},
            )
        for receipt in state.expectation_receipts[receipts_before_repair:]:
            yield PilotEvent(
                "expectation_committed",
                state.work.state.scan_id,
                {"receipt": receipt.diagnostic_snapshot()},
            )
        for receipt in state.failed_effect_receipts[failures_before_repair:]:
            yield PilotEvent(
                "failed_effect_explained",
                state.work.state.scan_id,
                {"receipt": receipt.diagnostic_snapshot()},
            )
        _run_shadow_hook(
            _record_shadow_requirement_delta,
            state,
            shadow_requirements_before_repair,
            identity=("requirement-repair", state.work.state.scan_id, repair.detail),
        )
        if repair.declined:
            _run_shadow_hook(_record_shadow_abandoned, state, TheoryTermination.CONFLICT)
            yield from _stopped_events(
                state,
                ctx,
                last_frame,
                repair.detail,
                journal_channel_tags,
                journal_acc_names,
                candidate_count=0,
            )
            return
        if repair.repaired:
            yield PilotEvent(
                "requirement_locally_repaired",
                (
                    repair.requirement.deadline.scan_id
                    if repair.requirement is not None
                    else state.work.state.scan_id
                ),
                {
                    "requirement": repair.requirement.diagnostic_snapshot()
                    if repair.requirement is not None
                    else None,
                    "assignments": repair.assignments,
                    "detail": repair.detail,
                },
            )
            _run_shadow_hook(
                _record_shadow_repair_result,
                state,
                requirement=repair.requirement,
                assignments=repair.assignments,
                remaining_budget=state.remaining_search_scans(ctx.max_scans),
            )
            # The repaired landing is the new live tip. Re-enter the outer loop
            # so Orientation reads that world from scratch.
            continue
        if repair.attempted and repair.knowledge_changed:
            # A rejected nested transaction produced stronger exact evidence.
            # Recalculate its schedule from the same causal source; never keep
            # the disposable prediction or an old action suffix.
            continue

        # Compass owns the current-world read and returns one world-bound result.
        raw_world = OrientationWorld(
            world_key=(),
            snapshot=dict(state.work.state.tags),
            frame=None,
            state=state,
            context=ctx,
            key_config=state.key_config,
        )
        target = ctx.target
        constraints = NavigationConstraints(
            avoid_predicate=ctx.avoid_pred,
            active_requirements=tuple(state.active_requirements),
        )
        result = ctx.compass.orient(raw_world, target, constraints)
        orientation_read = result.orientation
        if orientation_read is None:
            raise RuntimeError("Compass orientation omitted its current-world reading")
        orientation_world = orientation_read.world
        candidates = orientation_read.candidates
        frame = orientation_world.frame
        result, _recovery_program_step = _preempt_recovery_action_with_program_coast(
            result,
            frame,
            state,
            ctx,
            target,
        )
        last_frame = frame
        frontier = result.objective.frontier if isinstance(result, Bearing) else result.frontier
        last_frontier = frontier
        _prepare_oriented_result(state, result, orientation_world, frame)
        state.watch_tags.extend(sorted(frame.tree.pivot_tags() - set(state.watch_tags)))
        frame_lever_notes: dict[str, str] = {}
        for action in frame.raw_trace_action_details:
            if action.note:
                # Same physical lever may now retain several alternative
                # producer expectations. Diagnostics keep the established
                # first selected path rather than letting a later alternative
                # silently replace its note.
                frame_lever_notes.setdefault(action.tag, action.note)
        state.lever_notes.update(frame_lever_notes)
        for branch in frame.tree.ordered_crossing_branches():
            for action in branch.actions:
                if action.note:
                    state.lever_notes[action.tag] = action.note
        yield from _record_pending_landing(frame, state)
        yield PilotEvent(
            "iteration", state.work.state.scan_id, _iteration_payload(frame, state, ctx)
        )
        yield PilotEvent(
            "candidates_built",
            state.work.state.scan_id,
            _candidates_built_payload(candidates, state.lever_notes),
        )

        if isinstance(result, NeedProbe):
            observations = probe_live_guard_frontiers(frame, state, ctx)
            ctx.compass, changed = ctx.compass.apply(observations)
            ctx.compass, _ = ctx.compass.apply((ProbeExhaustedObservation(frame.key),))
            yield PilotEvent(
                "skiff",
                state.work.state.scan_id,
                {
                    "observations": len(observations),
                    "reason": result.request.reason,
                    "changed": changed,
                },
            )
            # The bounded probe-count receipt always changes navigation
            # knowledge, even when no new live-guard observation was found.
            # Re-read until Orientation returns the complete-world Stuck.
            continue

        if isinstance(result, Stuck):
            terminal_reason = _with_avoid_reason(
                _stopped_reason(),
                state,
                ctx,
                frame,
            ) + _frontier_clause(frontier, frame.snap)
            _run_shadow_hook(_record_shadow_abandoned, state, TheoryTermination.STUCK)
            yield from _stopped_events(
                state,
                ctx,
                frame,
                terminal_reason,
                journal_channel_tags,
                journal_acc_names,
                candidate_count=len(candidates.options) if candidates is not None else 0,
                diagnosis=result,
            )
            return

        assert isinstance(result, Bearing)
        act = result.act
        try_event = _act_event(
            "try",
            act,
            state.work.state.scan_id,
            rationale=result.rationale,
            prerequisites=result.prerequisites,
            target_tag=ctx.target.tag,
        )
        if try_event is not None:
            yield try_event

        seen_keys_before_commit = frozenset(state.seen_keys)
        requirements_before = len(state.active_requirements)
        receipts_before = len(state.expectation_receipts)
        failures_before = len(state.failed_effect_receipts)
        transition = _transition_once(
            state,
            ctx,
            target,
            constraints,
            oriented=result,
        )
        attempt = transition.attempt
        assert attempt is not None
        _run_shadow_hook(
            _record_shadow_transition,
            state,
            transition.shadow_observation,
            remaining_budget=state.remaining_search_scans(ctx.max_scans),
        )
        for requirement in state.active_requirements[requirements_before:]:
            yield PilotEvent(
                "requirement_activated",
                requirement.deadline.scan_id,
                {"requirement": requirement.diagnostic_snapshot()},
            )
        for receipt in state.expectation_receipts[receipts_before:]:
            yield PilotEvent(
                "expectation_committed",
                state.work.state.scan_id,
                {"receipt": receipt.diagnostic_snapshot()},
            )
        for receipt in state.failed_effect_receipts[failures_before:]:
            yield PilotEvent(
                "failed_effect_explained",
                state.work.state.scan_id,
                {"receipt": receipt.diagnostic_snapshot()},
            )

        if attempt.trial is None:
            rejected_event = _act_event(
                "rejected",
                act,
                state.work.state.scan_id,
                attempt=attempt,
            )
            assert rejected_event is not None
            try:
                yield rejected_event
            finally:
                _release_attempt_projections(attempt)
            continue

        trial = transition.trial
        assert trial is not None
        accepted_event = _act_event(
            "accepted",
            act,
            trial.attempt.pulse.fork.state.scan_id,
            trial=trial,
            frame=frame,
            state=state,
            seen_keys=seen_keys_before_commit,
        )
        assert accepted_event is not None
        try:
            yield accepted_event
            requirements_before_monitor = _shadow_requirement_identities(state)
            yield from _monitor_committed_trial(
                trial,
                frame,
                state,
                ctx,
                continuation_hop=transition.continuation_hop,
            )
            _run_shadow_hook(
                _record_shadow_requirement_delta,
                state,
                requirements_before_monitor,
                identity=(
                    "post-commit",
                    transition.shadow_observation.identity
                    if transition.shadow_observation is not None
                    else (),
                ),
            )
            _run_shadow_hook(
                _record_shadow_advance,
                state,
                transition.shadow_observation,
                remaining_budget=state.remaining_search_scans(ctx.max_scans),
            )
        finally:
            _release_attempt_projections(attempt)
        state.last_wait_log = None
        continue

    # ── This invocation spent its relative search budget ──
    # Unproductive scans that drain the budget are a stall, not a wrap-up:
    # route the terminal through a fresh frame so the reason names the
    # outstanding frontier, and revert to the last checkpoint like the stuck
    # exits do ("How we fail" #1 — every stop points at a named leaf).
    snap = dict(state.work.state.tags)
    reached = target_reached(snap, ctx.target.tag, ctx.target.value, ctx.target.predicate)
    if not reached:
        frame = last_frame
        reason = _with_avoid_reason(
            f"budget exhausted ({state.search_scans} scans searched + {state.dwell_scans} waited)",
            state,
            ctx,
            frame,
        ) + _frontier_clause(last_frontier, frame.snap if frame is not None else None)
        _run_shadow_hook(_record_shadow_abandoned, state, TheoryTermination.BUDGET)
        yield from _stopped_events(
            state,
            ctx,
            frame,
            reason,
            journal_channel_tags,
            journal_acc_names,
            candidate_count=0,
        )
        return
    _run_shadow_hook(_record_shadow_proved, state)
    yield _finished_event(
        state,
        ctx,
        journal_channel_tags,
        journal_acc_names,
        reached=True,
        reason="target reached",
    )


def _pilot_loop(
    plc: PLC,
    ctx: _PilotContext,
    *,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> _DriveOutcome:
    """Run the PILOT loop and assemble its terminal event as a named result.

    ``journey`` is the full attempt log, including reverted rounds. ``reason``
    is the terminal diagnostic on failure and ``None`` when reached.
    ``knowledge`` carries the recording fields that survive a world revert.
    """
    final: PilotEvent | None = None
    for event in _pilot_loop_events(plc, ctx):
        if on_event is not None:
            on_event(event)
        if event.kind == "finished":
            final = event

    if final is None:
        return _DriveOutcome(
            reached=False,
            work=plc,
            journal=(),
            journey=(),
            reason=None,
            knowledge={},
            root_route=None,
        )
    reached = bool(final.data["reached"])
    return _DriveOutcome(
        reached=reached,
        work=final.data["work"],
        journal=tuple(final.data.get("plan_journal", ())),
        journey=tuple(final.data.get("journey", ())),
        reason=None if reached else final.data.get("reason"),
        knowledge=dict(final.data.get("knowledge", {})),
        root_route=final.data.get("root_route"),
    )


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------


def _harness_couplings(plc: PLC) -> tuple[tuple[str, str], ...]:
    """The ``(en, fb)`` pairs the Harness still synthesizes on *plc*, for the
    linked-feedback diagnostic.  Empty when there is no harness (no couplings)
    or every coupling was ``unlink``-ed away."""
    harness = getattr(plc, "_harness", None)
    if harness is None:
        return ()
    return tuple((c.en_name, c.fb_name) for c in harness.couplings())


def _linked_feedback_block(
    target_tag: str,
    target_value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    couplings: tuple[tuple[str, str], ...],
) -> str | None:
    """Honest diagnostic for an unreachable target gated by a harness link.

    When the target's backward-trace route contains both a synthesized feedback
    tag ``fb`` *and* its driver ``en`` (the ``link=`` source), the Harness holds
    ``fb`` lockstep with ``en`` — so the moment the route drives ``en`` to its
    active value, the link drives ``fb`` to the opposite of what the route needs
    (valve open ⇒ flow sensor reads active, defeating the "no flow" watchdog).
    PILOT may not steer ``fb`` (the Harness owns it), so the target is
    unreachable until the link is defeated.  Returns a message naming the
    offending link(s) and the ``unlink=`` override, or ``None`` if no link gates
    the route (then the caller falls back to the generic budget reason).
    """
    if not couplings:
        return None
    try:
        tree = trace_back(target_tag, target_value, snapshot, pdg, program, steerable)
    except UnsupportedConstruct:
        raise
    except Exception:  # noqa: BLE001 — diagnostic only; never mask the real failure
        return None
    route_tags = {n.tag for n in tree.iter_nodes()}
    blockers = [
        (en, fb)
        for en, fb in couplings
        if fb in route_tags and en in route_tags and fb not in steerable
    ]
    if not blockers:
        return None
    links = ", ".join(f"{fb}<-{en}" for en, fb in blockers)
    names = ", ".join(repr(fb) for _en, fb in blockers)
    return (
        f"pilot: {target_tag}={target_value!r} is blocked by physical link(s) "
        f"{links}; the harness holds the sensor lockstep with its driver, so it "
        f"cannot rest at the value this route needs. Retry with unlink=[{names}] "
        f"to model a dead sensor (fault injection)."
    )


def _target_is_value_route(target_predicate: Any) -> bool:
    """Does this target get route enumeration?

    Any concrete equality target — ``Bool == True``, ``Bool == False``, or a
    word ``tag == value`` — is a frozen value the route machinery can enumerate
    writers/OR-arms for (``_can_produce`` against that value).  A live relational
    predicate (``State > 5``) is *not*: its goal is the relation, not a frozen
    value, so ``target_value`` is only a display representative and there is no
    producible-value writer set to route over.  Those targets flow unlocked and
    are honestly reported without a ``RouteTaken``.
    """
    return target_predicate is None


def _route_name(route: TraceChoice) -> str:
    """Human name for a route."""
    if route.route_condition is not None:
        tag, value = route.route_condition
        return tag if value is True else f"{tag}=={value!r}"
    return route.label


def _build_route_taken(
    default: TraceChoice,
    survivors: tuple[TraceChoice, ...],
    steerable: frozenset[str],
) -> RouteTaken:
    """Describe the chosen *default* route plus the routes not taken.

    Models the fork as one pivot whose ``alternatives`` are the other surviving
    routes. ``salient`` is True when any route in the fork is
    gated by a non-steerable discriminator (an internal coil/state the engineer
    commits to) — the trivial all-input fork (``Or(Auto, Manual)``) stays
    non-salient and hidden from the headline.
    """
    others = tuple(ch for ch in survivors if ch.id != default.id)
    alternatives = tuple(RouteAlt(label=_route_name(ch)) for ch in others)
    conditions = [default.route_condition, *(ch.route_condition for ch in others)]
    salient = any(
        condition is not None and condition[0] not in steerable for condition in conditions
    )
    dtag, dvalue = (
        default.route_condition if default.route_condition is not None else (default.label, True)
    )
    pivot = RoutePivot(
        tag=dtag,
        value=dvalue,
        label=_route_name(default),
        kind="writer" if default.writer_locks else "or-arm",
        avoid_hint=default.route_condition,
        alternatives=alternatives,
        salient=salient,
    )
    return RouteTaken(
        label=_route_name(default),
        pivots=(pivot,),
        dominant=len(survivors) <= 1,
    )


def _report_selected_route(
    prepared: RouteTaken | None,
    selected: TraceChoice | None,
) -> RouteTaken | None:
    """Make the public route receipt name the route that actually finished.

    ``prepared`` describes the initially preferred fork so the engineer can see
    its alternatives before execution. If the route that ultimately reaches the
    target differs, rotate the same root pivot around that result. This is
    reporting only; no alternative list feeds back into navigation.
    """

    if prepared is None or selected is None or not prepared.pivots:
        return prepared
    selected_name = _route_name(selected)
    pivot = prepared.pivots[0]
    if pivot.label == selected_name:
        return prepared

    alternatives = [
        RouteAlt(label=pivot.label),
        *(alt for alt in pivot.alternatives if alt.label != selected_name),
    ]
    selected_condition = selected.route_condition
    selected_tag, selected_value = (
        selected_condition if selected_condition is not None else (selected.label, True)
    )
    return RouteTaken(
        label=selected_name,
        pivots=(
            RoutePivot(
                tag=selected_tag,
                value=selected_value,
                label=selected_name,
                kind="writer" if selected.writer_locks else "or-arm",
                avoid_hint=selected_condition,
                alternatives=tuple(alternatives),
                salient=pivot.salient,
            ),
        ),
        dominant=prepared.dominant,
    )


def _prepare_route(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    *,
    target_predicate: Any = None,
    avoid_pred: Any = None,
) -> RouteTaken | None:
    """Describe the preferred current-world route.

    Works for any concrete equality target — ``Bool == True``, ``Bool == False``,
    or a word ``tag == value``; a live relational predicate gets no route (see
    :func:`_target_is_value_route`).  ``how()`` never reports ambiguous: it
    enumerates the routes, prunes any that ``avoid=`` forbids, ranks the
    cheapest survivor (gate-eligible routes preferred, trace score next, rung
    order breaking ties), and records the alternatives on the returned
    :class:`RouteTaken`. Execution remains unlocked so every current-world read
    can choose any admissible root route.
    """
    snapshot = dict(plc.state.tags)
    if not (
        _target_is_value_route(target_predicate)
        and not _values_match(snapshot.get(target_tag), target_value)
    ):
        return None
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, program)
    choices, traced = rank_trace_choices(
        target_tag,
        target_value,
        snapshot,
        pdg,
        program,
        steerable,
        constraints=TraceReadConstraints(
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            avoid_pred=avoid_pred,
        ),
    )
    if not choices:
        return None
    if not traced:
        return None
    default = traced[0][0]
    survivors = tuple(choice for choice, _tree in traced)
    return _build_route_taken(default, survivors, steerable)


# ---------------------------------------------------------------------------
# Prover context — value domains + state key config
# ---------------------------------------------------------------------------


def _build_prover_context(
    program: Any,
    snapshot: dict[str, Any],
) -> _ProverContext:
    """Build prover context for value domains and state key projection.

    Fields are ``None`` on failure, so PILOT falls back to Bool-only probing,
    pivot-tag state keys, and local static evidence.
    """
    try:
        from dataclasses import replace as _replace

        from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel
        from pyrung.core.analysis.pilot.evidence import build_transition_evidence
        from pyrung.core.analysis.prove import _build_explore_context
        from pyrung.core.analysis.prove.passes import _OptConfig
        from pyrung.core.analysis.prove.results import Intractable

        opt = _replace(_OptConfig(), domains_only=True)
        compiled = _compile_kernel(program, blockless=True, proof_metadata=True)
        ctx = _build_explore_context(
            program,
            _opt_config=opt,
            compiled=compiled,
            initial_state=snapshot,
            allow_partial=True,
        )
        if isinstance(ctx, Intractable):
            return _ProverContext()
        nd = getattr(ctx, "nondeterministic_dims", None)
        stateful = getattr(ctx, "stateful_dims", None)
        evidence = build_transition_evidence(ctx)
        if nd:
            logger.info("pilot: nd_domains ready (%d dims)", len(nd))

        # Build state key config from ExploreContext.
        #
        # The pilot's macro-state key needs the *pre-elision* stateful set.
        # Elision drops scan-local registers because BFS enumerates inputs, so a
        # register that is a pure function of the inputs each scan is redundant in
        # the BFS key.  The pilot does the opposite — it *holds* inputs and
        # *observes* registers — so a scan-local channel (e.g. a config/mode
        # register decoded from a command) is the observable proxy for its own
        # steering; dropping it makes an establish move (change the channel) read
        # as SPIN.  Restore the elided tags, appended after the originals so the
        # done/threshold spec indices (which point into the original positions)
        # stay valid.
        stateful_names = ctx.stateful_names + tuple(
            sorted(set(ctx.elided_tags) - set(ctx.stateful_names))
        )
        done_specs = ctx.state_key_done_specs
        threshold_vector_specs = ctx.threshold_vector_specs

        acc_names: set[str] = set()
        for spec in done_specs:
            acc_names.add(spec.acc_name)
        for spec in threshold_vector_specs:
            acc_names.add(spec.acc_name)
        acc_indices = frozenset(i for i, name in enumerate(stateful_names) if name in acc_names)

        if not stateful_names:
            logger.info("pilot: stateful_names empty, falling back to pivot_tags")
            return _ProverContext(
                nd_domains=nd,
                stateful_domains=stateful,
                evidence=evidence,
            )

        key_config = _StateKeyConfig(
            stateful_names=stateful_names,
            done_specs=done_specs,
            threshold_vector_specs=threshold_vector_specs,
            acc_indices=acc_indices,
        )
        logger.info(
            "pilot: state key ready (%d dims, %d done, %d threshold, %d acc masked)",
            len(stateful_names),
            len(done_specs),
            len(threshold_vector_specs),
            len(acc_indices),
        )
        return _ProverContext(
            nd_domains=nd,
            stateful_domains=stateful,
            key_config=key_config,
            evidence=evidence,
        )
    except Exception:  # noqa: BLE001
        logger.debug("pilot: context build failed", exc_info=True)
        return _ProverContext()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _relational_target_atom(cond: Any) -> Any | None:
    """Build a simplified inequality ``Atom`` from a ``Compare*`` target, or None.

    Maps the ordered comparisons (``<``, ``<=``, ``>``, ``>=``) to their atom
    forms so a relational ``how(A > B)`` target rides the same trace machinery as
    a relational prerequisite (live predicate + reactive levers + coast).  The
    operand is the RHS tag name (a *live* threshold) or a literal.
    """
    from pyrung.core.analysis.simplified import Atom
    from pyrung.core.condition import CompareGe, CompareGt, CompareLe, CompareLt
    from pyrung.core.tag import Tag

    forms = {CompareLt: "lt", CompareLe: "le", CompareGt: "gt", CompareGe: "ge"}
    form = forms.get(type(cond))
    if form is None:
        return None
    tag = cond.tag
    tag_name = tag.name if isinstance(tag, Tag) else str(tag)
    operand = cond.value.name if isinstance(cond.value, Tag) else cond.value
    return Atom(
        tag=tag_name,
        form=form,
        operand=operand,
        operand_is_tag=isinstance(cond.value, Tag),
    )


def _parse_one(cond: Any) -> tuple[str, Any, Any]:
    """Extract ``(tag_name, target_value, predicate)`` from ONE condition.

    Accepts:
    - A Tag object (implies ``tag == True``)
    - A ``tag == value`` comparison (CompareEq)
    - A relational comparison ``A < / <= / > / >= B`` — returned as a live
      ``predicate`` Atom (the goal is the relation, not a frozen value); the
      ``(tag, value)`` pair is a representative for display/keying only.
    """
    from pyrung.core.condition import CompareEq
    from pyrung.core.tag import Tag

    if isinstance(cond, Tag):
        return cond.name, True, None

    if isinstance(cond, CompareEq):
        tag = cond.tag
        tag_name = tag.name if isinstance(tag, Tag) else str(tag)
        value = cond.value
        if isinstance(value, Tag):
            # The RHS is a Tag, not a concrete value — it would ride through the
            # trace as a TagExpr and crash the (unhashable) crossings machinery.
            # Require an explicit scalar so the target is a frozen value; for a
            # readonly constant (a named-array/enum element) point at its literal.
            hint = f" (e.g. {value.name}.default = {value.default!r})" if value.readonly else ""
            raise ValueError(
                f"pilot: how() target {tag_name} == {value.name!r} compares against a "
                f"Tag, not a concrete value. Pass the value it stands for{hint} or a "
                f"literal so the target is a frozen scalar."
            )
        return tag_name, value, None

    atom = _relational_target_atom(cond)
    if atom is not None:
        return atom.tag, atom.operand, atom

    raise ValueError(
        f"pilot: cannot extract a target from {cond!r}.  Pass a Tag (Bool target), "
        "tag == value, or a relational comparison (tag < / <= / > / >= value)."
    )


def _parse_targets(*conditions: Any) -> list[tuple[str, Any, Any]]:
    """Extract one ``(tag, value, predicate)`` per condition (multi-target goals)."""
    if not conditions:
        raise ValueError("pilot: how() requires at least one target condition")
    return [_parse_one(c) for c in conditions]


def _parse_target(*conditions: Any) -> tuple[str, Any, Any]:
    """Single-target parse — for the diagnostic/live entry points."""
    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")
    return _parse_one(conditions[0])


def _single_target_plan(
    setup: _DriveSetup,
    outcome: _DriveOutcome,
    target_tag: str,
    target_value: Any,
    route_taken: RouteTaken | None,
    *,
    include_journal: bool,
) -> Plan:
    """Assemble the common fork/live single-target result without policy drift."""

    linked_block = (
        None
        if outcome.reached
        else _linked_feedback_block(
            target_tag,
            target_value,
            setup.diag_snapshot,
            setup.pdg,
            setup.program,
            setup.steerable,
            _harness_couplings(setup.work),
        )
    )
    return Plan(
        reachable=outcome.reached,
        target_tag=target_tag,
        target_value=target_value,
        fork=outcome.work if outcome.reached else None,
        reason=linked_block or outcome.reason,
        status=(
            PlanStatus.REACHED
            if outcome.reached
            else PlanStatus.CANNOT_REACH
            if linked_block is not None
            else PlanStatus.STOPPED
        ),
        route=(
            _report_selected_route(route_taken, outcome.root_route) if outcome.reached else None
        ),
        journal=outcome.journal if include_journal else (),
        anchor_scan=setup.anchor_scan,
        journey=outcome.journey,
        hold_log=outcome.knowledge.get("hold_log", ()),
        lever_notes=outcome.knowledge.get("lever_notes", {}),
        avoid_names=outcome.knowledge.get("avoid_names", ()),
    )


def pilot_events(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events.

    ``unlink`` frees the named harness-feedback tags for fault injection (see
    :func:`pilot_how`). ``avoid_pred`` excludes routes, actions, and observed
    states the same way ``how(avoid=...)`` does.
    """
    target_tag, target_value, target_predicate = _parse_target(*conditions)
    setup = _prepare_drive(plc, unlink=unlink)
    ctx, _route_taken = _prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
    )
    yield from _pilot_loop_events(setup.work, ctx)


def pilot_how(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """PILOT on a fork — drive to the target and return the recording. Nothing changes.

    For a multi-route value target (``Bool == True/False`` or word
    ``tag == value``) PILOT starts with a deterministic preferred route and
    records the route that actually reached the goal on ``Plan.route``;
    ``avoid_pred`` excludes a reported route so PILOT can take another.

    ``unlink`` names harness-synthesized feedback tags to free for fault
    injection: the Harness stops driving them and they become steerable, so
    PILOT can reach faults that the intact physical link would otherwise hold
    out of reach (e.g. a dead flow sensor with the valve open).
    """
    targets = _parse_targets(*conditions)
    if len(targets) > 1:
        return _pilot_how_multi(
            plc,
            targets,
            max_scans=max_scans,
            avoid_pred=avoid_pred,
            unlink=unlink,
            on_event=on_event,
        )
    target_tag, target_value, target_predicate = targets[0]
    setup = _prepare_drive(plc, unlink=unlink)
    ctx, route_taken = _prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
    )
    outcome = _pilot_loop(
        setup.work,
        ctx,
        on_event=on_event,
    )

    return _single_target_plan(
        setup,
        outcome,
        target_tag,
        target_value,
        route_taken,
        include_journal=True,
    )


def _failed_multi_plan(
    label: str,
    targets: tuple[_ActionPair, ...],
    reason: str | None,
    status: PlanStatus,
    anchor_scan: int,
) -> Plan:
    """Build the one unreachable multi-target result shape."""

    return Plan(
        reachable=False,
        target_tag=label,
        target_value=True,
        targets=targets,
        reason=reason,
        status=status,
        anchor_scan=anchor_scan,
    )


def _pilot_how_multi(
    plc: PLC,
    targets: list[tuple[str, Any, Any]],
    *,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """Multi-target ``how(A, B, …)`` — reach one committed scan where every target holds.

    Static read only (``pilot/multitarget.py``): a sound mutual-exclusion prune +
    a clobberer-first order, then the single-target drive loop is run
    sequentially per target on ONE fork.  The fork's recording is the artifact —
    it replays to a state with every target true.  When the static read cannot
    prove ME it falls open to this drive; the final all-targets check is the
    honest oracle (the drive loop is execution truth, never a skiff probe).
    """
    from pyrung.core.analysis.pilot import multitarget as _mt  # noqa: PLC0415

    label = " & ".join(f"{tt}={tv!r}" for tt, tv, _ in targets)
    setup = _prepare_drive(plc, unlink=unlink)

    goal_pairs = tuple((tt, tv) for tt, tv, _ in targets)

    ok, reason, ordered = _mt.analyze(
        setup.diag_snapshot,
        setup.pdg,
        setup.program,
        setup.steerable,
        targets,
    )
    if not ok:
        return _failed_multi_plan(
            label,
            goal_pairs,
            reason,
            PlanStatus.CANNOT_REACH,
            setup.anchor_scan,
        )

    work = setup.work
    compass = setup.compass
    last_knowledge: dict[str, Any] = {}
    last_journey: tuple[Any, ...] = ()
    # The per-target drives run sequentially on ONE fork, so their journals are already
    # in scan order — concatenating them gives the whole passage, not the last leg only.
    journal_steps: list[Any] = []
    for t_tag, t_val, t_pred in ordered:
        if target_reached(dict(work.state.tags), t_tag, t_val, t_pred):
            continue  # already pulled in by an earlier target's drive
        # Same route discipline as single-target how(): infer every admissible
        # current-world route and let Orientation choose among them. ``avoid=``
        # is not tied to any one target, so it constrains every target uniformly.
        ctx, _route_taken = _prepare_target_context(
            setup,
            t_tag,
            t_val,
            t_pred,
            compass=compass,
            max_scans=max_scans,
            avoid_pred=avoid_pred,
            work=work,
        )
        outcome = _pilot_loop(work, ctx, on_event=on_event)
        work = outcome.work
        last_knowledge = outcome.knowledge
        compass = outcome.knowledge.get("compass", compass)
        last_journey = outcome.journey
        journal_steps.extend(outcome.journal)
        if not outcome.reached:
            detail = f"; {outcome.reason}" if outcome.reason else ""
            return _failed_multi_plan(
                label,
                goal_pairs,
                (
                    f"pilot: could not establish {t_tag}={t_val!r} while holding the "
                    f"other target(s){detail}"
                ),
                PlanStatus.STOPPED,
                setup.anchor_scan,
            )

    final = dict(work.state.tags)
    unmet = [(tt, tv) for tt, tv, tp in targets if not target_reached(final, tt, tv, tp)]
    if unmet:
        names = ", ".join(f"{tt}={tv!r}" for tt, tv in unmet)
        return _failed_multi_plan(
            label,
            goal_pairs,
            f"pilot: reached each target individually but {names} did not hold "
            "simultaneously (clobbered during co-establishment).",
            PlanStatus.STOPPED,
            setup.anchor_scan,
        )
    # recording: threaded from the LAST target's drive only (multi runs the loop
    # sequentially per target; the last drive's Knowledge is what survives on ``work``).
    return Plan(
        reachable=True,
        target_tag=label,
        target_value=True,
        targets=goal_pairs,
        fork=work,
        anchor_scan=setup.anchor_scan,
        journal=tuple(journal_steps),
        journey=last_journey,
        hold_log=last_knowledge.get("hold_log", ()),
        lever_notes=last_knowledge.get("lever_notes", {}),
        avoid_names=last_knowledge.get("avoid_names", ()),
    )
