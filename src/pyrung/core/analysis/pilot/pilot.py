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
from collections.abc import Callable, Iterator
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
    expectation_from_writer,
    fulfilled_expectation_observations,
    obligation_snapshot,
    observe_execution_window,
    occurrence_snapshot,
    promote_terminal_target_observation,
)
from pyrung.core.analysis.pilot.investigate import investigate_excursion
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    BatchPulse,
    Bearing,
    BearingObjective,
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
    classify_bound_operand_authority,
    derive_advance_requirement_from_effect,
    derive_guard_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
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
    PilotEvent,
    WorldView,
    _AcceptedTrial,
    _ActionPair,
    _AttemptResult,
    _BootstrapExecution,
    _CausalCheckpoint,
    _Checkpoint,
    _CommittedAct,
    _ConfirmedCorrection,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _Step,
    _StepContext,
    _World,
)
from pyrung.core.analysis.pilot.verify import verify_excursion_replay, verify_gates
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _rung_identity, _StateKeyConfig
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.pipeline_graph import StaticTransitionGraph
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


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
    overrides = source_work._input_overrides
    configured = frozenset((*overrides.forces, *overrides.pending_patches))
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
            derivation = derive_overwriter_guard_requirement_from_effect(
                effect.observation,
                receipt.projection,
                execution_epoch=receipt.execution_epoch,
                execution_owner=receipt.execution_owner,
                selected_writer=effect.designation.producer,
                source_world_key=receipt.checkpoint.key,
                source_checkpoint=receipt.checkpoint,
                provenance="bootstrap-overwriter",
            )
        _retain_active_requirement(state, derivation.requirement)


def _executed_attempt(attempt: _AttemptResult) -> Any:
    if attempt.trial is not None:
        return attempt.trial.attempt
    return attempt.executed or attempt.excursion_attempt


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
    executed = _executed_attempt(attempt)
    if executed is None:
        return
    fulfilled_obligations = {
        id(item.obligation)
        for item in executed.effect_observations
        if item.disposition == "SURVIVED"
    }
    index = None
    for observation in executed.effect_observations:
        if (
            observation.disposition == "SURVIVED"
            or id(observation.obligation) in fulfilled_obligations
        ):
            continue
        owner = observation.execution_owner
        epoch = observation.execution_epoch
        if owner is None or epoch is None:
            continue
        scans = tuple(
            occurrence.scan_id
            for occurrence in (
                observation.appeared,
                observation.consumer_read,
                observation.displacement,
                observation.displaced_read,
                *observation.observed_reads,
            )
            if occurrence is not None
        )
        scan = max(
            scans,
            default=(
                executed.pulse.action_scan
                if executed.pulse.action_scan is not None
                else (
                    executed.pulse.coast_receipt.end_scan
                    if executed.pulse.coast_receipt is not None
                    else executed.pulse.fork.state.scan_id
                )
            ),
        )
        projection = observation.execution_projection
        if projection is None or projection.scan_id != scan:
            projection = owner._runner()._replay_rung_write_projection_at(scan)
        if projection is None:
            continue
        if observation.disposition in {"ABSENT", "STRANDED"}:
            derivation = derive_guard_requirement_from_effect(
                observation,
                projection,
                execution_epoch=epoch,
                execution_owner=owner,
                selected_writer=observation.obligation.producer,
                source_world_key=checkpoint.key,
                source_checkpoint=checkpoint,
                provenance="steer",
            )
            explanation = derivation.explanation
        else:
            if index is None:
                index = build_advance_index(
                    ctx.program,
                    getattr(checkpoint.world.work, "_harness", None),
                )
            authorities = _bound_operand_authorities(projection, checkpoint, ctx)
            derivation = derive_advance_requirement_from_effect(
                index,
                projection,
                observation,
                operand_authorities=authorities,
                execution_epoch=epoch,
                execution_owner=owner,
                selected_writer=observation.obligation.producer,
                source_world_key=checkpoint.key,
                source_checkpoint=checkpoint,
                provenance="steer",
            )
            if derivation.requirement is None:
                derivation = derive_overwriter_guard_requirement_from_effect(
                    observation,
                    projection,
                    execution_epoch=epoch,
                    execution_owner=owner,
                    selected_writer=observation.obligation.producer,
                    source_world_key=checkpoint.key,
                    source_checkpoint=checkpoint,
                    provenance="steer-overwriter",
                    preserved_values=(
                        ((observation.obligation.tag, observation.obligation.value),)
                        if observation.obligation.terminal_target
                        else ()
                    ),
                )
            explanation = derivation.explanation
        failed = FailedEffectReceipt(
            explanation=explanation,
            observation=observation.diagnostic_snapshot(),
            selected_writer=observation.obligation.producer,
            source_world_key=checkpoint.key,
            checkpoint_owner=checkpoint.owner,
            execution_epoch=epoch,
            execution_owner=owner,
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
        producer_occurrence_objects=tuple(
            item.appeared for item in observations if item.appeared is not None
        ),
        consumer_occurrence_objects=tuple(
            item.consumer_read for item in observations if item.consumer_read is not None
        ),
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
        bootstrap_execution=state.bootstrap_execution,
        active_requirements=list(state.active_requirements),
        expectation_receipts=list(state.expectation_receipts),
        failed_effect_receipts=list(state.failed_effect_receipts),
        requirement_repair_attempts=set(state.requirement_repair_attempts),
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
        provenance=(*local_act.policy.provenance, "active guard requirement"),
    )
    if len(ordered_pairs) == 1:
        return Pulse(policy)
    return BatchPulse(policy)


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


def _compile_bootstrap_guard_schedule(
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
    guard_schedule, guard_detail = _compile_bootstrap_guard_schedule(
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


def _repaired_program_continuation(
    candidate: _PilotState,
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    expectation: EffectExpectation,
) -> bool:
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
        return False
    observations = fulfilled_expectation_observations(
        expectation,
        trial.attempt.effect_observations,
    )
    handoffs = tuple(
        item
        for item in observations
        if item.obligation.tag == channel and item.consumer_read is not None
    )
    if len(handoffs) != 1:
        return False
    handoff = handoffs[0]
    consumer = handoff.consumer_read
    consumer_projection = handoff.execution_projection
    assert consumer is not None
    if consumer_projection is None or consumer_projection.scan_id != consumer.scan_id:
        return False
    consumer_scan = consumer.scan_id

    same_scan_suffix = tuple(
        write
        for write in consumer_projection.writes
        if write.ordinal > consumer.ordinal
        and write.transition.tag_name == channel
        and write.run.enabled
    )
    consumer_operation_runs = consumer.run.rung_occurrences
    if any(
        all(write.run is not operation_run for operation_run in consumer_operation_runs)
        for write in same_scan_suffix
    ):
        # The scan-exit snapshot is a faithful consumer boundary only when any
        # later channel write belongs to that same dynamic consumer operation,
        # including its exact nested branch runs. A sibling/outer writer would
        # already have displaced the handoff before the historical fork on
        # which ProgramStep projects.
        return False

    try:
        handoff_work = fork_with_pilot_rungs(
            candidate.work,
            candidate.pilot_rungs,
            scan_id=consumer_scan,
        )
    except KeyError:
        return False

    handoff_snap = dict(handoff_work.state.tags)
    landing_value = candidate.work.state.tags.get(channel)
    if _values_match(handoff_snap.get(channel), landing_value):
        return False

    probe = _disposable_requirement_state(
        candidate,
        _CausalCheckpoint(
            key=None,
            world=candidate.world.set(work=handoff_work),
            objective=trial.attempt.bearing.objective,
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
        return False
    landing_writers = {
        node.writer_rung
        for node in orientation.world.frame.tree.iter_nodes()
        if node.tag == channel
        and _values_match(node.value, landing_value)
        and node.writer_rung is not None
    }
    if len(landing_writers) != 1:
        return False
    landing_writer = next(iter(landing_writers))
    selected_rung = resolve_rung(ctx.program, ctx.pdg.rung_nodes[landing_writer])
    if selected_rung is None:
        return False

    later_writes = list(same_scan_suffix)
    relevant_projections = [consumer_projection]
    for scan_id in range(consumer_scan + 1, candidate.work.state.scan_id + 1):
        projection = candidate.work._replay_rung_write_projection_at(scan_id)
        if projection is None:
            return False
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
        return False
    landing_occurrence = max(
        landing_occurrences,
        key=lambda write: (write.scan_id, write.ordinal),
    )
    if landing_occurrence.run.rung is not selected_rung:
        return False

    # The whole observed suffix must belong to one retained causal epoch. Scan
    # numbers alone are not occurrence ownership: a fork can reuse them under
    # another execution epoch.
    suffix_owner = candidate.work._causal_lineage.owner_at(consumer_scan)
    if suffix_owner is None or any(
        candidate.work._causal_lineage.owner_at(projection.scan_id) is not suffix_owner
        for projection in relevant_projections
    ):
        return False

    selected_node = ctx.pdg.rung_nodes[landing_writer]
    capture_indices = ctx.pdg.timeline_capture_indices_for_node(landing_writer)
    if selected_node.subroutine is not None:
        if len(capture_indices) != 1:
            return False
        if landing_occurrence.run.caller_rung != next(iter(capture_indices)):
            return False

    def dynamic_invocations(projection: Any) -> frozenset[int | None]:
        return frozenset(
            occurrence.call_invocation
            for occurrence in (*projection.reads, *projection.writes)
            if occurrence.run.rung is selected_rung
        )

    if any(len(dynamic_invocations(projection)) > 1 for projection in relevant_projections):
        return False

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
        return False
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
        return False

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
            return False
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
        return False
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

    return dynamic_address(projected_occurrence) == dynamic_address(landing_occurrence)


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
    local_act = _nested_guard_act(
        source,
        ctx,
        receipt.local_bearing,
        receipt.local_act,
        requirements,
    )
    if local_act is None:
        return _RequirementRepairResult(requirement=requirement, detail="guard repair is ambiguous")
    bearing = _rebound_bearing(source, ctx, receipt.local_bearing, local_act)
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
        )
        executed = _executed_attempt(transition.attempt) if transition.attempt is not None else None
        expectation = local_act.policy.expectation
        if (
            transition.trial is None
            or executed is None
            or expectation is None
            or not _whole_expectation_survived(executed, expectation)
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
        autonomous_continuation = _repaired_program_continuation(
            candidate,
            disposable_ctx,
            transition.trial,
            expectation,
        )
        if not autonomous_continuation:
            tuple(_monitor_trend(transition.trial, transition.frame, candidate, disposable_ctx))
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
        return Succeed(candidate)

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

    for requirement in reversed(state.active_requirements):
        if (
            requirement.status is not RequirementStatus.ACTIVE
            or requirement.phase is not RequirementPhase.STEADY
        ):
            continue
        bootstrap = _repair_bootstrap_requirement(requirement, state, ctx)
        if bootstrap.attempted or bootstrap.detail:
            return bootstrap
        failed = _repair_failed_requirement(requirement, state, ctx)
        if failed.attempted or failed.detail:
            return failed
    return _RequirementRepairResult()


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
    key = (
        _pilot_world_key(
            before_snap,
            state.key_config,
            state.pilot_rungs,
            state.active_requirements,
        )
        if state.key_config is not None
        else None
    )
    checkpoint = _CausalCheckpoint(
        key=key,
        world=state.snapshot_world(),
        objective=BearingObjective(ctx.target),
    )
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
    )


def _prepare_drive(
    plc: PLC,
    *,
    unlink: list[str] | None,
) -> _DriveSetup:
    """Build the shared program/runtime analysis for one public drive."""

    from pyrung.core.analysis.pdg import build_program_graph

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

    key_config = state.key_config
    assert key_config is not None
    pulse = executed.pulse
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


def _transition_once(
    state: _PilotState,
    ctx: _PilotContext,
    target: TargetSpec,
    constraints: NavigationConstraints,
    *,
    oriented: OrientationResult | None = None,
    resolve_excursion: bool = True,
    derive_requirements: bool = True,
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
        )
        if result.expectation is not None or terminal_target_expectation is not None
        else None
    )
    attempt = execute(result, orientation_world)
    if resolve_excursion:
        attempt = _resolve_excursion(attempt, frame, state, ctx)
    if terminal_target_expectation is not None:
        result, attempt = _promote_transient_target_failure(
            result,
            attempt,
            terminal_target_expectation,
            frame,
            state,
            ctx,
        )
        act = result.act
    if derive_requirements:
        _derive_attempt_requirements(
            attempt,
            state,
            ctx,
            expectation_checkpoint,
        )
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
            ctx.compass, _ = ctx.compass.apply(
                (ActionNogoodObservation(frame.key, act_identity(act)),)
            )
        return _IterationTransition(result=result, frame=frame, attempt=attempt)

    trial = _adopt_trial(attempt.trial, frame, state, ctx)
    _retain_expectation_receipt(
        trial,
        act,
        state,
        expectation_checkpoint,
    )
    return _IterationTransition(
        result=result,
        frame=frame,
        attempt=attempt,
        trial=trial,
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
) -> tuple[Bearing, _AttemptResult]:
    """Re-verify an act only after its selected target appeared and was lost."""

    executed = _executed_attempt(attempt)
    if executed is None:
        return result, attempt
    pulse = executed.pulse
    target_observations = observe_execution_window(
        target_expectation,
        pulse.fork,
        scan_before=pulse.scan_before,
        action_scan=(None if result.act.policy.motion.is_coast else pulse.action_scan),
        coast_receipt=pulse.coast_receipt,
        timeline=pulse.timeline,
    )
    promoted = promote_terminal_target_observation(target_observations)
    if promoted is None:
        return result, attempt

    existing = executed.bearing.expectation
    terminal_obligation = target_expectation.obligations[0]
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
            yield rejected_event
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
        yield accepted_event
        yield from _monitor_committed_trial(trial, frame, state, ctx)
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
