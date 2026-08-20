"""Execution and verification engine for PILOT drives.

This module dispatches the typed orientation results returned by ``Compass``.
It invokes execution, owns verification-time excursion investigation, applies
observations, commits eligible forks, and delegates post-commit recovery. It
does not parse public requests, assemble public plans, or synthesize a
navigation decision.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

import pyrung.core.analysis.pilot.attempt_transition as _attempt_transition
import pyrung.core.analysis.pilot.entry_execution as _entry_execution
import pyrung.core.analysis.pilot.requirement_repair as _requirement_repair
import pyrung.core.analysis.pilot.target_route as _target_route
import pyrung.core.analysis.pilot.theory_drive as _theory_drive
import pyrung.core.analysis.pilot.theory_recording as _theory_recording
from pyrung.core.analysis.graph import (
    PlanStep,
)
from pyrung.core.analysis.pilot.advance import iter_advance_owners
from pyrung.core.analysis.pilot.compass import (
    ProbeExhaustedObservation,
)
from pyrung.core.analysis.pilot.departure_state import (
    _record_pending_landing,
    _trial_checkpoint,
)
from pyrung.core.analysis.pilot.earned_work import (
    build_earned_work,
)
from pyrung.core.analysis.pilot.intrascan_research import (
    research_intrascan_boundary_realization,
    research_intrascan_traceback,
    research_retained_frontier_realization,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    BearingObjective,
    ComposeCorrection,
    IntrascanPulse,
    NavigationConstraints,
    NeedIntrascanBoundaryRealization,
    NeedIntrascanTraceback,
    NeedProbe,
    NeedResearch,
    ObserveScan,
    OrientationWorld,
    ProgramScan,
    Stuck,
    _ActionPair,
)
from pyrung.core.analysis.pilot.progress import (
    _monitor_trend,
)
from pyrung.core.analysis.pilot.recording import (
    _act_event,
    _build_plan_journal,
    _candidates_built_payload,
    _channel_position,
    _frontier_clause,
    _iteration_payload,
    _knowledge_payload,
)
from pyrung.core.analysis.pilot.requirement_evidence import (
    _attempt_productive_scan,
    _configured_input_names,
    _derive_settled_target_requirements,
    _release_attempt_projections,
)
from pyrung.core.analysis.pilot.route_judgment import route_forced_names
from pyrung.core.analysis.pilot.skiff import probe_live_guard_frontiers
from pyrung.core.analysis.pilot.theory_evidence import (
    _theory_boundary_from_checkpoint,
    _theory_requirement_snapshot,
    _theory_transition_after_monitor,
)
from pyrung.core.analysis.pilot.theory_reducer import (
    AbandonTheory,
    RecordConductivityResearch,
    RecordIntrascanTraceback,
    RecordIntrascanTracebackFrontier,
)
from pyrung.core.analysis.pilot.trace import target_reached, trace_back
from pyrung.core.analysis.pilot.trace_read import (
    TraceChoice,
    TraceReadConstraints,
)
from pyrung.core.analysis.pilot.trace_routes import enumerate_trace_choices
from pyrung.core.analysis.pilot.trace_tree import frontier_pairs
from pyrung.core.analysis.pilot.types import (
    PilotEvent,
    _AcceptedTrial,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _Step,
)
from pyrung.core.analysis.pilot.working_theory import (
    ConductivityResearchFinding,
    IntrascanOrdinarySteerFinding,
    IntrascanTracebackFinding,
    IntrascanTracebackFrontier,
    TheoryAttemptDisposition,
    TheoryTermination,
    active_theory,
    temporal_need_request,
    theory_view,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint, _World
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


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

    Enumerates the same routes as ``prepare_target_route`` from the current frame and,
    when they are all avoid-forced (no survivor), returns the union of the
    violated member names.  ``()`` when the target isn't a value-route target or
    any route survives.
    """
    avoid = getattr(ctx, "avoid_pred", None)
    if avoid is None:
        return ()
    snap = frame.snap
    if not (
        _target_route.target_is_value_route(ctx.target.predicate)
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
        forced = route_forced_names([tree], snap, avoid)
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


def _monitor_committed_trial(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> Iterator[PilotEvent]:
    """Emit one adopted trial and apply outer-loop progress policy."""

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
    # protocol.  Every accepted trial owns the enriched receipt on its attempt;
    # neither assertion horizon nor an active theory is an exemption.
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
        return
    yield from _monitor_trend(trial, frame, state, ctx)


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
    bootstrap_execution = _entry_execution.import_adjacent_entry_scan(state, ctx)

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
                _theory_recording._open_theory_from_program_guard_rebases(
                    state,
                    rebased_requirements,
                    remaining_budget=state.remaining_search_scans(ctx.max_scans),
                )
            else:
                _theory_recording._refine_active_theory_from_program_guard_rebases(
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
            _theory_recording._run_optional_theory_hook(
                _theory_recording._record_optional_theory_proved, state
            )
            if state.steps:
                # Target observation executes nothing. Every logical scan at
                # this tip must already belong to the committed operation.
                state.assert_replay_tip()
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
            defer_program_input_receipts=bool(
                state.bootstrap_execution is not None
                and not state.bootstrap_execution.route_bound
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
        bound_entry = _entry_execution.bind_entry_execution_to_route(state, ctx, result, frame)
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
        _attempt_transition.prepare_oriented_result(state, result, orientation_world, frame)
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
                    "configuration": (
                        result.configuration.assignments
                        if result.configuration is not None
                        else ()
                    ),
                    "pilot_rungs": result.pilot_rungs,
                    "conditions": tuple(
                        _theory_requirement_snapshot(requirement).condition_identity
                        for requirement in composed.requirements
                    ),
                    "requirement_conditions": tuple(
                        requirement.condition for requirement in composed.requirements
                    ),
                    "superseded_configuration_identities": (
                        composed.superseded_configuration_identities
                    ),
                    "superseded_pilot_rung_identities": (
                        composed.superseded_pilot_rung_identities
                    ),
                    "research_finding_identity": composed.research_finding_identity,
                    "reason": result.rationale,
                    "position": _channel_position(ctx, state.work.state.tags),
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
            _theory_recording._record_controlling_theory_fact(
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
                _theory_recording._record_controlling_theory_fact(
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
                _theory_recording._record_controlling_theory_fact(
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
                _theory_recording._record_controlling_theory_fact(
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
                _theory_recording._record_controlling_theory_fact(
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
            _theory_recording._run_optional_theory_hook(
                _theory_recording._record_optional_theory_abandoned, state, TheoryTermination.STUCK
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
        transition = _attempt_transition.transition_once(
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
            controlled_setup_attempt = _theory_recording._record_controlled_setup_attempt(
                state,
                controlling_setup_request,
                result,
                attempt,
                theory_source_checkpoint,
            )
        if controlled_setup_attempt is not None and attempt.trial is not None:
            transition = _attempt_transition.adopt_deferred_transition(transition, state, ctx)
        if attempt.trial is None:
            if controlled_setup_attempt is not None:
                if _theory_recording._records_controlling_need(transition.theory_transition):
                    assert transition.theory_transition is not None
                    _theory_recording._record_working_theory_transition(
                        state,
                        transition.theory_transition,
                        remaining_budget=state.remaining_search_scans(ctx.max_scans),
                    )
                else:
                    theory = active_theory(state.theory_state)
                    if theory is None:
                        raise ValueError("rejected temporal attempt lost its theory")
                    rejected_attempt_id = controlled_setup_attempt.attempt_id
                    _theory_recording._record_controlling_theory_fact(
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
            elif _theory_recording._records_controlling_need(transition.theory_transition):
                _theory_recording._record_working_theory_transition(
                    state,
                    transition.theory_transition,
                    remaining_budget=state.remaining_search_scans(ctx.max_scans),
                )
            else:
                _theory_recording._run_optional_theory_hook(
                    _theory_recording._record_working_theory_transition,
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
                if _theory_recording._records_controlling_need(transition.theory_transition):
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
            requirements_before_monitor = _theory_recording._requirement_identities(state)
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
            _theory_recording._advance_retained_productive_tip(
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
            transition_has_need = _theory_recording._records_controlling_need(
                theory_transition
            )
            # Post-commit recovery may already have recorded an exact
            # regression requirement and restored its source.  That is the
            # same controlling Working Theory handoff as an intrascan failure;
            # do not subsequently advance the rejected landing over it.
            monitor_need = temporal_need_request(state.theory_state)
            monitor_opened_need = (
                monitor_need is not None and monitor_need != temporal_request
            )
            successor_need = transition_has_need or monitor_opened_need
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
                if transition_has_need:
                    assert theory_transition is not None
                    _theory_recording._record_working_theory_transition(
                        state,
                        theory_transition,
                        remaining_budget=state.remaining_search_scans(ctx.max_scans),
                    )
            else:
                if transition_has_need:
                    assert theory_transition is not None
                    _theory_recording._record_working_theory_transition(
                        state,
                        theory_transition,
                        remaining_budget=state.remaining_search_scans(ctx.max_scans),
                    )
                elif not successor_need:
                    if active_theory(state.theory_state) is not None:
                        _theory_recording._record_theory_transition(
                            state,
                            theory_transition,
                            remaining_budget=state.remaining_search_scans(ctx.max_scans),
                            record_fact=_theory_recording._record_controlling_theory_fact,
                        )
                        _theory_recording._record_theory_execution_advance(
                            state,
                            ctx,
                            trial,
                            theory_transition,
                        )
                        _theory_recording._complete_intrascan_consumer(
                            state,
                            temporal_request,
                            trial,
                            theory_transition,
                        )
                    else:
                        _theory_recording._run_optional_theory_hook(
                            _theory_recording._record_working_theory_transition,
                            state,
                            theory_transition,
                            remaining_budget=state.remaining_search_scans(ctx.max_scans),
                        )
                if not monitor_opened_need:
                    _theory_recording._run_optional_theory_hook(
                        _theory_recording._record_optional_requirement_delta,
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
        _theory_recording._run_optional_theory_hook(
            _theory_recording._record_optional_theory_abandoned, state, TheoryTermination.BUDGET
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
    _theory_recording._run_optional_theory_hook(
        _theory_recording._record_optional_theory_proved, state
    )
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
