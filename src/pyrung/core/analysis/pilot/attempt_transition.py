"""Execute, verify, and optionally adopt one Compass Bearing."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

import pyrung.core.analysis.pilot.attempt_verification as _attempt_verification
import pyrung.core.analysis.pilot.entry_execution as _entry_execution
import pyrung.core.analysis.pilot.recovery_continuation as _recovery_continuation
import pyrung.core.analysis.pilot.theory_drive as _theory_drive
import pyrung.core.analysis.pilot.trial_commit as _trial_commit
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    CoastObservation,
    EvidenceScope,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    BearingObjective,
    Coast,
    IntrascanPulse,
    NavigationConstraints,
    ObserveScan,
    OrientationResult,
    OrientationWorld,
    ProgramScan,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.progress import (
    _anchor_frame_receipt,
    _install_confirmed_correction,
)
from pyrung.core.analysis.pilot.recovery import assert_recovery_disposable_state
from pyrung.core.analysis.pilot.requirement_evidence import (
    _configured_input_names,
    _derive_attempt_requirements,
    _retain_expectation_receipt,
    _selected_terminal_target_expectation,
)
from pyrung.core.analysis.pilot.steer import execute
from pyrung.core.analysis.pilot.theory_evidence import (
    _theory_live_boundary,
    _theory_transition_from_attempt,
    _TheoryTransitionEvidence,
)
from pyrung.core.analysis.pilot.types import (
    _AcceptedTrial,
    _AttemptResult,
    _CausalCheckpoint,
    _Checkpoint,
    _IterationFrame,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttemptTransition:
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
    theory_transition: _TheoryTransitionEvidence | None = None
    adoption_checkpoint: _CausalCheckpoint | None = None


def record_attempt(
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


def prepare_oriented_result(
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


def transition_once(
    state: _PilotState,
    ctx: _PilotContext,
    target: TargetSpec,
    constraints: NavigationConstraints,
    *,
    oriented: OrientationResult | None = None,
    resolve_excursion: bool = True,
    derive_requirements: bool = True,
    derivation_checkpoint: _CausalCheckpoint | None = None,
    defer_adoption: bool = False,
    record_rejection: bool = True,
) -> AttemptTransition:
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
    # Preserve the exact route alternative selected by this Orientation read.
    # The shared drive context intentionally carries no retained route, but
    # execution and verification of this one bearing must see its chart edge.
    execution_ctx = replace(ctx, route=orientation_read.world.root_route)
    orientation_world = replace(
        orientation_read.world,
        state=state,
        context=execution_ctx,
        key_config=state.key_config or orientation_read.world.key_config,
    )
    frame = orientation_world.frame
    prepare_oriented_result(state, result, orientation_world, frame)
    result, recovery_program_step = (
        _recovery_continuation.preempt_recovery_action_with_program_coast(
            result,
            frame,
            state,
            ctx,
            target,
        )
    )
    if not isinstance(result, Bearing):
        return AttemptTransition(result=result, frame=frame)

    terminal_target_expectation = _selected_terminal_target_expectation(
        frame,
        target,
        ctx,
    )
    act = result.act
    attempt_source_checkpoint = _CausalCheckpoint(
        key=frame.key,
        world=state.snapshot_world(),
        objective=result.objective,
        configured_inputs=ctx.configured_inputs | _configured_input_names(state.work),
    )
    expectation_checkpoint = (
        attempt_source_checkpoint
        if (
            result.expectation is not None
            or terminal_target_expectation is not None
            or isinstance(result.act, (ObserveScan, ProgramScan, IntrascanPulse))
        )
        else None
    )
    requirements_before_theory_recording = _theory_drive._requirement_identities(state)
    attempt = execute(result, orientation_world)
    if resolve_excursion and attempt.excursion_attempt is not None:
        attempt = _attempt_verification.resolve_excursion(attempt, frame, state, ctx)
    prefix_proof = None
    prefix_execution = attempt.executed_attempt
    if terminal_target_expectation is not None and prefix_execution is not None:
        prefix_proof = _attempt_verification.certify_current_target_prefix(
            attempt,
            prefix_execution.pulse.scan_before,
            terminal_target_expectation,
            state,
            ctx,
        )
    if terminal_target_expectation is not None:
        result, attempt = _attempt_verification.promote_transient_target_failure(
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
        continuation_checkpoint = _recovery_continuation.adjacent_continuation_source(
            state,
            executed_for_derivation.pulse,
            prefix_proof,
        )
    landing_checkpoint = (
        attempt_source_checkpoint
        if executed_for_derivation is not None
        and (
            executed_for_derivation.landing_expectation is not None
            or (
                attempt.trial is not None
                and attempt.trial.execution.scan_progress is not None
                and attempt.trial.execution.scan_progress.landing_owns_tip
            )
        )
        else None
    )
    receipt_checkpoint = derivation_checkpoint or expectation_checkpoint or landing_checkpoint
    intrascan_report = None
    causal_checkpoint = continuation_checkpoint or receipt_checkpoint
    crossing = getattr(act, "crossing", None)
    verification_hypothesis = bool(crossing is not None and crossing.verify_required)
    # A verification-required crossing is itself the causal hypothesis.  Its
    # failed downstream expectation explains why verification rejected it, but
    # does not authorize turning that explanation into setup work for the same
    # conjecture.  Let ordinary whole-act nogooding expose a sibling branch.
    if derive_requirements and not verification_hypothesis and not attempt.avoid_names:
        # An avoid receipt is a final Compass admissibility judgment about this
        # path, not evidence that WorkingTheory should repair the rejected
        # execution.  Deriving setup work here would suppress the route nogood
        # below and repeatedly propose the same forbidden coast.
        intrascan_report = _derive_attempt_requirements(
            attempt,
            state,
            ctx,
            causal_checkpoint,
        )
    theory_transition = None
    try:
        theory_transition = _theory_transition_from_attempt(
            state,
            attempt,
            result,
            receipt_checkpoint,
            prior_requirement_identities=requirements_before_theory_recording,
            intrascan_report=intrascan_report,
        )
    except Exception:  # noqa: BLE001 - optional theory conversion cannot change the drive
        logger.debug("pilot: working theory observation failed", exc_info=True)
    record_attempt(attempt, frame, state, ctx, result.objective, act)

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
        if attempt.avoid_names:
            # Compass owns admissibility. An exact avoid receipt rejects this
            # executable act even if the same counterfactual also exposes a
            # temporal requirement or is running as a controlled setup.
            # WorkingTheory must not keep retrying a path the user's constraint
            # has already ruled out.
            ctx.compass, _ = ctx.compass.apply(
                (ActionNogoodObservation(result.world_key, act_identity(act)),)
            )
        elif not record_rejection:
            pass
        elif _theory_drive._records_controlling_need(theory_transition):
            # The act exposed a missing temporal condition. That is exact
            # refinement evidence, not proof that the act is impossible in
            # this world once the condition is composed into the scan.
            pass
        elif attempt.proof_rejection:
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
            proof_scope = EvidenceScope.capture(proof_world_key, frame.snap.items())
            state.proof_rejected_acts.add((proof_scope, act_identity(act)))
        else:
            ctx.compass, _ = ctx.compass.apply(
                (ActionNogoodObservation(result.world_key, act_identity(act)),)
            )
        return AttemptTransition(
            result=result,
            frame=frame,
            attempt=attempt,
            theory_transition=theory_transition,
        )

    if defer_adoption:
        return AttemptTransition(
            result=result,
            frame=frame,
            attempt=attempt,
            trial=attempt.trial,
            theory_transition=theory_transition,
            adoption_checkpoint=receipt_checkpoint,
        )

    trial = _trial_commit.adopt_trial(attempt.trial, frame, state, ctx)
    if isinstance(act, ObserveScan):
        if expectation_checkpoint is None:
            raise RuntimeError("entry observation lost its source checkpoint")
        executed = attempt.executed_attempt
        if executed is None:
            raise RuntimeError("entry observation lost its exact execution")
        _entry_execution.retain_entry_bearing_execution(
            state,
            expectation_checkpoint,
            executed,
        )
    continuation_hop = _recovery_continuation.advance_recovery_continuation(
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
        receipt_checkpoint,
    )
    if theory_transition is not None:
        try:
            theory_transition = replace(
                theory_transition,
                adopted_boundary=_theory_live_boundary(state),
            )
        except Exception:  # noqa: BLE001 - optional theory recording cannot change the drive
            logger.debug("pilot: theory adoption snapshot failed", exc_info=True)
    return AttemptTransition(
        result=result,
        frame=frame,
        attempt=attempt,
        trial=trial,
        continuation_hop=continuation_hop,
        theory_transition=theory_transition,
        adoption_checkpoint=receipt_checkpoint,
    )


def adopt_deferred_transition(
    transition: AttemptTransition,
    state: _PilotState,
    ctx: _PilotContext,
) -> AttemptTransition:
    """Adopt the exact fork whose controlling attempt was already recorded."""

    if transition.attempt is None or transition.trial is None:
        raise ValueError("deferred adoption requires one accepted trial")
    if not isinstance(transition.result, Bearing):
        raise ValueError("deferred adoption requires one Bearing")
    trial = _trial_commit.adopt_trial(transition.trial, transition.frame, state, ctx)
    _retain_expectation_receipt(
        trial,
        transition.result.act,
        state,
        transition.adoption_checkpoint,
    )
    observation = transition.theory_transition
    if observation is not None:
        observation = replace(observation, adopted_boundary=_theory_live_boundary(state))
    return replace(
        transition,
        trial=trial,
        theory_transition=observation,
        adoption_checkpoint=None,
    )
