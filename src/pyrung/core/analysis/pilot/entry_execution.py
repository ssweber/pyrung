"""Import and bind the execution adjacent to a Pilot invocation.

The runner owns physical scan history. This module retains the one adjacent
execution receipt Pilot may revisit and binds it to Compass's selected route
before WorkingTheory interprets it.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from pyrsistent import pvector

import pyrung.core.analysis.pilot.theory_drive as _theory_drive
from pyrung.core.analysis.pilot.bootstrap import (
    bind_observed_route_designations,
    observe_bootstrap_effects,
)
from pyrung.core.analysis.pilot.execution import (
    ChannelMotion,
    ExecutionReceipt,
    capture_execution_spans,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    BearingObjective,
    OrientationResult,
)
from pyrung.core.analysis.pilot.overlay import fork_with_pilot_rungs
from pyrung.core.analysis.pilot.requirement_evidence import (
    _configured_input_names,
    _derive_bootstrap_requirements,
)
from pyrung.core.analysis.pilot.trace import TraceReadConstraints, trace_back
from pyrung.core.analysis.pilot.types import (
    _BootstrapExecution,
    _CausalCheckpoint,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _World,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key

logger = logging.getLogger(__name__)


def entry_execution_receipt(
    checkpoint: _CausalCheckpoint,
    execution: Any,
    scan_after: int,
    *,
    existing: ExecutionReceipt | None = None,
) -> _BootstrapExecution:
    """Retain one adjacent program scan without interpreting its route yet."""

    projection = execution._replay_rung_write_projection_at(scan_after)
    if projection is None:
        raise RuntimeError("entry observation has no exact execution projection")
    scan_before = scan_after - 1
    execution_receipt = existing or ExecutionReceipt(
        before_snap=projection.entry_tags,
        after_snap=projection.exit_tags,
        channel_motion=ChannelMotion(),
        coast_receipt=None,
        timeline=(),
        spans=capture_execution_spans(execution, (scan_after,)),
        source_scan=scan_before,
    )
    return _BootstrapExecution(
        checkpoint=checkpoint,
        projection=projection,
        designations=(),
        appeared_effects=(),
        execution=execution_receipt,
        route_bound=False,
    )


def import_adjacent_entry_scan(
    state: _PilotState,
    ctx: _PilotContext,
) -> _BootstrapExecution | None:
    """Import the runner's exact adjacent history as the same entry receipt.

    The runner already owns the rolling history. Pilot retains only the one
    source checkpoint and ``N-1 -> N`` projection it has authority to revisit.
    """

    scan_after = state.work.state.scan_id
    if scan_after <= 0:
        return None
    try:
        source_work = fork_with_pilot_rungs(
            state.work,
            state.pilot_rungs,
            scan_id=scan_after - 1,
        )
    except KeyError:
        return None
    source_snap = dict(source_work.state.tags)
    source_world = _World(
        work=source_work,
        committed_acts=pvector([]),
        best_trend=None,
        pilot_rungs=state.pilot_rungs,
        dwell_scans=0,
    )
    checkpoint = _CausalCheckpoint(
        key=(
            _pilot_world_key(source_snap, state.key_config, (), ())
            if state.key_config is not None
            else None
        ),
        world=source_world,
        objective=BearingObjective(ctx.target),
        configured_inputs=ctx.configured_inputs | _configured_input_names(state.work),
    )
    try:
        receipt = entry_execution_receipt(checkpoint, state.work, scan_after)
    except RuntimeError:
        return None
    state.invocation_checkpoint = checkpoint
    state.bootstrap_execution = receipt
    state.search_start_scan = checkpoint.world.work.state.scan_id
    return receipt


def retain_entry_bearing_execution(
    state: _PilotState,
    checkpoint: _CausalCheckpoint,
    executed: Any,
) -> None:
    """Retain the exact scan produced by an accepted ObserveScan bearing."""

    execution = executed.execution
    if execution is None:
        raise RuntimeError("entry observation lost its immutable execution receipt")
    scan_after = executed.pulse.fork.state.scan_id
    receipt = entry_execution_receipt(
        checkpoint,
        executed.pulse.fork,
        scan_after,
        existing=execution,
    )
    state.invocation_checkpoint = checkpoint
    state.bootstrap_execution = receipt
    state.search_start_scan = checkpoint.world.work.state.scan_id


def bind_entry_execution_to_route(
    state: _PilotState,
    ctx: _PilotContext,
    result: OrientationResult,
    frame: _IterationFrame,
) -> _BootstrapExecution | None:
    """Interpret an adjacent scan only after Compass selected its landing route."""

    receipt = state.bootstrap_execution
    if receipt is None or receipt.route_bound:
        return None
    objective = (
        result.objective
        if isinstance(result, Bearing)
        else BearingObjective(ctx.target, frontier=result.frontier)
    )
    checkpoint = replace(receipt.checkpoint, objective=objective)
    channel_tags = {ctx.target.tag, *ctx.opaque_loop}
    channel_tags.update(role.channel_tag for role in (*ctx.pipeline_roles, *ctx.chart_roles))
    source_tree = frame.tree
    if ctx.target.predicate is None:
        try:
            source_work = receipt.checkpoint.world.work
            source_tree = trace_back(
                ctx.target.tag,
                ctx.target.value,
                dict(source_work.state.tags),
                ctx.pdg,
                ctx.program,
                ctx.steerable,
                constraints=TraceReadConstraints.from_context(
                    ctx,
                    source_work,
                    route=(
                        result.orientation.world.root_route
                        if result.orientation is not None
                        else None
                    ),
                    avoid_pred=ctx.avoid_pred,
                ),
            )
        except Exception:  # noqa: BLE001 - landing frame remains conservative fallback
            logger.debug("pilot: entry source route binding failed closed", exc_info=True)
    designations = bind_observed_route_designations(
        source_tree,
        ctx.pdg,
        ctx.program,
        receipt.projection,
        steerable=ctx.steerable,
        channel_tags=frozenset(channel_tags),
    )
    bound = replace(
        receipt,
        checkpoint=checkpoint,
        designations=designations,
        appeared_effects=observe_bootstrap_effects(designations, receipt.projection),
        route_bound=True,
    )
    state.invocation_checkpoint = checkpoint
    state.bootstrap_execution = bound
    _derive_bootstrap_requirements(state, ctx, bound)
    _theory_drive._record_bootstrap_theory_transition(
        state,
        ctx,
        bound,
        remaining_budget=state.remaining_search_scans(ctx.max_scans),
    )
    return bound
