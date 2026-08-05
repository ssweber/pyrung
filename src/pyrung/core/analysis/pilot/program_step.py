"""Prove whether one exact program producer can keep running.

This is a read-only decision made before Compass chooses an action.  It projects
an otherwise-unchanged controlled PLC world for a few scans, plus one
counterfactual input patch per required input, writer-locks the backward trace to
the selected rung, and reports one of four plain outcomes:

* keep running because a target-relative boundary moved, or because the program
  is crossing a boundary it owns whose motion dissolves a requirement that was
  read mid-crossing;
* supply the currently unmet external input;
* interrupted because the live pipeline moved before the producer could be read
  as waiting;
* unclear because no safe forward claim can be made.

An interruption is a reading, not a plan: it names the motion to observe and
returns the decision to the caller. This module never materializes an action
from the projected world, so a requirement that belongs to the world past an
owned boundary cannot enter candidate construction as live work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pyrung.core.analysis.observed import runs_for_node, writer_runs_for_node
from pyrung.core.analysis.pilot.advance import build_advance_index, demand_holds
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.trace import (
    TraceAction,
    TraceNode,
    trace_back,
)
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.crossing import AffineCmp, Cmp, Eq
from pyrung.core.instruction.advance import AdvanceStep, constraint_holds


class ProgramStepStatus(StrEnum):
    KEEP_RUNNING = "keep_running"
    NEEDS_INPUT = "needs_input"
    INTERRUPTED = "interrupted"
    UNCLEAR = "unclear"


@dataclass(frozen=True)
class ProgramInputHandoff:
    """One input's proved handoff to the next instruction-owned operation."""

    action: tuple[str, Any]
    boundary: Eq | Cmp | AffineCmp
    channel: str


@dataclass(frozen=True)
class ProgramMotion:
    """One exact observable channel motion proved by the unchanged projection."""

    channel_tag: str
    before_value: Any
    target_value: Any


@dataclass(frozen=True)
class ProgramStep:
    """Current-world proof result for one selected producer."""

    status: ProgramStepStatus
    producer: Any
    boundary: Eq | Cmp | AffineCmp | None
    channel: str | None
    # Exact evidence that the selected producer ran in the first projected
    # scan.  Later projected progress must not promise this write early.
    producer_observed: bool = False
    required_inputs: tuple[TraceAction, ...] = ()
    context_actions: tuple[tuple[str, Any], ...] = ()
    projected_changes: tuple[tuple[str, Any, Any], ...] = ()
    trace: TraceNode | None = None
    next_trace: TraceNode | None = None
    input_handoffs: tuple[ProgramInputHandoff, ...] = ()
    reason: str = ""
    # Pipeline channels that moved in the unchanged projection before this
    # producer could be read as waiting. The executable response is to preserve
    # the live value and observe that motion, not to choose an alternate route.
    preserve_channels: tuple[str, ...] = ()
    handoff_by_action: Mapping[tuple[str, Any], ProgramInputHandoff] = field(
        init=False,
        repr=False,
        compare=False,
    )
    uniform_handoff_boundary: Eq | Cmp | AffineCmp | None = field(
        init=False,
        default=None,
    )
    required_pairs: frozenset[tuple[str, Any]] = field(init=False, default=frozenset())
    inputs_with_lifetime: tuple[TraceAction, ...] = field(init=False, default=())
    observable_motions: tuple[ProgramMotion, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        handoffs = {handoff.action: handoff for handoff in self.input_handoffs}
        required_pairs = frozenset(action.pair for action in self.required_inputs)
        boundary: Eq | Cmp | AffineCmp | None = None
        if required_pairs and required_pairs <= handoffs.keys():
            required_handoffs = tuple(handoffs[action.pair] for action in self.required_inputs)
            candidate = required_handoffs[0].boundary
            if all(handoff.boundary == candidate for handoff in required_handoffs):
                boundary = candidate
        object.__setattr__(self, "handoff_by_action", MappingProxyType(handoffs))
        object.__setattr__(self, "uniform_handoff_boundary", boundary)
        object.__setattr__(self, "required_pairs", required_pairs)
        object.__setattr__(
            self,
            "inputs_with_lifetime",
            (
                tuple(replace(action, until=boundary) for action in self.required_inputs)
                if boundary is not None
                else self.required_inputs
            ),
        )
        observable_channels = frozenset(
            (*self.preserve_channels, *((self.channel,) if self.channel is not None else ()))
        )
        object.__setattr__(
            self,
            "observable_motions",
            tuple(
                ProgramMotion(tag, before, after)
                for tag, before, after in reversed(self.projected_changes)
                if tag in observable_channels and not _values_match(before, after)
            ),
        )

    def observable_motion(self, preferred_channel: str | None = None) -> ProgramMotion | None:
        """Return the owned motion, honoring a caller's outer route preference."""

        if preferred_channel is not None:
            preferred = next(
                (
                    motion
                    for motion in self.observable_motions
                    if motion.channel_tag == preferred_channel
                ),
                None,
            )
            if preferred is not None:
                return preferred
        return next(
            (motion for motion in self.observable_motions if motion.channel_tag == self.channel),
            self.observable_motions[0] if self.observable_motions else None,
        )


def _first_advance(root: TraceNode) -> AdvanceStep | None:
    # Producer traces are ordered execution paths: the first boundary is the
    # first unsatisfied advance in depth-first trace order, not the shallowest
    # advance anywhere in the tree.
    for node in root.iter_nodes(order="depth_first"):
        if node.advance is not None and not node.satisfied:
            return node.advance
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


def _input_reaches_exact_producer(
    action: TraceAction,
    channels: frozenset[str],
    ctx: Any,
    producer: Any,
    plc: Any,
    pilot_rungs: Sequence[Any],
) -> bool:
    """Whether one controlled scan carries *action* through its live barrier.

    Movement on a crossed pipeline channel is not enough: an earlier safety or
    mode transition can move that channel while bypassing the selected
    producer entirely.  The exact producer owns this read, so acceptance means
    that producer wrote its selected value in the occurrence replay.
    """
    if not channels:
        return True
    fork = fork_with_pilot_rungs(plc, pilot_rungs)
    fork.patch({action.tag: action.value})
    fork.step()
    runs = fork._replay_rung_runs_at(fork.state.scan_id)
    return bool(
        writer_runs_for_node(
            ctx.pdg,
            ctx.program,
            producer.rung_index,
            producer.command_tag,
            producer.command_value,
            runs,
        )
    )


def _input_handoffs(
    required: Sequence[TraceAction],
    context_actions: Sequence[tuple[str, Any]],
    ctx: Any,
    producer: Any,
    plc: Any,
    pilot_rungs: Sequence[Any],
) -> tuple[ProgramInputHandoff, ...]:
    """Project each input only to the next owned boundary, never through it."""
    index = build_advance_index(ctx.program, getattr(plc, "_harness", None))
    handoffs: list[ProgramInputHandoff] = []
    for action in required:
        fork = fork_with_pilot_rungs(plc, pilot_rungs)
        patch = dict(context_actions)
        patch[action.tag] = action.value
        fork.patch(patch)
        fork.step()
        trace = _trace_exact(ctx, producer, dict(fork.state.tags))
        advance = _first_advance(trace)
        boundary = advance.until if advance is not None else None
        if boundary is None or index.resolve(boundary.tag) is None:
            continue
        handoffs.append(
            ProgramInputHandoff(
                action=action.pair,
                boundary=boundary,
                channel=boundary.tag,
            )
        )
    return tuple(handoffs)


def read_program_step(
    ctx: Any,
    producer: Any,
    plc: Any,
    pilot_rungs: Sequence[Any] = (),
    *,
    resting: Mapping[str, Any] | None = None,
    projection_scans: int = 4,
) -> ProgramStep:
    """Project an unchanged controlled world and read the exact producer.

    The function never installs actions on the real PLC. ``pilot_rungs`` are
    rebuilt on the fork so "unchanged" includes PILOT's already-established
    holds.
    """

    before = dict(ctx.snapshot)
    fork = fork_with_pilot_rungs(plc, pilot_rungs)
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
    advance = _first_advance(trace)
    boundary = advance.until if advance is not None else None
    required, context_actions = _input_split(trace, trace_snapshot, resting or {})
    input_handoffs = _input_handoffs(
        required,
        context_actions,
        ctx,
        producer,
        plc,
        pilot_rungs,
    )
    barriers = _action_channel_barriers(trace, trace_snapshot, ctx)
    inputs_blocked_here = tuple(
        action
        for action in required
        if barriers.get(action.pair)
        and not _input_reaches_exact_producer(
            action,
            barriers[action.pair],
            ctx,
            producer,
            plc,
            pilot_rungs,
        )
    )

    index = build_advance_index(ctx.program, getattr(fork, "_harness", None))
    owner = index.resolve(boundary.tag) if boundary is not None else None
    linear = owner.profile.linear if owner is not None else None

    def _distance(snapshot: Mapping[str, Any]) -> Any:
        return (
            linear.distance(boundary, snapshot)
            if linear is not None and boundary is not None
            else None
        )

    before_distance = _distance(trace_snapshot)
    boundary_was_reached = (
        constraint_holds(boundary, trace_snapshot) is True if boundary is not None else False
    )
    progress_receipt = advance.progress if advance is not None else None

    scans = max(1, int(projection_scans))
    for _ in range(max(0, scans - consumed_scans)):
        fork.step()
        current = fork.state.tags
        if _values_match(current.get(producer.command_tag), producer.command_value):
            break
        if boundary is not None and constraint_holds(boundary, current) is True:
            break
        if boundary is not None and before_distance is not None:
            current_distance = _distance(current)
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
    relevant = {node.tag for tree in (trace, next_trace) for node in tree.iter_nodes()} | {
        producer.command_tag,
        *producer.co_writes,
    }
    projected_changes = tuple(
        (tag, before.get(tag), after.get(tag))
        for tag in sorted(relevant)
        if not _values_match(before.get(tag), after.get(tag))
    )
    departed_channels = tuple(
        role.channel_tag
        for role in getattr(ctx, "pipeline_roles", ())
        if not _values_match(before.get(role.channel_tag), after.get(role.channel_tag))
    )

    def _step(
        status: ProgramStepStatus,
        *,
        step_boundary: Eq | Cmp | AffineCmp | None = boundary,
        channel: str | None = None,
        required_inputs: tuple[TraceAction, ...],
        context: tuple[tuple[str, Any], ...] = (),
        handoffs: tuple[ProgramInputHandoff, ...] = (),
        reason: str,
        preserve_channels: tuple[str, ...] = (),
    ) -> ProgramStep:
        return ProgramStep(
            status,
            producer,
            step_boundary,
            (
                channel
                if channel is not None
                else step_boundary.tag
                if step_boundary is not None
                else producer.command_tag
            ),
            producer_observed=bool(writer_runs),
            required_inputs=required_inputs,
            context_actions=context,
            input_handoffs=handoffs,
            projected_changes=projected_changes,
            trace=trace,
            next_trace=next_trace,
            reason=reason,
            preserve_channels=preserve_channels,
        )

    if inputs_blocked_here:
        names = ", ".join(action.tag for action in inputs_blocked_here)
        return _step(
            ProgramStepStatus.UNCLEAR,
            required_inputs=(),
            reason=f"{names} is not accepted by the current program state",
        )

    if repeated_producer:
        return _step(
            ProgramStepStatus.UNCLEAR,
            required_inputs=required,
            context=context_actions,
            handoffs=input_handoffs,
            reason="the selected producer ran more than once with occurrence-specific state",
        )

    if boundary is not None:
        boundary_reached = constraint_holds(boundary, after) is True
        after_distance = _distance(after)
        moved_closer = (
            before_distance is not None
            and after_distance is not None
            and after_distance < before_distance
        )
        progress_observed = demand_holds(progress_receipt, after)
        if moved_closer or (boundary_reached and not boundary_was_reached) or progress_observed:
            reason = (
                f"the immediate boundary on {boundary.tag} moved closer"
                if moved_closer or boundary_reached
                else "the owner reports progress through its operation witness"
            )
            return _step(
                ProgramStepStatus.KEEP_RUNNING,
                required_inputs=(),
                reason=reason,
            )
        if boundary_was_reached:
            return _step(
                ProgramStepStatus.UNCLEAR,
                required_inputs=required,
                context=context_actions,
                handoffs=input_handoffs,
                reason="the boundary was ready but the selected result did not survive the scan",
            )

    if _values_match(after.get(producer.command_tag), producer.command_value):
        command_boundary = Eq(
            producer.command_tag,
            frozenset((producer.command_value,)),
        )
        return _step(
            ProgramStepStatus.KEEP_RUNNING,
            step_boundary=command_boundary,
            required_inputs=(),
            reason="the selected producer reaches its commanded value",
        )

    if writer_runs:
        return _step(
            ProgramStepStatus.UNCLEAR,
            required_inputs=(),
            reason="the selected producer wrote its value but it did not survive a later write",
        )

    if departed_channels:
        names = ", ".join(departed_channels)
        return _step(
            ProgramStepStatus.INTERRUPTED,
            required_inputs=(),
            reason=(
                f"{names} moved while checking the selected producer; "
                "its operation reading is no longer current"
            ),
            preserve_channels=departed_channels,
        )

    if required:
        # The program may own an automatic boundary that is still crossing while
        # this producer is read.  A requirement read mid-crossing describes the
        # world *after* that boundary, not an input the program is stopped at:
        # at a sequencer step that advances on its own, the next step's command
        # would be surfaced as this step's live work.  The settled projection is
        # the disproof -- an input the program is genuinely waiting on is still
        # required once its own motion finishes.
        #
        # This is progress, not interference: report the crossing itself as the
        # immediate boundary so the caller coasts to its landing and reads the
        # settled world again, rather than acting on a requirement from it.
        settled_required, _settled_context = _input_split(next_trace, after, resting or {})
        settled_pairs = {action.pair for action in settled_required}
        stale = tuple(action for action in required if action.pair not in settled_pairs)
        # Nothing was patched into this projection, so every change is the
        # program's own motion; an installed PILOT hold is excluded because its
        # effect is PILOT's, not the program's.  Only a coordinate this trace
        # actually read can be the boundary that invalidated it.
        trace_tags = {node.tag for node in trace.iter_nodes()}
        crossing = next(
            (
                (tag, after_value)
                for tag, _before_value, after_value in projected_changes
                if tag in trace_tags and tag not in getattr(ctx, "steerable", frozenset())
            ),
            None,
        )
        if stale and crossing is not None:
            stale_names = ", ".join(sorted({action.tag for action in stale}))
            return _step(
                ProgramStepStatus.KEEP_RUNNING,
                step_boundary=Eq(crossing[0], frozenset((crossing[1],))),
                channel=crossing[0],
                required_inputs=(),
                reason=(
                    f"{crossing[0]} is crossing a boundary the program owns; "
                    f"{stale_names} is not required once that motion settles"
                ),
            )
        return _step(
            ProgramStepStatus.NEEDS_INPUT,
            required_inputs=required,
            context=context_actions,
            handoffs=input_handoffs,
            reason="the exact producer is stopped at an external input",
        )

    return _step(
        ProgramStepStatus.UNCLEAR,
        required_inputs=(),
        reason="the exact producer did not make target-relative progress",
    )
