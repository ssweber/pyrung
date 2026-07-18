"""Pure rendering helpers for PILOT events and plan journals.

These functions translate already-decided navigation, execution, and progress
state into stable dictionaries, strings, and :class:`PlanStep` records.  They
do not choose an action, apply knowledge, or mutate the drive world.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.graph import PlanStep
from pyrung.core.analysis.pilot._ops import _semantic_key
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.trace import frontier_pairs
from pyrung.core.analysis.pilot.types import (
    TagChange,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _StepContext,
    _TrialResult,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.charts import StaticPath
    from pyrung.core.analysis.pilot.compass import Compass


def _fmt_need(tag: str, value: Any, snap: dict[str, Any]) -> str:
    """Render one ``still_need`` display entry."""
    from pyrung.core.analysis.pilot.trace import _atom_text
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
    return f" — still waiting on {head}{more}"


def _format_transition(sc: _StepContext, channel_tags: frozenset[str]) -> str:
    """Render the first changed semantic channel register."""
    for tag in sorted((set(sc.before_snap) | set(sc.after_snap)) & channel_tags):
        before = sc.before_snap.get(tag)
        after = sc.after_snap.get(tag)
        if before != after:
            return f"{tag} {before} → {after}"
    return ""


def _build_plan_journal(
    state: _PilotState,
    fork: Any,
    channel_tags: frozenset[str],
    acc_names: frozenset[str],
) -> tuple[PlanStep, ...]:
    """Build the annotated plan journal from the clean path and hold log."""
    if not state.steps:
        return ()

    ctx_by_scan: dict[int, _StepContext] = {c.scan_before: c for c in state.step_contexts}

    def _notes_for(inputs: Any) -> tuple[str, ...]:
        return tuple(state.lever_notes[t] for t, _v in inputs if t in state.lever_notes)

    entries: list[tuple[int, str, PlanStep]] = []

    for step in state.steps:
        sc = ctx_by_scan.get(step.scan_before)
        if sc is None:
            continue

        is_coast = sc.motion.is_coast
        transition = _format_transition(sc, channel_tags)
        span = step.scan_after - step.scan_before

        if is_coast:
            accel: list[tuple[str, Any]] = []
            if fork is not None:
                snap = fork._scan_log.snapshot()
                for scan_id in sorted(snap.patches_by_scan):
                    if scan_id < step.scan_before or scan_id > step.scan_after:
                        continue
                    for tag, val in snap.patches_by_scan[scan_id].items():
                        if (
                            isinstance(val, (int, float))
                            and not isinstance(val, bool)
                            and tag in acc_names
                        ):
                            accel.append((tag, val))

            entries.append(
                (
                    step.scan_before,
                    "b_coast",
                    PlanStep(
                        kind="coast",
                        scan=step.scan_before,
                        scans=span,
                        inputs=(),
                        label=sc.channel_tag or "",
                        transition=transition,
                        waiting_for=sc.frontier_tags,
                        steady_holds=sc.steady_holds,
                        pulsing_holds=sc.pulsing_holds,
                        accelerators=tuple(accel),
                        rungs=sc.control_rungs,
                    ),
                )
            )
        else:
            command_inputs = [
                (tag, val)
                for tag, val in step.inputs.items()
                if not (
                    isinstance(val, (int, float)) and not isinstance(val, bool) and tag in acc_names
                )
            ]
            if command_inputs:
                decision_tags = sorted(sc.candidate)
                label = ", ".join(decision_tags) if decision_tags else ""
                entries.append(
                    (
                        step.scan_before,
                        "b_command",
                        PlanStep(
                            kind="command",
                            scan=step.scan_before,
                            scans=span,
                            inputs=tuple(command_inputs),
                            label=label,
                            transition=transition,
                            notes=_notes_for(command_inputs),
                        ),
                    )
                )

    path_start = state.steps[0].scan_before
    path_end = state.steps[-1].scan_after

    seen_rungs: set[tuple[Any, ...]] = set()
    seen_legacy_holds: set[tuple[str, str]] = set()
    for entry in state.hold_log:
        if entry.scan < path_start or entry.scan > path_end:
            continue
        new_rungs: list[Any] = []
        for rung in entry.rungs:
            key = (
                rung.dest,
                _semantic_key(rung.value),
                _semantic_key(rung.guard),
            )
            if key in seen_rungs:
                continue
            seen_rungs.add(key)
            new_rungs.append(rung)

        if entry.rungs:
            hold_inputs = tuple((rung.dest, rung.value) for rung in new_rungs)
        else:
            legacy_inputs: list[tuple[str, Any]] = []
            for tag, value in entry.tags:
                key = (tag, repr(value))
                if key in seen_legacy_holds:
                    continue
                seen_legacy_holds.add(key)
                legacy_inputs.append((tag, value))
            hold_inputs = tuple(legacy_inputs)

        if hold_inputs:
            entries.append(
                (
                    entry.scan,
                    "a_hold",
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
        "target": (ctx.target_tag, ctx.target_value),
        "snapshot": frame.snap,
        "tree": frame.tree,
        "state_key": frame.key,
        "distance": frame.distance_before,
        "still_need": tuple(still_need),
        "raw_trace_actions": frame.raw_trace_actions,
        "raw_trace_action_details": frame.raw_trace_action_details,
        "nogoods": ctx.compass.knowledge.nogood_pairs(frame.key),
        "rungs": tuple(state.rungs),
        "seen_key_count": len(state.seen_keys),
        "checkpoint_count": len(state.checkpoints),
        "steps": tuple(state.steps),
        "watch_tags": tuple(state.watch_tags),
    }


def _candidates_built_payload(candidates: Any) -> dict[str, Any]:
    return {
        "candidate_list": candidates,
        "candidates": tuple(_candidate_payload(c) for c in candidates.candidates),
        "trace_actions": candidates.trace_actions,
        "trace_action_details": candidates.trace_action_details,
        "active_trace_actions": candidates.active_trace_actions,
        "route_candidates": candidates.route_candidates,
        "route_plan": _route_plan_payload(candidates.route_plan),
        "wake_cap": candidates.wake_cap,
        "wait_prescribed": candidates.wait_prescribed,
        "wait_reason": candidates.wait_reason,
        "prerequisite_rungs": candidates.prerequisite_rungs,
        "stuck_reason": candidates.stuck_reason,
        "completion_frontier": candidates.completion_frontier,
    }


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    return {
        "tag": candidate.tag,
        "value": candidate.value,
        "pair": candidate.pair,
        "influence_prescribed": candidate.influence_prescribed,
        "route_prescribed": candidate.route_prescribed,
        "bearing_channel_tag": candidate.bearing_channel_tag,
        "bearing_channel_value": candidate.bearing_channel_value,
        "current_prescribed": candidate.current_prescribed,
        "current_note": candidate.current_note,
        "provenance": candidate.provenance,
        "wake": candidate.wake,
        "prescribed": (
            candidate.route_prescribed
            or candidate.influence_prescribed
            or candidate.current_prescribed
        ),
        "scored": candidate.scored,
        "avail_tier": candidate.avail_tier,
        "over_wake": candidate.over_wake,
        "compass_score": candidate.compass_score,
    }


def _knowledge_payload(
    state: _PilotState,
    compass: Compass,
    *,
    skiff_decline: str | None = None,
) -> dict[str, Any]:
    """Render the knowledge fields that survive a world revert."""
    return {
        "hold_log": tuple(state.hold_log),
        "lever_notes": dict(state.lever_notes),
        "skiff_decline": skiff_decline,
        "avoid_names": tuple(sorted(state.avoid_names)),
        "compass": compass,
    }


def _route_plan_payload(plan: StaticPath | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    from pyrung.core.analysis.pilot.charts import ANY_FROM

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
    before: dict[str, Any],
    after: dict[str, Any],
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


def _zoom_accepted_payload(trial: _TrialResult) -> dict[str, Any]:
    """Render a ``zoom_accepted`` event payload."""
    landed = (
        trial.fork_snap.get(trial.zoom_channel_tag) if trial.zoom_channel_tag is not None else None
    )
    return {
        "new_key": trial.new_key,
        "trend": trial.trend,
        "outcome": trial.outcome.value if trial.outcome else None,
        "observe_label": trial.observe_label,
        "zoom_channel_tag": trial.zoom_channel_tag,
        "zoom_target_value": trial.zoom_target_value,
        "zoom_actual_value": landed,
        "ejected": trial.outcome == Outcome.AMBIENT_DRIFT,
        "scan_before": trial.scan_before,
        "scan_after": trial.fork.state.scan_id,
        "snapshot": trial.fork_snap,
    }


def _accepted_payload(
    candidate: Any,
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
) -> dict[str, Any]:
    watched_tags = set(state.watch_tags)
    action_tags = {tag for tag, _value in trial.applied}
    target_relevant = set(frame.tree.pivot_tags()) | action_tags
    target_relevant.add(frame.tree.tag)
    changes = {
        "post_pulse": _diff_snapshots(trial.before_snap, trial.post_pulse_snap),
        "settle": _diff_snapshots(trial.post_pulse_snap, trial.fork_snap),
        "total": _diff_snapshots(trial.before_snap, trial.fork_snap),
        "watched": _diff_snapshots(trial.before_snap, trial.fork_snap, tags=watched_tags),
        "target_relevant": _diff_snapshots(
            trial.before_snap,
            trial.fork_snap,
            tags=target_relevant,
        ),
    }
    return {
        "index": 0,
        "candidate": trial.candidate,
        "candidate_detail": _candidate_payload(candidate),
        "applied": trial.applied,
        "co_actions": tuple(pair for pair in trial.applied if pair != candidate.pair),
        "gates": trial.gate_events,
        "accepted_because": {
            "gate_events": trial.gate_events,
            "trend_before": frame.distance_before,
            "trend_after": trial.trend,
            "state_key_changed": trial.new_key is not None and trial.new_key != frame.key,
            "novel_key": trial.new_key is not None and trial.new_key not in state.seen_keys,
            "target_reached": _values_match(
                trial.fork_snap.get(frame.tree.tag),
                frame.tree.value,
            ),
        },
        "changes": changes,
        "snapshots": {
            "before": trial.before_snap,
            "post_pulse": trial.post_pulse_snap,
            "after_settle": trial.fork_snap,
        },
        "new_key": trial.new_key,
        "trend": trial.trend,
        "snapshot": trial.fork_snap,
        "scan_before": trial.scan_before,
        "scan_after": trial.fork.state.scan_id,
    }
