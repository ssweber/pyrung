"""Public entry points and outer orchestration for PILOT drives.

This module builds static/runtime context, prepares the user-selected trace
constraint, and dispatches the typed orientation results returned by
``Compass``.
It invokes execution, owns verification-time excursion investigation, applies
observations, commits eligible forks, delegates post-commit recovery, and
converts the event stream into public results. It does not synthesize a
navigation decision.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

import pyrung.core.analysis.pilot.recovery_continuation as _recovery_continuation
import pyrung.core.analysis.pilot.requirement_repair as _requirement_repair
import pyrung.core.analysis.pilot.theory_drive as _theory_drive
from pyrung.core.analysis.graph import (
    Plan,
    PlanStatus,
    PlanStep,
    RouteAlt,
    RoutePivot,
    RouteTaken,
)
from pyrung.core.analysis.pdg import resolve_rung
from pyrung.core.analysis.pilot.advance import iter_advance_owners
from pyrung.core.analysis.pilot.awaited_actions import sibling_producer_family
from pyrung.core.analysis.pilot.bootstrap import (
    bind_observed_route_designations,
    observe_bootstrap_effects,
)
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    CoastObservation,
    Compass,
    EvidenceScope,
    NavigationCatalog,
    ProbeExhaustedObservation,
)
from pyrung.core.analysis.pilot.earned_work import (
    build_earned_work,
    earned_work_is_useful_motion,
)
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    observe_execution_window,
    occurrence_snapshot,
    promote_certified_prefix_target_observation,
    promote_terminal_target_observation,
    terminal_target_replay_scan_ids,
)
from pyrung.core.analysis.pilot.execution import (
    ChannelMotion,
    ExecutionReceipt,
    capture_execution_spans,
    execution_owner,
)
from pyrung.core.analysis.pilot.intrascan import (
    research_intrascan_boundary_realization,
    research_intrascan_traceback,
    research_retained_frontier_realization,
)
from pyrung.core.analysis.pilot.investigate import investigate_excursion
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    BearingObjective,
    Coast,
    ComposeCorrection,
    IntrascanPulse,
    LocalProgressKind,
    NavigationConstraints,
    NeedIntrascanBoundaryRealization,
    NeedIntrascanTraceback,
    NeedProbe,
    NeedResearch,
    ObserveScan,
    OrientationResult,
    OrientationWorld,
    ProgramScan,
    Stuck,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _target_unresolved_condition,
    _until_unresolved_condition,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.pipeline_graph import (
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.program_facts import (
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
)
from pyrung.core.analysis.pilot.program_step import (
    read_program_step,
)
from pyrung.core.analysis.pilot.progress import (
    _anchor_bearing_receipt,
    _anchor_frame_receipt,
    _install_confirmed_correction,
    _monitor_trend,
    _promote_probationary_corrections,
    _record_pending_landing,
    _trial_checkpoint,
)
from pyrung.core.analysis.pilot.recording import (
    _act_event,
    _build_plan_journal,
    _candidates_built_payload,
    _frontier_clause,
    _iteration_payload,
    _knowledge_payload,
)
from pyrung.core.analysis.pilot.recovery import (
    assert_recovery_disposable_state,
    assert_recovery_inactive,
)
from pyrung.core.analysis.pilot.requirement_evidence import (
    _attempt_productive_scan,
    _configured_input_names,
    _derive_attempt_requirements,
    _derive_bootstrap_requirements,
    _derive_settled_target_requirements,
    _release_attempt_projections,
    _retain_expectation_receipt,
    _selected_terminal_target_expectation,
)
from pyrung.core.analysis.pilot.skiff import probe_live_guard_frontiers
from pyrung.core.analysis.pilot.steer import _install_prerequisites, execute
from pyrung.core.analysis.pilot.theory_evidence import (
    _theory_boundary_from_checkpoint,
    _theory_live_boundary,
    _theory_requirement_snapshot,
    _theory_transition_after_monitor,
    _theory_transition_from_attempt,
    _TheoryTransitionEvidence,
)
from pyrung.core.analysis.pilot.trace import (
    DomainPrior,
    TraceChoice,
    TraceReadConstraints,
    UnsupportedConstruct,
    _route_forced_names,
    enumerate_trace_choices,
    frontier_pairs,
    rank_trace_choices,
    target_reached,
    trace_back,
)
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    PilotEvent,
    WorldView,
    _AcceptedTrial,
    _ActionPair,
    _AttemptResult,
    _BootstrapExecution,
    _CausalCheckpoint,
    _Checkpoint,
    _CommittedAct,
    _ContinuationCheckpoint,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _Step,
    _StepContext,
    _World,
)
from pyrung.core.analysis.pilot.verify import (
    verify_excursion_replay,
    verify_gates,
)
from pyrung.core.analysis.pilot.working_theory import (
    AbandonTheory,
    ConductivityResearchFinding,
    IntrascanOrdinarySteerFinding,
    IntrascanTracebackFinding,
    IntrascanTracebackFrontier,
    RecordConductivityResearch,
    RecordIntrascanTraceback,
    RecordIntrascanTracebackFrontier,
    TheoryAttemptDisposition,
    TheoryTermination,
    active_theory,
    temporal_need_request,
    theory_view,
)
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
    _StateKeyConfig,
)
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.analysis.pilot.pipeline_graph import StaticTransitionGraph
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)
def _entry_execution_receipt(
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
        source_world=checkpoint.key,
        decision_identity=("executed-program-scan", scan_before, scan_after),
    )
    return _BootstrapExecution(
        checkpoint=checkpoint,
        projection=projection,
        designations=(),
        appeared_effects=(),
        execution=execution_receipt,
        route_bound=False,
    )


def _import_adjacent_entry_scan(
    state: _PilotState, ctx: _PilotContext
) -> _BootstrapExecution | None:
    """Import the runner's exact adjacent history as the same entry receipt.

    The runner already owns the rolling history. PILOT retains only the one
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
        receipt = _entry_execution_receipt(checkpoint, state.work, scan_after)
    except RuntimeError:
        return None
    state.invocation_checkpoint = checkpoint
    state.bootstrap_execution = receipt
    state.search_start_scan = checkpoint.world.work.state.scan_id
    return receipt


def _retain_entry_bearing_execution(
    state: _PilotState,
    checkpoint: _CausalCheckpoint,
    executed: Any,
) -> None:
    """Retain the exact scan produced by an accepted ObserveScan bearing."""

    execution = executed.execution
    if execution is None:
        raise RuntimeError("entry observation lost its immutable execution receipt")
    scan_after = executed.pulse.fork.state.scan_id
    receipt = _entry_execution_receipt(
        checkpoint,
        executed.pulse.fork,
        scan_after,
        existing=execution,
    )
    state.invocation_checkpoint = checkpoint
    state.bootstrap_execution = receipt
    state.search_start_scan = checkpoint.world.work.state.scan_id


def _bind_entry_execution_to_route(
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


@dataclass(frozen=True)
class _DriveSetup:
    """Static/runtime preparation shared by every target driven on one PLC."""

    work: PLC
    program: Any
    pdg: ProgramGraph
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    anchor_scan: int
    diag_snapshot: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    stateful_domains: dict[str, tuple[Any, ...]] | None
    key_config: _StateKeyConfig | None
    evidence: TransitionEvidence | None
    compass: Compass
    opaque_loop: frozenset[str]
    configured_inputs: frozenset[str]


@dataclass(frozen=True)
class _DriveOutcome:
    """Named result assembled from the terminal event of one drive loop."""

    reached: bool
    work: PLC
    journal: tuple[PlanStep, ...]
    journey: tuple[_Step, ...]
    reason: str | None
    knowledge: dict[str, Any]
    root_route: TraceChoice | None


@dataclass(frozen=True)
class _IterationTransition:
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


@dataclass(frozen=True)
class _ProverContext:
    """Best-effort static evidence shared by drive setup and target context."""

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    stateful_domains: dict[str, tuple[Any, ...]] | None = None
    key_config: _StateKeyConfig | None = None
    evidence: TransitionEvidence | None = None


# ---------------------------------------------------------------------------
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


def _commit_step(
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


def _make_pilot_context(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    edge_tags: set[str],
    resting: dict[str, Any],
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None,
    stateful_domains: dict[str, tuple[Any, ...]] | None,
    evidence: TransitionEvidence | None,
    key_config: _StateKeyConfig | None,
    compass: Compass | None,
    opaque_loop: frozenset[str],
    route: TraceChoice | None,
    max_scans: int,
    avoid_pred: Any = None,
    target_predicate: Any = None,
    configured_inputs: frozenset[str] = frozenset(),
) -> _PilotContext:
    from pyrung.core.analysis.pilot.evidence import discover_chart_roles

    pipeline_roles = _infer_pipeline_roles_for_context(
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )
    chart_roles = discover_chart_roles(
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )
    pipeline_internal_tags = frozenset(
        tag for role in pipeline_roles for tag in role.trace_internal_tags
    )
    prior_compass = compass or Compass()
    compass = Compass(
        catalog=NavigationCatalog(
            slices=prior_compass.catalog.slices,
            graphs=_build_static_transition_graphs_for_context(
                pipeline_roles,
                pdg,
                program,
                steerable,
                opaque_loop,
                evidence,
            ),
            chart_graphs=_build_static_transition_graphs_for_context(
                chart_roles,
                pdg,
                program,
                steerable,
                opaque_loop,
                evidence,
            ),
        ),
        knowledge=prior_compass.knowledge,
    )
    # Domain prior for trace's inequality resolution: nondeterministic domains
    # for free inputs, stateful domains for program-owned tags, and affine
    # func-deps for derived tags. All are receipts from the same ExploreContext.
    domain_prior = DomainPrior(
        nd_domains=nd_domains,
        stateful_domains=stateful_domains,
        func_deps=evidence.affine_projections() if evidence is not None else None,
    )
    # Clear-only (ack-cleared momentary) command tags: a subset of ``steerable``
    # kept off prerequisite holds and off preferred init/reset writer selection.
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, program)
    return _PilotContext(
        target=TargetSpec(target_tag, target_value, target_predicate),
        pdg=pdg,
        program=program,
        steerable=steerable,
        edge_tags=edge_tags,
        clear_only=clear_only,
        resting=resting,
        nd_domains=nd_domains,
        domain_prior=domain_prior,
        evidence=evidence,
        key_config=key_config,
        compass=compass,
        opaque_loop=opaque_loop,
        pipeline_roles=pipeline_roles,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        blocked_actions=frozenset(),
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        configured_inputs=configured_inputs,
        chart_roles=chart_roles,
    )


def _prepare_drive(
    plc: PLC,
    *,
    unlink: list[str] | None,
) -> _DriveSetup:
    """Build the shared program/runtime analysis for one public drive."""

    from pyrung.core.analysis.pdg import build_program_graph

    configured_inputs = _configured_input_names(plc)
    work = fork_with_pilot_rungs(plc, (), history_budget=math.inf)
    program = plc._program
    pdg = build_program_graph(program)
    harness_fb = install_harness(work, unlink=unlink)
    ref_consts = compute_reference_constants(pdg, program, work._known_tags_by_name)
    steerable = compute_steerable(pdg, work._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, work._known_tags_by_name, pdg, program)
    diag_snapshot = dict(work.state.tags)
    prover = _build_prover_context(
        program,
        diag_snapshot,
    )
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    return _DriveSetup(
        work=work,
        program=program,
        pdg=pdg,
        steerable=steerable,
        edge_tags=edge_tags,
        resting=resting,
        anchor_scan=work.state.scan_id,
        diag_snapshot=diag_snapshot,
        nd_domains=prover.nd_domains,
        stateful_domains=prover.stateful_domains,
        key_config=prover.key_config,
        evidence=prover.evidence,
        compass=Compass(NavigationCatalog(slices=tuple(opaque_slices))),
        opaque_loop=detect_opaque_loop(pdg, program),
        configured_inputs=configured_inputs,
    )


def _prepare_target_context(
    setup: _DriveSetup,
    target_tag: str,
    target_value: Any,
    target_predicate: Any,
    *,
    max_scans: int,
    avoid_pred: Any,
    compass: Compass | None = None,
    work: PLC | None = None,
) -> tuple[_PilotContext, RouteTaken | None]:
    """Bind one target and its initial route report to a prepared drive."""

    target_work = setup.work if work is None else work
    route_taken = _prepare_route(
        target_work,
        target_tag,
        target_value,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        target_predicate=target_predicate,
        avoid_pred=avoid_pred,
    )
    ctx = _make_pilot_context(
        target_work,
        target_tag,
        target_value,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.edge_tags,
        setup.resting,
        nd_domains=setup.nd_domains,
        stateful_domains=setup.stateful_domains,
        evidence=setup.evidence,
        key_config=setup.key_config,
        compass=compass or setup.compass,
        opaque_loop=setup.opaque_loop,
        route=None,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        target_predicate=target_predicate,
        configured_inputs=setup.configured_inputs,
    )
    return ctx, route_taken


def _infer_pipeline_roles_for_context(
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[PipelineRoles, ...]:
    if not opaque_loop:
        return ()

    from pyrung.core.analysis.pilot.evidence import infer_pipeline_roles

    roles: list[PipelineRoles] = []
    for tag in sorted(opaque_loop):
        if evidence is not None and not evidence.is_stepping(tag):
            continue
        role = infer_pipeline_roles(tag, pdg, program, steerable, opaque_loop, evidence)
        if role.request_tags:
            roles.append(role)
    return tuple(roles)


def _build_static_transition_graphs_for_context(
    pipeline_roles: tuple[PipelineRoles, ...],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[StaticTransitionGraph, ...]:
    if not pipeline_roles:
        return ()
    from pyrung.core.analysis.pilot.pipeline_graph import build_static_transition_graphs

    return build_static_transition_graphs(
        pipeline_roles,
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )


def _with_avoid_reason(
    base: str,
    state: _PilotState,
    ctx: _PilotContext,
    frame: _IterationFrame | None = None,
) -> str:
    """Append the violated ``avoid=`` condition(s) to a terminal reason.

    Keeps the decline legible — (concrete frontier tag, target, outcome class) —
    so ``how(..., avoid=X)`` that excludes every path names ``X`` rather than
    surfacing a bare ``stuck``.
    """
    if getattr(ctx, "avoid_pred", None) is None:
        return base
    named = set(getattr(state, "avoid_names", set()) or ())
    if not named and frame is not None:
        # No candidate was ever action-gated (the route gate pruned every route
        # to the target silently).  Re-derive which avoid conditions forced those
        # routes so the decline still names them.
        named.update(_avoid_route_names(frame, ctx))
    if not named and frame is not None:
        # Opaque writer cuts can prevent route enumeration from reconstructing
        # the pruned Or arms.  In that case, name only avoid conditions that are
        # structurally upstream of the outstanding frontier.
        related: set[str] = set()
        for tag, _value in frontier_pairs(frame.tree, frame.snap):
            related.update(ctx.pdg.upstream_slice(tag, follow_calls=True))
        named.update(set(getattr(ctx.avoid_pred, "names", ())) & related)
    names = sorted(named)
    if not names:
        return base
    if frame is not None:
        fr = frontier_pairs(frame.tree, frame.snap)
        frontier = fr[0][0] if fr else ctx.target.tag
    else:
        frontier = ctx.target.tag
    return (
        f"{base}: avoid excludes {', '.join(names)} (frontier {frontier}, target {ctx.target.tag})"
    )


def _stopped_reason() -> str:
    """Translate internal orientation taxonomy into an honest public stop."""
    return "No productive next action was found"


def _avoid_route_names(frame: _IterationFrame, ctx: _PilotContext) -> tuple[str, ...]:
    """Avoid-condition names that forced *every* route to the value target.

    Enumerates the same routes as ``_prepare_route`` from the current frame and,
    when they are all avoid-forced (no survivor), returns the union of the
    violated member names.  ``()`` when the target isn't a value-route target or
    any route survives.
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    snap = frame.snap
    if not (
        _target_is_value_route(ctx.target.predicate)
        and not _values_match(snap.get(ctx.target.tag), ctx.target.value)
    ):
        return ()
    choices = enumerate_trace_choices(
        ctx.target.tag,
        ctx.target.value,
        snap,
        ctx.pdg,
        ctx.program,
        steerable=ctx.steerable,
        clear_only=ctx.clear_only,
    )
    read = TraceReadConstraints(
        clear_only=ctx.clear_only,
        opaque_loop=ctx.opaque_loop,
    )
    names: set[str] = set()
    survivor = False
    forced_any = False
    for ch in choices:
        tree = trace_back(
            ctx.target.tag,
            ctx.target.value,
            snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            constraints=replace(read, route=ch),
        )
        forced = _route_forced_names([tree], snap, avoid)
        if forced:
            names.update(forced)
            forced_any = True
        else:
            survivor = True
    if survivor or not forced_any:
        return ()
    # Every route to the target was avoid-forced.  Arm collapse (one Or-arm per
    # traced route) can hide members, so report the full avoid set that blocked
    # it, falling back to the observed names for a bare-callable avoid.
    return tuple(getattr(avoid, "names", ()) or sorted(names))


def _record_attempt(
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


def _resolve_excursion(
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


def _step_context(
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


def _adopt_trial(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AcceptedTrial:
    """Adopt one gate-approved trial without applying post-commit policy.

    Verification already ran inside the steering wrapper and
    ``_record_attempt`` already committed its knowledge.  This is the shared
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
    _commit_trial(trial, frame, state, ctx)
    return trial


def _monitor_committed_trial(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    continuation_hop: bool = False,
) -> Iterator[PilotEvent]:
    """Emit one adopted trial and apply outer-loop progress policy."""

    assert_recovery_inactive("monitor a committed trial")
    policy = trial.attempt.bearing.act.policy
    yield PilotEvent(
        "trial_committed",
        state.work.state.scan_id,
        {
            "candidate": dict(policy.action_pairs),
            "applied": policy.applied,
            "steps": tuple(state.steps),
            "snapshot": dict(state.work.state.tags),
        },
    )
    if isinstance(trial.attempt.bearing.act, (ObserveScan, ProgramScan, IntrascanPulse)):
        # A single program scan yields immediately. Entry observation still
        # needs route binding; an intrascan stage already owns an exact write
        # receipt but its consumer action belongs to the next Compass World.
        return
    # Verification is the one authority for whether this exact S0 -> S1/S2
    # execution advanced its selected working edge.  Re-proving the receipt
    # here from a newly traversed tree creates a second, drift-prone progress
    # protocol.  An accepted trial without a receipt still reaches legacy trend
    # handling; neither assertion horizon nor an active theory is an exemption.
    progress = trial.execution.scan_progress
    retained_selected_landing = bool(
        progress is not None and progress.kind == "selected-producer" and progress.landing_owns_tip
    )
    if (
        progress is not None
        and progress.landing_owns_tip
        and progress.kind
        in {
            "target",
            "selected-producer",
            "frontier",
        }
        and state.pending_departure is None
        and (not trial.execution.channel_motion.departed or retained_selected_landing)
    ):
        # A generic frontier crossed before a channel departure is only useful
        # local motion; the missed bearing still enters ordinary departure
        # investigation.  A selected-producer receipt is stronger when its
        # *retained landing* owns the trace tip: the program crossed the narrow
        # heading and completed the next structural edge in the same accepted
        # execution.  That is an overshoot in the heading coordinate, not an
        # ejection from the selected route.
        # The receipt does not merely exempt this landing from legacy trend
        # judgment: it *is* the recovery/checkpoint authority for the new
        # working edge. Bank the exact retained fork so a later regression is
        # investigated from this tip rather than an older trend checkpoint.
        # Raw trace distance remains a coordinate for later comparisons; it is
        # not asked to re-prove the receipt.
        if progress.kind != "target":
            state.checkpoints.append(_trial_checkpoint(trial, state))
            if progress.distance_after is not None:
                state.best_trend = progress.distance_after
            _promote_probationary_corrections(state)
        return
    if not continuation_hop:
        yield from _monitor_trend(trial, frame, state, ctx)


def _commit_trial(
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
    work, steps = _commit_step(
        pulse.fork,
        step_inputs,
        pulse.scan_before,
        ctx.resting,
        ctx.edge_tags,
        edge_inputs=dict(policy.applied),
    )
    act = _CommittedAct(steps=steps, context=_step_context(trial, frame, state))
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


def _prepare_oriented_result(
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


def _certify_current_target_prefix(
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


def _transition_once(
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
) -> _IterationTransition:
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
    _prepare_oriented_result(state, result, orientation_world, frame)
    result, recovery_program_step = _recovery_continuation.preempt_recovery_action_with_program_coast(
        result,
        frame,
        state,
        ctx,
        target,
    )
    if not isinstance(result, Bearing):
        return _IterationTransition(result=result, frame=frame)

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
        attempt = _resolve_excursion(attempt, frame, state, ctx)
    prefix_proof = None
    prefix_execution = attempt.executed_attempt
    if terminal_target_expectation is not None and prefix_execution is not None:
        prefix_proof = _certify_current_target_prefix(
            attempt,
            prefix_execution.pulse.scan_before,
            terminal_target_expectation,
            state,
            ctx,
        )
    if terminal_target_expectation is not None:
        result, attempt = _promote_transient_target_failure(
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
    _record_attempt(attempt, frame, state, ctx, result.objective, act)

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
        return _IterationTransition(
            result=result,
            frame=frame,
            attempt=attempt,
            theory_transition=theory_transition,
        )

    if defer_adoption:
        return _IterationTransition(
            result=result,
            frame=frame,
            attempt=attempt,
            trial=attempt.trial,
            theory_transition=theory_transition,
            adoption_checkpoint=receipt_checkpoint,
        )

    trial = _adopt_trial(attempt.trial, frame, state, ctx)
    if isinstance(act, ObserveScan):
        if expectation_checkpoint is None:
            raise RuntimeError("entry observation lost its source checkpoint")
        executed = attempt.executed_attempt
        if executed is None:
            raise RuntimeError("entry observation lost its exact execution")
        _retain_entry_bearing_execution(state, expectation_checkpoint, executed)
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
    return _IterationTransition(
        result=result,
        frame=frame,
        attempt=attempt,
        trial=trial,
        continuation_hop=continuation_hop,
        theory_transition=theory_transition,
        adoption_checkpoint=receipt_checkpoint,
    )


def _adopt_deferred_transition(
    transition: _IterationTransition,
    state: _PilotState,
    ctx: _PilotContext,
) -> _IterationTransition:
    """Adopt the exact fork whose controlling attempt was already recorded."""

    if transition.attempt is None or transition.trial is None:
        raise ValueError("deferred adoption requires one accepted trial")
    if not isinstance(transition.result, Bearing):
        raise ValueError("deferred adoption requires one Bearing")
    trial = _adopt_trial(transition.trial, transition.frame, state, ctx)
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


def _promote_transient_target_failure(
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


def _finished_event(
    state: _PilotState,
    ctx: _PilotContext,
    journal_channel_tags: frozenset[str],
    journal_acc_names: frozenset[str],
    *,
    reached: bool,
    reason: str,
) -> PilotEvent:
    """Build the single terminal recording shape for every loop exit."""

    return PilotEvent(
        "finished",
        state.work.state.scan_id,
        {
            "reached": reached,
            "steps": tuple(state.steps),
            "journey": tuple(state.journey),
            "knowledge": _knowledge_payload(state, ctx.compass),
            "root_route": ctx.route or state.recorded_root_route,
            "work": state.work,
            "reason": reason,
            "plan_journal": _build_plan_journal(
                state,
                state.work,
                journal_channel_tags,
                journal_acc_names,
            ),
        },
    )


def _stuck_event(
    state: _PilotState,
    ctx: _PilotContext,
    frame: _IterationFrame | None,
    reason: str,
    *,
    candidate_count: int,
    diagnosis: Stuck | None = None,
) -> PilotEvent:
    """Build the common terminal-stuck diagnostic shape."""

    data: dict[str, Any] = {
        "reason": reason,
        "distance": frame.distance_before if frame is not None else None,
        "candidate_count": candidate_count,
        "nogoods_at_key": (
            len(ctx.compass.knowledge.nogood_identities(frame.key)) if frame is not None else 0
        ),
        "terminal": True,
    }
    if diagnosis is not None:
        data["diagnosis"] = diagnosis
    return PilotEvent("stuck", state.work.state.scan_id, data)


def _stopped_events(
    state: _PilotState,
    ctx: _PilotContext,
    frame: _IterationFrame | None,
    reason: str,
    journal_channel_tags: frozenset[str],
    journal_acc_names: frozenset[str],
    *,
    candidate_count: int,
    diagnosis: Stuck | None = None,
) -> Iterator[PilotEvent]:
    """Emit one failed terminal sequence and restore its checkpoint world."""

    yield _stuck_event(
        state,
        ctx,
        frame,
        reason,
        candidate_count=candidate_count,
        diagnosis=diagnosis,
    )
    if state.checkpoints:
        state.load_world(state.checkpoints[-1].world)
    yield _finished_event(
        state,
        ctx,
        journal_channel_tags,
        journal_acc_names,
        reached=False,
        reason=reason,
    )


def _pilot_loop_events(
    plc: PLC,
    ctx: _PilotContext,
) -> Iterator[PilotEvent]:
    """Run the PILOT loop as a structured event stream."""

    assert_recovery_inactive("invoke the drive loop")
    # Semantic sets for the plan journal (see ``_build_plan_journal``): the
    # channel registers (opaque-loop tags + each pipeline role's
    # ``channel_tag``) pick the transition label; the accumulator registers
    # (from every accumulating instruction's profile, incl. harness couplings)
    # split accelerator patches from command inputs.  Both are static for the
    # life of this loop, so computed once here rather than per journal build.
    journal_channel_tags = frozenset(ctx.opaque_loop) | frozenset(
        role.channel_tag for role in ctx.pipeline_roles
    )
    journal_acc_names = frozenset(
        owner.profile.accumulator.name
        for owner in iter_advance_owners(ctx.program, harness=getattr(plc, "_harness", None))
        if owner.profile.accumulator is not None
    )
    state = _PilotState(
        world=_World(
            work=plc,
            committed_acts=pvector([]),
            best_trend=None,
            pilot_rungs=pvector([]),
            dwell_scans=0,
        ),
        key_config=ctx.key_config,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
        search_start_scan=plc.state.scan_id,
    )
    invocation_snapshot = dict(state.work.state.tags)
    state.invocation_checkpoint = _CausalCheckpoint(
        key=(
            _pilot_world_key(
                invocation_snapshot,
                state.key_config,
                state.pilot_rungs,
                state.active_requirements,
            )
            if state.key_config is not None
            else None
        ),
        world=state.snapshot_world(),
        objective=BearingObjective(ctx.target),
        configured_inputs=ctx.configured_inputs | _configured_input_names(state.work),
    )
    # The target-relative earned-work model (earned_work.py): event-earned
    # ordinals the threshold-masked search key deliberately aliases.  Static
    # for the loop's life; knowledge side (never reverted). Best-effort: an
    # empty model leaves target-relative coordinates uncredited.
    try:
        state.earned_work = build_earned_work(
            ctx.pdg,
            ctx.program,
            ctx.target.tag,
            ctx.key_config,
            steerable=ctx.steerable,
            clear_only=ctx.clear_only,
            edge_tags=frozenset(ctx.edge_tags),
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            channel_tags=frozenset(role.channel_tag for role in ctx.pipeline_roles),
            harness=getattr(plc, "_harness", None),
        )
    except Exception:  # noqa: BLE001 — diagnostics must not break the drive
        logger.debug("pilot: earned-work build failed", exc_info=True)

    # A warmed runner already owns its adjacent execution history. Import one
    # exact edge; at boundary zero Compass instead chooses ObserveScan and
    # produces the same receipt through the ordinary execution lifecycle.
    bootstrap_execution = _import_adjacent_entry_scan(state, ctx)

    yield PilotEvent(
        "started",
        state.work.state.scan_id,
        {
            "target": (ctx.target.tag, ctx.target.value),
            "steerable_count": len(ctx.steerable),
            "opaque_loop": ctx.opaque_loop,
            "pipeline_roles": ctx.pipeline_roles,
            "pipeline_internal_tags": ctx.pipeline_internal_tags,
            "route": ctx.route,
            "bootstrap_execution": (
                bootstrap_execution.diagnostic_snapshot()
                if bootstrap_execution is not None
                else None
            ),
            "active_requirements": tuple(
                requirement.diagnostic_snapshot() for requirement in state.active_requirements
            ),
        },
    )
    for requirement in state.active_requirements:
        yield PilotEvent(
            "requirement_activated",
            requirement.deadline.scan_id,
            {"requirement": requirement.diagnostic_snapshot()},
        )

    # Each turn reads the current world and builds candidate modes. Every mode
    # executes and verifies on a fork inside steer.py, after which the loop
    # applies its observations. A gate-approved fork is then committed and sent
    # to progress.py, which may checkpoint it, keep a departure pending, or
    # investigate and revert it. Rejected modes fall through to the next mode in
    # the same turn.
    # ``max_scans`` counts new search scans from this invocation's start.
    # Accepted productive coasts credit their dwell back (see
    # ``_World.dwell_scans``); tentative fork scans still count until their
    # operation is accepted. An armed self-advancing dwell — a 39k-scan dry
    # timer the coast rides — is the machine doing its own work, not the pilot
    # spending effort.
    last_frame: _IterationFrame | None = None
    last_frontier: tuple[_ActionPair, ...] = ()
    while state.search_scans < ctx.max_scans:
        requirements_before_rebase = len(state.active_requirements)
        rebased_requirements = _requirement_repair.derive_program_guard_rebases(state, ctx)
        if rebased_requirements:
            if active_theory(state.theory_state) is None:
                _theory_drive._open_theory_from_program_guard_rebases(
                    state,
                    rebased_requirements,
                    remaining_budget=state.remaining_search_scans(ctx.max_scans),
                )
            else:
                _theory_drive._refine_active_theory_from_program_guard_rebases(
                    state,
                    rebased_requirements,
                )
        for active in state.active_requirements[requirements_before_rebase:]:
            yield PilotEvent(
                "requirement_activated",
                active.deadline.scan_id,
                {"requirement": active.diagnostic_snapshot()},
            )
        temporal_request = temporal_need_request(state.theory_state)
        temporal_requirements = (
            _theory_drive._resolved_temporal_requirements(state, temporal_request)
            if temporal_request is not None
            else ()
        )
        temporal_trigger_requirements = (
            _theory_drive._resolve_temporal_requirement_snapshots(
                state,
                tuple(temporal_request.requirements),
            )
            if temporal_request is not None
            else ()
        )
        temporal_source_checkpoint = (
            _theory_drive._temporal_source_checkpoint(
                state, temporal_request, temporal_requirements
            )
            if temporal_request is not None
            else None
        )
        if temporal_request is not None and temporal_source_checkpoint is not None:
            _theory_drive._restore_temporal_source(
                state, temporal_request, temporal_source_checkpoint
            )
            rebased_source = _theory_drive._rebase_restored_theory_world(
                state,
                temporal_request,
                temporal_source_checkpoint,
            )
            if rebased_source is not None:
                yield PilotEvent(
                    "theory_world_rebased",
                    state.work.state.scan_id,
                    {
                        "source": temporal_request.source,
                        "rebased_source": rebased_source,
                        "reason": "retained temporal setup changed the restored World",
                    },
                )
                # Progress now names the executable World Compass was already
                # given. Re-read so the temporal request and Orientation share
                # that exact same boundary.
                continue
        snap = dict(state.work.state.tags)
        entry_execution = state.bootstrap_execution
        if (entry_execution is None or entry_execution.route_bound) and target_reached(
            snap, ctx.target.tag, ctx.target.value, ctx.target.predicate
        ):
            _theory_drive._run_optional_theory_hook(
                _theory_drive._record_optional_theory_proved, state
            )
            _promote_probationary_corrections(state)
            if state.steps:
                # The terminal let-run's span extends to the actual finish scan;
                # rewrite the last step (and its journey twin, the same object) so
                # both the clean path and the journey carry the true coast length.
                state.extend_last_step(state.work.state.scan_id)
            yield _finished_event(
                state,
                ctx,
                journal_channel_tags,
                journal_acc_names,
                reached=True,
                reason="target reached",
            )
            return

        # Compass owns the current-world read and returns one world-bound result.
        raw_world = OrientationWorld(
            world_key=(),
            snapshot=dict(state.work.state.tags),
            frame=None,
            state=state,
            context=ctx,
            key_config=state.key_config,
        )
        target = ctx.target
        constraints = NavigationConstraints(
            avoid_predicate=ctx.avoid_pred,
            active_requirements=tuple(state.active_requirements),
            theory_view=theory_view(state.theory_state),
            temporal_requirements=temporal_requirements,
            temporal_trigger_requirements=temporal_trigger_requirements,
            temporal_source_anchor=(
                (temporal_source_checkpoint.owner, temporal_source_checkpoint.key)
                if temporal_source_checkpoint is not None
                else None
            ),
        )
        result = ctx.compass.orient(raw_world, target, constraints)
        orientation_read = result.orientation
        if orientation_read is None:
            raise RuntimeError("Compass orientation omitted its current-world reading")
        orientation_world = orientation_read.world
        candidates = orientation_read.candidates
        frame = orientation_world.frame
        requirements_before_entry_bind = len(state.active_requirements)
        bound_entry = _bind_entry_execution_to_route(state, ctx, result, frame)
        if bound_entry is not None:
            yield PilotEvent(
                "entry_scan_observed",
                bound_entry.scan_after,
                {"execution": bound_entry.diagnostic_snapshot()},
            )
            for requirement in state.active_requirements[requirements_before_entry_bind:]:
                yield PilotEvent(
                    "requirement_activated",
                    requirement.deadline.scan_id,
                    {"requirement": requirement.diagnostic_snapshot()},
                )
            # Route binding adds target-relative theory knowledge and expires
            # this read. SETUP_FIRST restoration and all later work therefore
            # begin with a fresh Compass orientation.
            continue
        result, _recovery_program_step = (
            _recovery_continuation.preempt_recovery_action_with_program_coast(
                result,
                frame,
                state,
                ctx,
                target,
            )
        )
        controlling_setup_request = _theory_drive._setup_request_for_result(
            temporal_request, result
        )
        theory_source_checkpoint = (
            _CausalCheckpoint(
                key=frame.key,
                world=state.snapshot_world(),
                objective=result.objective,
                configured_inputs=ctx.configured_inputs | _configured_input_names(state.work),
            )
            if active_theory(state.theory_state) is not None and isinstance(result, Bearing)
            else None
        )
        last_frame = frame
        frontier = result.objective.frontier if isinstance(result, Bearing) else result.frontier
        last_frontier = frontier
        _prepare_oriented_result(state, result, orientation_world, frame)
        state.watch_tags.extend(sorted(frame.tree.pivot_tags() - set(state.watch_tags)))
        frame_lever_notes: dict[str, str] = {}
        for action in frame.raw_trace_action_details:
            if action.note:
                # Same physical lever may now retain several alternative
                # producer expectations. Diagnostics keep the established
                # first selected path rather than letting a later alternative
                # silently replace its note.
                frame_lever_notes.setdefault(action.tag, action.note)
        state.lever_notes.update(frame_lever_notes)
        for branch in frame.tree.ordered_crossing_branches():
            for action in branch.actions:
                if action.note:
                    state.lever_notes[action.tag] = action.note
        yield from _record_pending_landing(frame, state)
        yield PilotEvent(
            "iteration", state.work.state.scan_id, _iteration_payload(frame, state, ctx)
        )
        yield PilotEvent(
            "candidates_built",
            state.work.state.scan_id,
            _candidates_built_payload(candidates, state.lever_notes),
        )

        if isinstance(result, ComposeCorrection):
            if temporal_request is None:
                raise RuntimeError("Compass requested composition without a temporal need")
            composed = _theory_drive._compose_theory_correction(
                state,
                temporal_request,
                result,
            )
            yield PilotEvent(
                "theory_correction_composed",
                state.work.state.scan_id,
                {
                    "configuration": result.configuration.assignments,
                    "conditions": tuple(
                        _theory_requirement_snapshot(requirement).condition_identity
                        for requirement in composed.requirements
                    ),
                    "superseded_configuration_identities": (
                        composed.superseded_configuration_identities
                    ),
                    "research_finding_identity": composed.research_finding_identity,
                    "reason": result.rationale,
                },
            )
            # Composition changes the durable case but consumes no PLC scan.
            # Compass must read that refined theory before choosing a steer.
            continue

        if isinstance(result, NeedResearch):
            request = result.request
            finding = ConductivityResearchFinding(
                theory_id=request.theory_id,
                version_id=request.version_id,
                source=request.source,
                comparison_identity=request.comparison.identity,
                compared_attempt_ids=(
                    request.comparison.earlier_attempt_id,
                    request.comparison.later_attempt_id,
                ),
                displacement=request.displacement,
                enabling_reads=request.enabling_reads,
                requirement_drift_identities=tuple(
                    drift.identity for drift in request.comparison.requirement_drifts
                ),
            )
            _theory_drive._record_controlling_theory_fact(
                state,
                RecordConductivityResearch(finding),
            )
            yield PilotEvent(
                "conductivity_research_requested",
                state.work.state.scan_id,
                {
                    "displacement": request.displacement,
                    "enabling_reads": tuple(
                        {
                            "tag": read.tag,
                            "rung": read.rung,
                            "values": read.values,
                        }
                        for read in request.enabling_reads
                    ),
                    "requirement_drifts": tuple(
                        {
                            "earlier": drift.earlier.condition_identity,
                            "later": drift.later.condition_identity,
                        }
                        for drift in request.comparison.requirement_drifts
                    ),
                    "finding_identity": finding.identity,
                    "reason": request.reason,
                },
            )
            # Recording changes theory knowledge, not the executable World.
            # Discard this candidate read and let Compass reread that same
            # World with the exact research finding now visible.
            continue

        if isinstance(result, NeedIntrascanBoundaryRealization):
            if temporal_request is None:
                raise RuntimeError("Compass requested boundary realization without a temporal need")
            realization = research_retained_frontier_realization(
                result,
                state.work,
                state.pilot_rungs,
            )
            frontier_receipt = result.traceback_frontier
            finding = None
            if realization.witnessed:
                finding = IntrascanTracebackFinding(
                    theory_id=temporal_request.theory_id,
                    version_id=temporal_request.version_id,
                    source=temporal_request.source,
                    request_identity=frontier_receipt.request_identity,
                    hop_identity=frontier_receipt.hop_identity,
                    requirement_identities=frontier_receipt.requirement_identities,
                    witness=frontier_receipt.witness,
                    realization=realization,
                    parent_frontier_id=frontier_receipt.identity,
                    parent_producer_goal_id=result.producer_goal.identity,
                    parent_attempt_id=result.producer_attempt_id,
                )
                _theory_drive._record_controlling_theory_fact(
                    state,
                    RecordIntrascanTraceback(finding),
                )
            yield PilotEvent(
                "intrascan_boundary_realization_researched",
                state.work.state.scan_id,
                {
                    "frontier_identity": frontier_receipt.identity,
                    "producer_goal_identity": result.producer_goal.identity,
                    "producer_attempt_id": result.producer_attempt_id,
                    "boundary_realization": realization,
                    "finding_identity": finding.identity if finding is not None else None,
                    "reason": realization.detail,
                },
            )
            if finding is not None:
                continue
            terminal_reason = (
                "Working theory's accepted intrascan producer did not reproduce "
                "its retained consumer boundary"
            )
            diagnosis = Stuck(
                world_key=result.world_key,
                reason_code="intrascan_boundary_realization_pending",
                frontier=result.frontier,
                evidence=(frontier_receipt, realization),
                rationale=terminal_reason,
                orientation=result.orientation,
            )
            yield from _stopped_events(
                state,
                ctx,
                frame,
                terminal_reason,
                journal_channel_tags,
                journal_acc_names,
                candidate_count=len(candidates.options) if candidates is not None else 0,
                diagnosis=diagnosis,
            )
            return

        if isinstance(result, NeedIntrascanTraceback):
            if temporal_request is None:
                raise RuntimeError("Compass requested traceback without a temporal need")
            witness = research_intrascan_traceback(
                result.request,
                state.work,
                state.pilot_rungs,
            )
            realization = research_intrascan_boundary_realization(
                result.request,
                witness,
                state.work,
                state.pilot_rungs,
            )
            finding = None
            traceback_frontier = None
            if realization.witnessed:
                finding = IntrascanTracebackFinding(
                    theory_id=temporal_request.theory_id,
                    version_id=temporal_request.version_id,
                    source=temporal_request.source,
                    request_identity=result.request.identity,
                    hop_identity=result.request.hop_identity,
                    requirement_identities=tuple(
                        _theory_requirement_snapshot(requirement).semantic_identity
                        for requirement in result.request.requirements
                    ),
                    witness=witness,
                    realization=realization,
                    parent_frontier_id=result.request.parent_frontier_id,
                    parent_producer_goal_id=result.request.parent_producer_goal_id,
                    parent_attempt_id=result.request.parent_attempt_id,
                )
                _theory_drive._record_controlling_theory_fact(
                    state,
                    RecordIntrascanTraceback(finding),
                )
            elif (
                witness.applied_exactly_once
                and witness.traceback_step is None
                and not witness.blocked_edges
                and result.request.consumer_assignments
                and all(tag in ctx.steerable for tag, _value in result.request.consumer_assignments)
            ):
                finding = IntrascanOrdinarySteerFinding(
                    theory_id=temporal_request.theory_id,
                    version_id=temporal_request.version_id,
                    source=temporal_request.source,
                    request_identity=result.request.identity,
                    hop_identity=result.request.hop_identity,
                    requirement_identities=tuple(
                        _theory_requirement_snapshot(requirement).semantic_identity
                        for requirement in result.request.requirements
                    ),
                    witness=witness,
                    consumer_assignments=result.request.consumer_assignments,
                    parent_frontier_id=result.request.parent_frontier_id,
                    parent_producer_goal_id=result.request.parent_producer_goal_id,
                    parent_attempt_id=result.request.parent_attempt_id,
                )
                _theory_drive._record_controlling_theory_fact(
                    state,
                    RecordIntrascanTraceback(finding),
                )
            elif witness.traceback_step is not None and realization.unresolved_producer_goals:
                traceback_frontier = IntrascanTracebackFrontier(
                    theory_id=temporal_request.theory_id,
                    version_id=temporal_request.version_id,
                    source=temporal_request.source,
                    request_identity=result.request.identity,
                    hop_identity=result.request.hop_identity,
                    requirement_identities=tuple(
                        _theory_requirement_snapshot(requirement).semantic_identity
                        for requirement in result.request.requirements
                    ),
                    witness=witness,
                    producer_goals=realization.unresolved_producer_goals,
                    consumer_assignments=realization.consumer_assignments,
                    parent_frontier_id=result.request.parent_frontier_id,
                    parent_producer_goal_id=result.request.parent_producer_goal_id,
                    parent_attempt_id=result.request.parent_attempt_id,
                )
                _theory_drive._record_controlling_theory_fact(
                    state,
                    RecordIntrascanTracebackFrontier(traceback_frontier),
                )
            yield PilotEvent(
                "intrascan_traceback_researched",
                state.work.state.scan_id,
                {
                    "request_identity": witness.request_identity,
                    "source_scan": witness.source_scan,
                    "assertion_scan": witness.assertion_scan,
                    "applied_exactly_once": witness.applied_exactly_once,
                    "application_values": witness.application_values,
                    "downstream_writes": witness.downstream_writes,
                    "exit_changes": witness.exit_changes,
                    "traceback_step": witness.traceback_step,
                    "blocked_edges": witness.blocked_edges,
                    "consumer_horizon_read": witness.consumer_horizon_read,
                    "consumer_stop_reached": (witness.consumer_stop_reached),
                    "boundary_realization": realization,
                    "finding_identity": finding.identity if finding is not None else None,
                    "frontier_identity": (
                        traceback_frontier.identity if traceback_frontier is not None else None
                    ),
                    "witness_detail": witness.detail,
                    "reason": realization.detail,
                },
            )
            if finding is not None or traceback_frontier is not None:
                # The finding changes knowledge, not the executable World.
                # Compass must reread before selecting a stage scan or tracing
                # one open producer goal from this same physical boundary.
                continue
            terminal_reason = (
                "Working theory derived one intrascan traceback hop, but no ordinary "
                "scan-boundary realization was proved"
                if witness.traceback_step is not None
                else "Working theory found an exact consumer edge which must be rearmed"
                if witness.blocked_edges
                else "Working theory proved an occurrence-local counterfactual handoff; "
                "no useful downstream program write was identified"
                if witness.applied_exactly_once
                else "Working theory could not relocate its exact intrascan consumer"
            )
            diagnosis = Stuck(
                world_key=result.world_key,
                reason_code="intrascan_traceback_pending",
                frontier=result.frontier,
                evidence=(witness,),
                rationale=terminal_reason,
                orientation=result.orientation,
            )
            yield from _stopped_events(
                state,
                ctx,
                frame,
                terminal_reason,
                journal_channel_tags,
                journal_acc_names,
                candidate_count=len(candidates.options) if candidates is not None else 0,
                diagnosis=diagnosis,
            )
            return

        if isinstance(result, NeedProbe):
            observations = probe_live_guard_frontiers(frame, state, ctx)
            ctx.compass, changed = ctx.compass.apply(observations)
            ctx.compass, _ = ctx.compass.apply((ProbeExhaustedObservation(frame.key),))
            yield PilotEvent(
                "skiff",
                state.work.state.scan_id,
                {
                    "observations": len(observations),
                    "reason": result.request.reason,
                    "changed": changed,
                },
            )
            # The bounded probe-count receipt always changes navigation
            # knowledge, even when no new live-guard observation was found.
            # Re-read until Orientation returns the complete-world Stuck.
            continue

        if isinstance(result, Stuck):
            mandatory_blocker = _requirement_repair.mandatory_guard_blocker(
                tuple(state.active_requirements),
                state.work.state.tags,
            )
            terminal_reason = (
                _requirement_repair.mandatory_guard_decline_reason(
                    mandatory_blocker,
                    state.work.state.tags,
                    ctx.target,
                )
                if mandatory_blocker is not None
                else _with_avoid_reason(
                    _stopped_reason(),
                    state,
                    ctx,
                    frame,
                )
                + _frontier_clause(frontier, frame.snap)
            )
            _theory_drive._run_optional_theory_hook(
                _theory_drive._record_optional_theory_abandoned, state, TheoryTermination.STUCK
            )
            yield from _stopped_events(
                state,
                ctx,
                frame,
                terminal_reason,
                journal_channel_tags,
                journal_acc_names,
                candidate_count=len(candidates.options) if candidates is not None else 0,
                diagnosis=result,
            )
            return

        assert isinstance(result, Bearing)
        act = result.act
        try_event = _act_event(
            "try",
            act,
            state.work.state.scan_id,
            rationale=result.rationale,
            prerequisites=result.prerequisites,
            target_tag=ctx.target.tag,
        )
        if try_event is not None:
            yield try_event

        seen_keys_before_commit = frozenset(state.seen_keys)
        requirements_before = len(state.active_requirements)
        receipts_before = len(state.expectation_receipts)
        failures_before = len(state.failed_effect_receipts)
        controlling_source_world = (
            state.snapshot_world() if controlling_setup_request is not None else None
        )
        prerequisite_source_world = (
            state.snapshot_world()
            if controlling_setup_request is None and result.prerequisites
            else None
        )
        transition = _transition_once(
            state,
            ctx,
            target,
            constraints,
            oriented=result,
            derivation_checkpoint=theory_source_checkpoint,
            defer_adoption=controlling_setup_request is not None,
            record_rejection=controlling_setup_request is None,
        )
        attempt = transition.attempt
        assert attempt is not None
        controlled_setup_attempt = None
        if controlling_setup_request is not None:
            assert theory_source_checkpoint is not None
            controlled_setup_attempt = _theory_drive._record_controlled_setup_attempt(
                state,
                controlling_setup_request,
                result,
                attempt,
                theory_source_checkpoint,
            )
        if controlled_setup_attempt is not None and attempt.trial is not None:
            transition = _adopt_deferred_transition(transition, state, ctx)
        if attempt.trial is None:
            if controlled_setup_attempt is not None:
                if _theory_drive._records_controlling_need(transition.theory_transition):
                    assert transition.theory_transition is not None
                    _theory_drive._record_working_theory_transition(
                        state,
                        transition.theory_transition,
                        remaining_budget=state.remaining_search_scans(ctx.max_scans),
                    )
                else:
                    theory = active_theory(state.theory_state)
                    if theory is None:
                        raise ValueError("rejected temporal attempt lost its theory")
                    rejected_attempt_id = controlled_setup_attempt.attempt_id
                    _theory_drive._record_controlling_theory_fact(
                        state,
                        AbandonTheory(
                            theory_id=theory.theory_id,
                            version_id=theory.current_version_id,
                            termination=TheoryTermination.BUDGET,
                            abandonment_identity=(
                                "working-theory-temporal-rejected",
                                rejected_attempt_id,
                            ),
                        ),
                    )
            elif _theory_drive._records_controlling_need(transition.theory_transition):
                _theory_drive._record_working_theory_transition(
                    state,
                    transition.theory_transition,
                    remaining_budget=state.remaining_search_scans(ctx.max_scans),
                )
            else:
                _theory_drive._run_optional_theory_hook(
                    _theory_drive._record_working_theory_transition,
                    state,
                    transition.theory_transition,
                    remaining_budget=state.remaining_search_scans(ctx.max_scans),
                )
        for requirement in state.active_requirements[requirements_before:]:
            yield PilotEvent(
                "requirement_activated",
                requirement.deadline.scan_id,
                {"requirement": requirement.diagnostic_snapshot()},
            )
        for receipt in state.expectation_receipts[receipts_before:]:
            yield PilotEvent(
                "expectation_committed",
                state.work.state.scan_id,
                {"receipt": receipt.diagnostic_snapshot()},
            )
        for receipt in state.failed_effect_receipts[failures_before:]:
            yield PilotEvent(
                "failed_effect_explained",
                state.work.state.scan_id,
                {"receipt": receipt.diagnostic_snapshot()},
            )

        if attempt.trial is None:
            rejected_event = _act_event(
                "rejected",
                act,
                state.work.state.scan_id,
                attempt=attempt,
            )
            assert rejected_event is not None
            try:
                yield rejected_event
            finally:
                _release_attempt_projections(attempt)
            if isinstance(act, (ObserveScan, ProgramScan, IntrascanPulse)):
                # Boundary zero has exactly one legal act: execute the first
                # program scan so Compass has an observed world to read. If
                # that act is gate-rejected, retrying cannot change either the
                # source World or the observation and therefore loops without
                # consuming scan budget. There is no alternative bearing yet.
                yield from _stopped_events(
                    state,
                    ctx,
                    frame,
                    _with_avoid_reason(
                        (
                            "The entry observation was rejected"
                            if isinstance(act, ObserveScan)
                            else "The exact intrascan scan was rejected"
                        ),
                        state,
                        ctx,
                        frame,
                    ),
                    journal_channel_tags,
                    journal_acc_names,
                    candidate_count=1,
                )
                return
            if prerequisite_source_world is not None:
                # A prerequisite is part of this exact Bearing attempt.  If
                # the act is rejected, its unaccepted overlay must roll back
                # with it; otherwise a later Compass read inherits temporary
                # logic that has no successful World-change receipt.
                state.load_world(prerequisite_source_world)
            if controlled_setup_attempt is not None:
                assert controlling_source_world is not None
                state.load_world(controlling_source_world)
                if _theory_drive._records_controlling_need(transition.theory_transition):
                    state.pending_departure = None
                    continue
                yield from _stopped_events(
                    state,
                    ctx,
                    frame,
                    "working theory's exact temporal Bearing was rejected",
                    journal_channel_tags,
                    journal_acc_names,
                    candidate_count=1,
                )
                return
            continue

        trial = transition.trial
        assert trial is not None
        executed_attempt = attempt.executed_attempt
        assert executed_attempt is not None
        accepted_event = _act_event(
            "accepted",
            act,
            trial.attempt.pulse.fork.state.scan_id,
            trial=trial,
            frame=frame,
            state=state,
            seen_keys=seen_keys_before_commit,
        )
        assert accepted_event is not None
        try:
            yield accepted_event
            requirements_before_monitor = _theory_drive._requirement_identities(state)
            if controlled_setup_attempt is None:
                settled_target_failure = _derive_settled_target_requirements(
                    trial,
                    state,
                    ctx,
                    transition.adoption_checkpoint,
                )
                if settled_target_failure is None:
                    yield from _monitor_committed_trial(
                        trial,
                        frame,
                        state,
                        ctx,
                        continuation_hop=(transition.continuation_hop),
                    )
                # An exact zero-net target occurrence is stronger than the
                # outer trend monitor's later macro-state reading.  Its failed
                # requirement below owns the rollback and next WorkingTheory
                # question; investigating the same departure generically can
                # coast far past the occurrence and lose the scan-local cause.
                for requirement in state.active_requirements:
                    if requirement.identity not in requirements_before_monitor:
                        yield PilotEvent(
                            "requirement_activated",
                            requirement.deadline.scan_id,
                            {"requirement": requirement.diagnostic_snapshot()},
                        )
            _theory_drive._advance_retained_productive_tip(
                state,
                ctx,
                trial,
                transition.theory_transition,
                prior_requirement_identities=requirements_before_monitor,
            )
            theory_transition, absorbed_requirement_ids = _theory_transition_after_monitor(
                state,
                transition.theory_transition,
                prior_requirement_identities=requirements_before_monitor,
                assertion_scan=_attempt_productive_scan(executed_attempt),
                trial=trial,
                source_checkpoint=transition.adoption_checkpoint,
            )
            successor_need = _theory_drive._records_controlling_need(theory_transition)
            if successor_need:
                # Keep the monitor's exact rollback world. The next fresh
                # Compass read re-executes this scan with the newly learned
                # condition present, following its intrascan conductivity
                # instead of beginning after a regressive settled landing.
                state.pending_departure = None
            elif (
                active_theory(state.theory_state) is not None
                and transition.adoption_checkpoint is not None
                and state.work.state.scan_id
                < transition.adoption_checkpoint.world.work.state.scan_id
                and tuple(state.pilot_rungs)
                == tuple(transition.adoption_checkpoint.world.pilot_rungs)
            ):
                # Ordinary progress policy rejected the look-ahead. Within an
                # active theory the scan immediately before that failed edge is
                # the technician's working tip; retain it and let fresh Compass
                # readers choose a different next edge instead of falling back
                # to an older global trend checkpoint.  A monitor-installed or
                # revoked correction changes the executable overlay, however;
                # its rollback world is then authoritative.  Restoring the
                # pre-investigation adoption checkpoint would silently discard
                # that newly proved execution state while leaving its receipt
                # behind.
                state.load_world(transition.adoption_checkpoint.world)
                state.pending_departure = None
                if theory_transition is not None:
                    theory_transition = replace(
                        theory_transition,
                        disposition=TheoryAttemptDisposition.REJECTED_EMPIRICAL,
                        adopted_boundary=None,
                        evidence=(
                            *theory_transition.evidence,
                            (
                                "working-tip-lookahead-rejected",
                                _theory_boundary_from_checkpoint(transition.adoption_checkpoint),
                            ),
                        ),
                    )
            if controlled_setup_attempt is not None:
                _theory_drive._complete_controlled_setup(
                    state,
                    ctx,
                    controlled_setup_attempt,
                    successor_need=successor_need,
                )
                if successor_need:
                    assert theory_transition is not None
                    _theory_drive._record_working_theory_transition(
                        state,
                        theory_transition,
                        remaining_budget=state.remaining_search_scans(ctx.max_scans),
                    )
            else:
                if successor_need:
                    assert theory_transition is not None
                    _theory_drive._record_working_theory_transition(
                        state,
                        theory_transition,
                        remaining_budget=state.remaining_search_scans(ctx.max_scans),
                    )
                else:
                    if active_theory(state.theory_state) is not None:
                        _theory_drive._record_theory_transition(
                            state,
                            theory_transition,
                            remaining_budget=state.remaining_search_scans(ctx.max_scans),
                            record_fact=_theory_drive._record_controlling_theory_fact,
                        )
                        _theory_drive._record_theory_execution_advance(
                            state,
                            ctx,
                            trial,
                            theory_transition,
                        )
                        _theory_drive._complete_intrascan_consumer(
                            state,
                            temporal_request,
                            trial,
                            theory_transition,
                        )
                    else:
                        _theory_drive._run_optional_theory_hook(
                            _theory_drive._record_working_theory_transition,
                            state,
                            theory_transition,
                            remaining_budget=state.remaining_search_scans(ctx.max_scans),
                        )
                _theory_drive._run_optional_theory_hook(
                    _theory_drive._record_optional_requirement_delta,
                    state,
                    requirements_before_monitor | absorbed_requirement_ids,
                    identity=(
                        "post-commit",
                        transition.theory_transition.identity
                        if transition.theory_transition is not None
                        else (),
                    ),
                )
        except Exception:
            if controlling_source_world is not None:
                state.load_world(controlling_source_world)
            raise
        finally:
            _release_attempt_projections(attempt)
        continue

    # ── This invocation spent its relative search budget ──
    # Unproductive scans that drain the budget are a stall, not a wrap-up:
    # route the terminal through a fresh frame so the reason names the
    # outstanding frontier, and revert to the last checkpoint like the stuck
    # exits do ("How we fail" #1 — every stop points at a named leaf).
    snap = dict(state.work.state.tags)
    reached = target_reached(snap, ctx.target.tag, ctx.target.value, ctx.target.predicate)
    if not reached:
        frame = last_frame
        reason = _with_avoid_reason(
            f"budget exhausted ({state.search_scans} scans searched + {state.dwell_scans} waited)",
            state,
            ctx,
            frame,
        ) + _frontier_clause(last_frontier, frame.snap if frame is not None else None)
        _theory_drive._run_optional_theory_hook(
            _theory_drive._record_optional_theory_abandoned, state, TheoryTermination.BUDGET
        )
        yield from _stopped_events(
            state,
            ctx,
            frame,
            reason,
            journal_channel_tags,
            journal_acc_names,
            candidate_count=0,
        )
        return
    _theory_drive._run_optional_theory_hook(_theory_drive._record_optional_theory_proved, state)
    yield _finished_event(
        state,
        ctx,
        journal_channel_tags,
        journal_acc_names,
        reached=True,
        reason="target reached",
    )


def _pilot_loop(
    plc: PLC,
    ctx: _PilotContext,
    *,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> _DriveOutcome:
    """Run the PILOT loop and assemble its terminal event as a named result.

    ``journey`` is the full attempt log, including reverted rounds. ``reason``
    is the terminal diagnostic on failure and ``None`` when reached.
    ``knowledge`` carries the recording fields that survive a world revert.
    """
    final: PilotEvent | None = None
    for event in _pilot_loop_events(plc, ctx):
        if on_event is not None:
            on_event(event)
        if event.kind == "finished":
            final = event

    if final is None:
        return _DriveOutcome(
            reached=False,
            work=plc,
            journal=(),
            journey=(),
            reason=None,
            knowledge={},
            root_route=None,
        )
    reached = bool(final.data["reached"])
    return _DriveOutcome(
        reached=reached,
        work=final.data["work"],
        journal=tuple(final.data.get("plan_journal", ())),
        journey=tuple(final.data.get("journey", ())),
        reason=None if reached else final.data.get("reason"),
        knowledge=dict(final.data.get("knowledge", {})),
        root_route=final.data.get("root_route"),
    )


# ---------------------------------------------------------------------------
# Failure diagnostics
# ---------------------------------------------------------------------------


def _harness_couplings(plc: PLC) -> tuple[tuple[str, str], ...]:
    """The ``(en, fb)`` pairs the Harness still synthesizes on *plc*, for the
    linked-feedback diagnostic.  Empty when there is no harness (no couplings)
    or every coupling was ``unlink``-ed away."""
    harness = getattr(plc, "_harness", None)
    if harness is None:
        return ()
    return tuple((c.en_name, c.fb_name) for c in harness.couplings())


def _linked_feedback_block(
    target_tag: str,
    target_value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    couplings: tuple[tuple[str, str], ...],
) -> str | None:
    """Honest diagnostic for an unreachable target gated by a harness link.

    When the target's backward-trace route contains both a synthesized feedback
    tag ``fb`` *and* its driver ``en`` (the ``link=`` source), the Harness holds
    ``fb`` lockstep with ``en`` — so the moment the route drives ``en`` to its
    active value, the link drives ``fb`` to the opposite of what the route needs
    (valve open ⇒ flow sensor reads active, defeating the "no flow" watchdog).
    PILOT may not steer ``fb`` (the Harness owns it), so the target is
    unreachable until the link is defeated.  Returns a message naming the
    offending link(s) and the ``unlink=`` override, or ``None`` if no link gates
    the route (then the caller falls back to the generic budget reason).
    """
    if not couplings:
        return None
    try:
        tree = trace_back(target_tag, target_value, snapshot, pdg, program, steerable)
    except UnsupportedConstruct:
        raise
    except Exception:  # noqa: BLE001 — diagnostic only; never mask the real failure
        return None
    route_tags = {n.tag for n in tree.iter_nodes()}
    blockers = [
        (en, fb)
        for en, fb in couplings
        if fb in route_tags and en in route_tags and fb not in steerable
    ]
    if not blockers:
        return None
    links = ", ".join(f"{fb}<-{en}" for en, fb in blockers)
    names = ", ".join(repr(fb) for _en, fb in blockers)
    return (
        f"pilot: {target_tag}={target_value!r} is blocked by physical link(s) "
        f"{links}; the harness holds the sensor lockstep with its driver, so it "
        f"cannot rest at the value this route needs. Retry with unlink=[{names}] "
        f"to model a dead sensor (fault injection)."
    )


def _target_is_value_route(target_predicate: Any) -> bool:
    """Does this target get route enumeration?

    Any concrete equality target — ``Bool == True``, ``Bool == False``, or a
    word ``tag == value`` — is a frozen value the route machinery can enumerate
    writers/OR-arms for (``_can_produce`` against that value).  A live relational
    predicate (``State > 5``) is *not*: its goal is the relation, not a frozen
    value, so ``target_value`` is only a display representative and there is no
    producible-value writer set to route over.  Those targets flow unlocked and
    are honestly reported without a ``RouteTaken``.
    """
    return target_predicate is None


def _route_name(route: TraceChoice) -> str:
    """Human name for a route."""
    if route.route_condition is not None:
        tag, value = route.route_condition
        return tag if value is True else f"{tag}=={value!r}"
    return route.label


def _build_route_taken(
    default: TraceChoice,
    survivors: tuple[TraceChoice, ...],
    steerable: frozenset[str],
) -> RouteTaken:
    """Describe the chosen *default* route plus the routes not taken.

    Models the fork as one pivot whose ``alternatives`` are the other surviving
    routes. ``salient`` is True when any route in the fork is
    gated by a non-steerable discriminator (an internal coil/state the engineer
    commits to) — the trivial all-input fork (``Or(Auto, Manual)``) stays
    non-salient and hidden from the headline.
    """
    others = tuple(ch for ch in survivors if ch.id != default.id)
    alternatives = tuple(RouteAlt(label=_route_name(ch)) for ch in others)
    conditions = [default.route_condition, *(ch.route_condition for ch in others)]
    salient = any(
        condition is not None and condition[0] not in steerable for condition in conditions
    )
    dtag, dvalue = (
        default.route_condition if default.route_condition is not None else (default.label, True)
    )
    pivot = RoutePivot(
        tag=dtag,
        value=dvalue,
        label=_route_name(default),
        kind="writer" if default.writer_locks else "or-arm",
        avoid_hint=default.route_condition,
        alternatives=alternatives,
        salient=salient,
    )
    return RouteTaken(
        label=_route_name(default),
        pivots=(pivot,),
        dominant=len(survivors) <= 1,
    )


def _report_selected_route(
    prepared: RouteTaken | None,
    selected: TraceChoice | None,
) -> RouteTaken | None:
    """Make the public route receipt name the route that actually finished.

    ``prepared`` describes the initially preferred fork so the engineer can see
    its alternatives before execution. If the route that ultimately reaches the
    target differs, rotate the same root pivot around that result. This is
    reporting only; no alternative list feeds back into navigation.
    """

    if prepared is None or selected is None or not prepared.pivots:
        return prepared
    selected_name = _route_name(selected)
    pivot = prepared.pivots[0]
    if pivot.label == selected_name:
        return prepared

    alternatives = [
        RouteAlt(label=pivot.label),
        *(alt for alt in pivot.alternatives if alt.label != selected_name),
    ]
    selected_condition = selected.route_condition
    selected_tag, selected_value = (
        selected_condition if selected_condition is not None else (selected.label, True)
    )
    return RouteTaken(
        label=selected_name,
        pivots=(
            RoutePivot(
                tag=selected_tag,
                value=selected_value,
                label=selected_name,
                kind="writer" if selected.writer_locks else "or-arm",
                avoid_hint=selected_condition,
                alternatives=tuple(alternatives),
                salient=pivot.salient,
            ),
        ),
        dominant=prepared.dominant,
    )


def _prepare_route(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    *,
    target_predicate: Any = None,
    avoid_pred: Any = None,
) -> RouteTaken | None:
    """Describe the preferred current-world route.

    Works for any concrete equality target — ``Bool == True``, ``Bool == False``,
    or a word ``tag == value``; a live relational predicate gets no route (see
    :func:`_target_is_value_route`).  ``how()`` never reports ambiguous: it
    enumerates the routes, prunes any that ``avoid=`` forbids, ranks the
    cheapest survivor (gate-eligible routes preferred, trace score next, rung
    order breaking ties), and records the alternatives on the returned
    :class:`RouteTaken`. Execution remains unlocked so every current-world read
    can choose any admissible root route.
    """
    snapshot = dict(plc.state.tags)
    if not (
        _target_is_value_route(target_predicate)
        and not _values_match(snapshot.get(target_tag), target_value)
    ):
        return None
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, program)
    choices, traced = rank_trace_choices(
        target_tag,
        target_value,
        snapshot,
        pdg,
        program,
        steerable,
        constraints=TraceReadConstraints(
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            avoid_pred=avoid_pred,
        ),
    )
    if not choices:
        return None
    if not traced:
        return None
    default = traced[0][0]
    survivors = tuple(choice for choice, _tree in traced)
    return _build_route_taken(default, survivors, steerable)


# ---------------------------------------------------------------------------
# Prover context — value domains + state key config
# ---------------------------------------------------------------------------


def _build_prover_context(
    program: Any,
    snapshot: dict[str, Any],
) -> _ProverContext:
    """Build prover context for value domains and state key projection.

    Fields are ``None`` on failure, so PILOT falls back to Bool-only probing,
    pivot-tag state keys, and local static evidence.
    """
    try:
        from dataclasses import replace as _replace

        from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel
        from pyrung.core.analysis.pilot.evidence import build_transition_evidence
        from pyrung.core.analysis.prove import _build_explore_context
        from pyrung.core.analysis.prove.passes import _OptConfig
        from pyrung.core.analysis.prove.results import Intractable

        opt = _replace(_OptConfig(), domains_only=True)
        compiled = _compile_kernel(program, blockless=True, proof_metadata=True)
        ctx = _build_explore_context(
            program,
            _opt_config=opt,
            compiled=compiled,
            initial_state=snapshot,
            allow_partial=True,
        )
        if isinstance(ctx, Intractable):
            return _ProverContext()
        nd = getattr(ctx, "nondeterministic_dims", None)
        stateful = getattr(ctx, "stateful_dims", None)
        evidence = build_transition_evidence(ctx)
        if nd:
            logger.info("pilot: nd_domains ready (%d dims)", len(nd))

        # Build state key config from ExploreContext.
        #
        # The pilot's macro-state key needs the *pre-elision* stateful set.
        # Elision drops scan-local registers because BFS enumerates inputs, so a
        # register that is a pure function of the inputs each scan is redundant in
        # the BFS key.  The pilot does the opposite — it *holds* inputs and
        # *observes* registers — so a scan-local channel (e.g. a config/mode
        # register decoded from a command) is the observable proxy for its own
        # steering; dropping it makes an establish move (change the channel) read
        # as SPIN.  Restore the elided tags, appended after the originals so the
        # done/threshold spec indices (which point into the original positions)
        # stay valid.
        stateful_names = ctx.stateful_names + tuple(
            sorted(set(ctx.elided_tags) - set(ctx.stateful_names))
        )
        done_specs = ctx.state_key_done_specs
        threshold_vector_specs = ctx.threshold_vector_specs

        acc_names: set[str] = set()
        for spec in done_specs:
            acc_names.add(spec.acc_name)
        for spec in threshold_vector_specs:
            acc_names.add(spec.acc_name)
        acc_indices = frozenset(i for i, name in enumerate(stateful_names) if name in acc_names)

        if not stateful_names:
            logger.info("pilot: stateful_names empty, falling back to pivot_tags")
            return _ProverContext(
                nd_domains=nd,
                stateful_domains=stateful,
                evidence=evidence,
            )

        key_config = _StateKeyConfig(
            stateful_names=stateful_names,
            done_specs=done_specs,
            threshold_vector_specs=threshold_vector_specs,
            acc_indices=acc_indices,
        )
        logger.info(
            "pilot: state key ready (%d dims, %d done, %d threshold, %d acc masked)",
            len(stateful_names),
            len(done_specs),
            len(threshold_vector_specs),
            len(acc_indices),
        )
        return _ProverContext(
            nd_domains=nd,
            stateful_domains=stateful,
            key_config=key_config,
            evidence=evidence,
        )
    except Exception:  # noqa: BLE001
        logger.debug("pilot: context build failed", exc_info=True)
        return _ProverContext()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _relational_target_atom(cond: Any) -> Any | None:
    """Build a simplified inequality ``Atom`` from a ``Compare*`` target, or None.

    Maps the ordered comparisons (``<``, ``<=``, ``>``, ``>=``) to their atom
    forms so a relational ``how(A > B)`` target rides the same trace machinery as
    a relational prerequisite (live predicate + reactive levers + coast).  The
    operand is the RHS tag name (a *live* threshold) or a literal.
    """
    from pyrung.core.analysis.simplified import Atom
    from pyrung.core.condition import CompareGe, CompareGt, CompareLe, CompareLt
    from pyrung.core.tag import Tag

    forms = {CompareLt: "lt", CompareLe: "le", CompareGt: "gt", CompareGe: "ge"}
    form = forms.get(type(cond))
    if form is None:
        return None
    tag = cond.tag
    tag_name = tag.name if isinstance(tag, Tag) else str(tag)
    operand = cond.value.name if isinstance(cond.value, Tag) else cond.value
    return Atom(
        tag=tag_name,
        form=form,
        operand=operand,
        operand_is_tag=isinstance(cond.value, Tag),
    )


def _parse_one(cond: Any) -> tuple[str, Any, Any]:
    """Extract ``(tag_name, target_value, predicate)`` from ONE condition.

    Accepts:
    - A Tag object (implies ``tag == True``)
    - A ``tag == value`` comparison (CompareEq)
    - A relational comparison ``A < / <= / > / >= B`` — returned as a live
      ``predicate`` Atom (the goal is the relation, not a frozen value); the
      ``(tag, value)`` pair is a representative for display/keying only.
    """
    from pyrung.core.condition import CompareEq
    from pyrung.core.tag import Tag

    if isinstance(cond, Tag):
        return cond.name, True, None

    if isinstance(cond, CompareEq):
        tag = cond.tag
        tag_name = tag.name if isinstance(tag, Tag) else str(tag)
        value = cond.value
        if isinstance(value, Tag):
            # The RHS is a Tag, not a concrete value — it would ride through the
            # trace as a TagExpr and crash the (unhashable) crossings machinery.
            # Require an explicit scalar so the target is a frozen value; for a
            # readonly constant (a named-array/enum element) point at its literal.
            hint = f" (e.g. {value.name}.default = {value.default!r})" if value.readonly else ""
            raise ValueError(
                f"pilot: how() target {tag_name} == {value.name!r} compares against a "
                f"Tag, not a concrete value. Pass the value it stands for{hint} or a "
                f"literal so the target is a frozen scalar."
            )
        return tag_name, value, None

    atom = _relational_target_atom(cond)
    if atom is not None:
        return atom.tag, atom.operand, atom

    raise ValueError(
        f"pilot: cannot extract a target from {cond!r}.  Pass a Tag (Bool target), "
        "tag == value, or a relational comparison (tag < / <= / > / >= value)."
    )


def _parse_targets(*conditions: Any) -> list[tuple[str, Any, Any]]:
    """Extract one ``(tag, value, predicate)`` per condition (multi-target goals)."""
    if not conditions:
        raise ValueError("pilot: how() requires at least one target condition")
    return [_parse_one(c) for c in conditions]


def _parse_target(*conditions: Any) -> tuple[str, Any, Any]:
    """Single-target parse — for the diagnostic/live entry points."""
    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")
    return _parse_one(conditions[0])


def _single_target_plan(
    setup: _DriveSetup,
    outcome: _DriveOutcome,
    target_tag: str,
    target_value: Any,
    route_taken: RouteTaken | None,
    *,
    include_journal: bool,
) -> Plan:
    """Assemble the common fork/live single-target result without policy drift."""

    linked_block = (
        None
        if outcome.reached
        else _linked_feedback_block(
            target_tag,
            target_value,
            setup.diag_snapshot,
            setup.pdg,
            setup.program,
            setup.steerable,
            _harness_couplings(setup.work),
        )
    )
    return Plan(
        reachable=outcome.reached,
        target_tag=target_tag,
        target_value=target_value,
        fork=outcome.work if outcome.reached else None,
        reason=linked_block or outcome.reason,
        status=(
            PlanStatus.REACHED
            if outcome.reached
            else PlanStatus.CANNOT_REACH
            if linked_block is not None
            else PlanStatus.STOPPED
        ),
        route=(
            _report_selected_route(route_taken, outcome.root_route) if outcome.reached else None
        ),
        journal=outcome.journal if include_journal else (),
        anchor_scan=setup.anchor_scan,
        journey=outcome.journey,
        hold_log=outcome.knowledge.get("hold_log", ()),
        lever_notes=outcome.knowledge.get("lever_notes", {}),
        avoid_names=outcome.knowledge.get("avoid_names", ()),
    )


def pilot_events(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events.

    ``unlink`` frees the named harness-feedback tags for fault injection (see
    :func:`pilot_how`). ``avoid_pred`` excludes routes, actions, and observed
    states the same way ``how(avoid=...)`` does.
    """
    target_tag, target_value, target_predicate = _parse_target(*conditions)
    setup = _prepare_drive(plc, unlink=unlink)
    ctx, _route_taken = _prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
    )
    yield from _pilot_loop_events(setup.work, ctx)


def pilot_how(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """PILOT on a fork — drive to the target and return the recording. Nothing changes.

    For a multi-route value target (``Bool == True/False`` or word
    ``tag == value``) PILOT starts with a deterministic preferred route and
    records the route that actually reached the goal on ``Plan.route``;
    ``avoid_pred`` excludes a reported route so PILOT can take another.

    ``unlink`` names harness-synthesized feedback tags to free for fault
    injection: the Harness stops driving them and they become steerable, so
    PILOT can reach faults that the intact physical link would otherwise hold
    out of reach (e.g. a dead flow sensor with the valve open).
    """
    targets = _parse_targets(*conditions)
    if len(targets) > 1:
        return _pilot_how_multi(
            plc,
            targets,
            max_scans=max_scans,
            avoid_pred=avoid_pred,
            unlink=unlink,
            on_event=on_event,
        )
    target_tag, target_value, target_predicate = targets[0]
    setup = _prepare_drive(plc, unlink=unlink)
    ctx, route_taken = _prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
    )
    outcome = _pilot_loop(
        setup.work,
        ctx,
        on_event=on_event,
    )

    return _single_target_plan(
        setup,
        outcome,
        target_tag,
        target_value,
        route_taken,
        include_journal=True,
    )


def _failed_multi_plan(
    label: str,
    targets: tuple[_ActionPair, ...],
    reason: str | None,
    status: PlanStatus,
    anchor_scan: int,
) -> Plan:
    """Build the one unreachable multi-target result shape."""

    return Plan(
        reachable=False,
        target_tag=label,
        target_value=True,
        targets=targets,
        reason=reason,
        status=status,
        anchor_scan=anchor_scan,
    )


def _pilot_how_multi(
    plc: PLC,
    targets: list[tuple[str, Any, Any]],
    *,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """Multi-target ``how(A, B, …)`` — reach one committed scan where every target holds.

    Static read only (``pilot/multitarget.py``): a sound mutual-exclusion prune +
    a clobberer-first order, then the single-target drive loop is run
    sequentially per target on ONE fork.  The fork's recording is the artifact —
    it replays to a state with every target true.  When the static read cannot
    prove ME it falls open to this drive; the final all-targets check is the
    honest oracle (the drive loop is execution truth, never a skiff probe).
    """
    from pyrung.core.analysis.pilot import multitarget as _mt  # noqa: PLC0415

    label = " & ".join(f"{tt}={tv!r}" for tt, tv, _ in targets)
    setup = _prepare_drive(plc, unlink=unlink)

    goal_pairs = tuple((tt, tv) for tt, tv, _ in targets)

    ok, reason, ordered = _mt.analyze(
        setup.diag_snapshot,
        setup.pdg,
        setup.program,
        setup.steerable,
        targets,
    )
    if not ok:
        return _failed_multi_plan(
            label,
            goal_pairs,
            reason,
            PlanStatus.CANNOT_REACH,
            setup.anchor_scan,
        )

    work = setup.work
    compass = setup.compass
    last_knowledge: dict[str, Any] = {}
    last_journey: tuple[Any, ...] = ()
    # The per-target drives run sequentially on ONE fork, so their journals are already
    # in scan order — concatenating them gives the whole passage, not the last leg only.
    journal_steps: list[Any] = []
    for t_tag, t_val, t_pred in ordered:
        if target_reached(dict(work.state.tags), t_tag, t_val, t_pred):
            continue  # already pulled in by an earlier target's drive
        # Same route discipline as single-target how(): infer every admissible
        # current-world route and let Orientation choose among them. ``avoid=``
        # is not tied to any one target, so it constrains every target uniformly.
        ctx, _route_taken = _prepare_target_context(
            setup,
            t_tag,
            t_val,
            t_pred,
            compass=compass,
            max_scans=max_scans,
            avoid_pred=avoid_pred,
            work=work,
        )
        outcome = _pilot_loop(work, ctx, on_event=on_event)
        work = outcome.work
        last_knowledge = outcome.knowledge
        compass = outcome.knowledge.get("compass", compass)
        last_journey = outcome.journey
        journal_steps.extend(outcome.journal)
        if not outcome.reached:
            detail = f"; {outcome.reason}" if outcome.reason else ""
            return _failed_multi_plan(
                label,
                goal_pairs,
                (
                    f"pilot: could not establish {t_tag}={t_val!r} while holding the "
                    f"other target(s){detail}"
                ),
                PlanStatus.STOPPED,
                setup.anchor_scan,
            )

    final = dict(work.state.tags)
    unmet = [(tt, tv) for tt, tv, tp in targets if not target_reached(final, tt, tv, tp)]
    if unmet:
        names = ", ".join(f"{tt}={tv!r}" for tt, tv in unmet)
        return _failed_multi_plan(
            label,
            goal_pairs,
            f"pilot: reached each target individually but {names} did not hold "
            "simultaneously (clobbered during co-establishment).",
            PlanStatus.STOPPED,
            setup.anchor_scan,
        )
    # recording: threaded from the LAST target's drive only (multi runs the loop
    # sequentially per target; the last drive's Knowledge is what survives on ``work``).
    return Plan(
        reachable=True,
        target_tag=label,
        target_value=True,
        targets=goal_pairs,
        fork=work,
        anchor_scan=setup.anchor_scan,
        journal=tuple(journal_steps),
        journey=last_journey,
        hold_log=last_knowledge.get("hold_log", ()),
        lever_notes=last_knowledge.get("lever_notes", {}),
        avoid_names=last_knowledge.get("avoid_names", ()),
    )
