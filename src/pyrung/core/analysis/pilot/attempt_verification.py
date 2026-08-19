"""Receipt-driven verification refinements for one executed PILOT attempt."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pyrung.core.analysis.pilot.recovery_continuation as _recovery_continuation
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.awaited_actions import sibling_producer_family
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    observe_execution_window,
    occurrence_snapshot,
    promote_certified_prefix_target_observation,
    promote_terminal_target_observation,
    terminal_target_replay_scan_ids,
)
from pyrung.core.analysis.pilot.execution import execution_owner
from pyrung.core.analysis.pilot.investigate import investigate_excursion
from pyrung.core.analysis.pilot.navigation_contracts import Bearing, Coast
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.program_step import read_program_step
from pyrung.core.analysis.pilot.theory_evidence import _theory_live_boundary
from pyrung.core.analysis.pilot.types import (
    WorldView,
    _AttemptResult,
    _CausalCheckpoint,
    _ContinuationCheckpoint,
    _IterationFrame,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.verify import verify_excursion_replay, verify_gates
from pyrung.core.analysis.pilot.working_theory import active_theory
from pyrung.core.analysis.pilot.world_key import _pilot_world_key
from pyrung.core.analysis.sp_values import _values_match


def resolve_excursion(
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


def certify_current_target_prefix(
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
    owner = execution_owner(pulse.fork, adoption_scan)
    if owner is None:
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
        execution_ref=owner.epoch.reference,
        landing_occurrence=occurrence_snapshot(historical),
    )


def promote_transient_target_failure(
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
    if _values_match(final_landing_value, terminal_obligation.value):
        # The selected execution landed on its target. There is no transient
        # target loss to promote, and recovery-continuation evidence must not
        # be consulted merely because the target also appeared in projection.
        return result, attempt
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
    theory = active_theory(state.theory_state)
    if promoted is None and theory is not None:
        progress = state.theory_state.ledger.progress[theory.current_progress_id]
        if _theory_live_boundary(state) == progress.provisional_tip:
            promoted = promote_certified_prefix_target_observation(
                target_observations,
                final_landing_value=final_landing_value,
            )
    existing = executed.bearing.expectation
    if promoted is None and attempt.trial is not None and existing is not None:
        checkpoint_scan = _recovery_continuation.repaired_program_continuation(
            state,
            ctx,
            attempt.trial,
            existing,
            execution_work=pulse.fork,
        )
        if checkpoint_scan is not None:
            promoted = _recovery_continuation.promoted_target_suffix_observation(
                target_expectation,
                pulse,
                checkpoint_scan,
            )
        if promoted is None and _recovery_continuation.exact_local_repair_window(
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
    if (
        promoted is None
        and _recovery_continuation.adjacent_continuation_source(
            state,
            pulse,
            prefix_proof,
        )
        is not None
    ):
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
