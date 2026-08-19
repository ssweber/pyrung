"""Exact program-continuation evidence for a repaired Pilot route.

This module owns the narrow question of whether a corrected consumer handoff
can continue through one exact, unchanged program window.  It never chooses a
general navigation action and never commits the outer Pilot world.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.awaited_actions import sibling_producer_family
from pyrung.core.analysis.pilot.effect_observation import (
    fulfilled_expectation_observations,
    observe_execution_window,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    exact_last_landing_write,
    occurrence_snapshot,
    promote_terminal_target_observation,
)
from pyrung.core.analysis.pilot.execution import MotionKind, execution_owner
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    Bearing,
    ChannelHeading,
    Coast,
    LandingReceiptAuthority,
    NavigationConstraints,
    OrientationResult,
    OrientationWorld,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.program_step import (
    ProgramStepStatus,
    _program_step_from_bearing,
    read_program_step,
)
from pyrung.core.analysis.pilot.requirement_evidence import (
    _disposable_requirement_state,
    _selected_terminal_target_expectation,
)
from pyrung.core.analysis.pilot.types import (
    WorldView,
    _AcceptedTrial,
    _CausalCheckpoint,
    _ContinuationCheckpoint,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _RecoveryContinuation,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key
from pyrung.core.analysis.sp_values import _values_match


def repaired_program_continuation(
    candidate: _PilotState,
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    expectation: EffectExpectation,
    *,
    execution_work: Any | None = None,
) -> int | None:
    """Prove that a corrected consumer handoff folded only target-path work."""

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
        return None
    return landing_occurrence.scan_id


def promoted_target_suffix_observation(
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


def adjacent_continuation_source(
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
        and prefix_proof.execution_ref == tip.execution_ref
        and prefix_proof.landing_occurrence is not None
    )
    pulse_owner = execution_owner(pulse.fork, pulse.scan_before)
    exact_scan_ids = tuple(range(pulse.scan_before + 1, pulse.fork.state.scan_id + 1))
    if (
        not (tip.program_step_certified or ephemeral_prefix)
        or tip.scan_id != pulse.scan_before
        or tip.world_key != current_key
        or pulse_owner is None
        or pulse_owner.epoch.reference != tip.execution_ref
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


def exact_local_repair_window(
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
    return _program_step_from_bearing(trial.attempt.bearing)


def recovery_anchor_program_step(
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
    owner = execution_owner(state.work, state.work.state.scan_id)
    if owner is None or owner.epoch.reference != continuation.tip.execution_ref:
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


def preempt_recovery_action_with_program_coast(
    result: OrientationResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    target: TargetSpec,
) -> tuple[OrientationResult, Any | None]:
    """Prefer a proved unchanged program hop over a harmful fresh action."""

    if not isinstance(result, Bearing):
        return result, None
    if result.act.policy.local_progress is not None:
        return result, None
    step = recovery_anchor_program_step(frame, state, ctx, target)
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
            landing_receipt_authority=LandingReceiptAuthority.PROGRAM_STEP,
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


def advance_recovery_continuation(
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
    if not certified:
        state.recovery_continuation = None
        return False
    owner = execution_owner(pulse.fork, state.work.state.scan_id)
    projections = tuple(pulse.projection_at(scan_id) for scan_id in pulse.kernel_scan_ids)
    if owner is None or any(projection is None for projection in projections):
        state.recovery_continuation = None
        return False
    exact_projections = tuple(projection for projection in projections if projection is not None)
    assert motion is not None
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
                kind="unchanged_coast",
                execution_ref=owner.epoch.reference,
                landing_occurrence=landing_occurrence,
            ),
        ),
    )
    return True
