"""PILOT drive loop — orchestration and entry points.

Owns the iteration cycle: prepare frame → select candidates → steer
(``steer.py``) → commit/revert → monitor progress (``progress.py``).
The instruments live in their own modules:

- ``steer.py``      — Act (pulse, zoom, try-verify wrappers)
- ``verify.py``     — gate pipeline (SPIN, CYCLE, DEAD-END, outcome)
- ``progress.py``   — trend monitoring, checkpoints, regression recovery
- ``candidates.py`` — compass bearing → ranked candidate list
- ``investigate.py``— bounded incident investigation
- ``causal.py``     — cause-chain walker (shared utility)
- ``types.py``      — cross-boundary types
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Generator, Iterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

from pyrung.core.analysis.graph import Plan, PlanStep, RouteAlt, RoutePivot, RouteTaken
from pyrung.core.analysis.pilot._ops import (
    _append_rungs,
    _apply_pulse,
    _DebugFn,
    _pilot_world_key,
    _rungs_from_proposals,
    _StateKeyConfig,
    _target_unresolved_condition,
)
from pyrung.core.analysis.pilot.accumulators import iter_profiles
from pyrung.core.analysis.pilot.candidates import (
    _build_candidates,
    _Candidate,
    _candidate_applied,
    _co_actions,
)
from pyrung.core.analysis.pilot.charts import (
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.compass import (
    Compass,
    _action_sort_key,
)
from pyrung.core.analysis.pilot.gauge import build_gauge
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.progress import (
    _anchor_bearing_receipt,
    _anchor_provisional,
    _monitor_trend,
)
from pyrung.core.analysis.pilot.skiff import probe_live_guard_frontiers
from pyrung.core.analysis.pilot.steer import (
    _try_candidate,
    _try_prescribed_batch,
    _try_terminal_dwell,
    _try_terminal_letrun,
    _try_widening,
    _try_zoom,
)
from pyrung.core.analysis.pilot.trace import (
    DomainPrior,
    TraceAction,
    TraceChoice,
    TraceNode,
    _all_nodes,
    _route_conflicts,
    _route_forced_names,
    _route_forces,
    _trace_score,
    compute_clear_only,
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    compute_steerable,
    enumerate_trace_choices,
    frontier_pairs,
    route_rung_order,
    target_reached,
    trace_back,
    trace_relational,
    writer_route_eligible,
)
from pyrung.core.analysis.pilot.types import (
    PilotEvent,
    TagChange,
    _ActionPair,
    _Checkpoint,
    _HoldLogEntry,
    _IterationFrame,
    _ObserveFn,
    _PilotContext,
    _PilotState,
    _Step,
    _StepContext,
    _TrialResult,
    _World,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.charts import CompassGraph, CompassPlan
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


def _commit_step(
    work: PLC,
    fork: PLC,
    inputs: dict[str, Any],
    scan_before: int,
    steps: list[_Step],
    resting: dict[str, Any],
    edge_tags: set[str],
    live: bool,
) -> PLC:
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
        steps.append(
            _Step(inputs=edge_release, scan_before=scan_before, scan_after=scan_before + 1)
        )
        steps.append(
            _Step(
                inputs=dict(inputs),
                scan_before=scan_before + 1,
                scan_after=fork.state.scan_id,
            )
        )
    else:
        steps.append(
            _Step(
                inputs=dict(inputs),
                scan_before=scan_before,
                scan_after=fork.state.scan_id,
            )
        )
    if live:
        _apply_pulse(work, list(inputs.items()), resting, edge_tags)
        return work
    return fork


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
    influence: Compass | None,
    opaque_loop: frozenset[str],
    route: TraceChoice | None,
    blocked_route_actions: frozenset[_ActionPair],
    max_scans: int,
    live: bool,
    debug: bool,
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
    compass = influence or Compass()
    compass.set_graphs(
        _build_compass_graphs_for_context(
            pipeline_roles,
            pdg,
            program,
            steerable,
            opaque_loop,
            evidence,
        )
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
        compass=compass,
        opaque_loop=opaque_loop,
        pipeline_roles=pipeline_roles,
        pipeline_internal_tags=pipeline_internal_tags,
        route=route,
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )


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


def _build_compass_graphs_for_context(
    pipeline_roles: tuple[PipelineRoles, ...],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    evidence: TransitionEvidence | None,
) -> tuple[CompassGraph, ...]:
    if not pipeline_roles:
        return ()
    from pyrung.core.analysis.pilot.charts import build_compass_graphs

    return build_compass_graphs(
        pipeline_roles,
        pdg,
        program,
        steerable,
        opaque_loop,
        evidence,
    )


def _ensure_state_key_config(
    state: _PilotState,
    tree: Any,
    target_tag: str,
) -> _StateKeyConfig:
    """Install the trace-tree fallback key config when prover context is absent."""
    if state.key_config is None:
        tree_tags = tree.pivot_tags() | {target_tag}
        tree_tags.update(
            n.tag
            for n in tree.leaves()
            if not n.is_steerable and not getattr(n, "pipeline_internal", False)
        )
        state.key_config = _StateKeyConfig(
            stateful_names=tuple(sorted(tree_tags)),
            done_specs=(),
            threshold_vector_specs=(),
            acc_indices=frozenset(),
        )
    return state.key_config


def _expand_and_seed(
    tree: Any,
    state: _PilotState,
    ctx: _PilotContext,
) -> None:
    """Expand static routes for newly-discovered pivot tags and seed the compass."""
    from pyrung.core.analysis.pilot.evidence import expand_routes

    candidates = (tree.pivot_tags() | ctx.opaque_loop | {ctx.target_tag}) - state.expanded_tags
    for tag in sorted(candidates):
        routes = expand_routes(
            tag,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            ctx.opaque_loop,
            ctx.evidence,
        )
        if routes:
            ctx.compass.seed_routes(tag, routes)
        state.expanded_tags.add(tag)


def _prepare_iteration(
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _IterationFrame:
    snap = dict(state.work.state.tags)
    if ctx.target_predicate is not None:
        # Relational target (A op B): trace the live predicate so the target
        # gets the same relational frontier, reactive levers, and coast
        # disposition as a relational prerequisite.
        tree = trace_relational(
            ctx.target_predicate,
            snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=ctx.route,
            prior=ctx.domain_prior,
            avoid_pred=ctx.avoid_pred,
            via_pred=ctx.via_pred,
            harness=getattr(state.work, "_harness", None),
        )
    else:
        tree = trace_back(
            ctx.target_tag,
            ctx.target_value,
            snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=ctx.route,
            prior=ctx.domain_prior,
            avoid_pred=ctx.avoid_pred,
            via_pred=ctx.via_pred,
            harness=getattr(state.work, "_harness", None),
        )
    _expand_and_seed(tree, state, ctx)
    key_config = _ensure_state_key_config(state, tree, ctx.target_tag)
    if not state.watch_tags:
        state.watch_tags.extend(sorted(tree.pivot_tags()))
        dbg(f"# watch_tags ({len(state.watch_tags)}): {state.watch_tags[:8]}...")

    key = _pilot_world_key(snap, key_config, state.rungs)
    distance_before = tree.unsatisfied_count()
    action_details = tuple(
        TraceAction(
            tag=action.tag,
            value=action.value,
            provenance=action.provenance,
            wake=len(ctx.pdg.downstream_slice(action.tag, follow_calls=True)),
            until=action.until,
            oscillate=action.oscillate,
            establish=action.establish,
            heuristic=action.heuristic,
            note=action.note,
            availability=action.availability,
        )
        for action in tree.ordered_action_details()
    )
    # Harvest relational lever reports (last-write-wins) so the plan journal can
    # attach them to the matching force/pulse/command steps at finished time.
    for action in action_details:
        if action.note:
            state.lever_notes[action.tag] = action.note
    if state.best_trend is None:
        state.best_trend = distance_before
        state.seen_keys.add(key)

    return _IterationFrame(
        snap=snap,
        tree=tree,
        key=key,
        distance_before=distance_before,
        raw_trace_actions=tuple(action.pair for action in action_details),
        raw_trace_action_details=action_details,
    )


def _fmt_need(tag: str, value: Any, snap: dict[str, Any]) -> str:
    """One ``still_need`` display entry.  A relational need carries its Atom —
    render the relation (``PV < Lower``), never the Atom repr as a value."""
    from pyrung.core.analysis.pilot.trace import _atom_text
    from pyrung.core.analysis.simplified import Atom

    if isinstance(value, Atom):
        return f"{_atom_text(value)} (have {snap.get(tag)!r})"
    return f"{tag}={value!r} (have {snap.get(tag)!r})"


def _frontier_clause(frame: _IterationFrame | None) -> str:
    """``" — still waiting on …"`` terminal suffix naming the frame's
    outstanding frontier pairs.

    Every stop points at a named leaf ("How we fail"): the skiff decline is a
    caption from the first unreadable frontier and the stuck/budget headlines
    are witness-based, both lossy — so the whole chosen tree's unmet needs ride
    along on every terminal reason.
    """
    if frame is None:
        return ""
    needs = frontier_pairs(frame.tree, frame.snap)
    if not needs:
        return ""
    head = ", ".join(_fmt_need(t, v, frame.snap) for t, v in needs[:3])
    more = f" (+{len(needs) - 3} more)" if len(needs) > 3 else ""
    return f" — still waiting on {head}{more}"


def _debug_iteration(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> None:
    if not ctx.debug:
        return

    dbg(f"\n{'=' * 60}")
    dbg(f"# ITERATION  scan={state.work.state.scan_id}  distance={frame.distance_before}")
    if state.steps:
        dbg(f"# accomplished ({len(state.steps)}):")
        for si, step in enumerate(state.steps):
            dbg(f"#   [{si}] {step.inputs}")

    still_need = [_fmt_need(t, v, frame.snap) for t, v in frontier_pairs(frame.tree, frame.snap)]
    if still_need:
        dbg(f"# still need ({len(still_need)}): {still_need[:10]}")

    dbg(
        "# nogoods for key: "
        f"{sorted(state.nogoods.get(frame.key, set()), key=_action_sort_key) or '(none)'}"
    )
    dbg(f"# rungs: {state.rungs if state.rungs else '(none)'}")
    dbg(f"# seen_keys: {len(state.seen_keys)}  checkpoints: {len(state.checkpoints)}")
    dbg(f"# trace ordered_actions (raw, {len(frame.raw_trace_actions)}):")
    for t, v in frame.raw_trace_actions:
        cur = frame.snap.get(t)
        edge = " [EDGE]" if t in ctx.edge_tags else ""
        ng = " [NOGOOD]" if (t, v) in state.nogoods.get(frame.key, ()) else ""
        already = " [ALREADY]" if _values_match(cur, v) and t not in ctx.edge_tags else ""
        dbg(f"#   {t}={v!r}  (cur={cur!r}){edge}{ng}{already}")


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
) -> None:
    """RECORD: commit an attempt's knowledge — accepted or rejected alike.

    Runs unconditionally after every Act, before ASSESS (``_monitor_trend``)
    can revert the world.  Compass observations, excursion holds, and nogoods
    all commit even when the trial is rejected: negative knowledge (probe
    marks, contradictions) must survive, or the skiff's singles→pairs
    escalation never terminates.
    """
    # The commit point: apply() returns the next compass value; this single
    # assignment replaces the context's compass (a value, never a shared
    # mutable advanced behind readers' backs).
    ctx.compass, _ = ctx.compass.apply(attempt.observations)
    if attempt.excursion_holds:
        scope = _target_unresolved_condition(
            state.work, ctx.target_tag, ctx.target_value, ctx.target_predicate
        )
        state.rungs = _append_rungs(
            state.work,
            _rungs_from_proposals(state.work, list(attempt.excursion_holds), scope),
            state.rungs,
        )
        state.hold_log.append(
            _HoldLogEntry(
                scan=state.work.state.scan_id,
                tags=tuple(attempt.excursion_holds),
                source="excursion",
            )
        )
    if attempt.nogood_pairs:
        state.nogoods.setdefault(frame.key, set()).update(attempt.nogood_pairs)
    if attempt.avoid_names:
        # Knowledge: which avoid conditions excluded a path, for a naming decline.
        state.avoid_names.update(attempt.avoid_names)


def _record_step_context(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
) -> None:
    """Capture raw context for this committed trial — built into journal at finished time."""
    is_coast = trial.motion.is_coast

    frontier_tags: tuple[str, ...] = ()
    steady_holds: tuple[str, ...] = ()
    pulsing_holds: tuple[str, ...] = ()

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
        steady_holds = tuple(dict.fromkeys(r.dest for r in state.rungs))

    state.step_contexts = state.step_contexts.append(
        _StepContext(
            scan_before=trial.scan_before,
            observe_label=trial.observe_label,
            motion=trial.motion,
            candidate=dict(trial.candidate),
            frontier_tags=frontier_tags,
            steady_holds=steady_holds,
            pulsing_holds=pulsing_holds,
            channel_tag=trial.zoom_channel_tag,
            before_snap=dict(trial.before_snap),
            after_snap=dict(trial.fork_snap),
            channel_target=trial.zoom_target_value,
            timeline=trial.timeline,
        )
    )


def _format_transition(sc: _StepContext, channel_tags: frozenset[str]) -> str:
    """Pick the journal's "X before → after" label from the channel registers.

    ``channel_tags`` is the semantic source — ``ctx.opaque_loop`` plus each
    pipeline role's ``channel_tag`` — not a name-pattern guess.  A transition
    is only reported for a tag that PILOT already knows is a channel register;
    falls back to "" when none of them changed.
    """
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
    """Build the annotated plan journal from the clean path + hold log.

    Called once at finished time, after reverts have settled.  ``channel_tags``
    and ``acc_names`` are the semantic sets (channel registers / accumulator
    registers) computed once per loop from ``ctx`` — see the call sites in
    ``_pilot_loop_events``.
    """
    if not state.steps:
        return ()

    ctx_by_scan: dict[int, _StepContext] = {c.scan_before: c for c in state.step_contexts}

    def _notes_for(inputs: Any) -> tuple[str, ...]:
        """Relational lever reports for the tags this step steers."""
        return tuple(state.lever_notes[t] for t, _v in inputs if t in state.lever_notes)

    entries: list[tuple[int, str, PlanStep]] = []

    # --- Commands and coasts from clean steps ---
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

    # --- Interleave holds from hold_log ---
    if state.steps:
        path_start = state.steps[0].scan_before
        path_end = state.steps[-1].scan_after
    else:
        path_start = path_end = 0

    seen_hold_tags: set[str] = set()
    for entry in state.hold_log:
        if entry.scan < path_start or entry.scan > path_end:
            continue
        force_tags = [(t, v) for t, v in entry.tags if t not in seen_hold_tags]
        pulse_tags: list[tuple[str, Any]] = []
        for t, _v in entry.tags:
            seen_hold_tags.add(t)
        if force_tags:
            entries.append(
                (
                    entry.scan,
                    "a_hold",
                    PlanStep(
                        kind="force",
                        scan=entry.scan,
                        scans=0,
                        inputs=tuple(force_tags),
                        label=", ".join(t for t, _v in force_tags),
                        notes=_notes_for(force_tags),
                    ),
                )
            )
        if pulse_tags:
            entries.append(
                (
                    entry.scan,
                    "a_hold",
                    PlanStep(
                        kind="pulse",
                        scan=entry.scan,
                        scans=0,
                        inputs=tuple((t, True) for t, _v in pulse_tags),
                        label=", ".join(t for t, _v in pulse_tags),
                        notes=_notes_for(pulse_tags),
                    ),
                )
            )

    entries.sort(key=lambda e: (e[0], e[1]))
    return tuple(step for _, _, step in entries)


def _commit_and_monitor(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
    observe: _ObserveFn,
) -> Iterator[PilotEvent]:
    """Commit an accepted trial, then ASSESS it (progress.py).

    VERIFY already ran inside the Act's ``_try_*`` wrapper to mint this trial;
    RECORD (``_record_attempt``) already committed its knowledge.  Here the world
    advances and ``_monitor_trend`` runs the ASSESS phase — trend, checkpoint,
    revert — whose regression arm escalates to Investigate.
    """
    # Capture a satisfied bearing's launch world before commit. Its landing
    # is provisional until ordinary progress is banked; an Alarm ejection must
    # replays from this exact source with its PilotRungs, not an older trend CP.
    _anchor_bearing_receipt(trial, frame, state, dbg)

    # RECORD may have installed an excursion correction after VERIFY minted the
    # trial.  The accepted world key must describe that effective rung overlay,
    # not the pre-correction one used by the diagnostic fork.
    if trial.new_key is not None:
        assert state.key_config is not None
        trial = replace(
            trial,
            new_key=_pilot_world_key(trial.fork_snap, state.key_config, state.rungs),
        )
    _commit_trial(trial, state, ctx, observe, frame.snap)
    _record_step_context(trial, frame, state)
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
    yield from _monitor_trend(trial, frame, state, ctx, dbg)


def _commit_trial(
    trial: _TrialResult,
    state: _PilotState,
    ctx: _PilotContext,
    observe: _ObserveFn,
    before: dict[str, Any],
) -> None:
    observe(trial.observe_label, before, trial.fork)
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
    # ``steps`` is a persistent vector; stage the append on a plain list, then
    # assign it back through the world (``_commit_step`` appends in place).
    work_steps = list(state.steps)
    prev = len(work_steps)
    state.work = _commit_step(
        state.work,
        trial.fork,
        step_inputs,
        trial.scan_before,
        work_steps,
        ctx.resting,
        ctx.edge_tags,
        ctx.live,
    )
    state.steps = work_steps
    # Mirror the freshly-appended step(s) into the append-only journey; ``steps``
    # (the world) is restored to the checkpoint's on revert, ``journey`` is not.
    state.journey.extend(work_steps[prev:])
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
            or (state.gauge is not None and state.gauge.ordinal_advanced(before, trial.fork_snap))
        )
        if productive:
            state.dwell_scans += state.work.state.scan_id - trial.scan_before


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
        "nogoods": frozenset(state.nogoods.get(frame.key, set())),
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
    }


def _candidate_payload(candidate: _Candidate) -> dict[str, Any]:
    return {
        "tag": candidate.tag,
        "value": candidate.value,
        "pair": candidate.pair,
        "influence_prescribed": candidate.influence_prescribed,
        "route_prescribed": candidate.route_prescribed,
        "bearing_channel_tag": candidate.bearing_channel_tag,
        "bearing_channel_value": candidate.bearing_channel_value,
        # A program-owned current (currents.py): the one operator action the
        # program is dwelling on at the current state — recorded with its
        # recognition note so "why InterlockAck here" is readable off the
        # candidate event.
        "current_prescribed": candidate.current_prescribed,
        "current_note": candidate.current_note,
        "provenance": candidate.provenance,
        "wake": candidate.wake,
        # Rank rationale (recording only): why this candidate sorted where it did.
        # ``prescribed`` edges bypass scoring — ``scored`` is False and the three
        # rank dimensions carry the forced bypass values, not measured ones.
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
    *,
    skiff_decline: str | None = None,
) -> dict[str, Any]:
    """The Knowledge half of the loop's state — the fields that survive revert and
    are threaded onto :class:`Plan` (recording only).  ``journey`` is already in the
    finished event's data (it is the world-restored step log); these are the rest:
    holds installed, relational lever notes, and the honest-decline evidence."""
    return {
        "hold_log": tuple(state.hold_log),
        "lever_notes": dict(state.lever_notes),
        # Public recording stays singular for compatibility, but the internal
        # evidence is world-keyed.  Callers pass only the decline applicable to
        # the terminal frame; unrelated-world captions never leak onto the Plan.
        "skiff_decline": skiff_decline,
        "avoid_names": tuple(sorted(state.avoid_names)),
    }


def _route_plan_payload(plan: CompassPlan | None) -> dict[str, Any] | None:
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
    """Payload for a ``zoom_accepted`` event.

    Surfaces the trial fields that decide downstream monitoring — ``observe_label``
    and the channel tag/value — so a consumer can tell a genuine coast from a
    terminal-letrun *ejection* (``ejected``) without re-deriving it.  The event
    name is kept stable for existing consumers; the ``ejected`` flag is the
    honest signal that an AMBIENT_DRIFT was committed under it.

    ``zoom_target_value`` is the *requested* bearing; ``zoom_actual_value`` is
    where the channel actually **landed** after the coast settled (matching the
    ZOOM-STALL gate event's field name).  A coast that overshot (requested 6,
    landed 8 under ``ejected``) must record both — surfacing only the requested
    value made that class of regression read as a clean advance.
    """
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
    candidate: _Candidate,
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
        "index": None,
        "candidate": trial.candidate,
        "candidate_detail": _candidate_payload(candidate),
        "applied": trial.applied,
        "co_actions": _co_actions(candidate, trial.applied),
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


# A stuck state key earns a bounded number of skiff (ORIENT last-tier) laps before
# the loop stops honestly.  One lap is enough for a small-domain live-guard frontier
# (the skiff gate learns its pair edge in a single round); the budget only bounds
# the pathological case — a huge free-word / config-word probe space that would
# otherwise accumulate fresh probe marks forever while the world never moves.
_SKIFF_KEY_BUDGET = 2


def _orient_escalate_skiff(
    reason: str,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> Generator[PilotEvent, None, bool]:
    """ORIENT's hardest reading tier — send out the skiff.

    The reading-escalation ladder is trace transparent → trace opaque-but-constant
    value graph (both in ``_prepare_iteration``) → let-run dwell (an Act tier) →
    **skiff**, the last tier.  The skiff fires only at a *stuck exit*: when
    no static instrument produced a bearing, run isolated fork-pin-step experiments
    over the live-guard frontier and feed any observed edges into the compass as
    bearings (never a plan).

    This owns that tier's decision — probe, apply observations at RECORD, emit the
    ``skiff`` event — for **both** stuck exits (no-bearing and all-rejected).  Use
    via ``yield from``: it yields the ``skiff`` event when observations were learned
    and *returns* whether the caller should ``continue`` (re-orient next iteration)
    or fall through to the terminal ``stuck`` exit.  ``reason`` is the only thing
    the two sites differ on; the event order is byte-identical to the inlined form.

    **Exhausted-key escalation rule (the skiff row of the trigger table).** A skiff
    round buys another orient lap only when it changed knowledge (``Compass.apply``'s
    no-new-knowledge signal — an identical re-probe adds nothing, so it must not spin)
    **and** the per-key skiff budget is unspent.  The world reverts between laps but
    ``stuck_keys`` (Knowledge) does not: re-arriving stuck at the same key with only
    fresh probe-mark churn means the skiff is not moving the world, so after
    ``_SKIFF_KEY_BUDGET`` laps the loop STOPS honestly (the caller falls to the
    terminal ``stuck`` dump) rather than alternating let-run ↔ terminal-dwell forever.
    """
    skiff_obs = probe_live_guard_frontiers(frame, state, ctx)
    before = ctx.compass
    ctx.compass, changed = before.apply(skiff_obs)
    laps = state.stuck_keys.get(frame.key, 0)
    if skiff_obs and changed and laps < _SKIFF_KEY_BUDGET:
        state.stuck_keys[frame.key] = laps + 1
        yield PilotEvent(
            "skiff",
            state.work.state.scan_id,
            {"observations": len(skiff_obs), "reason": reason},
        )
        return True
    return False


def _pilot_loop_events(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    edge_tags: set[str],
    resting: dict[str, Any],
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    evidence: TransitionEvidence | None = None,
    key_config: _StateKeyConfig | None = None,
    influence: Compass | None = None,
    opaque_loop: frozenset[str] = frozenset(),
    route: TraceChoice | None = None,
    blocked_route_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_scans: int = 3000,
    live: bool = False,
    debug: bool = False,
    avoid_pred: Any = None,
    via_pred: Any = None,
    target_predicate: Any = None,
) -> Iterator[PilotEvent]:
    """Run the PILOT loop as a structured event stream."""
    ctx = _make_pilot_context(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        evidence=evidence,
        influence=influence,
        opaque_loop=opaque_loop,
        route=route,
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        target_predicate=target_predicate,
    )
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
        profile.accumulator.name
        for profile, _instr in iter_profiles(program, harness=getattr(plc, "_harness", None))
    )
    state = _PilotState(
        world=_World(
            work=plc,
            steps=pvector([]),
            step_contexts=pvector([]),
            best_trend=None,
            rungs=pvector([]),
            dwell_scans=0,
        ),
        key_config=key_config,
        seen_keys=set(),
        nogoods={},
        checkpoints=[],
        watch_tags=[],
    )
    # The target-relative progress gauge (gauge.py): event-earned
    # ordinals the threshold-masked search key deliberately aliases.  Static
    # for the loop's life; knowledge side (never reverted).  Best-effort — an
    # an empty gauge degrades every consumer to its earlier behavior.
    try:
        state.gauge = build_gauge(
            pdg,
            program,
            target_tag,
            key_config,
            steerable=steerable,
            clear_only=ctx.clear_only,
            edge_tags=frozenset(edge_tags),
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            channel_tags=frozenset(role.channel_tag for role in ctx.pipeline_roles),
            harness=getattr(plc, "_harness", None),
        )
    except Exception:  # noqa: BLE001 — diagnostics must not break the drive
        logger.debug("pilot: gauge build failed", exc_info=True)

    def _dbg(msg: str) -> None:
        return None

    def _dbg_observe(label: str, before: dict[str, Any], after: PLC) -> None:
        return None

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

    # One turn of the loop runs five phases, interleaved per Act rather than laid
    # out linearly (a rejected Act falls through to the next in the same turn):
    #   ORIENT   — read the charts, consult the compass for a bearing (below).
    #   ACT      — steer toward the bearing (steer.py); each Act is followed by →
    #   RECORD   — _record_attempt, the sole compass write path, before revert; →
    #   VERIFY   — the trial's gate verdict (verify.py / outcome.py), run inside the
    #              _try_* wrappers, then →
    #   ASSESS   — _monitor_trend (progress.py) via _commit_and_monitor.
    # Compass is a noun (the knowledge store), never a phase; Investigate is an
    # escalation inside ASSESS's regression arm, not a phase of its own.
    # The budget charges *searching*, never *waiting*: committed scan-ids minus
    # the accepted-coast dwell credit (see ``_World.dwell_scans``).  An armed
    # self-advancing dwell — a 39k-scan dry timer the coast rides — is the
    # machine doing its own work, not the pilot spending effort.
    while state.work.state.scan_id - state.dwell_scans < ctx.max_scans:
        snap = dict(state.work.state.tags)
        if target_reached(snap, ctx.target_tag, ctx.target_value, ctx.target_predicate):
            if state.steps:
                # The terminal let-run's span extends to the actual finish scan;
                # rewrite the last step (and its journey twin, the same object) so
                # both the clean path and the journey carry the true coast length.
                final_step = _Step(
                    inputs=state.steps[-1].inputs,
                    scan_before=state.steps[-1].scan_before,
                    scan_after=state.work.state.scan_id,
                )
                if state.journey and state.journey[-1] is state.steps[-1]:
                    state.journey[-1] = final_step
                state.steps = state.steps.set(len(state.steps) - 1, final_step)
            yield PilotEvent(
                "finished",
                state.work.state.scan_id,
                {
                    "reached": True,
                    "steps": tuple(state.steps),
                    "journey": tuple(state.journey),
                    "knowledge": _knowledge_payload(state),
                    "work": state.work,
                    "reason": "target reached",
                    "plan_journal": _build_plan_journal(
                        state, state.work, journal_channel_tags, journal_acc_names
                    ),
                },
            )
            return

        # ═══════════════════════ ORIENT ═══════════════════════
        # Read as hard as the charts require, along the reading-escalation ladder:
        # trace transparent → trace opaque-but-constant value graph (both inside
        # _prepare_iteration) → let-run dwell (an Act tier, below) → skiff
        # (_orient_escalate_skiff, this loop's two stuck exits).  Then consult the
        # compass for a fresh bearing → ranked candidates (_build_candidates).
        frame = _prepare_iteration(state, ctx, _dbg)
        if not state.checkpoints:
            # Seed an entry checkpoint so the first regression — or a terminal
            # let-run ejection from a pre-positioned start (e.g. dropped straight
            # into Execute) — has somewhere to revert to.  "No checkpoint" should
            # mean "go back to the beginning", not "let the ejected state stand".
            state.checkpoints.append(
                _Checkpoint(
                    key=frame.key,
                    world=state.snapshot_world(),
                    trend=frame.distance_before,
                    frontier=frontier_pairs(frame.tree, frame.snap),
                )
            )
        yield from _anchor_provisional(frame, state, _dbg)
        _debug_iteration(frame, state, ctx, _dbg)
        yield PilotEvent(
            "iteration", state.work.state.scan_id, _iteration_payload(frame, state, ctx)
        )
        candidates = _build_candidates(frame, state, ctx, _dbg)
        yield PilotEvent(
            "candidates_built",
            state.work.state.scan_id,
            _candidates_built_payload(candidates),
        )

        # ── Stuck: instruments can't read the bearing ──
        if candidates.stuck_reason is not None:
            # Escalate to the skiff before declaring terminal (ORIENT's hardest
            # reading tier — owned by _orient_escalate_skiff): on live-guard
            # frontiers (unreadable writer guards) run isolated probes and feed
            # observed edges into the compass — bearings only; the next iteration
            # proposes them as candidates and the verify pipeline confirms live.
            # Zero new observations -> genuinely stuck.
            if (yield from _orient_escalate_skiff(candidates.stuck_reason, frame, state, ctx)):
                continue
            skiff_decline = state.skiff_declines.get(frame.key)
            terminal_reason = (
                skiff_decline
                or ("stuck: " + _with_avoid_reason(candidates.stuck_reason, state, ctx, frame))
            ) + _frontier_clause(frame)
            yield PilotEvent(
                "stuck",
                state.work.state.scan_id,
                {
                    "reason": terminal_reason,
                    "distance": frame.distance_before,
                    "candidate_count": 0,
                    "nogoods_at_key": len(state.nogoods.get(frame.key, set())),
                    "terminal": True,
                },
            )
            if state.checkpoints:
                state.load_world(state.checkpoints[-1].world)
            yield PilotEvent(
                "finished",
                state.work.state.scan_id,
                {
                    "reached": False,
                    "steps": tuple(state.steps),
                    "journey": tuple(state.journey),
                    "knowledge": _knowledge_payload(state, skiff_decline=skiff_decline),
                    "work": state.work,
                    "reason": terminal_reason,
                    "plan_journal": _build_plan_journal(
                        state, state.work, journal_channel_tags, journal_acc_names
                    ),
                },
            )
            return

        # ═══════════════════════ ACT ═══════════════════════
        # Steer toward the bearing (steer.py), trying each Act in turn until one is
        # accepted: zoom → skiff-prescribed batch → command candidates → widening →
        # terminal let-run/dwell.  Every _try_* wrapper runs VERIFY (verify.py /
        # outcome.py) internally to produce a trial; each Act is then followed by
        # RECORD (_record_attempt) and, on acceptance, ASSESS (_commit_and_monitor →
        # _monitor_trend, progress.py — where Investigate escalates on regression).
        accepted = False

        # ── Establish prerequisites (level holds — steerable inputs, not state) ──
        if candidates.prerequisite_rungs:
            state.rungs = _append_rungs(
                state.work, list(candidates.prerequisite_rungs), state.rungs
            )
            state.hold_log.append(
                _HoldLogEntry(
                    scan=state.work.state.scan_id,
                    tags=tuple((r.dest, r.value) for r in candidates.prerequisite_rungs),
                    source="prerequisite",
                )
            )

        # ── Act: zoom (timer-gated frontier) ──
        if candidates.wait_prescribed:
            yield PilotEvent(
                "zoom",
                state.work.state.scan_id,
                {
                    "prescribed": True,
                    "reason": candidates.wait_reason,
                    "prerequisite_rungs": candidates.prerequisite_rungs,
                    "channel_tag": (
                        candidates.route_plan.role.channel_tag
                        if candidates.route_plan is not None
                        else None
                    ),
                },
            )
            attempt = _try_zoom(candidates, frame, state, ctx, _dbg)
            _record_attempt(attempt, frame, state, ctx)
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "zoom_accepted",
                    trial.fork.state.scan_id,
                    _zoom_accepted_payload(trial),
                )
                yield from _commit_and_monitor(trial, frame, state, ctx, _dbg, _dbg_observe)
                accepted = True
            else:
                yield PilotEvent(
                    "zoom_rejected",
                    state.work.state.scan_id,
                    {"gates": attempt.gate_events},
                )

        # ── Act: skiff-prescribed batch (composite learned edge) ──
        if not accepted and candidates.prescribed_batch:
            attempt = _try_prescribed_batch(candidates.prescribed_batch, frame, state, ctx, _dbg)
            _record_attempt(attempt, frame, state, ctx)
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "batch_accepted",
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
                yield from _commit_and_monitor(trial, frame, state, ctx, _dbg, _dbg_observe)
                accepted = True

        # ── Act: command candidates ──
        if not accepted:
            for ci, candidate in enumerate(candidates.candidates):
                applied = _candidate_applied(candidate, candidates, ctx)
                yield PilotEvent(
                    "candidate_try",
                    state.work.state.scan_id,
                    {
                        "index": ci,
                        "total": len(candidates.candidates),
                        "candidate": _candidate_payload(candidate),
                        "applied": applied,
                        "co_actions": _co_actions(candidate, applied),
                    },
                )
                attempt = _try_candidate(candidate, candidates, frame, state, ctx, _dbg)
                _record_attempt(attempt, frame, state, ctx)
                if attempt.trial is None:
                    yield PilotEvent(
                        "candidate_rejected",
                        state.work.state.scan_id,
                        {
                            "index": ci,
                            "candidate": _candidate_payload(candidate),
                            "applied": applied,
                            "co_actions": _co_actions(candidate, applied),
                            "gates": attempt.gate_events,
                        },
                    )
                    continue
                trial = attempt.trial
                accepted_payload = _accepted_payload(candidate, trial, frame, state)
                accepted_payload["index"] = ci
                yield PilotEvent(
                    "candidate_accepted",
                    trial.fork.state.scan_id,
                    accepted_payload,
                )
                yield from _commit_and_monitor(trial, frame, state, ctx, _dbg, _dbg_observe)
                accepted = True
                break

        # ── Widening fallback ──
        if (
            not accepted
            and not candidates.wait_prescribed
            and len(candidates.active_trace_actions) >= 2
        ):
            attempt = _try_widening(candidates.active_trace_actions, frame, state, ctx, _dbg)
            _record_attempt(attempt, frame, state, ctx)
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "widening_accepted",
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
                yield from _commit_and_monitor(trial, frame, state, ctx, _dbg, _dbg_observe)
                accepted = True

        if accepted:
            state.last_wait_log = None
            continue

        # ── Terminal let-run (generalized: hold macro-state, coast to target) ──
        # No route, no candidate, no widening — but the cone is still live.  Hold
        # the current macro-state and let the program's self-advancing frontier
        # coast toward the global target.  Reached -> CONFIRMED; the program
        # leaving the held macro-state -> AMBIENT_DRIFT, handed to investigation.
        if frame.key in state.letrun_memo:
            # A trusted receipt already covers this world key: the coast ejected
            # here (deterministic re-eject — the world key includes the rung
            # overlay, so only a new hold re-opens it) or stalled quiescent (no
            # pending effects, so the masked key genuinely captured the world).
            # Re-running its ejection-guard coast would only re-eject and
            # re-investigate.  Do ONE verified
            # cone-settle dwell instead: a self-advancing frontier that crosses the
            # target during the dwell is CONFIRMED through the shared verify target
            # gate; anything else is a legible terminal stall that falls through to
            # the skiff / stuck exit below.  (This replaces a bare _settle_cone on
            # state.work — the one execution that skipped verify.)
            yield PilotEvent(
                "zoom",
                state.work.state.scan_id,
                {
                    "prescribed": True,
                    "reason": "terminal dwell (re-coast skip: let-run already tried at key)",
                    "prerequisite_rungs": (),
                    "channel_tag": ctx.target_tag,
                },
            )
            attempt = _try_terminal_dwell(frame, state, ctx, _dbg)
            _record_attempt(attempt, frame, state, ctx)
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "zoom_accepted",
                    trial.fork.state.scan_id,
                    _zoom_accepted_payload(trial),
                )
                yield from _commit_and_monitor(trial, frame, state, ctx, _dbg, _dbg_observe)
                state.last_wait_log = None
                continue
            # No new input is possible here, so a dwell that settled short of the
            # target is terminal — fall through to the shared skiff / stuck exit
            # rather than looping (the dwell forked, so state.work is unchanged).
            yield PilotEvent(
                "zoom_rejected",
                state.work.state.scan_id,
                {"gates": attempt.gate_events},
            )
        else:
            yield PilotEvent(
                "zoom",
                state.work.state.scan_id,
                {
                    "prescribed": True,
                    "reason": "terminal let-run (hold macro-state, coast to target)",
                    "prerequisite_rungs": (),
                    "channel_tag": ctx.target_tag,
                },
            )
            attempt = _try_terminal_letrun(frame, state, ctx, _dbg)
            _record_attempt(attempt, frame, state, ctx)
            if attempt.trial is not None:
                trial = attempt.trial
                # A committed coast (ejection handed to investigation, or target
                # reached) is deterministic at this pre-coast world key: memo it
                # so a post-revert re-arrival dwells instead of re-ejecting.
                state.letrun_memo[frame.key] = (
                    trial.coast_receipt.stop_reason
                    if trial.coast_receipt is not None
                    else "committed"
                )
                yield PilotEvent(
                    "zoom_accepted",
                    trial.fork.state.scan_id,
                    _zoom_accepted_payload(trial),
                )
                yield from _commit_and_monitor(trial, frame, state, ctx, _dbg, _dbg_observe)
                state.last_wait_log = None
                continue
            # Stall: memoize only a *quiescent* stall.  A stall with pending
            # effects (a timer mid-flight when the budget ran out) stays
            # re-runnable — the world key masks accumulators, and a same-key
            # world could complete where this one timed out (audit C2).  The
            # re-run cost is bounded by the skiff key budget.
            if attempt.stall_receipt is not None and not attempt.stall_pending:
                state.letrun_memo[frame.key] = attempt.stall_receipt.stop_reason
            yield PilotEvent(
                "zoom_rejected",
                state.work.state.scan_id,
                {"gates": attempt.gate_events},
            )

        # ── Stuck: all candidates rejected, terminal let-run failed ──
        # Same skiff escalation as the no-bearing exit above (ORIENT's hardest
        # reading tier, _orient_escalate_skiff): unreadable-guard frontiers get one
        # round of isolated probes before the loop gives up.
        if (yield from _orient_escalate_skiff("all_rejected", frame, state, ctx)):
            continue
        stuck_reason = _diagnose_stuck(frame, candidates, state, ctx)
        # A free-word decline discovered while the skiff surveyed the remaining
        # tree is useful world-scoped knowledge, but it is not the cause of an
        # all-rejected exit.  Keep the actual rejection class as the headline;
        # the applicable decline remains available on Plan.skiff_decline.
        skiff_decline = state.skiff_declines.get(frame.key)
        terminal_reason = f"stuck: {stuck_reason}" + _frontier_clause(frame)
        yield PilotEvent(
            "stuck",
            state.work.state.scan_id,
            {
                "reason": terminal_reason,
                "distance": frame.distance_before,
                "candidate_count": len(candidates.candidates),
                "nogoods_at_key": len(state.nogoods.get(frame.key, set())),
                "terminal": True,
            },
        )
        if state.checkpoints:
            state.load_world(state.checkpoints[-1].world)
        yield PilotEvent(
            "finished",
            state.work.state.scan_id,
            {
                "reached": False,
                "steps": tuple(state.steps),
                "journey": tuple(state.journey),
                "knowledge": _knowledge_payload(state, skiff_decline=skiff_decline),
                "work": state.work,
                "reason": terminal_reason,
            },
        )
        return

    # ── Budget exhausted: the work fork ran past max_scans ──
    # A dwell that drains the budget is a stall, not a wrap-up: route the
    # terminal through a fresh frame so the reason names the outstanding
    # frontier, and revert to the last checkpoint like the stuck exits do
    # ("How we fail" #1 — every stop points at a named leaf).
    snap = dict(state.work.state.tags)
    reached = target_reached(snap, ctx.target_tag, ctx.target_value, ctx.target_predicate)
    terminal_skiff_decline: str | None = None
    if reached:
        reason = "target reached"
    else:
        frame = None
        try:
            frame = _prepare_iteration(state, ctx, _dbg)
        except Exception:  # noqa: BLE001 — terminal diagnostics never mask the exit
            logger.debug("budget terminal: frontier trace raised", exc_info=True)
        reason = _with_avoid_reason(
            f"budget exhausted ({ctx.max_scans} scans searched + {state.dwell_scans} waited)",
            state,
            ctx,
            frame,
        ) + _frontier_clause(frame)
        if frame is not None:
            terminal_skiff_decline = state.skiff_declines.get(frame.key)
        yield PilotEvent(
            "stuck",
            state.work.state.scan_id,
            {
                "reason": reason,
                "distance": frame.distance_before if frame is not None else None,
                "candidate_count": 0,
                "nogoods_at_key": (
                    len(state.nogoods.get(frame.key, set())) if frame is not None else 0
                ),
                "terminal": True,
            },
        )
        if state.checkpoints:
            state.load_world(state.checkpoints[-1].world)
    yield PilotEvent(
        "finished",
        state.work.state.scan_id,
        {
            "reached": reached,
            "steps": tuple(state.steps),
            "journey": tuple(state.journey),
            "knowledge": _knowledge_payload(
                state,
                skiff_decline=terminal_skiff_decline,
            ),
            "work": state.work,
            "reason": reason,
            "plan_journal": _build_plan_journal(
                state, state.work, journal_channel_tags, journal_acc_names
            ),
        },
    )


def _pilot_loop(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    edge_tags: set[str],
    resting: dict[str, Any],
    *,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    evidence: TransitionEvidence | None = None,
    key_config: _StateKeyConfig | None = None,
    influence: Compass | None = None,
    opaque_loop: frozenset[str] = frozenset(),
    route: TraceChoice | None = None,
    blocked_route_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_scans: int = 3000,
    live: bool = False,
    debug: bool = False,
    avoid_pred: Any = None,
    via_pred: Any = None,
    target_predicate: Any = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> tuple[bool, list[_Step], list[_Step], PLC, tuple[PlanStep, ...], str | None, dict[str, Any]]:
    """Run the PILOT loop and return
    ``(reached, steps, journey, work, journal, reason, knowledge)``.

    ``steps`` is the clean, sequentially-replayable path; ``journey`` is the full
    attempt log (incl. reverted rounds) for ``debug=True``.  ``journal`` is the
    annotated step sequence for the Plan repr.  ``reason`` is the terminal
    diagnostic the loop named on failure (``stuck: …`` / ``budget exhausted``),
    ``None`` when the target was reached — so ``how()`` never returns a silent
    unreachable.  ``knowledge`` is the Knowledge half threaded onto :class:`Plan`
    (``hold_log`` / ``lever_notes`` / ``skiff_decline`` / ``avoid_names``) —
    recording only (see :func:`_knowledge_payload`).
    """
    final: PilotEvent | None = None
    for event in _pilot_loop_events(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        evidence=evidence,
        key_config=key_config,
        influence=influence,
        opaque_loop=opaque_loop,
        route=route,
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        target_predicate=target_predicate,
    ):
        if on_event is not None:
            on_event(event)
        if event.kind == "finished":
            final = event

    if final is None:
        return False, [], [], plc, (), None, {}
    reached = bool(final.data["reached"])
    return (
        reached,
        list(final.data["steps"]),
        list(final.data.get("journey", ())),
        final.data["work"],
        tuple(final.data.get("plan_journal", ())),
        None if reached else final.data.get("reason"),
        dict(final.data.get("knowledge", {})),
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
        f"{links} — the harness holds the sensor lockstep with its driver, so it "
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
    """Pick the deterministic default route for a multi-route value target.

    Works for any concrete equality target — ``Bool == True``, ``Bool == False``,
    or a word ``tag == value``; a live relational predicate gets no route (see
    :func:`_target_is_value_route`).  ``how()`` never reports ambiguous: it
    enumerates the routes, prunes any that ``avoid=`` forbids or that ``via=``
    does not pass through, then locks the cheapest survivor (gate-eligible routes
    preferred, trace score next, rung order breaking ties) and records the rest
    as redirectable pivots on the returned :class:`RouteTaken`.

    Returns ``(route_lock, blocked_route_actions, route_taken)``.  All ``None``/
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
    choices = enumerate_trace_choices(
        target_tag, target_value, snapshot, pdg, program, steerable=steerable, clear_only=clear_only
    )
    if not choices:
        return None, frozenset(), None

    # Trace each route once; prune by avoid (route forces it) / via (route does
    # not force it), then rank the survivors.
    traced: list[tuple[TraceChoice, list[TraceNode]]] = []
    for ch in choices:
        tree = trace_back(
            target_tag,
            target_value,
            snapshot,
            pdg,
            program,
            steerable,
            clear_only=clear_only,
            opaque_loop=opaque_loop,
            route=ch,
        )
        if avoid_pred is not None and _route_forces([tree], snapshot, avoid_pred):
            continue
        if via_pred is not None and not _route_forces([tree], snapshot, via_pred):
            continue
        traced.append((ch, [tree]))
    if not traced:
        return None, frozenset(), None

    # Cross-route contradiction baseline: an identical conflict witness (tag,
    # incompatible value sets, and trace sources) shared by *every* route is
    # inherent to the goal — an SFC sequencing S_StateCurrent 3→6 shows up on all
    # of them.  A witness unique to a route is that route's own contradiction (a
    # manual-mode caller gate over a body that needs production mode), and it can
    # never be satisfied — yet an already-held gate makes such a route look cheap
    # to the trace scorer.  Witnesses must not collapse to tag names: common
    # ``Mode 0 ↔ 1`` sequencing must not hide Manual's distinct ``Mode 3 ↔ 1``.
    route_conflicts = [
        frozenset().union(*(_route_conflicts(n, pdg, program) for n in nodes))
        if nodes
        else frozenset()
        for _, nodes in traced
    ]
    shared_conflicts = frozenset.intersection(*route_conflicts) if route_conflicts else frozenset()

    def _rank(indexed: tuple[int, tuple[TraceChoice, list[TraceNode]]]) -> tuple[Any, ...]:
        idx, (ch, nodes) = indexed
        unique_conflicts = len(route_conflicts[idx] - shared_conflicts)
        eligible = bool(ch.writer_locks) and writer_route_eligible(
            ch.writer_locks[0][2], target_tag, pdg, program, steerable
        )
        return (
            unique_conflicts,
            0 if eligible else 1,
            _trace_score(nodes, pdg),
            route_rung_order(ch),
        )

    order = sorted(range(len(traced)), key=lambda i: _rank((i, traced[i])))
    traced = [traced[i] for i in order]
    default = traced[0][0]
    survivors = tuple(ch for ch, _ in traced)
    route_taken = _build_route_taken(default, survivors, steerable)
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


def _build_pilot_context(
    program: Any,
    snapshot: dict[str, Any],
) -> tuple[
    dict[str, tuple[Any, ...]] | None,
    _StateKeyConfig | None,
    TransitionEvidence | None,
    tuple[dict[str, list[Any]], dict[str, str]] | None,
]:
    """Build prover context for nd_domains and state key projection.

    Returns ``(nd_domains, key_config, evidence, semantic)`` where ``semantic``
    is ``(atom_index, domain_sources)`` for path-render constraint annotation.
    Values are ``None`` on failure — pilot falls back to Bool-only probing,
    pivot-tag state keys, local static evidence, and raw (un-annotated) path
    rendering.
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
            return None, None, None, None
        nd = getattr(ctx, "nondeterministic_dims", None)
        evidence = build_transition_evidence(ctx)
        if nd:
            logger.info("pilot: nd_domains ready (%d dims)", len(nd))

        # Semantic metadata for path-render constraint annotations, derived from
        # the same ctx (no extra kernel compile).  Best-effort: on failure the
        # path renders with raw representatives instead of (> 75) / A > B.
        semantic: tuple[dict[str, list[Any]], dict[str, str]] | None
        try:
            from pyrung.core.analysis.prove import _build_semantic_metadata

            semantic = _build_semantic_metadata(ctx, program)
        except Exception:  # noqa: BLE001
            logger.debug("pilot: semantic metadata build failed", exc_info=True)
            semantic = None

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
            return nd, None, evidence, semantic

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
        return nd, key_config, evidence, semantic
    except Exception:  # noqa: BLE001
        logger.debug("pilot: context build failed", exc_info=True)
        return None, None, None, None


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
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value, target_predicate = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork, unlink=unlink)
    ref_consts = compute_reference_constants(pdg, program, fork._known_tags_by_name)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence, _semantic = _build_pilot_context(
        program, dict(fork.state.tags)
    )
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    route_lock, blocked_route_actions, _route_taken = _prepare_route(
        fork,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        opaque_loop,
        target_predicate=target_predicate,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )

    yield from _pilot_loop_events(
        fork,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        evidence=evidence,
        key_config=key_config,
        influence=inf,
        opaque_loop=opaque_loop,
        route=route_lock,
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        live=False,
        debug=False,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        target_predicate=target_predicate,
    )


def pilot_how(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    debug: bool = False,
    avoid_pred: Any = None,
    via_pred: Any = None,
    unlink: list[str] | None = None,
    on_event: Callable[[PilotEvent], None] | None = None,
) -> Plan:
    """PILOT on a fork — drive to the target and return the recording. Nothing changes.

    For a multi-route value target (``Bool == True/False`` or word
    ``tag == value``) PILOT picks a deterministic default route and records it on
    ``Plan.route``; ``avoid_pred``/``via_pred`` redirect off/onto a route (the
    engineer names the alternative from ``Plan.route``).

    ``unlink`` names harness-synthesized feedback tags to free for fault
    injection: the Harness stops driving them and they become steerable, so
    PILOT can reach faults that the intact physical link would otherwise hold
    out of reach (e.g. a dead flow sensor with the valve open).
    """
    from pyrung.core.analysis.pdg import build_program_graph

    targets = _parse_targets(*conditions)
    if len(targets) > 1:
        return _pilot_how_multi(
            plc,
            targets,
            max_scans=max_scans,
            debug=debug,
            avoid_pred=avoid_pred,
            via_pred=via_pred,
            unlink=unlink,
        )
    target_tag, target_value, target_predicate = targets[0]
    program = plc._program

    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork, unlink=unlink)
    ref_consts = compute_reference_constants(pdg, program, fork._known_tags_by_name)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    anchor_scan = fork.state.scan_id
    diag_snapshot = dict(fork.state.tags)
    nd_domains, key_config, evidence, _semantic = _build_pilot_context(program, diag_snapshot)
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    route_lock, blocked_route_actions, route_taken = _prepare_route(
        fork,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        opaque_loop,
        target_predicate=target_predicate,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )

    reached, _steps, _journey, work, journal, loop_reason, knowledge = _pilot_loop(
        fork,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        evidence=evidence,
        key_config=key_config,
        influence=inf,
        opaque_loop=opaque_loop,
        route=route_lock,
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        debug=debug,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        target_predicate=target_predicate,
        on_event=on_event,
    )

    reason = (
        None
        if reached
        else _linked_feedback_block(
            target_tag,
            target_value,
            diag_snapshot,
            pdg,
            program,
            steerable,
            _harness_couplings(fork),
        )
        # Fall back to the loop's own terminal diagnostic (``stuck: …`` /
        # ``budget exhausted``) so an unreachable target always carries a reason
        # rather than surfacing as a silent ``reachable=False, reason=None``.
        or loop_reason
    )
    return Plan(
        reachable=reached,
        target_tag=target_tag,
        target_value=target_value,
        fork=work if reached else None,
        reason=reason,
        route=route_taken if reached else None,
        journal=journal,
        anchor_scan=anchor_scan,
        journey=tuple(_journey),
        hold_log=knowledge.get("hold_log", ()),
        lever_notes=knowledge.get("lever_notes", {}),
        skiff_decline=knowledge.get("skiff_decline"),
        avoid_names=knowledge.get("avoid_names", ()),
    )


def _pilot_how_multi(
    plc: PLC,
    targets: list[tuple[str, Any, Any]],
    *,
    max_scans: int = 3000,
    debug: bool = False,
    avoid_pred: Any = None,
    via_pred: Any = None,
    unlink: list[str] | None = None,
) -> Plan:
    """Multi-target ``how(A, B, …)`` — reach one committed scan where every target holds.

    Static read only (``pilot/multitarget.py``): a sound mutual-exclusion prune +
    a clobberer-first order, then the single-target drive loop is run
    sequentially per target on ONE fork.  The fork's recording is the artifact —
    it replays to a state with every target true.  When the static read cannot
    prove ME it falls open to this drive; the final all-targets check is the
    honest oracle (the drive loop is execution truth, never a skiff probe).
    """
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.pilot import multitarget as _mt  # noqa: PLC0415

    program = plc._program
    label = " & ".join(f"{tt}={tv!r}" for tt, tv, _ in targets)

    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork, unlink=unlink)
    ref_consts = compute_reference_constants(pdg, program, fork._known_tags_by_name)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    anchor_scan = fork.state.scan_id
    diag_snapshot = dict(fork.state.tags)
    nd_domains, key_config, evidence, _semantic = _build_pilot_context(program, diag_snapshot)
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)

    goal_pairs = tuple((tt, tv) for tt, tv, _ in targets)

    ok, reason, ordered = _mt.analyze(diag_snapshot, pdg, program, steerable, targets)
    if not ok:
        return Plan(
            reachable=False,
            target_tag=label,
            target_value=True,
            targets=goal_pairs,
            reason=reason,
            anchor_scan=anchor_scan,
        )

    work = fork
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
        route_lock, blocked_route_actions, _route_taken = _prepare_route(
            work,
            t_tag,
            t_val,
            pdg,
            program,
            steerable,
            opaque_loop,
            target_predicate=t_pred,
            avoid_pred=avoid_pred,
            via_pred=via_pred,
        )
        reached, _steps, _journey, work, journal_leg, loop_reason, last_knowledge = _pilot_loop(
            work,
            t_tag,
            t_val,
            pdg,
            program,
            steerable,
            edge_tags,
            resting,
            nd_domains=nd_domains,
            evidence=evidence,
            key_config=key_config,
            influence=inf,
            opaque_loop=opaque_loop,
            route=route_lock,
            blocked_route_actions=blocked_route_actions,
            max_scans=work.state.scan_id + max_scans,
            debug=debug,
            avoid_pred=avoid_pred,
            via_pred=via_pred,
            target_predicate=t_pred,
        )
        last_journey = tuple(_journey)
        journal_steps.extend(journal_leg)
        if not reached:
            detail = f" — {loop_reason}" if loop_reason else ""
            return Plan(
                reachable=False,
                target_tag=label,
                target_value=True,
                targets=goal_pairs,
                reason=(
                    f"pilot: could not establish {t_tag}={t_val!r} while holding the "
                    f"other target(s){detail}"
                ),
                anchor_scan=anchor_scan,
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
            anchor_scan=anchor_scan,
        )
    # recording: threaded from the LAST target's drive only (multi runs the loop
    # sequentially per target; the last drive's Knowledge is what survives on ``work``).
    return Plan(
        reachable=True,
        target_tag=label,
        target_value=True,
        targets=goal_pairs,
        fork=work,
        anchor_scan=anchor_scan,
        journal=tuple(journal_steps),
        journey=last_journey,
        hold_log=last_knowledge.get("hold_log", ()),
        lever_notes=last_knowledge.get("lever_notes", {}),
        skiff_decline=last_knowledge.get("skiff_decline"),
        avoid_names=last_knowledge.get("avoid_names", ()),
    )


def pilot_drive(
    plc: PLC,
    *conditions: Any,
    max_scans: int = 3000,
    debug: bool = False,
    avoid_pred: Any = None,
    via_pred: Any = None,
    unlink: list[str] | None = None,
) -> Plan:
    """PILOT on the live PLC — drive the state there.

    ``unlink`` frees the named harness-feedback tags for fault injection (see
    :func:`pilot_how`).
    """
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value, target_predicate = _parse_target(*conditions)
    program = plc._program

    pdg = build_program_graph(program)
    harness_fb = install_harness(plc, unlink=unlink)
    ref_consts = compute_reference_constants(pdg, program, plc._known_tags_by_name)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, plc._known_tags_by_name, pdg, program)
    anchor_scan = plc.state.scan_id
    diag_snapshot = dict(plc.state.tags)
    nd_domains, key_config, evidence, _semantic = _build_pilot_context(program, diag_snapshot)
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    route_lock, blocked_route_actions, route_taken = _prepare_route(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        opaque_loop,
        target_predicate=target_predicate,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
    )

    reached, _steps, _journey, work, _journal, loop_reason, knowledge = _pilot_loop(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        edge_tags,
        resting,
        nd_domains=nd_domains,
        evidence=evidence,
        key_config=key_config,
        influence=inf,
        opaque_loop=opaque_loop,
        route=route_lock,
        blocked_route_actions=blocked_route_actions,
        max_scans=max_scans,
        live=True,
        debug=debug,
        avoid_pred=avoid_pred,
        via_pred=via_pred,
        target_predicate=target_predicate,
    )

    # A live failure without a harness-link explanation falls back to the
    # loop's own terminal diagnostic (``stuck: …`` / ``budget exhausted``) so
    # an unreachable target always carries a reason ("How we fail" #2).
    reason = (
        None
        if reached
        else (
            _linked_feedback_block(
                target_tag,
                target_value,
                diag_snapshot,
                pdg,
                program,
                steerable,
                _harness_couplings(plc),
            )
            or loop_reason
        )
    )
    return Plan(
        reachable=reached,
        target_tag=target_tag,
        target_value=target_value,
        fork=work if reached else None,
        reason=reason,
        route=route_taken if reached else None,
        anchor_scan=anchor_scan,
        journey=tuple(_journey),
        hold_log=knowledge.get("hold_log", ()),
        lever_notes=knowledge.get("lever_notes", {}),
        skiff_decline=knowledge.get("skiff_decline"),
        avoid_names=knowledge.get("avoid_names", ()),
    )
