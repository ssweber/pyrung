"""PILOT loop: trace backward, apply forward, learn from cause() chains.

Acceptance logic uses state-key-based layers (causal momentum) instead of
distance-gated branches.  The state key reuses the prover's projection
(stateful_names + done-bit abstraction + threshold vectors) so accumulator
ticks are absorbed and only structural transitions change the key.

Layers 0-2 gate each candidate action:

  0. Don't Spin — state key must change
     0a. Excursion — key changed then reverted; derive holds, retry
  1. Don't Cycle — new key must not have been visited
  2. Don't Dead-End — frontier must be non-empty or async pending

Layers 3-4 monitor the committed sequence:

  3. Don't Wander — checkpoint on trend improvement
  4. Don't Regress — cause-chain recovery on trend regression

Layer 5 (influence mapping):

  5. Don't Rediscover — observed transitions become known topology
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.graph import Path, ReachabilityStep
from pyrung.core.analysis.pilot._ops import (
    _apply_pulse,
    _DebugFn,
    _install_holds,
    _pilot_state_key,
    _settle_delayed_effects,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.candidates import (
    _all_nodes,
    _build_candidates,
    _Candidate,
    _candidate_pulse_actions,
    _CandidateList,
    _context_actions,
)
from pyrung.core.analysis.pilot.compass import (
    WAIT,
    Action,
    Compass,
    TransitionCause,
    detect_opaque_loop,
    detect_opaque_pipelines,
    is_action,
)
from pyrung.core.analysis.pilot.investigate import (
    build_deviation_incident,
    build_replay_fn,
    chase_cause_roots,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.outcome import Outcome
from pyrung.core.analysis.pilot.physical import install_harness
from pyrung.core.analysis.pilot.trace import (
    TraceAction,
    TraceChoice,
    compute_edge_tags,
    compute_reference_constants,
    compute_resting_values,
    compute_steerable,
    enumerate_trace_choices,
    trace_back,
)
from pyrung.core.analysis.pilot.verify import (
    _AttemptResult,
    _PulseState,
    _TrialResult,
    verify_gates,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.analysis.pilot.compass import CompassGraph, CompassPlan
    from pyrung.core.analysis.pilot.evidence import PipelineRoles, TransitionEvidence
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recorded step — intermediate representation before Path construction
# ---------------------------------------------------------------------------


@dataclass
class _Step:
    action: dict[str, Any]
    scan_before: int
    scan_after: int

    @property
    def scans(self) -> int:
        return self.scan_after - self.scan_before


@dataclass(frozen=True)
class PilotEvent:
    """Structured diagnostic event emitted by :func:`pilot_events`.

    The payload intentionally carries Python objects where useful instead of a
    pre-rendered text log.  Callers can decide how much to display.
    """

    kind: str
    scan: int
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TagChange:
    """A single tag value transition between two snapshots."""

    tag: str
    before: Any
    after: Any


# Post-action settle coasts a trial fork briefly; let-run coasts the live state
# until a self-advancing frontier completes, so it gets a far larger ceiling.
_SETTLE_CONE_CEILING = 16
_LETRUN_DWELL_CEILING = 64


def _settle_cone(
    fork: PLC,
    cone: frozenset[str],
    *,
    floor: int = 2,
    ceiling: int = _SETTLE_CONE_CEILING,
) -> list[dict[str, Any]]:
    """Coast *fork* until the cone stops moving — dwell control only.

    Logic can take up to two scans to propagate, so step ``floor`` scans before
    judging anything.  After the floor, step one scan at a time and stop as soon
    as no tag in *cone* changed since the previous scan (a cone fixpoint), or
    once ``ceiling`` scans have run.  Returns the per-scan trajectory.

    Settle never accepts or rejects.  Attributing the trajectory to one of the
    five verify outcomes — who moved what — is the caller's job via ``cause()``.
    """
    ceiling = max(floor, ceiling)
    snaps: list[dict[str, Any]] = []
    prev = dict(fork.state.tags)
    for i in range(ceiling):
        fork.step()
        cur = dict(fork.state.tags)
        snaps.append(cur)
        if i + 1 >= floor and all(cur.get(t) == prev.get(t) for t in cone):
            break
        prev = cur
    return snaps


def _cone_tags(frame: _IterationFrame, ctx: _PilotContext) -> frozenset[str]:
    """The tags whose motion matters this iteration.

    The trace-tree prerequisites toward the goal — satisfied *and* unsatisfied,
    so a prerequisite slipping back (divergence) is visible, not just one being
    met — plus the governing / opaque-loop registers.  Steerable inputs are
    excluded: those are held, not watched.
    """
    tags = {n.tag for n in _all_nodes(frame.tree) if not n.is_steerable}
    return frozenset(tags | ctx.opaque_loop)


# ---------------------------------------------------------------------------
# Core PILOT loop — layered acceptance (causal momentum)
# ---------------------------------------------------------------------------


def _commit_step(
    work: PLC,
    fork: PLC,
    action: dict[str, Any],
    scan_before: int,
    steps: list[_Step],
    resting: dict[str, Any],
    edge_tags: set[str],
    live: bool,
) -> PLC:
    """Record a step and swap the work fork (or apply live)."""
    steps.append(
        _Step(
            action=action,
            scan_before=scan_before,
            scan_after=fork.state.scan_id,
        )
    )
    if live:
        _apply_pulse(work, list(action.items()), resting, edge_tags)
        return work
    return fork


_ActionPair = tuple[str, Any]
_StateKey = tuple[Any, ...]
_Checkpoint = tuple[_StateKey, Any, int]
_ObserveFn = Callable[[str, dict[str, Any], Any], None]


@dataclass
class _PilotContext:
    target_tag: str
    target_value: Any
    pdg: ProgramGraph
    program: Any
    steerable: frozenset[str]
    edge_tags: set[str]
    resting: dict[str, Any]
    nd_domains: dict[str, tuple[Any, ...]] | None
    evidence: TransitionEvidence | None
    compass: Compass
    opaque_loop: frozenset[str]
    pipeline_roles: tuple[PipelineRoles, ...]
    pipeline_internal_tags: frozenset[str]
    choice: TraceChoice | None
    blocked_choice_actions: frozenset[_ActionPair]
    max_scans: int
    live: bool
    debug: bool
    avoid_pred: Any = None

    def route_allowed(self, pair: _ActionPair) -> bool:
        return pair not in self.blocked_choice_actions


@dataclass
class _PilotState:
    work: PLC
    key_config: _StateKeyConfig | None
    seen_keys: set[_StateKey]
    nogoods: dict[_StateKey, set[_ActionPair]]
    checkpoints: list[_Checkpoint]
    forced_holds: dict[str, Any]
    steps: list[_Step]
    watch_tags: list[str]
    expanded_tags: set[str] = field(default_factory=set)
    best_trend: int | None = None
    last_wait_log: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class _IterationFrame:
    snap: dict[str, Any]
    tree: Any
    key: _StateKey
    distance_before: int
    raw_trace_actions: tuple[_ActionPair, ...]
    raw_trace_action_details: tuple[TraceAction, ...]


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
    return _PilotContext(
        target_tag=target_tag,
        target_value=target_value,
        pdg=pdg,
        program=program,
        steerable=steerable,
        edge_tags=edge_tags,
        resting=resting,
        nd_domains=nd_domains,
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
            dbg(f"#   [{si}] {step.action}")

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


def _pulse_actions(
    actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _PulseState:
    key_config = state.key_config
    assert key_config is not None

    fork = state.work.fork()
    _install_holds(fork, list(state.forced_holds.items()), {})
    scan_before = fork.state.scan_id
    patch = {t: v for t, v in actions}
    needs_edge = any(t in ctx.edge_tags for t in patch)

    if needs_edge:
        release = {t: ctx.resting.get(t, False) for t in patch if t in ctx.edge_tags}
        if release:
            fork.patch(release)
            fork.step()

    fork.patch(patch)
    fork.step()
    action_snap = dict(fork.state.tags)
    action_scan = fork.state.scan_id
    wait_snaps = _settle_cone(fork, _cone_tags(frame, ctx), floor=2)

    post_pulse_snap = dict(fork.state.tags)
    post_pulse_key = _pilot_state_key(post_pulse_snap, key_config)
    _settle_delayed_effects(
        fork,
        frame.snap,
        key_config,
        scan_budget=ctx.max_scans - fork.state.scan_id,
    )
    fork_snap = dict(fork.state.tags)
    if wait_snaps and wait_snaps[-1] != fork_snap:
        wait_snaps.append(fork_snap)
    elif not wait_snaps and action_snap != fork_snap:
        wait_snaps.append(fork_snap)
    return _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=action_scan,
        action_snap=action_snap,
        wait_snaps=tuple(wait_snaps),
        post_pulse_snap=post_pulse_snap,
        post_pulse_key=post_pulse_key,
        snap=fork_snap,
        key=_pilot_state_key(fork_snap, key_config),
    )


def _action_caused_change(
    fork: PLC,
    action_tag: str,
    changed_tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None,
) -> bool:
    """True if *action_tag* is a causal root of *changed_tag*'s transition.

    Distinguishes a change the pilot's control input produced from one that
    happened ambiently in the same scan (a timer or alarm firing).  This is the
    "control vs wind" check: only the former should be learned as an action
    transition.
    """
    roots, _holds = chase_cause_roots(fork, changed_tag, steerable, scan=scan)
    return action_tag in roots


def _record_compass_observations(
    cause: TransitionCause,
    frame: _IterationFrame,
    before_snap: dict[str, Any],
    after_snap: dict[str, Any],
    ctx: _PilotContext,
    *,
    record_no_change: bool,
    fork: PLC | None = None,
    scan: int | None = None,
) -> None:
    action_tag = cause[0] if is_action(cause) else None
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        old_v = before_snap.get(n.tag)
        new_v = after_snap.get(n.tag)
        if old_v != new_v and new_v is not None:
            # Attribute a transition to a steerable action only when the action
            # is a causal root of the change.  An ambient change (timer/alarm
            # firing in the same scan) is not the pilot's control input —
            # recording it as action-caused fills the compass with correlations.
            if (
                action_tag is not None
                and fork is not None
                and not _action_caused_change(fork, action_tag, n.tag, ctx.steerable, scan=scan)
            ):
                continue
            ctx.compass.record(n.tag, cause, old_v, new_v)
        elif record_no_change:
            ctx.compass.record_no_change(n.tag, cause, old_v)


def _label_action(action_pairs: tuple[_ActionPair, ...]) -> str:
    if len(action_pairs) == 1:
        t, v = action_pairs[0]
        return f"({t}={v!r})"
    return f"({', '.join(f'{t}={v!r}' for t, v in action_pairs)})"


def _try_action_batch(
    action_pairs: tuple[_ActionPair, ...],
    pulse_actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
    *,
    observe_label: str,
    target_observe_label: str,
    debug_name: str,
    influence_prescribed: bool,
    route_prescribed: bool,
    nogood_pair: _ActionPair | None,
    regression_nogoods: frozenset[_ActionPair],
    chase_regression_causes: bool,
    record_influence_action: Action | None = None,
) -> _AttemptResult:
    trial = _pulse_actions(pulse_actions, frame, state, ctx)

    if record_influence_action is not None:
        _record_compass_observations(
            record_influence_action,
            frame,
            frame.snap,
            trial.action_snap,
            ctx,
            record_no_change=True,
            fork=trial.fork,
            scan=trial.action_scan,
        )
    wait_before = trial.action_snap
    for wait_after in trial.wait_snaps:
        _record_compass_observations(
            WAIT,
            frame,
            wait_before,
            wait_after,
            ctx,
            record_no_change=False,
        )
        wait_before = wait_after

    return verify_gates(
        trial,
        action_pairs,
        pulse_actions,
        frame,
        state,
        ctx,
        dbg,
        observe_label=observe_label,
        target_observe_label=target_observe_label,
        debug_name=debug_name,
        influence_prescribed=influence_prescribed,
        route_prescribed=route_prescribed,
        nogood_pair=nogood_pair,
        regression_nogoods=regression_nogoods,
        chase_regression_causes=chase_regression_causes,
    )


def _try_candidate(
    candidate: _Candidate,
    candidates: _CandidateList,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    pair = candidate.pair
    pulse_actions = _candidate_pulse_actions(candidate, candidates, ctx)
    if len(pulse_actions) > 1:
        dbg(f"#     INFLUENCE-CONTEXT: +{len(candidates.trace_actions)} trace actions")

    return _try_action_batch(
        (pair,),
        pulse_actions,
        frame,
        state,
        ctx,
        dbg,
        observe_label="accept",
        target_observe_label="target",
        debug_name=_label_action((pair,)),
        influence_prescribed=candidate.influence_prescribed,
        route_prescribed=candidate.route_prescribed,
        nogood_pair=pair,
        regression_nogoods=frozenset({pair}),
        chase_regression_causes=True,
        record_influence_action=pair,
    )


def _try_widening(
    active_trace_actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    for width in range(2, len(active_trace_actions) + 1):
        batch = active_trace_actions[:width]
        dbg(f"# --- Width {width} ({len(batch)} actions) ---")
        attempt = _try_action_batch(
            batch,
            batch,
            frame,
            state,
            ctx,
            dbg,
            observe_label="width",
            target_observe_label="width-target",
            debug_name=f"WIDTH-{width}",
            influence_prescribed=False,
            route_prescribed=False,
            nogood_pair=None,
            regression_nogoods=frozenset(batch),
            chase_regression_causes=False,
        )
        if attempt.trial is not None:
            return attempt
    return _AttemptResult(trial=None)


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
    state.work = _commit_step(
        state.work,
        trial.fork,
        trial.action,
        trial.scan_before,
        state.steps,
        ctx.resting,
        ctx.edge_tags,
        ctx.live,
    )


def _monitor_trend(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> tuple[PilotEvent, ...]:
    if trial.new_key is None or trial.trend is None:
        return ()

    assert state.best_trend is not None

    # A FRONTIER outcome means the pilot knowingly entered a corridor with
    # more prerequisites.  Commit the observation, but keep the previous
    # checkpoint and high-water mark alive: if the new corridor keeps drifting
    # away, the next verify pass should revert to the pre-frontier checkpoint
    # and chase the PLC-side cause.
    if trial.outcome == Outcome.FRONTIER:
        dbg(f"#     FRONTIER: trend {state.best_trend} -> {trial.trend}")
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": trial.trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                    "frontier": True,
                    "baseline_trend": state.best_trend,
                },
            ),
        )

    if trial.trend < state.best_trend:
        state.checkpoints.append((trial.new_key, state.work.fork(), trial.trend))
        state.best_trend = trial.trend
        dbg(f"#     CHECKPOINT: trend {state.best_trend}")
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": state.best_trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                },
            ),
        )

    if trial.trend == state.best_trend and trial.outcome in {
        Outcome.CONFIRMED,
        Outcome.AUTO_EDGE,
    }:
        state.checkpoints.append((trial.new_key, state.work.fork(), trial.trend))
        dbg(f"#     CHECKPOINT-FLAT: trend {state.best_trend}")
        return (
            PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": state.best_trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                    "flat": True,
                },
            ),
        )

    if trial.trend <= state.best_trend or not state.checkpoints:
        return ()

    dbg(f"#     REGRESSION: trend {state.best_trend} -> {trial.trend}, reverting to checkpoint")
    cp_key, cp_fork, cp_trend = state.checkpoints[-1]
    investigation_holds: list[_ActionPair] = []
    investigation_nogoods: set[_ActionPair] = set()
    investigation_payload: dict[str, Any] = {}
    if trial.chase_regression_causes:
        bearing = tuple(
            (wt, frame.snap.get(wt))
            for wt in state.watch_tags
            if not _values_match(frame.snap.get(wt), trial.fork_snap.get(wt))
        )
        incident = build_deviation_incident(
            state.work,
            anchor_scan=cp_fork.state.scan_id,
            end_scan=state.work.state.scan_id,
            action=trial.pulse_actions,
            bearing=bearing,
            before_snap=frame.snap,
            after_snap=trial.fork_snap,
        )

        replay_steps = tuple(
            step for step in state.steps if step.scan_before >= cp_fork.state.scan_id
        )
        replay = build_replay_fn(
            cp_fork,
            cp_trend,
            dict(state.forced_holds),
            replay_steps,
            resting=ctx.resting,
            edge_tags=ctx.edge_tags,
            target_tag=ctx.target_tag,
            target_value=ctx.target_value,
            pdg=ctx.pdg,
            program=ctx.program,
            steerable=ctx.steerable,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            choice=ctx.choice,
        )

        investigation = investigate_deviation(state.work, incident, ctx, replay)
        investigation_nogoods.update(investigation.regression_nogoods)
        needed_tags = {a for a, _ in frame.tree.ordered_actions()}
        investigation_holds.extend(
            (ht, hv) for ht, hv in investigation.confirmed_holds if ht not in needed_tags
        )
        investigation_payload = {
            "hypotheses": len(investigation.hypotheses),
            "confirmed": len(investigation.confirmed),
            "rejected": len(investigation.rejected),
            "unresolved": investigation.unresolved,
        }
        if investigation_holds:
            _install_holds(state.work, investigation_holds, state.forced_holds)
            for ht, hv in investigation_holds:
                dbg(f"#     HOLD {ht}={hv!r} (from investigation)")

    regression_nogoods = investigation_nogoods | set(trial.regression_nogoods)
    state.nogoods.setdefault(cp_key, set()).update(regression_nogoods)
    dbg(f"#     REGRESSION-NOGOOD at checkpoint: {sorted(regression_nogoods)}")
    state.work = cp_fork.fork()
    _install_holds(state.work, list(state.forced_holds.items()), {})
    state.best_trend = cp_trend
    return (
        PilotEvent(
            "trend_regression",
            state.work.state.scan_id,
            {
                "from_trend": trial.trend,
                "to_trend": cp_trend,
                "checkpoint_key": cp_key,
                "regression_nogoods": frozenset(regression_nogoods),
                "forced_holds": dict(state.forced_holds),
                "investigation": investigation_payload,
            },
        ),
    )


# ---------------------------------------------------------------------------
# Let-run — zoom past timer plateaus, verified like any other move
# ---------------------------------------------------------------------------


def _try_zoom(
    candidates: _CandidateList,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    """Let-run zoom through the verify pipeline — same shape as _try_candidate.

    Forks, zooms past timer/step-counter plateaus, then runs the shared
    verify gates.  The outcome classifier sees zoom results the same way it
    sees command results: SPIN if nothing moved, CONFIRMED if the governing
    register transitioned forward, AMBIENT_DRIFT if the program ejected.

    An ejection (e.g. S_StateCurrent 3→9) is AMBIENT_DRIFT with trend
    regression.  ``_monitor_trend`` reverts to the last checkpoint; a future
    investigation layer should own bounded incident analysis and replay-tested
    corrective holds.
    """
    governing_tag = (
        candidates.route_plan.role.governing_tag if candidates.route_plan is not None else None
    )
    target_value = (
        candidates.route_plan.first_edge.to_value if candidates.route_plan is not None else None
    )

    fork = state.work.fork()
    scan_before = fork.state.scan_id
    snap_before = dict(fork.state.tags)

    dwell = _letrun_zoom(fork, governing_tag, target_value, cone=_cone_tags(frame, ctx))

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_state_key(snap_after, key_config)

    wait_before = snap_before
    for wait_after in dwell:
        _record_compass_observations(
            WAIT,
            frame,
            wait_before,
            wait_after,
            ctx,
            record_no_change=False,
        )
        wait_before = wait_after

    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=scan_before,
        action_snap=snap_before,
        wait_snaps=tuple(dwell),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=key_after,
    )

    return verify_gates(
        trial,
        action_pairs=(),
        pulse_actions=(),
        frame=frame,
        state=state,
        ctx=ctx,
        dbg=dbg,
        observe_label="zoom",
        target_observe_label="zoom-target",
        debug_name="ZOOM",
        influence_prescribed=False,
        route_prescribed=candidates.route_plan is not None,
        nogood_pair=None,
        regression_nogoods=frozenset(),
        chase_regression_causes=True,
    )


_ZOOM_BUDGET = 10_000


def _letrun_zoom(
    work: PLC,
    governing_tag: str | None,
    target_value: Any,
    cone: frozenset[str],
) -> list[dict[str, Any]]:
    """Coast the live state past timer/step-counter plateaus.

    The zoom has its own generous budget (``_ZOOM_BUDGET``) — it does NOT
    consume the pilot's iteration budget.  Timer dwell is waiting, not
    searching.

    With a governing register and target value, install a ``when().pause()``
    guard for ejection (governing tag goes somewhere unexpected), then
    ``run_until`` the target.  If the guard fires first, the zoom stops
    immediately at the ejection scan — no budget wasted.

    Without a governing register, fall back to the bounded single-step cone
    settle.
    """
    if governing_tag is None:
        return _settle_cone(work, cone, floor=2, ceiling=_LETRUN_DWELL_CEILING)

    def _reached(s: Any) -> bool:
        return _values_match(s.tags.get(governing_tag), target_value)

    start_gov = work.state.tags.get(governing_tag)

    def _ejected(s: Any) -> bool:
        cur = s.tags.get(governing_tag)
        return not _values_match(cur, start_gov) and not _values_match(cur, target_value)

    guard = work.when(_ejected).pause()
    try:
        work.run_until(_reached, max_cycles=_ZOOM_BUDGET, fold=True)
    finally:
        guard.remove()
    return [dict(work.state.tags)]


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
        "action": trial.action,
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
        if _values_match(snap.get(ctx.target_tag), ctx.target_value):
            if state.steps:
                state.steps[-1] = _Step(
                    action=state.steps[-1].action,
                    scan_before=state.steps[-1].scan_before,
                    scan_after=state.work.state.scan_id,
                )
            yield PilotEvent(
                "finished",
                state.work.state.scan_id,
                {
                    "reached": True,
                    "steps": tuple(state.steps),
                    "work": state.work,
                    "reason": "target reached",
                },
            )
            return

        frame = _prepare_iteration(state, ctx, _dbg)
        _debug_iteration(frame, state, ctx, _dbg)
        yield PilotEvent(
            "iteration", state.work.state.scan_id, _iteration_payload(frame, state, ctx)
        )
        candidates = _build_candidates(frame, state, ctx, _dbg)
        yield PilotEvent(
            "candidates_built",
            state.work.state.scan_id,
            {
                "candidate_list": candidates,
                "candidates": tuple(_candidate_payload(c) for c in candidates.candidates),
                "trace_actions": candidates.trace_actions,
                "trace_action_details": candidates.trace_action_details,
                "active_trace_actions": candidates.active_trace_actions,
                "route_candidates": candidates.route_candidates,
                "route_plan": _route_plan_payload(candidates.route_plan),
                "influence_candidates": candidates.influence_candidates,
                "upstream_candidate_count": len(candidates.upstream_candidates),
                "blast_cap": candidates.blast_cap,
                "wait_prescribed": candidates.wait_prescribed,
                "wait_reason": candidates.wait_reason,
                "prerequisite_holds": candidates.prerequisite_holds,
            },
        )

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
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "zoom_accepted",
                    trial.fork.state.scan_id,
                    {
                        "new_key": trial.new_key,
                        "trend": trial.trend,
                        "outcome": trial.outcome.value if trial.outcome else None,
                        "scan_before": trial.scan_before,
                        "scan_after": trial.fork.state.scan_id,
                    },
                )
                _commit_trial(trial, state, ctx, _dbg_observe, frame.snap)
                yield PilotEvent(
                    "trial_committed",
                    state.work.state.scan_id,
                    {
                        "action": trial.action,
                        "pulse_actions": trial.pulse_actions,
                        "steps": tuple(state.steps),
                        "snapshot": dict(state.work.state.tags),
                    },
                )
                yield from _monitor_trend(trial, frame, state, ctx, _dbg)
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
                _commit_trial(trial, state, ctx, _dbg_observe, frame.snap)
                yield PilotEvent(
                    "trial_committed",
                    state.work.state.scan_id,
                    {
                        "action": trial.action,
                        "pulse_actions": trial.pulse_actions,
                        "steps": tuple(state.steps),
                        "snapshot": dict(state.work.state.tags),
                    },
                )
                yield from _monitor_trend(trial, frame, state, ctx, _dbg)
                accepted = True
                break

        # ── Widening fallback ──
        if (
            not accepted
            and not candidates.wait_prescribed
            and len(candidates.active_trace_actions) >= 2
        ):
            attempt = _try_widening(candidates.active_trace_actions, frame, state, ctx, _dbg)
            if attempt.trial is not None:
                trial = attempt.trial
                yield PilotEvent(
                    "widening_accepted",
                    trial.fork.state.scan_id,
                    {
                        "action": trial.action,
                        "pulse_actions": trial.pulse_actions,
                        "gates": trial.gate_events,
                        "new_key": trial.new_key,
                        "trend": trial.trend,
                        "snapshot": trial.fork_snap,
                        "scan_before": trial.scan_before,
                        "scan_after": trial.fork.state.scan_id,
                    },
                )
                _commit_trial(trial, state, ctx, _dbg_observe, frame.snap)
                yield PilotEvent(
                    "trial_committed",
                    state.work.state.scan_id,
                    {
                        "action": trial.action,
                        "pulse_actions": trial.pulse_actions,
                        "steps": tuple(state.steps),
                        "snapshot": dict(state.work.state.tags),
                    },
                )
                yield from _monitor_trend(trial, frame, state, ctx, _dbg)
                accepted = True

        if accepted:
            state.last_wait_log = None
            continue

        # ── Coast fallback (settle only, no zoom — last resort) ──
        ceiling = min(_LETRUN_DWELL_CEILING, ctx.max_scans - state.work.state.scan_id)
        _settle_cone(state.work, _cone_tags(frame, ctx), floor=2, ceiling=max(2, ceiling))

    yield PilotEvent(
        "finished",
        state.work.state.scan_id,
        {
            "reached": _values_match(state.work.state.tags.get(ctx.target_tag), ctx.target_value),
            "steps": tuple(state.steps),
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
) -> tuple[bool, list[_Step], PLC]:
    """Run the PILOT loop and return the final result."""
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
    ):
        if event.kind == "finished":
            final = event

    if final is None:
        return False, [], plc
    return bool(final.data["reached"]), list(final.data["steps"]), final.data["work"]


# ---------------------------------------------------------------------------
# Path construction
# ---------------------------------------------------------------------------


def _build_path(
    reached: bool,
    recorded_steps: list[_Step],
    target_tag: str,
    target_value: Any,
) -> Path:
    """Convert recorded PILOT steps into a ``Path``."""
    if not reached:
        return Path(
            reachable=False,
            steps=(),
            total_changes=0,
            total_scans=0,
            reason=f"pilot: {target_tag}={target_value!r} not reached within budget",
        )

    path_steps: list[ReachabilityStep] = []
    for s in recorded_steps:
        path_steps.append(
            ReachabilityStep(
                action=s.action,
                source_key=(s.scan_before,),
                dest_key=(s.scan_after,),
                scans=s.scans,
            )
        )

    return Path(
        reachable=True,
        steps=tuple(path_steps),
        total_changes=sum(len(s.action) for s in recorded_steps),
        total_scans=sum(s.scans for s in recorded_steps),
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
) -> tuple[Path | None, TraceChoice | None, frozenset[tuple[str, Any]]]:
    """Resolve an ambiguous Bool-output route choice for an entry point.

    Returns ``(early_path, trace_choice, blocked_choice_actions)``.  When
    *early_path* is not ``None`` the caller returns it immediately — the
    target has multiple output routes and no (or an invalid) choice was given.
    """
    snapshot = dict(plc.state.tags)
    trace_choice: TraceChoice | None = None
    choices: tuple[TraceChoice, ...] = ()
    if _target_is_bool_true(plc, target_tag, target_value) and not _values_match(
        snapshot.get(target_tag), target_value
    ):
        choices = enumerate_trace_choices(target_tag, target_value, snapshot, pdg, program)
        trace_choice = _resolve_trace_choice(choice, choices)
        if choices and choice is None:
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
]:
    """Build prover context for nd_domains and state key projection.

    Returns ``(nd_domains, key_config, evidence)``. Values are ``None`` on
    failure — pilot falls back to Bool-only probing, pivot-tag state keys, and
    local static evidence.
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
            return None, None, None
        nd = getattr(ctx, "nondeterministic_dims", None)
        evidence = build_transition_evidence(ctx)
        if nd:
            logger.info("pilot: nd_domains ready (%d dims)", len(nd))

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
            return nd, None, evidence

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
        return nd, key_config, evidence
    except Exception:  # noqa: BLE001
        logger.debug("pilot: context build failed", exc_info=True)
        return None, None, None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def _parse_target(
    *conditions: Any,
) -> tuple[str, Any]:
    """Extract a single ``(tag_name, target_value)`` from conditions.

    Accepts:
    - A Tag object (implies ``tag == True``)
    - A ``tag == value`` comparison condition (CompareEq)
    """
    from pyrung.core.condition import CompareEq
    from pyrung.core.tag import Tag

    if len(conditions) != 1:
        raise ValueError("pilot currently supports exactly one target condition")

    cond = conditions[0]

    if isinstance(cond, Tag):
        return cond.name, True

    if isinstance(cond, CompareEq):
        tag = cond.tag
        tag_name = tag.name if isinstance(tag, Tag) else str(tag)
        value = cond.value
        return tag_name, value

    raise ValueError(
        f"pilot: cannot extract (tag, value) from {cond!r}. "
        "Pass a Tag object (for Bool targets) or tag == value."
    )


def pilot_events(
    plc: PLC,
    *conditions: Any,
    choice: int | str | TraceChoice | None = None,
    max_scans: int = 3000,
) -> Iterator[PilotEvent]:
    """PILOT on a fork, yielding structured diagnostic events."""
    from pyrung.core.analysis.pdg import build_program_graph

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork()
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence = _build_pilot_context(program, dict(fork.state.tags))
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

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    fork = plc.fork()
    pdg = build_program_graph(program)
    harness_fb = install_harness(fork)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, fork._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, fork._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence = _build_pilot_context(program, dict(fork.state.tags))
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        fork, target_tag, target_value, pdg, program, steerable, opaque_loop, choice
    )
    if early is not None:
        return early

    reached, steps, _work = _pilot_loop(
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
    )

    return _build_path(reached, steps, target_tag, target_value)


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

    target_tag, target_value = _parse_target(*conditions)
    program = plc._program

    pdg = build_program_graph(program)
    harness_fb = install_harness(plc)
    ref_consts = compute_reference_constants(pdg, program)
    steerable = compute_steerable(pdg, plc._known_tags_by_name, program) - harness_fb - ref_consts
    edge_tags = compute_edge_tags(pdg, program)
    resting = compute_resting_values(steerable, plc._known_tags_by_name, pdg, program)
    nd_domains, key_config, evidence = _build_pilot_context(program, dict(plc.state.tags))
    opaque_slices = detect_opaque_pipelines(pdg, program, steerable)
    inf = Compass(opaque_slices)
    opaque_loop = detect_opaque_loop(pdg, program)
    early, trace_choice, blocked_choice_actions = _prepare_trace_choice(
        plc, target_tag, target_value, pdg, program, steerable, opaque_loop, choice
    )
    if early is not None:
        return early

    reached, steps, _work = _pilot_loop(
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
    )

    return _build_path(reached, steps, target_tag, target_value)
