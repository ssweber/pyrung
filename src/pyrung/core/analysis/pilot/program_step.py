"""Prove whether one exact program producer can keep running unchanged.

This is a read-only decision made before Compass chooses an action.  It projects
the same controlled PLC world for a few scans, writer-locks the backward trace
to the selected rung, and reports one of three plain outcomes:

* keep running because a target-relative boundary moved;
* supply the currently unmet external input;
* unclear because no safe forward claim can be made.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pyrung.core.analysis.observed import runs_for_node, writer_runs_for_node
from pyrung.core.analysis.pilot._ops import fork_with_rungs
from pyrung.core.analysis.pilot.advance import build_advance_index
from pyrung.core.analysis.pilot.trace import TraceAction, TraceNode, trace_back
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.crossing import Cmp, Eq
from pyrung.core.instruction.advance import constraint_holds


class ProgramStepStatus(StrEnum):
    KEEP_RUNNING = "keep_running"
    NEEDS_INPUT = "needs_input"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class ProgramStep:
    """Current-world proof result for one selected producer."""

    status: ProgramStepStatus
    producer: Any
    boundary: Eq | Cmp | None
    channel: str | None
    required_inputs: tuple[TraceAction, ...] = ()
    context_actions: tuple[tuple[str, Any], ...] = ()
    projected_changes: tuple[tuple[str, Any, Any], ...] = ()
    trace: TraceNode | None = None
    next_trace: TraceNode | None = None
    reason: str = ""


def _nodes(root: TraceNode) -> list[TraceNode]:
    result = [root]
    for child in root.children:
        result.extend(_nodes(child))
    return result


def _first_boundary(root: TraceNode) -> Eq | Cmp | None:
    for node in _nodes(root):
        if node.advance is not None and not node.satisfied:
            return node.advance.until
    return None


def _trace_exact(ctx: Any, producer: Any, snapshot: Mapping[str, Any]) -> TraceNode:
    opaque = getattr(ctx, "opaque_loop", frozenset())
    pipeline = getattr(ctx, "pipeline_internal_tags", frozenset())
    return trace_back(
        producer.command_tag,
        producer.command_value,
        dict(snapshot),
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        clear_only=getattr(ctx, "clear_only", frozenset()),
        opaque_loop=opaque & pipeline,
        pipeline_internal_tags=pipeline,
        writer_locks={
            (producer.command_tag, producer.command_value): producer.rung_index,
        },
        prior=getattr(ctx, "prior", getattr(ctx, "domain_prior", None)),
        avoid_pred=getattr(ctx, "avoid_pred", None),
        via_pred=getattr(ctx, "via_pred", None),
        harness=getattr(ctx, "harness", None),
    )


def _input_split(
    trace: TraceNode,
    snapshot: Mapping[str, Any],
    resting: Mapping[str, Any],
) -> tuple[tuple[TraceAction, ...], tuple[tuple[str, Any], ...]]:
    required = tuple(
        action
        for action in trace.ordered_action_details()
        if not _values_match(snapshot.get(action.tag), action.value) or action.pulse
    )
    context: list[tuple[str, Any]] = []
    for action in required:
        if not action.pulse or not _values_match(snapshot.get(action.tag), action.value):
            continue
        rest = resting.get(action.tag, False)
        if not _values_match(rest, action.value):
            context.append((action.tag, rest))
    return required, tuple(context)


def _run_snapshot(run: Any, fallback: Mapping[str, Any], ctx: Any) -> dict[str, Any]:
    """Materialize the exact tag image one producer occurrence read."""
    names = set(fallback) | set(getattr(ctx.pdg, "tags", {}))
    return {name: run.view.get_tag(name, fallback.get(name)) for name in names}


def _action_channel_barriers(
    trace: TraceNode,
    snapshot: Mapping[str, Any],
    ctx: Any,
) -> dict[tuple[str, Any], frozenset[str]]:
    """Pipeline channels crossed on the way from each input to the producer."""
    channels = {
        role.channel_tag
        for role in getattr(ctx, "pipeline_roles", ())
        if role.channel_tag in getattr(ctx, "opaque_loop", frozenset())
    }
    barriers: dict[tuple[str, Any], set[str]] = {}

    def _walk(node: TraceNode, above: frozenset[str]) -> None:
        current = above
        if (
            node.tag in channels
            and not node.satisfied
            and not _values_match(snapshot.get(node.tag), node.value)
        ):
            current = above | {node.tag}
        if node.is_steerable and not node.satisfied:
            barriers.setdefault((node.tag, node.value), set()).update(current)
        for child in node.children:
            _walk(child, current)

    _walk(trace, frozenset())
    return {pair: frozenset(tags) for pair, tags in barriers.items()}


def _input_moves_live_channel(
    action: TraceAction,
    channels: frozenset[str],
    ctx: Any,
    plc: Any,
    rungs: Sequence[Any],
) -> bool:
    """Whether one real controlled scan accepts *action* at a crossed channel."""
    if not channels:
        return True
    fork = fork_with_rungs(plc, rungs)
    before = dict(fork.state.tags)
    fork.patch({action.tag: action.value})
    fork.step()
    after = fork.state.tags
    for role in getattr(ctx, "pipeline_roles", ()):
        if role.channel_tag not in channels:
            continue
        watched = {role.channel_tag, *role.request_tags}
        if any(not _values_match(before.get(tag), after.get(tag)) for tag in watched):
            return True
    return False


def read_program_step(
    ctx: Any,
    producer: Any,
    plc: Any,
    rungs: Sequence[Any] = (),
    *,
    resting: Mapping[str, Any] | None = None,
    projection_scans: int = 4,
) -> ProgramStep:
    """Project an unchanged controlled world and read the exact producer.

    The function never installs actions on the real PLC. ``rungs`` are rebuilt
    on the fork so "unchanged" includes PILOT's already-established holds.
    """

    before = dict(ctx.snapshot)
    fork = fork_with_rungs(plc, rungs)
    # One real projected scan is the observation primitive.  Its interpreted
    # replay preserves the exact view of every producer occurrence, including
    # earlier same-scan writes and installed PILOT holds.
    fork.step()
    consumed_scans = 1
    projected_frame = dict(fork.state.tags)
    runs = fork._replay_rung_runs_at(fork.state.scan_id)
    producer_runs = runs_for_node(ctx.pdg, ctx.program, producer.rung_index, runs)
    repeated_producer = len(producer_runs) > 1
    trace_snapshot = (
        _run_snapshot(producer_runs[0], projected_frame, ctx)
        if len(producer_runs) == 1
        else projected_frame
    )

    trace = _trace_exact(ctx, producer, trace_snapshot)
    boundary = _first_boundary(trace)
    required, context_actions = _input_split(trace, trace_snapshot, resting or {})
    barriers = _action_channel_barriers(trace, trace_snapshot, ctx)
    inputs_blocked_here = tuple(
        action
        for action in required
        if barriers.get(action.pair)
        and not _input_moves_live_channel(
            action,
            barriers[action.pair],
            ctx,
            plc,
            rungs,
        )
    )

    index = build_advance_index(ctx.program, getattr(fork, "_harness", None))
    owner = index.resolve(boundary.tag) if boundary is not None else None
    before_distance = (
        owner.profile.linear.distance(boundary, trace_snapshot)
        if owner is not None and owner.profile.linear is not None and boundary is not None
        else None
    )
    boundary_was_reached = (
        constraint_holds(boundary, trace_snapshot) is True if boundary is not None else False
    )

    scans = max(1, int(projection_scans))
    for _ in range(max(0, scans - consumed_scans)):
        fork.step()
        current = fork.state.tags
        if _values_match(current.get(producer.command_tag), producer.command_value):
            break
        if boundary is not None and constraint_holds(boundary, current) is True:
            break
        if (
            boundary is not None
            and owner is not None
            and owner.profile.linear is not None
            and before_distance is not None
        ):
            current_distance = owner.profile.linear.distance(boundary, current)
            if current_distance is not None and current_distance < before_distance:
                break

    after = dict(fork.state.tags)
    writer_runs = writer_runs_for_node(
        ctx.pdg,
        ctx.program,
        producer.rung_index,
        producer.command_tag,
        producer.command_value,
        runs,
    )
    next_trace = _trace_exact(ctx, producer, after)
    relevant = {node.tag for node in (*_nodes(trace), *_nodes(next_trace))} | {
        producer.command_tag,
        *producer.co_writes,
    }
    projected_changes = tuple(
        (tag, before.get(tag), after.get(tag))
        for tag in sorted(relevant)
        if not _values_match(before.get(tag), after.get(tag))
    )

    if inputs_blocked_here:
        names = ", ".join(action.tag for action in inputs_blocked_here)
        return ProgramStep(
            ProgramStepStatus.UNCLEAR,
            producer,
            boundary,
            boundary.tag if boundary is not None else producer.command_tag,
            projected_changes=projected_changes,
            trace=trace,
            next_trace=next_trace,
            reason=f"{names} is not accepted by the current program state",
        )

    if repeated_producer:
        return ProgramStep(
            ProgramStepStatus.UNCLEAR,
            producer,
            boundary,
            boundary.tag if boundary is not None else producer.command_tag,
            required_inputs=required,
            context_actions=context_actions,
            projected_changes=projected_changes,
            trace=trace,
            next_trace=next_trace,
            reason="the selected producer ran more than once with occurrence-specific state",
        )

    if boundary is not None:
        boundary_reached = constraint_holds(boundary, after) is True
        after_distance = (
            owner.profile.linear.distance(boundary, after)
            if owner is not None and owner.profile.linear is not None
            else None
        )
        moved_closer = (
            before_distance is not None
            and after_distance is not None
            and after_distance < before_distance
        )
        if moved_closer or (boundary_reached and not boundary_was_reached):
            return ProgramStep(
                ProgramStepStatus.KEEP_RUNNING,
                producer,
                boundary,
                boundary.tag,
                projected_changes=projected_changes,
                trace=trace,
                next_trace=next_trace,
                reason=f"the immediate boundary on {boundary.tag} moved closer",
            )
        if boundary_was_reached:
            return ProgramStep(
                ProgramStepStatus.UNCLEAR,
                producer,
                boundary,
                boundary.tag,
                required_inputs=required,
                context_actions=context_actions,
                projected_changes=projected_changes,
                trace=trace,
                next_trace=next_trace,
                reason="the boundary was ready but the selected result did not survive the scan",
            )

    if _values_match(after.get(producer.command_tag), producer.command_value):
        command_boundary = Eq(
            producer.command_tag,
            frozenset((producer.command_value,)),
        )
        return ProgramStep(
            ProgramStepStatus.KEEP_RUNNING,
            producer,
            command_boundary,
            producer.command_tag,
            projected_changes=projected_changes,
            trace=trace,
            next_trace=next_trace,
            reason="the selected producer reaches its commanded value",
        )

    if writer_runs:
        return ProgramStep(
            ProgramStepStatus.UNCLEAR,
            producer,
            boundary,
            boundary.tag if boundary is not None else producer.command_tag,
            projected_changes=projected_changes,
            trace=trace,
            next_trace=next_trace,
            reason="the selected producer wrote its value but it did not survive a later write",
        )

    if required:
        return ProgramStep(
            ProgramStepStatus.NEEDS_INPUT,
            producer,
            boundary,
            boundary.tag if boundary is not None else producer.command_tag,
            required_inputs=required,
            context_actions=context_actions,
            projected_changes=projected_changes,
            trace=trace,
            next_trace=next_trace,
            reason="the exact producer is stopped at an external input",
        )

    return ProgramStep(
        ProgramStepStatus.UNCLEAR,
        producer,
        boundary,
        boundary.tag if boundary is not None else producer.command_tag,
        projected_changes=projected_changes,
        trace=trace,
        next_trace=next_trace,
        reason="the exact producer did not make target-relative progress",
    )
