"""Exact program-continuation evidence for a repaired Pilot route.

This module owns the narrow question of whether a corrected consumer handoff
can continue through one exact, unchanged program window.  It never chooses a
general navigation action and never commits the outer Pilot world.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.awaited_actions import sibling_producer_family
from pyrung.core.analysis.pilot.effect_observation import (
    fulfilled_expectation_observations,
    observe_execution_window,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    promote_terminal_target_observation,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    NavigationConstraints,
    OrientationWorld,
)
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.program_step import (
    ProgramStepStatus,
    read_program_step,
)
from pyrung.core.analysis.pilot.requirement_evidence import (
    _disposable_requirement_state,
)
from pyrung.core.analysis.pilot.trace_read import WorldView
from pyrung.core.analysis.pilot.types import (
    _AcceptedTrial,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
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
