"""Retain and bind the first scan a Pilot invocation runs from scan 0.

A plan starts from the session, and a scan the session already ran is not Pilot's to
revisit: leaving that state takes a restart, which is a step of the plan (``restart.py``).
At scan 0 nothing has run, so Compass's ObserveScan runs the first scan on Pilot's own fork.
This module retains that execution receipt and binds it to Compass's selected route before
WorkingTheory interprets it.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pyrung.core.analysis.pilot.theory_recording as _theory_recording
from pyrung.core.analysis.pilot.bootstrap import (
    _BootstrapExecution,
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
from pyrung.core.analysis.pilot.requirement_evidence import _derive_bootstrap_requirements
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.trace_read import TraceReadConstraints
from pyrung.core.analysis.pilot.types import (
    _IterationFrame,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint

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
    _theory_recording._record_bootstrap_theory_transition(
        state,
        ctx,
        bound,
        remaining_budget=state.remaining_search_scans(ctx.max_scans),
    )
    return bound
