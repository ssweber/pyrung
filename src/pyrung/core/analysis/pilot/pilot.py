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
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.graph import Path, ReachabilityStep
from pyrung.core.analysis.pilot._ops import (
    _apply_pulse,
    _DebugFn,
    _install_holds,
    _pilot_state_key,
    _split_holds,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.candidates import (
    _build_candidates,
    _Candidate,
    _candidate_pulse_actions,
    _context_actions,
)
from pyrung.core.analysis.pilot.compass import (
    Compass,
    detect_opaque_loop,
    detect_opaque_pipelines,
)
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.progress import _monitor_trend
from pyrung.core.analysis.pilot.steer import (
    _LETRUN_DWELL_CEILING,
    _cone_tags,
    _settle_cone,
    _try_candidate,
    _try_terminal_letrun,
    _try_widening,
    _try_zoom,
)
from pyrung.core.analysis.pilot.trace import (
    DomainPrior,
    TraceAction,
    TraceChoice,
    _all_nodes,
    _route_forces,
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    compute_steerable,
    enumerate_trace_choices,
    target_reached,
    trace_back,
    trace_relational,
)
from pyrung.core.analysis.pilot.types import (
    PilotEvent,
    TagChange,
    _ActionPair,
    _IterationFrame,
    _ObserveFn,
    _PilotContext,
    _PilotState,
    _Step,
    _TrialResult,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.compass import CompassGraph, CompassPlan
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
    reactive_holds: dict[str, Any] | None = None,
) -> PLC:
    """Record a step (or release+pulse pair) and swap the work fork.

    ``inputs`` is the full applied set (``trial.pulse_actions``), not the narrow
    ``trial.decision``.  A ``rise()``/``fall()`` gate needs an edge — a transition
    — but a recorded ``_Step`` holds its ``inputs`` constant across the step's
    scans and the patch persists into the next step, so the naive replay
    (``patch(inputs); step``) cannot recreate the transition once the edge is
    already at the pulsed level (the consecutive-command case).  PILOT's live
    pulse drops the edge to resting for one scan before raising it
    (``_pulse_actions``); mirror that here by recording an explicit 1-scan release
    step whenever the inputs drive an edge tag *off* resting, so the replay
    reproduces the same edge.

    ``reactive_holds`` (let-run steps only) records the oscillators that animated
    the span, so the recorded path is self-describing — an edge release/pulse
    never carries them (it has no coast).
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
                reactive_holds=dict(reactive_holds) if reactive_holds else {},
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
    choice: TraceChoice | None,
    blocked_choice_actions: frozenset[_ActionPair],
    max_scans: int,
    live: bool,
    debug: bool,
    avoid_pred: Any = None,
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
    return _PilotContext(
        target_tag=target_tag,
        target_value=target_value,
        target_predicate=target_predicate,
        pdg=pdg,
        program=program,
        steerable=steerable,
        edge_tags=edge_tags,
        resting=resting,
        nd_domains=nd_domains,
        domain_prior=domain_prior,
        evidence=evidence,
        compass=compass,
        opaque_loop=opaque_loop,
        pipeline_roles=pipeline_roles,
        pipeline_internal_tags=pipeline_internal_tags,
        choice=choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
        avoid_pred=avoid_pred,
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
    from pyrung.core.analysis.pilot.compass import build_compass_graphs

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
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            choice=ctx.choice,
            prior=ctx.domain_prior,
            avoid_pred=ctx.avoid_pred,
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
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            choice=ctx.choice,
            prior=ctx.domain_prior,
            avoid_pred=ctx.avoid_pred,
            harness=getattr(state.work, "_harness", None),
        )
    _expand_and_seed(tree, state, ctx)
    key_config = _ensure_state_key_config(state, tree, ctx.target_tag)
    if not state.watch_tags:
        state.watch_tags.extend(sorted(tree.pivot_tags()))
        dbg(f"# watch_tags ({len(state.watch_tags)}): {state.watch_tags[:8]}...")

    key = _pilot_state_key(snap, key_config)
    distance_before = tree.unsatisfied_count()
    action_details = tuple(
        TraceAction(
            tag=action.tag,
            value=action.value,
            provenance=action.provenance,
            blast_radius=len(ctx.pdg.downstream_slice(action.tag, follow_calls=True)),
            oscillate=action.oscillate,
        )
        for action in tree.ordered_action_details()
    )
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

    still_need: list[str] = []
    seen_need: set[tuple[str, Any]] = set()
    for n in _all_nodes(frame.tree):
        if (
            not n.satisfied
            and not n.is_steerable
            and not getattr(n, "pipeline_internal", False)
            and n.children
        ):
            cur = frame.snap.get(n.tag)
            if cur != n.value:
                nk = (n.tag, repr(n.value))
                if nk not in seen_need:
                    seen_need.add(nk)
                    still_need.append(f"{n.tag}={n.value!r} (have {cur!r})")
    if still_need:
        dbg(f"# still need ({len(still_need)}): {still_need[:10]}")

    dbg(f"# nogoods for key: {sorted(state.nogoods.get(frame.key, set())) or '(none)'}")
    dbg(f"# forced_holds: {dict(state.forced_holds) if state.forced_holds else '(none)'}")
    dbg(f"# seen_keys: {len(state.seen_keys)}  checkpoints: {len(state.checkpoints)}")
    dbg(f"# trace ordered_actions (raw, {len(frame.raw_trace_actions)}):")
    for t, v in frame.raw_trace_actions:
        cur = frame.snap.get(t)
        edge = " [EDGE]" if t in ctx.edge_tags else ""
        ng = " [NOGOOD]" if (t, v) in state.nogoods.get(frame.key, ()) else ""
        already = " [ALREADY]" if _values_match(cur, v) and t not in ctx.edge_tags else ""
        dbg(f"#   {t}={v!r}  (cur={cur!r}){edge}{ng}{already}")


def _diagnose_stuck(
    frame: _IterationFrame,
    candidates: Any,
    state: _PilotState,
) -> str:
    if candidates.stuck_reason is not None:
        return candidates.stuck_reason
    key_nogoods = state.nogoods.get(frame.key, set())
    if not candidates.candidates:
        return "no_candidates"
    if all(c.pair in key_nogoods for c in candidates.candidates):
        return "all_rejected"
    return "all_rejected"


def _apply_attempt_memory(
    attempt: Any,
    frame: _IterationFrame,
    state: _PilotState,
) -> None:
    if attempt.excursion_holds:
        _install_holds(state.work, list(attempt.excursion_holds), state.forced_holds)
    if attempt.nogood_pairs:
        state.nogoods.setdefault(frame.key, set()).update(attempt.nogood_pairs)


def _commit_and_monitor(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
    observe: _ObserveFn,
) -> Iterator[PilotEvent]:
    _commit_trial(trial, state, ctx, observe, frame.snap)
    yield PilotEvent(
        "trial_committed",
        state.work.state.scan_id,
        {
            "decision": trial.decision,
            "pulse_actions": trial.pulse_actions,
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
    if trial.new_key is not None:
        state.seen_keys.add(trial.new_key)
    # Record what was physically pulsed — the candidate plus its co-actions (the
    # command button and its one-shot ``rise(CmdChgRequest)`` edge gate) — not the
    # narrow candidate *decision* (``trial.decision``).  Replay and live apply must
    # reproduce every input that drove the transition.  ``pulse_actions`` is the
    # full applied set and is empty exactly for zoom/let-run, where an empty
    # action correctly means "coast, no input".
    # A terminal let-run animates conditional holds during its coast; record them
    # on the step so the path is self-describing.  ``forced_holds`` is the live
    # round-by-round accumulator — snapshot the conditional ones active now.  A
    # pulse/zoom step animates nothing, so it carries no reactive holds.
    #
    # The *steady* holds active during the coast (e.g. the Enable that drives a
    # harness sensor's ramp) are the input that makes the coast advance — fold
    # them into the recorded inputs so replay re-establishes them.  ``pulse_actions``
    # is empty for a let-run, so this is the only place the driver is recorded.
    reactive_holds: dict[str, Any] = {}
    step_inputs = dict(trial.pulse_actions)
    if trial.observe_label in ("letrun", "letrun-target"):
        steady, reactive_holds = _split_holds(list(state.forced_holds.items()))
        step_inputs = {**dict(steady), **step_inputs}
    prev = len(state.steps)
    state.work = _commit_step(
        state.work,
        trial.fork,
        step_inputs,
        trial.scan_before,
        state.steps,
        ctx.resting,
        ctx.edge_tags,
        ctx.live,
        reactive_holds=reactive_holds,
    )
    # Mirror the freshly-appended step(s) into the append-only journey; ``steps``
    # is later truncated on revert (``_PilotState.revert_to``), ``journey`` is not.
    state.journey.extend(state.steps[prev:])


def _iteration_payload(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> dict[str, Any]:
    still_need: list[str] = []
    seen_need: set[tuple[str, str]] = set()
    for n in _all_nodes(frame.tree):
        if (
            not n.satisfied
            and not n.is_steerable
            and not getattr(n, "pipeline_internal", False)
            and n.children
        ):
            cur = frame.snap.get(n.tag)
            if cur != n.value:
                nk = (n.tag, repr(n.value))
                if nk not in seen_need:
                    seen_need.add(nk)
                    still_need.append(f"{n.tag}={n.value!r} (have {cur!r})")

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
        "forced_holds": dict(state.forced_holds),
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
        "blast_cap": candidates.blast_cap,
        "wait_prescribed": candidates.wait_prescribed,
        "wait_reason": candidates.wait_reason,
        "prerequisite_holds": candidates.prerequisite_holds,
        "stuck_reason": candidates.stuck_reason,
    }


def _candidate_payload(candidate: _Candidate) -> dict[str, Any]:
    return {
        "tag": candidate.tag,
        "value": candidate.value,
        "pair": candidate.pair,
        "influence_prescribed": candidate.influence_prescribed,
        "route_prescribed": candidate.route_prescribed,
        "provenance": candidate.provenance,
        "blast_radius": candidate.blast_radius,
    }


def _route_plan_payload(plan: CompassPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    from pyrung.core.analysis.pilot.compass import ANY_FROM

    return {
        "needed": (plan.needed_tag, plan.needed_value),
        "governing_tag": plan.role.governing_tag,
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
    and the governing tag/value — so a consumer can tell a genuine coast from a
    terminal-letrun *ejection* (``ejected``) without re-deriving it.  The event
    name is kept stable for existing consumers; the ``ejected`` flag is the
    honest signal that an AMBIENT_DRIFT was committed under it.
    """
    return {
        "new_key": trial.new_key,
        "trend": trial.trend,
        "outcome": trial.outcome.value if trial.outcome else None,
        "observe_label": trial.observe_label,
        "zoom_governing_tag": trial.zoom_governing_tag,
        "zoom_target_value": trial.zoom_target_value,
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
    action_tags = {tag for tag, _value in trial.pulse_actions}
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
        "candidate": _candidate_payload(candidate),
        "decision": trial.decision,
        "pulse_actions": trial.pulse_actions,
        "context_actions": _context_actions(candidate, trial.pulse_actions),
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
    choice: TraceChoice | None = None,
    blocked_choice_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_scans: int = 3000,
    live: bool = False,
    debug: bool = False,
    avoid_pred: Any = None,
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
        choice=choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
        avoid_pred=avoid_pred,
        target_predicate=target_predicate,
    )
    state = _PilotState(
        work=plc,
        key_config=key_config,
        seen_keys=set(),
        nogoods={},
        checkpoints=[],
        forced_holds={},
        steps=[],
        watch_tags=[],
    )

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
            "choice": ctx.choice,
            "blocked_choice_actions": ctx.blocked_choice_actions,
        },
    )

    while state.work.state.scan_id < ctx.max_scans:
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
                    reactive_holds=state.steps[-1].reactive_holds,
                )
                if state.journey and state.journey[-1] is state.steps[-1]:
                    state.journey[-1] = final_step
                state.steps[-1] = final_step
            yield PilotEvent(
                "finished",
                state.work.state.scan_id,
                {
                    "reached": True,
                    "steps": tuple(state.steps),
                    "journey": tuple(state.journey),
                    "work": state.work,
                    "reason": "target reached",
                },
            )
            return

        frame = _prepare_iteration(state, ctx, _dbg)
        if not state.checkpoints:
            # Seed an entry checkpoint so the first regression — or a terminal
            # let-run ejection from a pre-positioned start (e.g. dropped straight
            # into Execute) — has somewhere to revert to.  "No checkpoint" should
            # mean "go back to the beginning", not "let the ejected state stand".
            state.checkpoints.append((frame.key, state.work.fork(), frame.distance_before))
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
            yield PilotEvent(
                "stuck",
                state.work.state.scan_id,
                {
                    "reason": candidates.stuck_reason,
                    "distance": frame.distance_before,
                    "candidate_count": 0,
                    "nogoods_at_key": len(state.nogoods.get(frame.key, set())),
                    "terminal": True,
                },
            )
            if state.checkpoints:
                _cp_key, cp_fork, _cp_trend = state.checkpoints[-1]
                state.revert_to(cp_fork)
            yield PilotEvent(
                "finished",
                state.work.state.scan_id,
                {
                    "reached": False,
                    "steps": tuple(state.steps),
                    "journey": tuple(state.journey),
                    "work": state.work,
                    "reason": f"stuck: {candidates.stuck_reason}",
                },
            )
            return

        accepted = False

        # ── Establish prerequisites (level holds — steerable inputs, not state) ──
        if candidates.prerequisite_holds:
            _install_holds(
                state.work,
                list(candidates.prerequisite_holds),
                state.forced_holds,
            )

        # ── Act: zoom (timer-gated frontier) ──
        if candidates.wait_prescribed:
            yield PilotEvent(
                "zoom",
                state.work.state.scan_id,
                {
                    "prescribed": True,
                    "reason": candidates.wait_reason,
                    "prerequisite_holds": candidates.prerequisite_holds,
                    "governing_tag": (
                        candidates.route_plan.role.governing_tag
                        if candidates.route_plan is not None
                        else None
                    ),
                },
            )
            attempt = _try_zoom(candidates, frame, state, ctx, _dbg)
            _apply_attempt_memory(attempt, frame, state)
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

        # ── Act: command candidates ──
        if not accepted:
            for ci, candidate in enumerate(candidates.candidates):
                pulse_actions = _candidate_pulse_actions(candidate, candidates, ctx)
                yield PilotEvent(
                    "candidate_try",
                    state.work.state.scan_id,
                    {
                        "index": ci,
                        "total": len(candidates.candidates),
                        "candidate": _candidate_payload(candidate),
                        "pulse_actions": pulse_actions,
                        "context_actions": _context_actions(candidate, pulse_actions),
                    },
                )
                attempt = _try_candidate(candidate, candidates, frame, state, ctx, _dbg)
                _apply_attempt_memory(attempt, frame, state)
                if attempt.trial is None:
                    yield PilotEvent(
                        "candidate_rejected",
                        state.work.state.scan_id,
                        {
                            "index": ci,
                            "candidate": _candidate_payload(candidate),
                            "pulse_actions": pulse_actions,
                            "context_actions": _context_actions(candidate, pulse_actions),
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
            _apply_attempt_memory(attempt, frame, state)
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "widening_accepted",
                    trial.fork.state.scan_id,
                    {
                        "decision": trial.decision,
                        "pulse_actions": trial.pulse_actions,
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
        # Skip if we already coasted this key with no fewer holds — deterministic,
        # so it would only re-burn the budget (or re-eject) without new input.
        if state.letrun_tried.get(frame.key, -1) >= len(state.forced_holds):
            ceiling = min(_LETRUN_DWELL_CEILING, ctx.max_scans - state.work.state.scan_id)
            _settle_cone(state.work, _cone_tags(frame, ctx), floor=2, ceiling=max(2, ceiling))
            continue
        state.letrun_tried[frame.key] = len(state.forced_holds)
        yield PilotEvent(
            "zoom",
            state.work.state.scan_id,
            {
                "prescribed": True,
                "reason": "terminal let-run (hold macro-state, coast to target)",
                "prerequisite_holds": (),
                "governing_tag": ctx.target_tag,
            },
        )
        attempt = _try_terminal_letrun(frame, state, ctx, _dbg)
        _apply_attempt_memory(attempt, frame, state)
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
        # Stall: the key is already recorded in letrun_tried (set before firing),
        # so we won't re-coast it unless a new hold is installed.
        yield PilotEvent(
            "zoom_rejected",
            state.work.state.scan_id,
            {"gates": attempt.gate_events},
        )

        # ── Stuck: all candidates rejected, terminal let-run failed ──
        stuck_reason = _diagnose_stuck(frame, candidates, state)
        yield PilotEvent(
            "stuck",
            state.work.state.scan_id,
            {
                "reason": stuck_reason,
                "distance": frame.distance_before,
                "candidate_count": len(candidates.candidates),
                "nogoods_at_key": len(state.nogoods.get(frame.key, set())),
                "terminal": True,
            },
        )
        if state.checkpoints:
            _cp_key, cp_fork, _cp_trend = state.checkpoints[-1]
            state.revert_to(cp_fork)
        yield PilotEvent(
            "finished",
            state.work.state.scan_id,
            {
                "reached": False,
                "steps": tuple(state.steps),
                "journey": tuple(state.journey),
                "work": state.work,
                "reason": f"stuck: {stuck_reason}",
            },
        )
        return

    yield PilotEvent(
        "finished",
        state.work.state.scan_id,
        {
            "reached": _values_match(state.work.state.tags.get(ctx.target_tag), ctx.target_value),
            "steps": tuple(state.steps),
            "journey": tuple(state.journey),
            "work": state.work,
            "reason": "budget exhausted",
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
    choice: TraceChoice | None = None,
    blocked_choice_actions: frozenset[tuple[str, Any]] = frozenset(),
    max_scans: int = 3000,
    live: bool = False,
    debug: bool = False,
    avoid_pred: Any = None,
    target_predicate: Any = None,
) -> tuple[bool, list[_Step], list[_Step], PLC]:
    """Run the PILOT loop and return ``(reached, steps, journey, work)``.

    ``steps`` is the clean, sequentially-replayable path; ``journey`` is the full
    attempt log (incl. reverted rounds) for ``debug=True``.
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
        choice=choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=live,
        debug=debug,
        avoid_pred=avoid_pred,
        target_predicate=target_predicate,
    ):
        if event.kind == "finished":
            final = event

    if final is None:
        return False, [], [], plc
    return (
        bool(final.data["reached"]),
        list(final.data["steps"]),
        list(final.data.get("journey", ())),
        final.data["work"],
    )


# ---------------------------------------------------------------------------
# Path construction
# ---------------------------------------------------------------------------


def _annotate_pilot_steps(
    plc: PLC,
    steps: list[ReachabilityStep],
    atom_index: dict[str, list[Any]],
    domain_sources: dict[str, str],
) -> list[ReachabilityStep]:
    """Attach semantic constraints to the public reachability steps.

    Replays the (clean, non-overlapping) steps on a fresh fork and classifies
    each step's inputs against the prover-derived atom index, so the path
    renders ``Temp=76 (> 75)`` / ``A > B`` instead of the raw representative.

    Operates on the public ``ReachabilityStep`` shape only — never the internal
    ``_Step`` — and re-installs each step's ``reactive_holds`` (when a let-run
    coast carries them) so the replay reproduces the recorded transition.  Today
    command paths carry none and ``ReachabilityStep`` has no such field, so the
    hold install is a no-op; it lights up the moment let-run steps are recorded
    on the public path.
    """
    from dataclasses import replace

    from pyrung.core.analysis.graph import _classify_step_inputs
    from pyrung.core.analysis.pilot._ops import _install_reactive_holds

    fork = plc.fork()
    install_harness(fork)

    annotated: list[ReachabilityStep] = []
    for step in steps:
        action = step.action
        if action:
            fork.patch(action)
        holds = getattr(step, "reactive_holds", None)
        handles = _install_reactive_holds(fork, holds) if holds else []
        try:
            for _ in range(step.scans):
                fork.step()
        finally:
            for handle in handles:
                handle.remove()
        constraints = (
            _classify_step_inputs(action, atom_index, domain_sources, dict(fork.state.tags))
            if action
            else None
        ) or None
        annotated.append(replace(step, constraints=constraints))
    return annotated


def _reach_step(s: _Step) -> ReachabilityStep:
    """Public ``ReachabilityStep`` from an internal ``_Step``.

    Carries the let-run oscillation (``reactive_holds``) onto the public step so
    the path stays self-describing — a replay re-installs ``when(guard).do(patch)``
    and reproduces the coast.  ``_Step.inputs`` is the applied set; the public
    field stays ``action`` (shared with BFS paths).
    """
    return ReachabilityStep(
        action=s.inputs,
        source_key=(s.scan_before,),
        dest_key=(s.scan_after,),
        scans=s.scans,
        reactive_holds=s.reactive_holds or None,
    )


def _build_path(
    reached: bool,
    recorded_steps: list[_Step],
    target_tag: str,
    target_value: Any,
    *,
    plc: PLC | None = None,
    atom_index: dict[str, list[Any]] | None = None,
    domain_sources: dict[str, str] | None = None,
    journey: list[_Step] | None = None,
) -> Path:
    """Convert recorded PILOT steps into a ``Path``.

    ``plc`` + ``atom_index`` + ``domain_sources`` (keyword-only) enable semantic
    constraint annotation of the rendered path; absent → the raw representative
    rendering (today's behavior for the no-metadata case).  ``journey`` (the full
    attempt log incl. reverted rounds) is surfaced verbatim on ``Path.journey``
    for ``how(..., debug=True)``; it is NOT annotated, since its overlapping
    attempts are not soundly replayable in sequence.
    """
    if not reached:
        return Path(
            reachable=False,
            steps=(),
            total_changes=0,
            total_scans=0,
            reason=f"pilot: {target_tag}={target_value!r} not reached within budget",
        )

    path_steps: list[ReachabilityStep] = [_reach_step(s) for s in recorded_steps]

    if plc is not None and atom_index is not None and domain_sources is not None:
        try:
            path_steps = _annotate_pilot_steps(plc, path_steps, atom_index, domain_sources)
        except Exception:  # noqa: BLE001
            logger.debug("pilot: step annotation failed; rendering raw", exc_info=True)

    return Path(
        reachable=True,
        steps=tuple(path_steps),
        total_changes=sum(len(s.inputs) for s in recorded_steps),
        total_scans=sum(s.scans for s in recorded_steps),
        journey=tuple(_reach_step(s) for s in journey) if journey else None,
    )


def _target_is_bool_true(plc: PLC, target_tag: str, target_value: Any) -> bool:
    from pyrung.core.tag import TagType

    tag_obj = plc._known_tags_by_name.get(target_tag)
    return getattr(tag_obj, "type", None) is TagType.BOOL and _values_match(target_value, True)


def _resolve_trace_choice(
    requested: int | str | TraceChoice | None,
    choices: tuple[TraceChoice, ...],
) -> TraceChoice | None:
    if requested is None:
        return None
    if isinstance(requested, TraceChoice):
        return requested
    if isinstance(requested, int):
        idx = requested - 1
        return choices[idx] if 0 <= idx < len(choices) else None
    requested_text = str(requested)
    for option in choices:
        if requested_text == option.id or requested_text == option.label:
            return option
    return None


def _ambiguous_path(
    target_tag: str,
    target_value: Any,
    choices: tuple[TraceChoice, ...],
) -> Path:
    return Path(
        reachable=False,
        steps=(),
        total_changes=0,
        total_scans=0,
        reason=f"pilot: {target_tag}={target_value!r} has multiple Bool output routes",
        choices=choices,
    )


def _exclusive_choice_actions(
    selected: TraceChoice | None,
    choices: tuple[TraceChoice, ...],
    target_tag: str,
    target_value: Any,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
) -> frozenset[tuple[str, Any]]:
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
            opaque_loop=opaque_loop,
            choice=selected,
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
                opaque_loop=opaque_loop,
                choice=option,
            ).ordered_actions()
        )
    return frozenset(other_actions - selected_actions)


def _prepare_trace_choice(
    plc: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    steerable: frozenset[str],
    opaque_loop: frozenset[str],
    choice: int | str | TraceChoice | None,
    *,
    avoid_pred: Any = None,
) -> tuple[Path | None, TraceChoice | None, frozenset[tuple[str, Any]]]:
    """Resolve an ambiguous Bool-output route choice for an entry point.

    Returns ``(early_path, trace_choice, blocked_choice_actions)``.  When
    *early_path* is not ``None`` the caller returns it immediately — the
    target has multiple output routes and no (or an invalid) choice was given.

    ``avoid_pred`` prunes the route set first: a choice whose route forces the
    avoided condition is dropped, so a target reachable only via a non-avoided
    arm resolves to that single route instead of reporting ambiguous.
    """
    snapshot = dict(plc.state.tags)
    trace_choice: TraceChoice | None = None
    choices: tuple[TraceChoice, ...] = ()
    if _target_is_bool_true(plc, target_tag, target_value) and not _values_match(
        snapshot.get(target_tag), target_value
    ):
        choices = enumerate_trace_choices(
            target_tag, target_value, snapshot, pdg, program, steerable=steerable
        )
        if avoid_pred is not None and choices:
            choices = tuple(
                ch
                for ch in choices
                if not _route_forces(
                    [
                        trace_back(
                            target_tag,
                            target_value,
                            snapshot,
                            pdg,
                            program,
                            steerable,
                            opaque_loop=opaque_loop,
                            choice=ch,
                        )
                    ],
                    snapshot,
                    avoid_pred,
                )
            )
        trace_choice = _resolve_trace_choice(choice, choices)
        if choices and choice is None:
            if len(choices) == 1:
                # Only one route survives (e.g. after avoid-pruning) — take it
                # rather than reporting the target as ambiguous.
                trace_choice = choices[0]
            else:
                return _ambiguous_path(target_tag, target_value, choices), None, frozenset()
        if choice is not None and trace_choice is None:
            return (
                Path(
                    reachable=False,
                    steps=(),
                    total_changes=0,
                    total_scans=0,
                    reason=f"pilot: invalid choice {choice!r} for {target_tag}={target_value!r}",
                    choices=choices,
                ),
                None,
                frozenset(),
            )
    blocked = _exclusive_choice_actions(
        trace_choice,
        choices,
        target_tag,
        target_value,
        snapshot,
        pdg,
        program,
        steerable,
        opaque_loop,
    )
    return None, trace_choice, blocked


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

        # Build state key config from ExploreContext
        stateful_names = ctx.stateful_names
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


def _parse_target(
    *conditions: Any,
) -> tuple[str, Any, Any]:
    """Extract ``(tag_name, target_value, predicate)`` from conditions.

    Accepts:
    - A Tag object (implies ``tag == True``)
    - A ``tag == value`` comparison (CompareEq)
    - A relational comparison ``A < / <= / > / >= B`` — returned as a live
      ``predicate`` Atom (the goal is the relation, not a frozen value); the
      ``(tag, value)`` pair is a representative for display/keying only.
    """
    from pyrung.core.condition import CompareEq
    from pyrung.core.tag import Tag

    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")

    cond = conditions[0]

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


def pilot_events(
    plc: PLC,
    *conditions: Any,
    choice: int | str | TraceChoice | None = None,
    max_scans: int = 3000,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value, target_predicate = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence, _semantic = _build_pilot_context(
        program, dict(fork.state.tags)
    )
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        fork, target_tag, target_value, pdg, program, steerable, opaque_loop, choice
    )
    if early is not None:
        yield PilotEvent(
            "finished",
            fork.state.scan_id,
            {
                "reached": False,
                "steps": (),
                "work": fork,
                "path": early,
                "reason": early.reason,
                "choices": early.choices,
            },
        )
        return

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
        choice=trace_choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=False,
        debug=False,
        target_predicate=target_predicate,
    )


def pilot_how(
    plc: PLC,
    *conditions: Any,
    choice: int | str | TraceChoice | None = None,
    max_scans: int = 3000,
    debug: bool = False,
    avoid_pred: Any = None,
) -> Path:
    """PILOT on a fork — discover the path, return it. Nothing changes."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value, target_predicate = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork(history_budget=math.inf)
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence, semantic = _build_pilot_context(
        program, dict(fork.state.tags)
    )
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        fork,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        opaque_loop,
        choice,
        avoid_pred=avoid_pred,
    )
    if early is not None:
        return early

    reached, steps, journey, _work = _pilot_loop(
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
        choice=trace_choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        debug=debug,
        avoid_pred=avoid_pred,
        target_predicate=target_predicate,
    )

    atom_index, domain_sources = semantic if semantic else (None, None)
    return _build_path(
        reached,
        steps,
        target_tag,
        target_value,
        plc=plc,
        atom_index=atom_index,
        domain_sources=domain_sources,
        journey=journey if debug else None,
    )


def pilot_drive(
    plc: PLC,
    *conditions: Any,
    choice: int | str | TraceChoice | None = None,
    max_scans: int = 3000,
    debug: bool = False,
    avoid_pred: Any = None,
) -> Path:
    """PILOT on the live PLC — drive the state there."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value, target_predicate = _parse_target(*conditions)
    program = plc._program

    pdg = build_program_graph(program)
    harness_fb = install_harness(plc)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, plc._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence, _semantic = _build_pilot_context(
        program, dict(plc.state.tags)
    )
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        plc,
        target_tag,
        target_value,
        pdg,
        program,
        steerable,
        opaque_loop,
        choice,
        avoid_pred=avoid_pred,
    )
    if early is not None:
        return early

    reached, steps, journey, _work = _pilot_loop(
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
        choice=trace_choice,
        blocked_choice_actions=blocked_choice_actions,
        max_scans=max_scans,
        live=True,
        debug=debug,
        avoid_pred=avoid_pred,
        target_predicate=target_predicate,
    )

    return _build_path(reached, steps, target_tag, target_value, journey=journey if debug else None)
