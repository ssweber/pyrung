"""Atomic adoption of one verified PILOT trial into the working World."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.earned_work import earned_work_is_useful_motion
from pyrung.core.analysis.pilot.navigation_contracts import LocalProgressKind
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _target_unresolved_condition,
    _until_unresolved_condition,
)
from pyrung.core.analysis.pilot.progress import _anchor_bearing_receipt
from pyrung.core.analysis.pilot.recovery import assert_recovery_disposable_state
from pyrung.core.analysis.pilot.steer import _install_prerequisites
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    _AcceptedTrial,
    _CommittedAct,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _Step,
    _StepContext,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


def _record_replay_steps(
    fork: PLC,
    inputs: dict[str, Any],
    scan_before: int,
    resting: dict[str, Any],
    edge_tags: set[str],
    *,
    edge_inputs: dict[str, Any] | None = None,
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
    pulsed_inputs = inputs if edge_inputs is None else edge_inputs
    edge_release = {
        t: resting.get(t, False)
        for t in pulsed_inputs
        if t in edge_tags and not _values_match(pulsed_inputs[t], resting.get(t, False))
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


def _build_step_context(
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


def adopt_trial(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AcceptedTrial:
    """Adopt one gate-approved trial without applying post-commit policy.

    Verification already ran inside the steering wrapper and
    ``record_attempt`` already committed its knowledge.  This is the shared
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
    commit_trial(trial, frame, state, ctx)
    return trial


def commit_trial(
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
    configuration_inputs = {
        tag: value
        for configuration in execution.applied_configurations
        for tag, value in configuration.assignments
    }
    step_inputs = {**configuration_inputs, **dict(policy.applied)}
    work, steps = _record_replay_steps(
        pulse.fork,
        step_inputs,
        pulse.scan_before,
        ctx.resting,
        ctx.edge_tags,
        edge_inputs=dict(policy.applied),
    )
    act = _CommittedAct(steps=steps, context=_build_step_context(trial, frame, state))
    # Adopt the physical fork and its replay evidence in one persistent-world
    # update. No consumer can observe steps detached from their operation owner.
    state.world = state.world.set(
        work=work,
        committed_acts=state.committed_acts.append(act),
    )
    if policy.local_progress in {
        LocalProgressKind.TRACE_SETUP,
        LocalProgressKind.TEMPORAL_SETUP,
        LocalProgressKind.THEORY_CORRECTIVE,
    }:
        if policy.local_progress is LocalProgressKind.TRACE_SETUP:
            ctx.compass = replace(
                ctx.compass,
                knowledge=ctx.compass.knowledge.after_stable_context_change(frame.key),
            )
        orientation = bearing.orientation
        trace_details = (
            orientation.candidates.trace.detail_by_pair if orientation is not None else {}
        )
        retained_list: list[PilotRung] = []
        for tag, value in policy.applied:
            detail = trace_details.get((tag, value))
            operation = getattr(detail, "operation", None)
            lifetime = getattr(detail, "until", None)
            if lifetime is None:
                lifetime = getattr(operation, "until", None)
            if (
                tag in ctx.edge_tags
                or tag in ctx.clear_only
                or not _values_match(state.work.state.tags.get(tag), value)
            ):
                continue
            if lifetime is None:
                if policy.local_progress not in {
                    LocalProgressKind.TEMPORAL_SETUP,
                    LocalProgressKind.THEORY_CORRECTIVE,
                }:
                    continue
                guard = _target_unresolved_condition(
                    state.work,
                    ctx.target.tag,
                    ctx.target.value,
                    ctx.target.predicate,
                )
            else:
                try:
                    guard = _until_unresolved_condition(state.work, lifetime)
                except (KeyError, ValueError):
                    continue
            retained_list.append(PilotRung(tag, value, guard, operation=operation))
        retained = tuple(retained_list)
        _install_prerequisites(state, retained)
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
