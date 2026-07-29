"""Pure rendering helpers for PILOT events and plan journals.

These functions translate already-decided navigation, execution, and progress
state into stable dictionaries, strings, and :class:`PlanStep` records.  They
do not choose an action, apply knowledge, or mutate the drive world.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

from pyrung.core.analysis.graph import PlanStep
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActPolicy,
    ActSource,
    BatchPulse,
    Coast,
    Dwell,
    Pulse,
    act_identity,
)
from pyrung.core.analysis.pilot.outcome import BearingEffect
from pyrung.core.analysis.pilot.overlay import _rung_execution_receipt
from pyrung.core.analysis.pilot.trace import UnsupportedConstruct, frontier_pairs
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    PilotEvent,
    TagChange,
    TargetReached,
    _AcceptedTrial,
    _AttemptResult,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _RecoveryOrigin,
    _StepContext,
)
from pyrung.core.analysis.pilot.world_key import _rung_identity
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.validation.render import (
    caret_of,
    operand_name,
    render_condition,
    with_rung_line,
)

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.compass import Compass
    from pyrung.core.analysis.pilot.options import CandidateRead, _Candidate
    from pyrung.core.analysis.pilot.pipeline_graph import StaticPath
    from pyrung.core.analysis.pilot.program_step import ProgramStep


def render_unsupported_construct(failure: UnsupportedConstruct) -> str:
    """Render Trace's unsupported declaration as a source-oriented diagnostic."""

    unsupported = failure.unsupported
    kind = failure.construct_kind
    name = type(unsupported).__name__
    if kind == "condition":
        token = render_condition(unsupported)
        source_line = with_rung_line((unsupported,))
    else:
        token = repr(unsupported)
        source_line = token

    context = failure.provenance[-1] if failure.provenance else "trace"
    if failure.source_file and failure.source_line:
        context += f" ({failure.source_file}:{failure.source_line})"
    elif failure.source_file:
        context += f" ({failure.source_file})"
    elif failure.source_line:
        context += f" (line {failure.source_line})"

    span = caret_of(source_line, token)
    lines = [
        f"PILOT cannot read {kind} {name}.",
        f" --> {context}",
        "  |",
        f"  |  {source_line}",
    ]
    if span is not None:
        start, length = span
        lines.append(f"  |  {' ' * start}{'^' * length} unsupported {kind}")
    lines.extend(
        (
            "  |",
            f"  = hint: add a PILOT trace rule for {name}.",
        )
    )
    return "\n".join(lines)


def _investigation_started_event(
    trial: _AcceptedTrial,
    origin: _RecoveryOrigin,
) -> PilotEvent:
    """Announce expensive causal replay before it starts."""

    pulse = trial.attempt.pulse
    policy = trial.attempt.bearing.act.policy
    execution = trial.execution
    channel_tag = execution.channel_motion.channel_tag
    return PilotEvent(
        "investigation_started",
        pulse.fork.state.scan_id,
        {
            "channel_tag": channel_tag,
            "from_value": (
                origin.before_snap.get(channel_tag) if channel_tag is not None else None
            ),
            "to_value": (
                execution.after_snap.get(channel_tag) if channel_tag is not None else None
            ),
            "action": policy.applied,
        },
    )


def _channel_transitions(
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    checkpoint_fork: Any,
    regressed_snap: Any,
) -> tuple[tuple[str, Any, Any], ...]:
    """Render the navigated channel transition a revert undoes."""

    execution = trial.execution
    try:
        checkpoint_snap = dict(getattr(checkpoint_fork.state, "tags", {}) or {})
    except (AttributeError, TypeError):
        checkpoint_snap = {}
    transitions: list[tuple[str, Any, Any]] = []
    channel_tags = tuple(
        dict.fromkeys(
            tag for tag in (ctx.target.tag, execution.channel_motion.channel_tag) if tag is not None
        )
    )
    for channel_tag in channel_tags:
        from_value = checkpoint_snap.get(channel_tag)
        to_value = (regressed_snap or {}).get(channel_tag)
        if (from_value is None and to_value is None) or _values_match(from_value, to_value):
            continue
        transitions.append((channel_tag, from_value, to_value))
    return tuple(transitions)


def _fmt_need(tag: str, value: Any, snap: dict[str, Any]) -> str:
    """Render one ``still_need`` display entry."""
    from pyrung.core.analysis.pilot.static_expressions import _atom_text
    from pyrung.core.analysis.simplified import Atom

    if isinstance(value, Atom):
        return f"{_atom_text(value)} (have {snap.get(tag)!r})"
    return f"{tag}={value!r} (have {snap.get(tag)!r})"


def _frontier_clause(frame: _IterationFrame | None) -> str:
    """Render the terminal suffix naming a frame's outstanding frontier."""
    if frame is None:
        return ""
    extra = getattr(frame, "completion_frontier", ())
    needs = extra + tuple(n for n in frontier_pairs(frame.tree, frame.snap) if n not in extra)
    if not needs:
        return ""
    head = ", ".join(_fmt_need(t, v, frame.snap) for t, v in needs[:3])
    more = f" (+{len(needs) - 3} more)" if len(needs) > 3 else ""
    return f"; still waiting on {head}{more}"


def _format_transition(sc: _StepContext, channel_tags: frozenset[str]) -> str:
    """Render the first changed semantic channel register."""
    before_snap = sc.execution.before_snap
    after_snap = sc.execution.after_snap
    for tag in sorted((set(before_snap) | set(after_snap)) & channel_tags):
        before = before_snap.get(tag)
        after = after_snap.get(tag)
        if before != after:
            return f"{tag} {_display_value(before)} -> {_display_value(after)}"
    return ""


def _display_value(value: Any) -> str:
    if value is True:
        return "True"
    if value is False:
        return "False"
    return str(value)


def _build_plan_journal(
    state: _PilotState,
    fork: Any,
    channel_tags: frozenset[str],
    acc_names: frozenset[str],
) -> tuple[PlanStep, ...]:
    """Build the annotated plan journal from the clean path and hold log."""
    if not state.committed_acts:
        return ()

    def _notes_for(inputs: Any) -> tuple[str, ...]:
        return tuple(state.lever_notes[t] for t, _v in inputs if t in state.lever_notes)

    hold_log = tuple(state.hold_log)

    def _controlled_at(scan: int, tag: str, value: Any, snapshot: Mapping[str, Any]) -> bool:
        active: dict[tuple[Any, ...], Any] = {}
        for entry in hold_log:
            if entry.scan > scan:
                continue
            for rung in entry.rungs:
                key = _rung_identity(rung)
                if entry.source == "revocation":
                    active.pop(key, None)
                else:
                    active[key] = rung
        owner = _rung_execution_receipt(tuple(active.values()), snapshot).owner(tag)
        return owner is not None and _values_match(owner.value, value)

    entries: list[tuple[int, str, PlanStep]] = []

    for act in state.committed_acts:
        sc = act.context
        first_step = act.steps[0]
        semantic_step = act.steps[-1]
        is_coast = sc.policy.motion.is_coast
        transition = _format_transition(sc, channel_tags)
        span = semantic_step.scan_after - first_step.scan_before

        if is_coast:
            accel: list[tuple[str, Any]] = list(sc.execution.accelerators)
            # Compatibility for ordinary runner folds, whose fold receipt does
            # not yet carry exact edits. CycleFold receipts are authoritative
            # and avoid mistaking program-owned accumulator resets for jumps.
            if not accel and fork is not None:
                snap = fork._scan_log.snapshot()
                for scan_id in sorted(snap.patches_by_scan):
                    if scan_id < first_step.scan_before or scan_id > semantic_step.scan_after:
                        continue
                    for tag, val in snap.patches_by_scan[scan_id].items():
                        if (
                            isinstance(val, (int, float))
                            and not isinstance(val, bool)
                            and tag in acc_names
                        ):
                            accel.append((tag, val))

            known_tags = getattr(fork, "_known_tags_by_name", {}) if fork is not None else {}
            display_accel = tuple(
                (operand_name(known_tags.get(tag, tag)), value) for tag, value in accel
            )

            entries.append(
                (
                    first_step.scan_before,
                    "b_coast",
                    PlanStep(
                        kind="coast",
                        scan=first_step.scan_before,
                        scans=span,
                        inputs=(),
                        label=sc.execution.channel_motion.channel_tag or "",
                        transition=transition,
                        waiting_for=sc.frontier_tags,
                        steady_holds=sc.steady_holds,
                        accelerators=display_accel,
                        rungs=sc.overlay_rules,
                    ),
                )
            )
        else:
            command_inputs = [
                (tag, val)
                for tag, val in semantic_step.inputs.items()
                if not (
                    isinstance(val, (int, float)) and not isinstance(val, bool) and tag in acc_names
                )
                and not _controlled_at(
                    first_step.scan_before,
                    tag,
                    val,
                    sc.execution.before_snap,
                )
            ]
            if command_inputs:
                decision_tags = sorted(dict(sc.policy.action_pairs))
                label = ", ".join(decision_tags) if decision_tags else ""
                entries.append(
                    (
                        first_step.scan_before,
                        "b_command",
                        PlanStep(
                            kind="pulse",
                            scan=first_step.scan_before,
                            scans=span,
                            inputs=tuple(command_inputs),
                            label=label,
                            transition=transition,
                            notes=_notes_for(command_inputs),
                        ),
                    )
                )

    path_start = state.committed_acts[0].steps[0].scan_before
    path_end = state.committed_acts[-1].steps[-1].scan_after

    seen_rungs: set[tuple[Any, ...]] = set()
    correction_receipts = getattr(state, "correction_receipts", ())
    managed_rungs = {
        _rung_identity(rung) for receipt in correction_receipts for rung in receipt.rungs
    }
    active_managed_rungs = {
        _rung_identity(rung)
        for receipt in correction_receipts
        if receipt.status.effective
        for rung in receipt.rungs
    }
    recorded_removals = {
        _rung_identity(rung)
        for entry in state.hold_log
        if entry.source == "revocation"
        for rung in entry.rungs
    }
    for log_index, entry in enumerate(state.hold_log):
        if entry.scan < path_start or entry.scan > path_end:
            continue
        if entry.source == "revocation":
            entries.append(
                (
                    entry.scan,
                    f"a_{log_index:08d}",
                    PlanStep(
                        kind="revoke",
                        scan=entry.scan,
                        scans=0,
                        inputs=tuple((rung.dest, rung.value) for rung in entry.rungs),
                        label=", ".join(dict.fromkeys(rung.dest for rung in entry.rungs)),
                        rungs=entry.rungs,
                        source=entry.source,
                    ),
                )
            )
            continue
        new_rungs: list[Any] = []
        for rung in entry.rungs:
            key = _rung_identity(rung)
            if (
                key in managed_rungs
                and key not in active_managed_rungs
                and key not in recorded_removals
            ):
                continue
            if key in seen_rungs:
                continue
            seen_rungs.add(key)
            new_rungs.append(rung)

        hold_inputs = tuple((rung.dest, rung.value) for rung in new_rungs)

        if hold_inputs:
            entries.append(
                (
                    entry.scan,
                    f"a_{log_index:08d}",
                    PlanStep(
                        kind="force",
                        scan=entry.scan,
                        scans=0,
                        inputs=hold_inputs,
                        label=", ".join(dict.fromkeys(tag for tag, _value in hold_inputs)),
                        notes=_notes_for(hold_inputs),
                        rungs=tuple(new_rungs),
                        source=entry.source,
                    ),
                )
            )

    entries.sort(key=lambda e: (e[0], e[1]))
    return tuple(step for _, _, step in entries)


def _iteration_payload(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> dict[str, Any]:
    still_need = [_fmt_need(t, v, frame.snap) for t, v in frontier_pairs(frame.tree, frame.snap)]
    return {
        "target": (ctx.target.tag, ctx.target.value),
        "snapshot": frame.snap,
        "tree": frame.tree,
        "state_key": frame.key,
        "distance": frame.distance_before,
        "still_need": tuple(still_need),
        "raw_trace_actions": frame.raw_trace_actions,
        "raw_trace_action_details": frame.raw_trace_action_details,
        "nogoods": ctx.compass.knowledge.nogood_pairs(frame.key),
        "rungs": tuple(state.overlay_rules),
        "seen_key_count": len(state.seen_keys),
        "checkpoint_count": len(state.checkpoints),
        "steps": tuple(state.steps),
        "watch_tags": tuple(state.watch_tags),
    }


def _candidates_built_payload(
    candidates: CandidateRead, lever_notes: dict[str, str] | None = None
) -> dict[str, Any]:
    route = candidates.route
    wait = candidates.wait
    prescription = wait.prescription if wait is not None else None
    prerequisites = candidates.prerequisites.rungs
    return {
        "candidates": tuple(_candidate_read_payload(c) for c in candidates.options),
        "trace_actions": candidates.trace.actions,
        "trace_action_details": candidates.trace.details,
        "active_trace_actions": candidates.trace.active_actions,
        "route_candidates": route.candidates if route is not None else (),
        "route_plan": _route_plan_payload(route.plan if route is not None else None),
        "wake_cap": candidates.wake_cap,
        "wait_prescribed": prescription is not None,
        "wait_reason": wait.reason if wait is not None else None,
        "prerequisite_rungs": prerequisites,
        "lever_notes": {
            rung.dest: lever_notes[rung.dest]
            for rung in prerequisites
            if lever_notes and rung.dest in lever_notes
        },
        "stuck_reason": candidates.diagnosis.reason if candidates.diagnosis is not None else None,
        "completion_frontier": wait.frontier if wait is not None else (),
        "program_step": _program_step_payload(wait.program_step if wait is not None else None),
    }


def _program_step_payload(step: ProgramStep | None) -> dict[str, Any] | None:
    """Compact, dumpable view of an exact-producer current-world reading."""
    if step is None:
        return None
    boundary = step.boundary
    return {
        "status": step.status.value,
        "producer": {
            "rung_index": step.producer.rung_index,
            "command": (step.producer.command_tag, step.producer.command_value),
        },
        "boundary": (
            {
                "tag": boundary.tag,
                "op": getattr(boundary, "op", "=="),
                "bound": getattr(boundary, "bound", getattr(boundary, "values", None)),
            }
            if boundary is not None
            else None
        ),
        "channel": step.channel,
        "required_inputs": tuple(action.pair for action in step.required_inputs),
        "input_handoffs": tuple(
            {
                "action": handoff.action,
                "channel": handoff.channel,
                "boundary": {
                    "tag": handoff.boundary.tag,
                    "op": getattr(handoff.boundary, "op", "=="),
                    "bound": getattr(
                        handoff.boundary,
                        "bound",
                        getattr(handoff.boundary, "values", None),
                    ),
                },
            }
            for handoff in step.input_handoffs
        ),
        "context_actions": step.context_actions,
        "projected_changes": step.projected_changes,
        "reason": step.reason,
    }


def _candidate_payload(policy: ActPolicy) -> dict[str, Any]:
    pair = policy.primary_action
    if pair is None:
        raise ValueError("candidate recording requires one primary action")
    tag, value = pair
    return {
        "tag": tag,
        "value": value,
        "pair": pair,
        "influence_prescribed": policy.learned_prescribed,
        "route_prescribed": policy.source is ActSource.ROUTE,
        "bearing_channel_tag": (policy.heading.channel_tag if policy.heading is not None else None),
        "bearing_channel_value": (
            policy.heading.target_value if policy.heading is not None else None
        ),
        "awaited_action_prescribed": policy.source is ActSource.AWAITED_ACTION,
        "awaited_action_note": (policy.note if policy.source is ActSource.AWAITED_ACTION else ""),
        "program_prescribed": policy.source is ActSource.PROGRAM,
        "program_note": policy.note if policy.source is ActSource.PROGRAM else "",
        "program_context_actions": policy.context_actions,
        "provenance": policy.provenance,
        "wake": policy.wake,
        "prescribed": policy.source is not ActSource.TRACE,
    }


def _candidate_read_payload(candidate: _Candidate) -> dict[str, Any]:
    """Render one unselected option reading before Orientation owns an act."""

    return {
        "tag": candidate.tag,
        "value": candidate.value,
        "pair": candidate.pair,
        "influence_prescribed": candidate.learned_prescribed,
        "route_prescribed": candidate.route_prescribed,
        "bearing_channel_tag": candidate.bearing_channel_tag,
        "bearing_channel_value": candidate.bearing_channel_value,
        "awaited_action_prescribed": candidate.awaited_action_prescribed,
        "awaited_action_note": candidate.awaited_action_note,
        "program_prescribed": candidate.program_prescribed,
        "program_note": candidate.program_note,
        "program_context_actions": candidate.program_context_actions,
        "provenance": candidate.provenance,
        "wake": candidate.wake,
        "prescribed": candidate.source is not ActSource.TRACE,
    }


def _knowledge_payload(
    state: _PilotState,
    compass: Compass,
) -> dict[str, Any]:
    """Render the knowledge fields that survive a world revert."""
    return {
        "hold_log": tuple(state.hold_log),
        "lever_notes": dict(state.lever_notes),
        "avoid_names": tuple(sorted(state.avoid_names)),
        "compass": compass,
    }


def _route_plan_payload(plan: StaticPath | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    from pyrung.core.analysis.pilot.pipeline_graph import ANY_FROM

    return {
        "needed": (plan.needed_tag, plan.needed_value),
        "channel_tag": plan.role.channel_tag,
        "target_value": plan.target_value,
        "path": tuple(
            {
                "from": "*" if edge.from_value is ANY_FROM else edge.from_value,
                "to": edge.to_value,
                "action": edge.action,
                "request": (
                    (edge.request_tag, edge.request_value) if edge.request_tag is not None else None
                ),
                "enablers": edge.enablers,
            }
            for edge in plan.edges
        ),
    }


def _diff_snapshots(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    tags: set[str] | frozenset[str] | None = None,
) -> tuple[TagChange, ...]:
    names = sorted(tags if tags is not None else (set(before) | set(after)))
    changes: list[TagChange] = []
    for tag in names:
        old = before.get(tag)
        new = after.get(tag)
        if not _values_match(old, new):
            changes.append(TagChange(tag=tag, before=old, after=new))
    return tuple(changes)


def _bearing_coast_accepted_payload(trial: _AcceptedTrial) -> dict[str, Any]:
    """Render the stable ``zoom_accepted`` event payload for a bearing coast."""
    attempt = trial.attempt
    pulse = attempt.pulse
    policy = attempt.bearing.act.policy
    execution = trial.execution
    motion = execution.channel_motion
    receipt = execution.coast_receipt
    verified = trial.verification
    assessed = verified if isinstance(verified, AssessedMotion) else None
    observe_label = (
        policy.target_observe_label if isinstance(verified, TargetReached) else policy.observe_label
    )
    landed = (
        execution.after_snap.get(motion.channel_tag) if motion.channel_tag is not None else None
    )
    return {
        "new_key": assessed.new_key if assessed is not None else None,
        "trend": assessed.trend if assessed is not None else None,
        "outcome": assessed.assessment.legacy_outcome.value if assessed is not None else None,
        "observe_label": observe_label,
        "zoom_channel_tag": motion.channel_tag,
        "zoom_before_value": (
            execution.before_snap.get(motion.channel_tag)
            if motion.channel_tag is not None
            else None
        ),
        "zoom_target_value": motion.target_value,
        "zoom_actual_value": landed,
        "bearing_stop_reason": motion.stop_reason,
        "ejected": (assessed is not None and assessed.assessment.bearing is BearingEffect.DEPARTED),
        "scan_before": pulse.scan_before,
        "scan_after": pulse.fork.state.scan_id,
        "coast_logical_scans": receipt.logical_scans if receipt is not None else None,
        "coast_kernel_scans": receipt.kernel_scans if receipt is not None else None,
        "coast_skipped_scans": receipt.skipped_scans if receipt is not None else None,
        "coast_macro_folds": receipt.macro_folds if receipt is not None else None,
        "coast_timer_quanta_replayed": (
            receipt.timer_quanta_replayed if receipt is not None else None
        ),
        "snapshot": dict(execution.after_snap),
    }


def _accepted_payload(
    policy: ActPolicy,
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
) -> dict[str, Any]:
    attempt = trial.attempt
    pulse = attempt.pulse
    trial_policy = attempt.bearing.act.policy
    execution = trial.execution
    verified = trial.verification
    assessed = verified if isinstance(verified, AssessedMotion) else None
    watched_tags = set(state.watch_tags)
    action_tags = {tag for tag, _value in trial_policy.applied}
    target_relevant = set(frame.tree.pivot_tags()) | action_tags
    target_relevant.add(frame.tree.tag)
    changes = {
        "post_pulse": _diff_snapshots(execution.before_snap, pulse.post_pulse_snap),
        "settle": _diff_snapshots(pulse.post_pulse_snap, execution.after_snap),
        "total": _diff_snapshots(execution.before_snap, execution.after_snap),
        "watched": _diff_snapshots(execution.before_snap, execution.after_snap, tags=watched_tags),
        "target_relevant": _diff_snapshots(
            execution.before_snap,
            execution.after_snap,
            tags=target_relevant,
        ),
    }
    return {
        "index": 0,
        "candidate": dict(trial_policy.action_pairs),
        "candidate_detail": _candidate_payload(policy),
        "applied": trial_policy.applied,
        "co_actions": tuple(pair for pair in trial_policy.applied if pair != policy.primary_action),
        "gates": trial.gate_events,
        "accepted_because": {
            "gate_events": trial.gate_events,
            "trend_before": frame.distance_before,
            "trend_after": assessed.trend if assessed is not None else None,
            "state_key_changed": (assessed is not None and assessed.new_key != frame.key),
            "novel_key": (assessed is not None and assessed.new_key not in state.seen_keys),
            "target_reached": _values_match(
                execution.after_snap.get(frame.tree.tag),
                frame.tree.value,
            ),
        },
        "changes": changes,
        "snapshots": {
            "before": dict(execution.before_snap),
            "post_pulse": pulse.post_pulse_snap,
            "after_settle": dict(execution.after_snap),
        },
        "new_key": assessed.new_key if assessed is not None else None,
        "trend": assessed.trend if assessed is not None else None,
        "snapshot": dict(execution.after_snap),
        "scan_before": pulse.scan_before,
        "scan_after": pulse.fork.state.scan_id,
    }


def _act_event(
    phase: Literal["try", "rejected", "accepted"],
    act: Any,
    scan: int,
    *,
    rationale: str = "",
    prerequisites: tuple[Any, ...] = (),
    target_tag: str | None = None,
    attempt: _AttemptResult | None = None,
    trial: _AcceptedTrial | None = None,
    frame: _IterationFrame | None = None,
    state: _PilotState | None = None,
) -> PilotEvent | None:
    """Render one navigation-act lifecycle event through a single kind dispatch."""

    kind = act_identity(act)[0]
    if kind == "pulse":
        assert isinstance(act, Pulse)
        if phase == "try":
            return PilotEvent(
                "candidate_try",
                scan,
                {
                    "index": 0,
                    "total": 1,
                    "candidate": _candidate_payload(act.policy),
                    "applied": act.applied,
                    "co_actions": tuple(pair for pair in act.applied if pair != act.action),
                },
            )
        if phase == "rejected":
            assert attempt is not None
            return PilotEvent(
                "candidate_rejected",
                scan,
                {
                    "index": 0,
                    "candidate": _candidate_payload(act.policy),
                    "applied": act.applied,
                    "co_actions": tuple(pair for pair in act.applied if pair != act.action),
                    "gates": attempt.gate_events,
                },
            )
        assert trial is not None and frame is not None and state is not None
        return PilotEvent(
            "candidate_accepted",
            scan,
            _accepted_payload(act.policy, trial, frame, state),
        )

    if kind in {"coast", "dwell"}:
        assert isinstance(act, (Coast, Dwell))
        if phase == "try":
            channel_tag = target_tag
            if isinstance(act, Coast) and act.mode == "bearing":
                heading = act.policy.heading
                route = heading.route if heading is not None else None
                if heading is not None:
                    channel_tag = route.channel_tag if route is not None else heading.channel_tag
            return PilotEvent(
                "zoom",
                scan,
                {
                    "prescribed": True,
                    "reason": rationale,
                    "prerequisite_rungs": prerequisites,
                    "channel_tag": channel_tag,
                },
            )
        if phase == "rejected":
            assert attempt is not None
            return PilotEvent("zoom_rejected", scan, {"gates": attempt.gate_events})
        assert trial is not None
        return PilotEvent("zoom_accepted", scan, _bearing_coast_accepted_payload(trial))

    assert kind == "batch" and isinstance(act, BatchPulse)
    if phase == "try":
        return None
    label = "batch" if act.policy.observe_label == "batch" else "widening"
    if phase == "rejected":
        assert attempt is not None
        return PilotEvent(
            f"{label}_rejected",
            scan,
            {"actions": act.actions, "gates": attempt.gate_events},
        )
    assert trial is not None
    pulse = trial.attempt.pulse
    policy = trial.attempt.bearing.act.policy
    execution = trial.execution
    verified = trial.verification
    assessed = verified if isinstance(verified, AssessedMotion) else None
    return PilotEvent(
        f"{label}_accepted",
        scan,
        {
            "candidate": dict(policy.action_pairs),
            "applied": policy.applied,
            "gates": trial.gate_events,
            "new_key": assessed.new_key if assessed is not None else None,
            "trend": assessed.trend if assessed is not None else None,
            "snapshot": dict(execution.after_snap),
            "scan_before": pulse.scan_before,
            "scan_after": pulse.fork.state.scan_id,
        },
    )
