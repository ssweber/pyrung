"""Receipt-driven verification refinements for one executed PILOT attempt."""

from __future__ import annotations

from dataclasses import replace

import pyrung.core.analysis.pilot.recovery_continuation as _recovery_continuation
from pyrung.core.analysis.pilot.effect_observation import (
    observe_execution_window,
    terminal_target_replay_scan_ids,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    promote_certified_prefix_target_observation,
    promote_terminal_target_observation,
)
from pyrung.core.analysis.pilot.investigation_replay import investigate_excursion
from pyrung.core.analysis.pilot.navigation_contracts import Bearing
from pyrung.core.analysis.pilot.theory_evidence import _theory_live_boundary
from pyrung.core.analysis.pilot.types import (
    _AttemptResult,
    _IterationFrame,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.verify import verify_excursion_replay, verify_gates
from pyrung.core.analysis.pilot.working_theory import active_theory
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
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


def promote_transient_target_failure(
    result: Bearing,
    attempt: _AttemptResult,
    target_expectation: EffectExpectation,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
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
