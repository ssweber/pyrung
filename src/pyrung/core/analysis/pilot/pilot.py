"""Public entry points and outer orchestration for PILOT drives.

This module builds static/runtime context, prepares the user-selected trace
route, and dispatches ``Bearing | NeedProbe | RouteExhausted | Stuck`` results
from ``Compass``.
It invokes execution, applies observations, commits eligible forks, delegates
post-commit recovery, and converts the event stream into public results.  It
does not synthesize a navigation decision.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

from pyrung.core.analysis.graph import (
    Plan,
    PlanStatus,
    PlanStep,
    RouteAlt,
    RoutePivot,
    RouteTaken,
)
from pyrung.core.analysis.pilot._ops import (
    _apply_pulse,
    _pilot_world_key,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.advance import iter_advance_owners
from pyrung.core.analysis.pilot.charts import (
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.compass import (
    ActionNogoodObservation,
    CoastObservation,
    Compass,
    NavigationCatalog,
    ProbeExhaustedObservation,
)
from pyrung.core.analysis.pilot.gauge import build_gauge
from pyrung.core.analysis.pilot.navigation import (
    Bearing,
    BearingObjective,
    Coast,
    Dwell,
    NavigationConstraints,
    NeedProbe,
    OrientationWorld,
    Pulse,
    RouteExhausted,
    Stuck,
    TargetSpec,
    act_identity,
)
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.progress import (
    _anchor_bearing_receipt,
    _anchor_frame_receipt,
    _install_confirmed_correction,
    _monitor_trend,
    _promote_probationary_corrections,
    _record_pending_landing,
)
from pyrung.core.analysis.pilot.recording import (
    _accepted_payload,
    _build_plan_journal,
    _candidate_payload,
    _candidates_built_payload,
    _frontier_clause,
    _iteration_payload,
    _knowledge_payload,
    _zoom_accepted_payload,
)
from pyrung.core.analysis.pilot.skiff import probe_live_guard_frontiers
from pyrung.core.analysis.pilot.steer import execute
from pyrung.core.analysis.pilot.trace import (
    DomainPrior,
    TraceChoice,
    _all_nodes,
    _route_forced_names,
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    enumerate_trace_choices,
    frontier_pairs,
    rank_trace_choices,
    target_reached,
    trace_back,
)
from pyrung.core.analysis.pilot.types import (
    PilotEvent,
    _ActionPair,
    _Checkpoint,
    _CommittedAct,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _Step,
    _StepContext,
    _TrialResult,
    _World,
)
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.analysis.steerable import compute_clear_only, compute_steerable

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.charts import StaticTransitionGraph
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


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
    key_config: _StateKeyConfig | None
    evidence: TransitionEvidence | None
    compass: Compass
    opaque_loop: frozenset[str]
    live: bool


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
class _ProverContext:
    """Best-effort static evidence shared by drive setup and target context."""

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    key_config: _StateKeyConfig | None = None
    evidence: TransitionEvidence | None = None


# ---------------------------------------------------------------------------
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


def _commit_step(
    work: PLC,
    fork: PLC,
    inputs: dict[str, Any],
    scan_before: int,
    resting: dict[str, Any],
    edge_tags: set[str],
    live: bool,
) -> tuple[PLC, tuple[_Step, ...]]:
    """Record a step (or release+pulse pair) and swap the work fork.

    ``inputs`` is the full applied set (``trial.applied``), not the narrow
    ``trial.candidate``.  A ``rise()``/``fall()`` gate needs an edge — a transition
    — but a recorded ``_Step`` holds its ``inputs`` constant across the step's
    scans and the patch persists into the next step, so the naive replay
    (``patch(inputs); step``) cannot recreate the transition once the edge is
    already at the pulsed level (the consecutive-command case).  PILOT's live
    pulse drops the edge to resting for one scan before raising it
    (``_apply_actions``); mirror that here by recording an explicit 1-scan release
    step whenever the inputs drive an edge tag *off* resting, so the replay
    reproduces the same edge.
    """
    edge_release = {
        t: resting.get(t, False)
        for t in inputs
        if t in edge_tags and not _values_match(inputs[t], resting.get(t, False))
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
    if live:
        _apply_pulse(work, list(inputs.items()), resting, edge_tags)
        return work, steps
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
    evidence: TransitionEvidence | None,
    key_config: _StateKeyConfig | None,
    influence: Compass | None,
    opaque_loop: frozenset[str],
    route: TraceChoice | None,
    blocked_route_actions: frozenset[_ActionPair],
    max_scans: int,
    live: bool,
    avoid_pred: Any = None,
    via_pred: Any = None,
    target_predicate: Any = None,
) -> _PilotContext:
    pipeline_roles = _infer_pipeline_roles_for_context(
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )
    pipeline_internal_tags = frozenset(
        tag for role in pipeline_roles for tag in role.trace_internal_tags
    )
    prior_compass = influence or Compass()
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
        ),
        knowledge=prior_compass.knowledge,
    )
    # Domain prior for trace's inequality resolution: nd_domains (free-input
    # value spaces) + affine func-deps (derived-tag → steerable source).  Both
    # come from the prover ExploreContext that already built nd_domains and
    # evidence; bundled here so a single handle threads through trace_back.
    domain_prior = DomainPrior(
        nd_domains=nd_domains,
        func_deps=evidence.affine_projections() if evidence is not None else None,
    )
    # Clear-only (ack-cleared momentary) command tags: a subset of ``steerable``
    # kept off prerequisite holds and off preferred init/reset writer selection.
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, program)
    return _PilotContext(
        target_tag=target_tag,
        target_value=target_value,
        target_predicate=target_predicate,
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
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        live=live,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )


def _prepare_drive(
    plc: PLC,
    *,
    live: bool,
    unlink: list[str] | None,
) -> _DriveSetup:
    """Build the shared program/runtime analysis for one public drive."""

    from pyrung.core.analysis.pdg import build_program_graph

    work = plc if live else plc.fork(history_budget=math.inf)
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
        key_config=prover.key_config,
        evidence=prover.evidence,
        compass=Compass(NavigationCatalog(slices=tuple(opaque_slices))),
        opaque_loop=detect_opaque_loop(pdg, program),
        live=live,
    )


def _prepare_target_context(
    setup: _DriveSetup,
    target_tag: str,
    target_value: Any,
    target_predicate: Any,
    *,
    max_scans: int,
    avoid_pred: Any,
    via_pred: Any,
    influence: Compass | None = None,
    work: PLC | None = None,
) -> tuple[_PilotContext, RouteTaken | None]:
    """Bind one target and any explicit user route lock to a prepared drive."""

    target_work = setup.work if work is None else work
    route, blocked_actions, route_taken = _prepare_route(
        target_work,
        target_tag,
        target_value,
        setup.pdg,
        setup.program,
        setup.steerable,
        setup.opaque_loop,
        target_predicate=target_predicate,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
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
        evidence=setup.evidence,
        key_config=setup.key_config,
        influence=influence or setup.compass,
        opaque_loop=setup.opaque_loop,
        route=route,
        blocked_route_actions=blocked_actions,
        max_scans=max_scans,
        live=setup.live,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        target_predicate=target_predicate,
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
    from pyrung.core.analysis.pilot.charts import build_static_transition_graphs

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
        frontier = fr[0][0] if fr else ctx.target_tag
    else:
        frontier = ctx.target_tag
    return (
        f"{base}: avoid excludes {', '.join(names)} (frontier {frontier}, target {ctx.target_tag})"
    )


def _stopped_reason(reason_code: str) -> str:
    """Translate internal orientation taxonomy into an honest public stop."""
    if reason_code == "all_rejected":
        return "Every available action failed its trial"
    return "No safe next action was found"


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
        _target_is_value_route(ctx.target_predicate)
        and not _values_match(snap.get(ctx.target_tag), ctx.target_value)
    ):
        return ()
    choices = enumerate_trace_choices(
        ctx.target_tag,
        ctx.target_value,
        snap,
        ctx.pdg,
        ctx.program,
        steerable=ctx.steerable,
        clear_only=getattr(ctx, "clear_only", frozenset()),
    )
    names: set[str] = set()
    survivor = False
    forced_any = False
    for ch in choices:
        tree = trace_back(
            ctx.target_tag,
            ctx.target_value,
            snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            clear_only=getattr(ctx, "clear_only", frozenset()),
            opaque_loop=ctx.opaque_loop,
            route=ch,
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


def _diagnose_stuck(
    frame: _IterationFrame,
    candidates: Any,
    state: _PilotState,
    ctx: _PilotContext,
) -> str:
    if candidates.stuck_reason is not None:
        base = candidates.stuck_reason
    elif not candidates.candidates:
        base = "no_candidates"
    else:
        base = "all_rejected"
    return _with_avoid_reason(base, state, ctx, frame)


def _record_attempt(
    attempt: Any,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    objective: BearingObjective,
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


def _step_context(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
) -> _StepContext:
    """Build the context owned by one committed operation."""
    is_coast = trial.motion.is_coast

    frontier_tags: tuple[str, ...] = ()
    control_rungs: tuple[Any, ...] = ()

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
        control_rungs = tuple(state.rungs)

    return _StepContext(
        motion=trial.motion,
        candidate=dict(trial.candidate),
        frontier_tags=frontier_tags,
        control_rungs=control_rungs,
        channel_tag=trial.zoom_channel_tag,
        before_snap=dict(trial.before_snap),
        after_snap=dict(trial.fork_snap),
        channel_target=trial.zoom_target_value,
        timeline=trial.timeline,
        accelerators=tuple(getattr(trial.coast_receipt, "advances", ())),
    )


def _commit_and_monitor(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> Iterator[PilotEvent]:
    """Commit a gate-approved trial, then run post-commit progress handling.

    Verification already ran inside the steering wrapper and
    ``_record_attempt`` already committed its knowledge. Here the world advances
    and ``_monitor_trend`` decides checkpoint, pending continuation, or
    recovery and revert.
    """
    # Capture a satisfied bearing's launch world before commit. Its landing
    # remains pending until ordinary progress is banked; an Alarm ejection must
    # replays from this exact source with its PilotRungs, not an older trend CP.
    _anchor_bearing_receipt(trial, frame, state)

    # Knowledge handling may have installed an excursion correction after verification built the
    # trial.  The accepted world key must describe that effective rung overlay,
    # not the pre-correction one used by the diagnostic fork.
    if trial.new_key is not None:
        assert state.key_config is not None
        trial = replace(
            trial,
            new_key=_pilot_world_key(trial.fork_snap, state.key_config, state.rungs),
        )
    _commit_trial(trial, frame, state, ctx)
    yield PilotEvent(
        "trial_committed",
        state.work.state.scan_id,
        {
            "candidate": trial.candidate,
            "applied": trial.applied,
            "steps": tuple(state.steps),
            "snapshot": dict(state.work.state.tags),
        },
    )
    yield from _monitor_trend(trial, frame, state, ctx)


def _commit_trial(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> None:
    key_was_seen = trial.new_key is not None and trial.new_key in state.seen_keys
    if trial.new_key is not None:
        state.seen_keys.add(trial.new_key)
    # Record what was physically applied — the candidate plus its co-actions (the
    # command button and its one-shot ``rise(CmdChgRequest)`` edge gate) — not the
    # narrow ``trial.candidate``.  Replay and live apply must reproduce every input
    # that drove the transition.  ``applied`` is the full set and is empty exactly
    # for zoom/let-run, where an empty action correctly means "coast, no input".
    # A terminal let-run animates conditional holds during its coast; record them
    # on the step so the path is self-describing.  ``rungs`` is the live
    # round-by-round accumulator — snapshot the conditional ones active now.  A
    # pulse/zoom step animates nothing, so it carries no reactive holds.
    #
    # The *steady* holds active during the coast (e.g. the Enable that drives a
    # harness sensor's ramp) are the input that makes the coast advance — fold
    # them into the recorded inputs so replay re-establishes them.  ``applied``
    # is empty for a let-run, so this is the only place the driver is recorded.
    step_inputs = dict(trial.applied)
    work, steps = _commit_step(
        state.work,
        trial.fork,
        step_inputs,
        trial.scan_before,
        ctx.resting,
        ctx.edge_tags,
        ctx.live,
    )
    act = _CommittedAct(steps=steps, context=_step_context(trial, frame, state))
    # Adopt the physical fork and its replay evidence in one persistent-world
    # update. No consumer can observe steps detached from their operation owner.
    state.world = state.world.set(
        work=work,
        committed_acts=state.committed_acts.append(act),
    )
    # The world record reverts; the flattened journey is the append-only public
    # history of every physical step, including later-reverted operations.
    state.journey.extend(steps)
    # Waiting is not searching: an accepted coast's span is dwell — the machine
    # advancing itself while the pilot holds heading — so it must not drain the
    # search budget (the loop charges ``scan_id - dwell_scans``).  A revert
    # rewinds this credit with the world.  The credit is earned only when the
    # machine actually moved its own work — the coast reached its channel
    # target or advanced the progress gauge; a coast that parks with
    # nothing moving is the *search* failing, and sterile laps must still
    # drain the budget (the old-wiring live run spun at HELD committing 100k
    # scan-ids per lap — free dwell there means no terminating force).
    if trial.motion.is_coast:
        productive = (
            not key_was_seen
            or (
                trial.zoom_channel_tag is not None
                and _values_match(
                    trial.fork_snap.get(trial.zoom_channel_tag), trial.zoom_target_value
                )
            )
            or (
                state.gauge is not None
                and state.gauge.ordinal_advanced(frame.snap, trial.fork_snap)
            )
        )
        if productive:
            state.dwell_scans += state.work.state.scan_id - trial.scan_before


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
            "root_route": ctx.route or state.inferred_route_commitment,
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
    diagnosis: Stuck | RouteExhausted | None = None,
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


def _pilot_loop_events(
    plc: PLC,
    ctx: _PilotContext,
) -> Iterator[PilotEvent]:
    """Run the PILOT loop as a structured event stream."""
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
            rungs=pvector([]),
            dwell_scans=0,
        ),
        key_config=ctx.key_config,
        seen_keys=set(),
        checkpoints=[],
        watch_tags=[],
    )
    # The target-relative progress gauge (gauge.py): event-earned
    # ordinals the threshold-masked search key deliberately aliases.  Static
    # for the loop's life; knowledge side (never reverted).  Best-effort — an
    # an empty gauge degrades every consumer to its earlier behavior.
    try:
        state.gauge = build_gauge(
            ctx.pdg,
            ctx.program,
            ctx.target_tag,
            ctx.key_config,
            steerable=ctx.steerable,
            clear_only=ctx.clear_only,
            edge_tags=frozenset(ctx.edge_tags),
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            channel_tags=frozenset(role.channel_tag for role in ctx.pipeline_roles),
            harness=getattr(plc, "_harness", None),
        )
    except Exception:  # noqa: BLE001 — diagnostics must not break the drive
        logger.debug("pilot: gauge build failed", exc_info=True)

    # Settle: at scan 0, calc-computed intermediates are still at defaults
    # and may trivially satisfy conditions that fail once rungs execute
    # (e.g. PV >= Lower where Lower is calc'd from SetPoint).
    if state.work.state.scan_id == 0:
        state.work.step()

    yield PilotEvent(
        "started",
        state.work.state.scan_id,
        {
            "target": (ctx.target_tag, ctx.target_value),
            "steerable_count": len(ctx.steerable),
            "opaque_loop": ctx.opaque_loop,
            "pipeline_roles": ctx.pipeline_roles,
            "pipeline_internal_tags": ctx.pipeline_internal_tags,
            "route": ctx.route,
            "blocked_route_actions": ctx.blocked_route_actions,
        },
    )

    # Each turn reads the current world and builds candidate modes. Every mode
    # executes and verifies on a fork inside steer.py, after which the loop
    # applies its observations. A gate-approved fork is then committed and sent
    # to progress.py, which may checkpoint it, keep a departure pending, or
    # investigate and revert it. Rejected modes fall through to the next mode in
    # the same turn.
    # The budget charges *searching*, never *waiting*: committed scan-ids minus
    # the accepted-coast dwell credit (see ``_World.dwell_scans``).  An armed
    # self-advancing dwell — a 39k-scan dry timer the coast rides — is the
    # machine doing its own work, not the pilot spending effort.
    last_frame: _IterationFrame | None = None
    while state.search_scan < ctx.max_scans:
        snap = dict(state.work.state.tags)
        if target_reached(snap, ctx.target_tag, ctx.target_value, ctx.target_predicate):
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
        target = TargetSpec(ctx.target_tag, ctx.target_value, ctx.target_predicate)
        constraints = NavigationConstraints(
            blocked_actions=ctx.blocked_route_actions,
            avoid_predicate=ctx.avoid_pred,
            active_root_route=state.inferred_route_commitment,
            exhausted_root_routes=(
                frozenset(
                    state.exhausted_route_ids.get(
                        _pilot_world_key(
                            dict(state.work.state.tags),
                            state.key_config,
                            state.rungs,
                        ),
                        set(),
                    )
                )
                if state.key_config is not None
                else frozenset()
            ),
        )
        result = ctx.compass.orient(raw_world, target, constraints)
        trace = result.trace
        if trace is None:
            raise RuntimeError("Compass orientation omitted its current-world reading")
        orientation_world = trace.world
        candidates = trace.candidates
        frame = orientation_world.frame
        last_frame = frame
        if state.key_config is None:
            state.key_config = orientation_world.key_config
        if not state.watch_tags:
            state.watch_tags.extend(sorted(frame.tree.pivot_tags()))
        for action in frame.raw_trace_action_details:
            if action.note:
                state.lever_notes[action.tag] = action.note
        if state.best_trend is None:
            state.best_trend = frame.distance_before
            state.seen_keys.add(frame.key)
        if not state.checkpoints and isinstance(result, Bearing):
            # Seed an entry checkpoint so the first regression — or a terminal
            # let-run ejection from a pre-positioned start (e.g. dropped straight
            # into Execute) — has somewhere to revert to.  "No checkpoint" should
            # mean "go back to the beginning", not "let the ejected state stand".
            state.checkpoints.append(
                _Checkpoint(
                    key=frame.key,
                    world=state.snapshot_world(),
                    trend=frame.distance_before,
                    objective=result.objective,
                )
            )
        yield from _record_pending_landing(frame, state)
        yield PilotEvent(
            "iteration", state.work.state.scan_id, _iteration_payload(frame, state, ctx)
        )
        yield PilotEvent(
            "candidates_built",
            state.work.state.scan_id,
            _candidates_built_payload(candidates, state.lever_notes),
        )

        if isinstance(result, RouteExhausted):
            state.exhausted_route_ids.setdefault(result.world_key, set()).add(result.route_identity)
            if result.revocable:
                state.inferred_route_commitment = None
            yield PilotEvent(
                "route_exhausted",
                state.work.state.scan_id,
                {
                    "route": result.route,
                    "identity": result.route_identity,
                    "rejected_actions": result.rejected_actions,
                    "revoked": result.revocable,
                },
            )
            if result.revocable:
                continue
            terminal_reason = (
                "Every available action on the requested via route failed its trial"
                + _frontier_clause(frame)
            )
            yield _stuck_event(
                state,
                ctx,
                frame,
                terminal_reason,
                candidate_count=len(candidates.candidates),
                diagnosis=result,
            )
            if state.checkpoints:
                state.load_world(state.checkpoints[-1].world)
            yield _finished_event(
                state,
                ctx,
                journal_channel_tags,
                journal_acc_names,
                reached=False,
                reason=terminal_reason,
            )
            return

        if ctx.route is None and state.inferred_route_commitment is None:
            state.inferred_route_commitment = orientation_world.root_route

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
            if not changed:
                terminal_reason = _with_avoid_reason(
                    "No safe next action was found",
                    state,
                    ctx,
                    frame,
                ) + _frontier_clause(frame)
                yield _stuck_event(
                    state,
                    ctx,
                    frame,
                    terminal_reason,
                    candidate_count=0,
                )
                if state.checkpoints:
                    state.load_world(state.checkpoints[-1].world)
                yield _finished_event(
                    state,
                    ctx,
                    journal_channel_tags,
                    journal_acc_names,
                    reached=False,
                    reason=terminal_reason,
                )
                return
            continue

        if isinstance(result, Stuck):
            terminal_reason = _with_avoid_reason(
                _stopped_reason(result.reason_code),
                state,
                ctx,
                frame,
            ) + _frontier_clause(frame)
            yield _stuck_event(
                state,
                ctx,
                frame,
                terminal_reason,
                candidate_count=len(candidates.candidates) if candidates is not None else 0,
                diagnosis=result,
            )
            if state.checkpoints:
                state.load_world(state.checkpoints[-1].world)
            yield _finished_event(
                state,
                ctx,
                journal_channel_tags,
                journal_acc_names,
                reached=False,
                reason=terminal_reason,
            )
            return

        assert isinstance(result, Bearing)
        act = result.act
        if isinstance(act, Pulse):
            candidate = act.option
            yield PilotEvent(
                "candidate_try",
                state.work.state.scan_id,
                {
                    "index": 0,
                    "total": 1,
                    "candidate": _candidate_payload(candidate),
                    "applied": act.applied,
                    "co_actions": tuple(pair for pair in act.applied if pair != act.action),
                },
            )
        elif isinstance(act, (Coast, Dwell)):
            yield PilotEvent(
                "zoom",
                state.work.state.scan_id,
                {
                    "prescribed": True,
                    "reason": result.rationale,
                    "prerequisite_rungs": result.prerequisites,
                    "channel_tag": (
                        act.route_channel_tag or act.channel_tag
                        if isinstance(act, Coast)
                        and act.mode == "bearing"
                        and act.channel_tag is not None
                        else ctx.target_tag
                    ),
                },
            )

        attempt = execute(result, orientation_world)
        _record_attempt(attempt, frame, state, ctx, result.objective)

        if isinstance(act, Coast) and act.mode == "terminal":
            stop_reason = (
                attempt.stall_receipt.stop_reason
                if attempt.stall_receipt is not None
                else (
                    attempt.trial.coast_receipt.stop_reason
                    if attempt.trial is not None and attempt.trial.coast_receipt is not None
                    else "terminal-coast"
                )
            )
            ctx.compass, _ = ctx.compass.apply((CoastObservation(frame.key, stop_reason),))

        if attempt.trial is None:
            # A rejected act is durable empirical evidence scoped to this exact
            # world.  The next loop turn recomputes before selecting anything.
            ctx.compass, _ = ctx.compass.apply(
                (ActionNogoodObservation(frame.key, act_identity(act)),)
            )
            if isinstance(act, Pulse):
                yield PilotEvent(
                    "candidate_rejected",
                    state.work.state.scan_id,
                    {
                        "index": 0,
                        "candidate": _candidate_payload(act.option),
                        "applied": act.applied,
                        "co_actions": tuple(pair for pair in act.applied if pair != act.action),
                        "gates": attempt.gate_events,
                    },
                )
            elif isinstance(act, (Coast, Dwell)):
                yield PilotEvent(
                    "zoom_rejected",
                    state.work.state.scan_id,
                    {"gates": attempt.gate_events},
                )
            else:
                yield PilotEvent(
                    "batch_rejected" if act.source == "learned" else "widening_rejected",
                    state.work.state.scan_id,
                    {"actions": act.actions, "gates": attempt.gate_events},
                )
            continue

        trial = attempt.trial
        if isinstance(act, Pulse):
            yield PilotEvent(
                "candidate_accepted",
                trial.fork.state.scan_id,
                _accepted_payload(act.option, trial, frame, state),
            )
        elif isinstance(act, (Coast, Dwell)):
            yield PilotEvent(
                "zoom_accepted",
                trial.fork.state.scan_id,
                _zoom_accepted_payload(trial),
            )
        else:
            yield PilotEvent(
                "batch_accepted" if act.source == "learned" else "widening_accepted",
                trial.fork.state.scan_id,
                {
                    "candidate": trial.candidate,
                    "applied": trial.applied,
                    "gates": trial.gate_events,
                    "new_key": trial.new_key,
                    "trend": trial.trend,
                    "snapshot": trial.fork_snap,
                    "scan_before": trial.scan_before,
                    "scan_after": trial.fork.state.scan_id,
                },
            )
        yield from _commit_and_monitor(trial, frame, state, ctx)
        state.last_wait_log = None
        continue

    # ── Budget exhausted: the work fork ran past max_scans ──
    # A dwell that drains the budget is a stall, not a wrap-up: route the
    # terminal through a fresh frame so the reason names the outstanding
    # frontier, and revert to the last checkpoint like the stuck exits do
    # ("How we fail" #1 — every stop points at a named leaf).
    snap = dict(state.work.state.tags)
    reached = target_reached(snap, ctx.target_tag, ctx.target_value, ctx.target_predicate)
    if reached:
        reason = "target reached"
    else:
        frame = last_frame
        reason = _with_avoid_reason(
            f"budget exhausted ({ctx.max_scans} scans searched + {state.dwell_scans} waited)",
            state,
            ctx,
            frame,
        ) + _frontier_clause(frame)
        yield _stuck_event(
            state,
            ctx,
            frame,
            reason,
            candidate_count=0,
        )
        if state.checkpoints:
            state.load_world(state.checkpoints[-1].world)
    yield _finished_event(
        state,
        ctx,
        journal_channel_tags,
        journal_acc_names,
        reached=reached,
        reason=reason,
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
    except Exception:  # noqa: BLE001 — diagnostic only; never mask the real failure
        return None
    route_tags = {n.tag for n in _all_nodes(tree)}
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


def _exclusive_route_actions(
    selected: TraceChoice | None,
    choices: tuple[TraceChoice, ...],
    target_tag: str,
    target_value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    clear_only: frozenset[str] = frozenset(),
) -> frozenset[tuple[str, Any]]:
    """Actions that belong only to a *non-selected* route — block them so the
    drive loop never drifts onto a route PILOT didn't take (incl. avoided/pruned
    ones).  Diffed against the full enumerated set, not just the survivors."""
    if selected is None or not choices:
        return frozenset()
    selected_actions = set(
        trace_back(
            target_tag,
            target_value,
            snapshot,
            pdg,
            program,
            steerable,
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            route=selected,
        ).ordered_actions()
    )
    other_actions: set[tuple[str, Any]] = set()
    for option in choices:
        if option.id == selected.id:
            continue
        other_actions.update(
            trace_back(
                target_tag,
                target_value,
                snapshot,
                pdg,
                program,
                steerable,
                clear_only=clear_only,
                opaque_loop=opaque_loop,
                route=option,
            ).ordered_actions()
        )
    return frozenset(other_actions - selected_actions)


def _route_name(route: TraceChoice) -> str:
    """Human name for a route — the discriminator the engineer would type."""
    if route.via_hint is not None:
        tag, value = route.via_hint
        return tag if value is True else f"{tag}=={value!r}"
    return route.label


def _build_route_taken(
    default: TraceChoice,
    survivors: tuple[TraceChoice, ...],
    steerable: frozenset[str],
) -> RouteTaken:
    """Describe the chosen *default* route plus the routes not taken.

    Models the fork as one redirectable pivot whose ``alternatives`` are the
    other surviving routes.  ``salient`` is True when any route in the fork is
    gated by a non-steerable discriminator (an internal coil/state the engineer
    commits to) — the trivial all-input fork (``Or(Auto, Manual)``) stays
    non-salient and hidden from the headline, though still redirectable.
    """
    others = tuple(ch for ch in survivors if ch.id != default.id)
    alternatives = tuple(RouteAlt(label=_route_name(ch), via_hint=ch.via_hint) for ch in others)
    hints = [default.via_hint, *(ch.via_hint for ch in others)]
    salient = any(h is not None and h[0] not in steerable for h in hints)
    dtag, dvalue = default.via_hint if default.via_hint is not None else (default.label, True)
    pivot = RoutePivot(
        tag=dtag,
        value=dvalue,
        label=_route_name(default),
        kind="writer" if default.writer_locks else "or-arm",
        via_hint=default.via_hint,
        alternatives=alternatives,
        salient=salient,
    )
    return RouteTaken(
        label=f"via {_route_name(default)}",
        pivots=(pivot,),
        dominant=len(survivors) <= 1,
    )


def _report_selected_route(
    prepared: RouteTaken | None,
    selected: TraceChoice | None,
) -> RouteTaken | None:
    """Make the public route receipt name the route that actually finished.

    ``prepared`` describes the initially preferred fork so the engineer can see
    its alternatives before execution. If that inferred commitment is later
    exhausted and replaced, rotate the same root pivot around the route that
    ultimately reached the target. This is reporting only; no alternative list
    feeds back into navigation.
    """

    if prepared is None or selected is None or not prepared.pivots:
        return prepared
    selected_name = _route_name(selected)
    pivot = prepared.pivots[0]
    if pivot.label == selected_name:
        return prepared

    alternatives = [
        RouteAlt(label=pivot.label, via_hint=pivot.via_hint),
        *(alt for alt in pivot.alternatives if alt.label != selected_name),
    ]
    selected_hint = selected.via_hint
    selected_tag, selected_value = (
        selected_hint if selected_hint is not None else (selected.label, True)
    )
    return RouteTaken(
        label=f"via {selected_name}",
        pivots=(
            RoutePivot(
                tag=selected_tag,
                value=selected_value,
                label=selected_name,
                kind="writer" if selected.writer_locks else "or-arm",
                via_hint=selected_hint,
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
    via_pred: Any = None,
) -> tuple[TraceChoice | None, frozenset[tuple[str, Any]], RouteTaken | None]:
    """Describe the preferred route and bind an explicit ``via=`` route lock.

    Works for any concrete equality target — ``Bool == True``, ``Bool == False``,
    or a word ``tag == value``; a live relational predicate gets no route (see
    :func:`_target_is_value_route`).  ``how()`` never reports ambiguous: it
    enumerates the routes, prunes any that ``avoid=`` forbids or that ``via=``
    does not pass through, then ranks the cheapest survivor (gate-eligible routes
    preferred, trace score next, rung order breaking ties) and records the rest
    as redirectable pivots on the returned :class:`RouteTaken`.

    Only ``via=`` expresses a durable choice: it returns the selected
    ``route_lock`` and excludes actions belonging solely to other root routes.
    An inferred default becomes one revocable session commitment instead; only
    exact exhaustion releases it so another admissible route can be selected.

    Returns ``(route_lock, blocked_route_actions, route_taken)``. All ``None``/
    empty when the target is not a multi-route value target, or when the
    constraint excludes every route (the loop then runs unlocked and honestly
    reports the miss; the ``avoid=`` verify gate still vetoes resting in the
    avoided region).
    """
    snapshot = dict(plc.state.tags)
    if not (
        _target_is_value_route(target_predicate)
        and not _values_match(snapshot.get(target_tag), target_value)
    ):
        return None, frozenset(), None
    clear_only = compute_clear_only(pdg, plc._known_tags_by_name, program)
    choices, traced = rank_trace_choices(
        target_tag,
        target_value,
        snapshot,
        pdg,
        program,
        steerable,
        clear_only=clear_only,
        opaque_loop=opaque_loop,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )
    if not choices:
        return None, frozenset(), None
    if not traced:
        return None, frozenset(), None
    default = traced[0][0]
    survivors = tuple(choice for choice, _tree in traced)
    route_taken = _build_route_taken(default, survivors, steerable)
    # The selected route is a permanent execution constraint only when the user
    # explicitly requested it.  A default is a preference/reporting choice, and
    # ``avoid=`` already owns its exclusions through the route/action/scan gates.
    # Neither may turn every other clean route into a durable rejection.
    if via_pred is None:
        return None, frozenset(), route_taken
    blocked = _exclusive_route_actions(
        default,
        choices,
        target_tag,
        target_value,
        snapshot,
        pdg,
        program,
        steerable,
        opaque_loop,
        clear_only,
    )
    return default, blocked, route_taken


# ---------------------------------------------------------------------------
# Prover context — nd_domains + state key config
# ---------------------------------------------------------------------------


def _build_prover_context(
    program: Any,
    snapshot: dict[str, Any],
) -> _ProverContext:
    """Build prover context for nd_domains and state key projection.

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
            return _ProverContext(nd_domains=nd, evidence=evidence)

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
    return Atom(tag=tag_name, form=form, operand=operand)


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


def pilot_events(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    via_pred: Any = None,
    unlink: list[str] | None = None,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events.

    ``unlink`` frees the named harness-feedback tags for fault injection (see
    :func:`pilot_how`).  ``avoid_pred``/``via_pred`` constrain the route the same
    way ``how(avoid=...)`` / ``how(via=...)`` do.
    """
    target_tag, target_value, target_predicate = _parse_target(*conditions)
    setup = _prepare_drive(plc, live=False, unlink=unlink)
    ctx, _route_taken = _prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )
    yield from _pilot_loop_events(setup.work, ctx)


def pilot_how(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    via_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """PILOT on a fork — drive to the target and return the recording. Nothing changes.

    For a multi-route value target (``Bool == True/False`` or word
    ``tag == value``) PILOT starts with a deterministic preferred route and
    records the route that actually reached the goal on ``Plan.route``;
    ``avoid_pred``/``via_pred`` redirect off/onto a route (the engineer names the
    alternative from ``Plan.route``).

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
            via_pred=via_pred,
            unlink=unlink,
            on_event=on_event,
        )
    target_tag, target_value, target_predicate = targets[0]
    setup = _prepare_drive(plc, live=False, unlink=unlink)
    ctx, route_taken = _prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )
    outcome = _pilot_loop(
        setup.work,
        ctx,
        on_event=on_event,
    )

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
    reason = linked_block or outcome.reason
    return Plan(
        reachable=outcome.reached,
        target_tag=target_tag,
        target_value=target_value,
        fork=outcome.work if outcome.reached else None,
        reason=reason,
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
        journal=outcome.journal,
        anchor_scan=setup.anchor_scan,
        journey=outcome.journey,
        hold_log=outcome.knowledge.get("hold_log", ()),
        lever_notes=outcome.knowledge.get("lever_notes", {}),
        avoid_names=outcome.knowledge.get("avoid_names", ()),
    )


def _pilot_how_multi(
    plc: PLC,
    targets: list[tuple[str, Any, Any]],
    *,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    via_pred: Any = None,
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
    setup = _prepare_drive(plc, live=False, unlink=unlink)

    goal_pairs = tuple((tt, tv) for tt, tv, _ in targets)

    ok, reason, ordered = _mt.analyze(
        setup.diag_snapshot,
        setup.pdg,
        setup.program,
        setup.steerable,
        targets,
    )
    if not ok:
        return Plan(
            reachable=False,
            target_tag=label,
            target_value=True,
            targets=goal_pairs,
            reason=reason,
            status=PlanStatus.CANNOT_REACH,
            anchor_scan=setup.anchor_scan,
        )

    work = setup.work
    inf = setup.compass
    last_knowledge: dict[str, Any] = {}
    last_journey: tuple[Any, ...] = ()
    # The per-target drives run sequentially on ONE fork, so their journals are already
    # in scan order — concatenating them gives the whole passage, not the last leg only.
    journal_steps: list[Any] = []
    for t_tag, t_val, t_pred in ordered:
        if target_reached(dict(work.state.tags), t_tag, t_val, t_pred):
            continue  # already pulled in by an earlier target's drive
        # Same route discipline as single-target how(): pick the default route and
        # block the other routes' actions so the drive can't drift onto a route that
        # clobbers a sibling (e.g. the auto route through a state machine).
        # ``avoid=``/``via=`` are route predicates over tag values, not tied to any
        # one target, so they constrain every target's route selection uniformly —
        # a route (for any target) that forces the avoided predicate is pruned.
        ctx, _route_taken = _prepare_target_context(
            setup,
            t_tag,
            t_val,
            t_pred,
            influence=inf,
            max_scans=work.state.scan_id + max_scans,
            avoid_pred=avoid_pred,
            via_pred=via_pred,
            work=work,
        )
        outcome = _pilot_loop(work, ctx, on_event=on_event)
        work = outcome.work
        last_knowledge = outcome.knowledge
        inf = outcome.knowledge.get("compass", inf)
        last_journey = outcome.journey
        journal_steps.extend(outcome.journal)
        if not outcome.reached:
            detail = f"; {outcome.reason}" if outcome.reason else ""
            return Plan(
                reachable=False,
                target_tag=label,
                target_value=True,
                targets=goal_pairs,
                reason=(
                    f"pilot: could not establish {t_tag}={t_val!r} while holding the "
                    f"other target(s){detail}"
                ),
                status=PlanStatus.STOPPED,
                anchor_scan=setup.anchor_scan,
            )

    final = dict(work.state.tags)
    unmet = [(tt, tv) for tt, tv, tp in targets if not target_reached(final, tt, tv, tp)]
    if unmet:
        names = ", ".join(f"{tt}={tv!r}" for tt, tv in unmet)
        return Plan(
            reachable=False,
            target_tag=label,
            target_value=True,
            targets=goal_pairs,
            reason=f"pilot: reached each target individually but {names} did not hold "
            "simultaneously (clobbered during co-establishment).",
            status=PlanStatus.STOPPED,
            anchor_scan=setup.anchor_scan,
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


def pilot_drive(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    avoid_pred: Any = None,
    via_pred: Any = None,
    unlink: list[str] | None = None,
) -> Plan:
    """PILOT on the live PLC — drive the state there.

    ``unlink`` frees the named harness-feedback tags for fault injection (see
    :func:`pilot_how`).
    """
    target_tag, target_value, target_predicate = _parse_target(*conditions)
    setup = _prepare_drive(plc, live=True, unlink=unlink)
    ctx, route_taken = _prepare_target_context(
        setup,
        target_tag,
        target_value,
        target_predicate,
        max_scans=max_scans,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )
    outcome = _pilot_loop(setup.work, ctx)

    # A live failure without a harness-link explanation falls back to the
    # loop's own terminal diagnostic (``stuck: …`` / ``budget exhausted``) so
    # an unreachable target always carries a reason ("How we fail" #2).
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
    reason = linked_block or outcome.reason
    return Plan(
        reachable=outcome.reached,
        target_tag=target_tag,
        target_value=target_value,
        fork=outcome.work if outcome.reached else None,
        reason=reason,
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
        anchor_scan=setup.anchor_scan,
        journey=outcome.journey,
        hold_log=outcome.knowledge.get("hold_log", ()),
        lever_notes=outcome.knowledge.get("lever_notes", {}),
        avoid_names=outcome.knowledge.get("avoid_names", ()),
    )
