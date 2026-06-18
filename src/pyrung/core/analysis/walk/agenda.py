"""The agenda: one deepest-first loop and the resolvers it drives.

``_drive`` is the scheduler; ``_establish``/``_recover``/``_residuals``
are the resolver pipelines feeding it; the plan tree (``_PlanNode``) is
born here and flattened once at Path-build time.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _EMPTY_CAP,
    _MAX_BACKJUMP_SEGMENTS,
    _MAX_RECHECK_ITERS,
    _NO_MONITORS,
    _PULSE_REACT_CAP,
    HoldStore,
    NoGoodFact,
    NoGoodStore,
    _Action,
    _MustStay,
    _progress_depth_limit,
    _Steer,
    _StepMonitors,
    _values_match,
    _WalkBudget,
    _WalkContext,
)
from pyrung.core.analysis.walk.explore import (
    _counterfactual_hold_sweep,
    _explore,
    _explore_corridor,
)
from pyrung.core.analysis.walk.fold import _build_jump_context
from pyrung.core.analysis.walk.passes import run_walk_passes
from pyrung.core.analysis.walk.priors import (
    _functional_deps,
    _governing,
    _log_decomposition_hint,
    _reference_constants,
    _steer_alphabet,
    _unsatisfied_condition_groups,
    _unsatisfied_conditions,
    _writer_candidates,
    _WriterCandidate,
)
from pyrung.core.analysis.walk.rules import (
    _last_committed_scan,
    mine_regression_holds,
    recursive_cause_evidence,
    temporal_cycle_recovery,
)
from pyrung.core.analysis.walk.steer import _apply_steer, _apply_steer_compound

logger = logging.getLogger(__name__)

# The spin guard is loop machinery (a termination guard, not registry
# advice); this switch exists for the directional A/B in tests only.
_SPIN_GUARD = True

# Cap on goals mined per frontier-terminated why() regression (the fallback
# goal source when explore, static prereqs, and oracle recovery all came up
# empty).  Goals are priors validated by the interpreted walk — the cap only
# bounds wasted budget, never correctness.
_MAX_WHY_GOALS = 6

# Test-only ablation switch for the why-regression fallback goal source
# (mirrors _SPIN_GUARD): directional pins disable it to show the walk
# honestly fails without the source.
_WHY_REGRESSION = True

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC


def _format_chain(chain: Any) -> str:
    lines: list[str] = [f"mode={chain.mode}"]
    for i, step in enumerate(chain.steps):
        trigs = [(t.tag_name, t.to_value) for t in step.triggers]
        enabs = [(e.tag_name, e.value) for e in step.enablers]
        lines.append(f"  step {i}: rung={step.rung_index}")
        if trigs:
            lines.append(f"    triggers: {trigs}")
        if enabs:
            lines.append(f"    enablers: {enabs}")
    roots = [(r.tag_name, r.to_value) for r in chain.conjunctive_roots]
    lines.append(f"  conjunctive_roots: {roots}")
    if chain.ambiguous_roots:
        amb = [(r.tag_name, r.to_value) for r in chain.ambiguous_roots]
        lines.append(f"  ambiguous_roots: {amb}")
    if hasattr(chain, "blockers") and chain.blockers:
        for b in chain.blockers:
            lines.append(f"  blocker: {b.blocked_tag}={b.needed_value!r}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The agenda: one deepest-first loop driving establish pipelines
# ---------------------------------------------------------------------------
#
# The four historical solve loops (plan_walk's compound-goal loop, the
# _walk_to_goal prereq tail, _recover_via_oracle, _check_residuals) are now
# goal sources feeding this one loop: pipelines yield _Request items for
# sub-goals (open conditions) and the scheduler drives them deepest-first.
# Threats (steers against held inputs) are still detected by construction
# inside _explore and resolved by the divest probe; they become agenda
# items only when execution monitors land (post-consolidation).


@dataclass(frozen=True)
class _Request:
    """An open condition headed for the agenda: drive *goal* on *runner*.

    ``provenance`` names the goal source — ``"target-decomposition"`` (a
    term of the user's expression), ``"writer-sp-tree"`` (an unsatisfied
    enabling condition from a writer rung; latch-break goals flow through
    this source too, via ``_unsatisfied_conditions``'s fallback),
    ``"oracle-recheck"`` (a cause()-mined blocker from recovery),
    ``"independent-probe"`` (a speculative per-fork sub-walk),
    ``"why-regression"`` (a frontier-terminated why()-mined sub-goal, the
    fallback source when explore/static/oracle all came up empty), or
    ``"goal"`` (a direct ``_walk_to_goal`` entry).  ``budget`` is the
    remaining action allowance for this branch, sliced exactly as the old
    recursion sliced it; ``visited`` is the branch's cycle guard.
    """

    runner: PLC
    goal: tuple[str, Any]
    depth: int
    visited: frozenset[tuple[str, Any]]
    budget: int
    provenance: str
    monitors: _StepMonitors = _NO_MONITORS


@dataclass
class _PlanNode:
    """One goal's node in the plan tree.

    ``segments`` is the chronological record of how the goal was solved:
    each entry is either a committed action chunk (``list[_Action]`` —
    corridor steps, a merged multi-steer, recovery steps) or a child
    ``_PlanNode`` (a sub-goal driven through the agenda).  The flattened
    tree (:func:`_flatten_plan`) reproduces the work fork's commit order
    exactly and is the single source for ``Path`` steps.

    Failed nodes keep their segments — diagnosis (post-consolidation D4)
    reads the best partial plan from them — and contribute only their
    *solved* descendants to the flattened plan: a sub-goal committed to the
    work fork is part of the executed prefix even when its parent goal later
    failed (the copy-source chains land mode/state commits under
    boundary-unreachable conduit goals, and dropping them made the plan lie
    about the work prefix).  A failed node's own raw segments stay out, as
    before, and replay verification decides whether the plan stands.
    """

    goal: tuple[str, Any] | None  # None for the walk root
    provenance: str
    depth: int
    segments: list[Any] = field(default_factory=list)
    status: str = "open"  # open | solved | failed
    # Diagnosis feed (Stage D4), set when the goal fails: why it failed
    # ("explore-stuck" | "diverged" | "bounds" | "recovery-exhausted" |
    # "no-recovery-goals" | "budget-exhausted") and the last cause()-named
    # blocking goals mined for it.
    failure: str | None = None
    blockers: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class _RecoverySignal:
    """Actionable goals plus the richer facts they came from."""

    goals: list[tuple[str, Any]]
    facts: frozenset[NoGoodFact]


def _holds_snapshot(holds: HoldStore | None) -> list[tuple[str, Any, str]]:
    if holds is None:
        return []
    return sorted((h.name, h.value, h.goal[0]) for h in holds)


def _emit_bounds_refusal(
    ctx: _WalkContext,
    *,
    target_tag: str,
    target_value: Any,
    current_value: Any,
    goal_in_visited: bool,
    depth: int,
    depth_limit: int,
    budget: int,
    provenance: str,
) -> None:
    if ctx.debug_sink is None:
        return
    if goal_in_visited:
        reason = "cycle"
    elif depth > depth_limit:
        reason = f"depth {depth} exceeds limit {depth_limit}"
    elif budget <= 0:
        reason = "branch budget exhausted"
    else:
        reason = "bounds"
    ctx.debug_sink.emit(
        "bounds-refusal",
        tag=target_tag,
        value=target_value,
        depth=depth,
        detail=(
            f"reason={reason}, current={current_value!r}, "
            f"progress_credits={len(ctx.progress_goals)}, depth_limit={depth_limit}, "
            f"budget={budget}, provenance={provenance}"
        ),
    )


def _emit_recovery_snapshot(
    ctx: _WalkContext,
    *,
    work: PLC,
    target_tag: str,
    target_value: Any,
    iteration: int,
    mined_goals: list[tuple[str, Any]],
    depth: int,
    visited: frozenset[tuple[str, Any]],
    budget: int,
    recovered_len: int,
    provenance: str,
) -> None:
    sink = ctx.debug_sink
    if sink is None:
        return
    target_current = work.state.tags.get(target_tag)
    mined = [(tag, value, work.state.tags.get(tag)) for tag, value in mined_goals]
    holds = _holds_snapshot(ctx.holds)
    event = sink.emit(
        "recovery-snapshot",
        tag=target_tag,
        value=target_value,
        depth=depth,
        detail=(
            f"iter={iteration}, target_current={target_current!r}, "
            f"target_desired={target_value!r}, mined_goals={mined}, "
            f"holds={len(holds)} {holds}, progress_credits={len(ctx.progress_goals)}, "
            f"depth_limit={_progress_depth_limit(ctx)}, visited={len(visited)}, "
            f"budget={budget}, recovered_len={recovered_len}, provenance={provenance}"
        ),
    )
    sink.diag.recovery_snapshots.append(event)


def _emit_dead_end_snapshot(
    ctx: _WalkContext,
    frames: list[tuple[_Pipeline, _PlanNode, PLC]],
) -> None:
    sink = ctx.debug_sink
    if sink is None:
        return
    open_goals = [
        (node.goal, node.provenance, node.depth)
        for _gen, node, _runner in frames
        if node.goal is not None
    ]
    holds = _holds_snapshot(ctx.holds)
    progress = sorted(ctx.progress_goals, key=repr)
    recovery = [(ev.tag, ev.value, ev.depth, ev.detail) for ev in sink.diag.recovery_snapshots[-5:]]
    nogoods = list(ctx.nogoods.entries())
    sink.emit(
        "dead-end-snapshot",
        detail="\n".join(
            [
                f"open_goals={open_goals}",
                f"holds={holds}",
                f"progress_credits={progress}",
                f"last_recovery_snapshots={recovery}",
                f"nogoods_count={len(nogoods)}",
                f"nogoods_first5={nogoods[:5]}",
            ]
        ),
    )


def _check_progress_regression(
    ctx: _WalkContext,
    work: PLC,
    completed_node: _PlanNode,
) -> list[tuple[str, Any]]:
    """Detect regressed committed goals and mine protective input holds."""
    sink = ctx.debug_sink
    holds: list[tuple[str, Any]] = []
    for (ptag, _pval), committed in list(ctx.committed_values.items()):
        if (ptag, committed) in ctx.unprotectable_commits:
            continue
        current = work.state.tags.get(ptag)
        if not _values_match(current, committed):
            if sink is not None:
                sink.emit(
                    "progress-regression",
                    tag=ptag,
                    value=committed,
                    depth=completed_node.depth,
                    detail=(
                        f"committed={committed!r}, current={current!r}, "
                        f"clobbered_by={completed_node.goal!r}, "
                        f"provenance={completed_node.provenance}"
                    ),
                )
            mined = mine_regression_holds(ctx, work, (ptag, committed))
            if not mined and (ctx.advice is None or ctx.advice.has("counterfactual_fallback")):
                mined = _counterfactual_fallback_holds(ctx, work, (ptag, committed))
                if mined and sink is not None:
                    sink.emit(
                        "counterfactual-fallback",
                        tag=ptag,
                        value=committed,
                        depth=completed_node.depth,
                        detail=f"swept protective holds: {mined}",
                    )
            if not mined:
                ctx.unprotectable_commits.add((ptag, committed))
                if sink is not None:
                    sink.emit(
                        "unprotectable-regression",
                        tag=ptag,
                        value=committed,
                        depth=completed_node.depth,
                        detail=(
                            f"no hold found for {ptag}={committed!r}; skipping on future frames"
                        ),
                    )
            holds.extend(mined)
    return holds


def _counterfactual_fallback_holds(
    ctx: _WalkContext,
    work: PLC,
    goal: tuple[str, Any],
) -> list[tuple[str, Any]]:
    """Empirical fallback when a regression cause chain named no protective hold.

    Forks the work runner at the pre-departure scan where *goal* still held,
    then runs the cone-bounded sensitivity sweep (Crossings Phase 0).  Returns
    the proposed ``(input, value)`` holds, installed through the same path as
    the cause-mined holds.
    """
    anchor_scan = _last_committed_scan(work, goal[0], goal[1])
    if anchor_scan is None:
        return []
    if ctx.budget.exhausted:
        return []
    try:
        anchor = work.fork(anchor_scan)
    except Exception:  # noqa: BLE001 - empirical fallback is best-effort
        logger.debug("walk: counterfactual anchor fork(%s) raised", anchor_scan, exc_info=True)
        return []
    ctx.budget.forks += 1
    return _counterfactual_hold_sweep(ctx, anchor, goal[0], goal)


def _flatten_plan(node: _PlanNode) -> list[_Action]:
    """Flatten the plan tree into execution-ordered actions.

    Raw action segments contribute only from solved nodes (a failed node's
    own segments may be diagnostic, never-applied explore traces); child
    nodes are descended regardless of status, so sub-goals committed to the
    work fork survive a later failure of their parent goal.
    """
    out: list[_Action] = []
    for seg in node.segments:
        if isinstance(seg, _PlanNode):
            out.extend(_flatten_plan(seg))
        elif node.status == "solved":
            out.extend(seg)
    return out


# A pipeline yields sub-goal requests and finally returns its realized
# actions (None = the goal could not be established).
_Pipeline = Generator["_Request", "list[_Action] | None", "list[_Action] | None"]


def _child_monitors(
    inherited: _StepMonitors,
    work: PLC,
    parent_tag: str,
    parent_value: Any,
) -> _StepMonitors:
    """Context for a child: parent state must stay true until parent lands."""
    from_value = work.state.tags.get(parent_tag)
    if _values_match(from_value, parent_value):
        return inherited
    if not _single_transition_context(from_value, parent_value):
        return inherited
    guard = _MustStay(must=((parent_tag, from_value),), until=((parent_tag, parent_value),))
    return inherited.with_guard(guard)


def _single_transition_context(from_value: Any, to_value: Any) -> bool:
    """Whether a child should preserve the current parent transition context."""
    if isinstance(from_value, bool) and isinstance(to_value, bool):
        return from_value is not to_value
    if (
        isinstance(from_value, int)
        and not isinstance(from_value, bool)
        and isinstance(to_value, int)
        and not isinstance(to_value, bool)
    ):
        return abs(to_value - from_value) == 1
    return False


class _Disposition(IntEnum):
    PREFERRED = 0
    NORMAL = 1
    DEFERRED = 2
    REJECTED = 3


def _candidate_monitors(
    inherited: _StepMonitors,
    candidate: _WriterCandidate | None,
    governing: str,
    gov_value: Any,
) -> _StepMonitors:
    """Promote a selected writer's satisfied guards into child must-stays."""
    if candidate is None or not candidate.satisfied:
        return inherited
    guard = _MustStay(must=candidate.satisfied, until=((governing, gov_value),))
    return inherited.with_guard(guard)


def _must_conditions(monitors: _StepMonitors) -> frozenset[tuple[str, Any]]:
    return frozenset(cond for guard in monitors.must_stay for cond in guard.must)


def _classify_disposition(
    candidate: _WriterCandidate,
    monitors: _StepMonitors,
) -> _Disposition:
    for ptag, pval in candidate.unsatisfied:
        for guard in monitors.must_stay:
            for mtag, mval in guard.must:
                if ptag == mtag and not _values_match(pval, mval):
                    return _Disposition.REJECTED

    must_set = _must_conditions(monitors)
    if must_set and any(cond in must_set for cond in candidate.satisfied):
        return _Disposition.PREFERRED

    if candidate.all_writes & monitors.protected_tags():
        return _Disposition.DEFERRED

    return _Disposition.NORMAL


def _context_score(
    candidate: _WriterCandidate,
    monitors: _StepMonitors,
    visited: frozenset[tuple[str, Any]],
) -> int:
    must_set = _must_conditions(monitors)
    score = 0
    score += 100 * sum(1 for cond in candidate.satisfied if cond in must_set)
    score += 10 * len(candidate.satisfied)
    score += sum(1 for cond in candidate.satisfied if cond in visited)
    return score


def _candidate_sort_key(
    candidate: _WriterCandidate,
    monitors: _StepMonitors,
    visited: frozenset[tuple[str, Any]],
    deprioritized: frozenset[str],
) -> tuple[_Disposition, bool, int, int]:
    return (
        _classify_disposition(candidate, monitors),
        any(t in deprioritized for t, _v in candidate.unsatisfied),
        -_context_score(candidate, monitors, visited),
        len(candidate.unsatisfied),
    )


def _drive(
    ctx: _WalkContext,
    gen: _Pipeline,
    node: _PlanNode,
    runner: PLC,
) -> list[_Action] | None:
    """The agenda loop: drive *gen* and every sub-goal it spawns, deepest-first.

    The frame stack IS the agenda — pushing a yielded :class:`_Request`'s
    pipeline and popping on completion gives deepest-first ordering by
    construction.  Each frame's pipeline starts with a satisfied-check, so
    stale items (goals a sibling already achieved) skip themselves.  The
    global fork/scan budget is checked before every resolver step; on
    exhaustion the whole stack unwinds, open nodes are marked failed, and
    the walk reports an honest "budget exhausted" instead of a wrong
    "unreachable".

    On a goal frame's successful completion the scheduler registers the
    goal's holds (the old ``_walk_to_goal`` wrapper) — every committed
    sub-goal registers its own commitments, including speculative
    independent-probe walks (rolled back by their caller on failure).
    """
    frames: list[tuple[_Pipeline, _PlanNode, PLC]] = [(gen, node, runner)]
    to_send: list[_Action] | None = None
    exhausted = False
    while frames:
        fgen, fnode, frunner = frames[-1]
        if exhausted or ctx.budget.exhausted:
            if not exhausted:
                exhausted = True
                logger.info(
                    "walk: budget exhausted (%d forks, %d scans) — unwinding",
                    ctx.budget.forks,
                    ctx.budget.scans,
                )
                if ctx.debug_sink is not None:
                    ctx.debug_sink.emit(
                        "budget-exhausted",
                        detail=ctx.budget.describe_exhaustion(),
                    )
                    _emit_dead_end_snapshot(ctx, frames)
            fgen.close()
            if fnode.status == "open":
                fnode.status = "failed"
                fnode.failure = fnode.failure or "budget-exhausted"
            frames.pop()
            to_send = None
            continue
        try:
            request = fgen.send(to_send)
        except StopIteration as stop:
            result: list[_Action] | None = stop.value
            fnode.status = "solved" if result is not None else "failed"
            if ctx.debug_sink is not None and fnode.goal is not None:
                if result is not None:
                    ctx.debug_sink.emit(
                        "goal-resolved",
                        tag=fnode.goal[0],
                        value=fnode.goal[1],
                        depth=fnode.depth,
                        detail=f"provenance={fnode.provenance}",
                    )
                else:
                    ctx.debug_sink.emit(
                        "goal-failed",
                        tag=fnode.goal[0],
                        value=fnode.goal[1],
                        depth=fnode.depth,
                        detail=f"provenance={fnode.provenance}, failure={fnode.failure}",
                    )
            if fnode.goal is not None and result is None:
                # Spin guard bookkeeping: this goal failed at this
                # nogood-projected state under the current store generation.
                key = (fnode.goal, ctx.nogoods.project(dict(frunner.state.tags)))
                ctx.failed_goals[key] = len(ctx.nogoods)
            if (
                fnode.goal is not None
                and result
                and ctx.holds is not None
                and _values_match(frunner.state.tags.get(fnode.goal[0]), fnode.goal[1])
            ):
                _commit_holds(ctx, result, fnode.goal[0], fnode.goal[1])
            frames.pop()
            to_send = result
            # Top-level target-decomposition regressions are owned by
            # plan_walk's must-stay reorder loop; repairing them here would
            # hide the regression from that loop's ordering fix.
            if fnode.provenance != "target-decomposition":
                protective = _check_progress_regression(ctx, frunner, fnode)
                if protective and ctx.holds is not None:
                    goal = fnode.goal or ("regression", None)
                    patch: dict[str, Any] = {}
                    held: list[tuple[str, Any]] = []
                    for name, val in protective:
                        ctx.holds.protect(name, val, goal)
                        held_value = ctx.holds.protected().get(name, val)
                        patch[name] = held_value
                        held.append((name, held_value))
                    if patch:
                        frunner.patch(patch)
                        frunner.step()
                        ctx.budget.scans += 1
                        if ctx.debug_sink is not None:
                            ctx.debug_sink.emit(
                                "hold-protect",
                                tag=goal[0],
                                value=goal[1],
                                depth=fnode.depth,
                                detail=f"held inputs: {held}",
                            )
            continue
        # Spin guard (findings §2c): recovery rounds at every level recreate
        # each other's goals; a re-request that already failed at the same
        # nogood-projected state with nothing new learned since cannot
        # succeed — fail it without re-walking the subtree.
        key = (request.goal, ctx.nogoods.project(dict(request.runner.state.tags)))
        if _SPIN_GUARD and ctx.failed_goals.get(key) == len(ctx.nogoods):
            logger.info(
                "walk: spin guard — %s -> %r already failed under unchanged nogoods, skipping",
                request.goal[0],
                request.goal[1],
            )
            if ctx.debug_sink is not None:
                ctx.debug_sink.emit(
                    "spin-guard",
                    tag=request.goal[0],
                    value=request.goal[1],
                    depth=request.depth,
                )
            child = _PlanNode(goal=request.goal, provenance=request.provenance, depth=request.depth)
            child.status = "failed"
            child.failure = "spin-guard"
            fnode.segments.append(child)
            to_send = None
            continue
        if ctx.debug_sink is not None:
            ctx.debug_sink.emit(
                "goal-start",
                tag=request.goal[0],
                value=request.goal[1],
                depth=request.depth,
                detail=f"provenance={request.provenance}",
            )
        child = _PlanNode(goal=request.goal, provenance=request.provenance, depth=request.depth)
        fnode.segments.append(child)
        frames.append((_establish(ctx, request, child), child, request.runner))
        to_send = None
    return None if exhausted else to_send


# ---------------------------------------------------------------------------
# The resolvers: establish / recover / residuals pipelines
# ---------------------------------------------------------------------------


def _advance_work(ctx: _WalkContext, work: PLC, steps: list[_Action]) -> None:
    """Replay *steps* on the work fork so it reaches the post-corridor state."""
    for action, scans in steps:
        if action:
            work.patch(action)
        for _ in range(scans):
            work.step()
        ctx.budget.scans += scans


def _reconcile_divests(steps: list[_Action], holds: HoldStore | None) -> None:
    """Release holds whose protected input a committed action rewrote.

    A committed steer that writes a protected name to a different value was
    divest-probe-approved in ``_explore`` (the probe verified the hold's goal
    survives the change).  Making the release official here — at commit time,
    never mid-explore — keeps speculative branches from mutating the shared
    store and stops later sub-walks from re-probing a settled divest.
    """
    if holds is None or not len(holds):
        return
    for action, _scans in steps:
        if not action:
            continue
        held = holds.protected()
        for name, val in action.items():
            if name in held and not _values_match(held[name], val):
                goal = holds.goal_of(name)
                holds.release(name)
                logger.info(
                    "walk: divest point — released %s (was protecting %s)",
                    name,
                    goal[0] if goal is not None else "?",
                )


def _recheck_prereqs(
    ctx: _WalkContext,
    work: PLC,
    target_tag: str,
    target_value: Any,
    monitors: _StepMonitors = _NO_MONITORS,
) -> _RecoverySignal:
    """Ask the projected causal oracle what still blocks *target_tag*.

    Used after the serial prerequisite walk leaves the governing tag stuck:
    walking one prerequisite may have clobbered an earlier one (a side effect
    that broke a condition the governing tag needs).  ``cause(tag, to=value)``
    returns either a projected chain (proximate-cause ``triggers``) or, when it
    cannot find a single-step path, an ``unreachable`` chain whose ``blockers``
    name the load-bearing condition.  Both are mined for actionable
    ``(tag, value)`` sub-walk goals, skipping any already satisfied.
    """
    try:
        from pyrung.core.analysis.causal.projected import projected_cause

        chain = projected_cause(
            logic=work._logic,
            history=work._history,
            tag=target_tag,
            to_value=target_value,
            pdg=ctx.pdg,
            timelines=work._rung_firing_timelines,
            program=ctx.program,
            nd_domains=ctx.nd_domains,
            func_deps=_functional_deps(ctx.explore_context),
            structural=True,
        )
    except Exception:  # noqa: BLE001 - oracle is best-effort; never break the walk
        # A swallowed oracle crash starves recovery into "no-recovery-goals"
        # (the return_early leak hid behind this for a whole probe arc) —
        # keep the trace visible.
        logger.debug(
            "walk: cause(%s, to=%s) raised; recovery gets no goals",
            target_tag,
            target_value,
            exc_info=True,
        )
        return _RecoverySignal([], frozenset())
    if chain is None:
        return _RecoverySignal([], frozenset())
    if ctx.debug_sink is not None:
        ctx.debug_sink.emit(
            "oracle-cause",
            tag=target_tag,
            value=target_value,
            detail=f"mode={chain.mode}",
            chain_dump=_format_chain(chain),
        )

    tags = work.state.tags
    goals: list[tuple[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    facts: set[NoGoodFact] = set()

    def _add(name: str, value: Any) -> None:
        key = (name, value)
        if key in seen or name == target_tag:
            return
        if _values_match(tags.get(name), value):
            return
        seen.add(key)
        goals.append(key)

    def _add_relation(blocker: Any) -> bool:
        relation = getattr(blocker, "relation", None)
        if relation is None:
            return False
        facts.add(
            NoGoodFact.relation(
                relation.lhs_tag,
                relation.operator,
                relation.rhs_repr,
                relation.rhs_value,
                relation.tags,
            )
        )
        for move in getattr(relation, "candidate_moves", ()):
            _add(move.tag, move.value)
        return bool(getattr(relation, "candidate_moves", ()))

    for step in chain.steps:
        for trig in step.triggers:
            _add(trig.tag_name, trig.to_value)
    for blocker in getattr(chain, "blockers", ()):  # unreachable mode
        used_relation = _add_relation(blocker)
        if not used_relation:
            _add(blocker.blocked_tag, blocker.needed_value)
        for sub in getattr(blocker, "sub_blockers", ()):
            used_sub_relation = _add_relation(sub)
            if not used_sub_relation:
                _add(sub.blocked_tag, sub.needed_value)
    evidence = recursive_cause_evidence(
        ctx,
        work,
        chain,
        target_tag=target_tag,
        monitors=monitors,
    )
    for name, value in evidence.goals:
        _add(name, value)
    if ctx.debug_sink is not None and goals:
        ctx.debug_sink.emit(
            "goals-mined",
            detail=f"source=cause, goals={goals}",
        )
    return _RecoverySignal(goals, frozenset(facts))


def _classify_blockers(
    goals: list[tuple[str, Any]],
    holds: HoldStore | None,
) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
    """Partition cause()-named blockers into program facts and self-conflicts.

    A self-conflict is a blocker whose tag is one of the walker's own held
    external inputs, needed at a value that breaks the hold — there cause()
    is explaining the walker's hand, not the program.  Self-conflicts are
    classified one layer up: routed to the divest probe (release the hold
    when its committed goal survives empirically), never into the
    ``NoGoodStore`` — nogoods record program facts only.
    """
    if holds is None or not len(holds):
        return list(goals), []
    held = holds.protected()
    program_facts: list[tuple[str, Any]] = []
    self_conflicts: list[tuple[str, Any]] = []
    for tag, value in goals:
        if tag in held and not _values_match(held[tag], value):
            self_conflicts.append((tag, value))
        else:
            program_facts.append((tag, value))
    return program_facts, self_conflicts


def _goal_value_plausible(ctx: _WalkContext, tag: str, value: Any) -> bool:
    """Whether *value* has a plausible shape for *tag*'s declared default."""
    known = ctx.known.get(tag)
    default = getattr(known, "default", None)
    if not isinstance(value, bool) or default is None or isinstance(default, bool):
        return True
    return isinstance(default, int) and not isinstance(default, bool) and default in (0, 1)


def _recovery_goals(
    ctx: _WalkContext,
    work: PLC,
    target_tag: str,
    target_value: Any,
    monitors: _StepMonitors = _NO_MONITORS,
) -> _RecoverySignal:
    """Cause()-named blockers, with static prereqs as a typed fallback."""
    signal = _recheck_prereqs(ctx, work, target_tag, target_value, monitors)
    causal = [(t, v) for t, v in signal.goals if _goal_value_plausible(ctx, t, v)]
    if causal:
        return _RecoverySignal(causal, signal.facts)
    static = _unsatisfied_conditions(
        target_tag,
        target_value,
        dict(work.state.tags),
        ctx.pdg,
        ctx.program,
        nd_domains=ctx.nd_domains,
        known=ctx.known,
        func_deps=_functional_deps(ctx.explore_context),
    )
    return _RecoverySignal(static, frozenset())


def _apply_temporal_recovery(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    target_tag: str,
    target_value: Any,
    budget: int,
    monitors: _StepMonitors,
) -> list[_Action] | None:
    """Validate and commit a temporal-rule candidate for a stuck goal."""
    steps = temporal_cycle_recovery(ctx, work, target_tag, target_value, budget, monitors)
    if steps is None:
        return None
    _advance_work(ctx, work, steps)
    _commit_holds(ctx, steps, target_tag, target_value)
    if steps:
        node.segments.append(list(steps))
    return steps


def _apply_recovery_corridor(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    target_tag: str,
    target_value: Any,
    monitors: _StepMonitors,
    alphabet: list[_Steer] | None = None,
) -> list[_Action] | None:
    """Try the target corridor from the current recovered work state."""
    if alphabet is None:
        alphabet = _steer_alphabet(
            target_tag,
            ctx.pdg,
            ctx.known,
            ctx.program,
            target_value,
            nd_domains=ctx.nd_domains,
            advice=ctx.advice,
        )
    steps = _explore(
        ctx,
        work,
        target_tag,
        target_value,
        alphabet,
        holds=ctx.holds,
        monitors=monitors,
    )
    if steps is None:
        return None
    _advance_work(ctx, work, steps)
    _commit_holds(ctx, steps, target_tag, target_value)
    if steps:
        node.segments.append(list(steps))
    return steps


def _divest_blocker(
    ctx: _WalkContext,
    work: PLC,
    name: str,
    needed: Any,
    holds: HoldStore,
) -> bool:
    """Empirically check whether the hold on *name* is releasable for a
    recovery blocker that needs it at *needed*.

    A hold whose recorded goal is already broken on the current work state
    is a dead causal link — its protection interval was violated by the very
    clobber being recovered from — so releasing it cannot break anything
    (the blocker exists precisely because the goal must be re-established).
    Otherwise, fork the work state, write the needed value, settle a few
    scans, and check the hold's goal survives — the seal-in case, where the
    input established a latch and is no longer load-bearing.  ``True`` means
    the hold may be divested; ``False`` means changing the input would break
    a still-standing committed goal (a real conflict, not a stale
    protection).
    """
    goal = holds.goal_of(name)
    if goal is None:
        return True
    if not _values_match(work.state.tags.get(goal[0]), goal[1]):
        return True  # stale hold: its goal is already broken
    probe = work.fork()
    ctx.budget.forks += 1
    probe.patch({name: needed})
    for _ in range(1 + _PULSE_REACT_CAP):
        probe.step()
    ctx.budget.scans += 1 + _PULSE_REACT_CAP
    return _values_match(probe.state.tags.get(goal[0]), goal[1])


def _deprioritized_goal_tags(ctx: _WalkContext) -> frozenset[str]:
    """Implementation-detail goals that stay available but sort last."""
    elided = getattr(ctx.explore_context, "elided_tags", None)
    derived = frozenset(str(k) for k in elided) if isinstance(elided, dict) else frozenset[str]()
    return ctx.ref_constants | derived


def _deprioritized_last(
    goals: list[tuple[str, Any]],
    deprioritized: frozenset[str],
) -> tuple[list[tuple[str, Any]], int]:
    """Stable-partition *goals* so implementation-detail goals come last.

    Returns ``(ordered, deferred_at)`` — the reordered list and the index
    where the deferred tail begins (``len(ordered)`` when nothing is
    deferred, ``0`` when everything is).  Relative order within each half
    is preserved, so an empty *deprioritized* set returns the input unchanged.
    """
    head = [g for g in goals if g[0] not in deprioritized]
    tail = [g for g in goals if g[0] in deprioritized]
    return head + tail, len(head)


def _ref_constants_last(
    goals: list[tuple[str, Any]],
    refs: frozenset[str],
) -> tuple[list[tuple[str, Any]], int]:
    """Compatibility wrapper for the reference-constant ordering tests."""
    return _deprioritized_last(goals, refs)


_SOFT_FAILURES = frozenset({"bounds", "budget-exhausted"})


def _last_child_node(
    node: _PlanNode,
    goal: tuple[str, Any],
    provenance: str,
) -> _PlanNode | None:
    """Most recent child matching *goal*/*provenance* on *node*."""
    for seg in reversed(node.segments):
        if isinstance(seg, _PlanNode) and seg.goal == goal and seg.provenance == provenance:
            return seg
    return None


def _has_soft_failure(node: _PlanNode) -> bool:
    """Whether *node*'s failed subtree contains a soft retryable failure."""
    if node.status == "failed" and node.failure in _SOFT_FAILURES:
        return True
    return any(isinstance(seg, _PlanNode) and _has_soft_failure(seg) for seg in node.segments)


def _cacheable_writer_sp_failure(child: _PlanNode | None) -> bool:
    """Whether a writer-SP failure is hard enough to skip in same-depth recovery."""
    if child is None or child.status != "failed" or child.failure is None:
        return False
    if child.failure in _SOFT_FAILURES:
        return False
    return not _has_soft_failure(child)


def _recover(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    target_tag: str,
    target_value: Any,
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
    monitors: _StepMonitors,
    *,
    skip_goals: frozenset[tuple[str, Any]] = frozenset(),
) -> _Pipeline:
    """Oracle-driven serial-clobber recovery for *target_tag* -> *target_value*.

    The nogood-and-retry stage of the establish pipeline (``yield from``-ed
    by :func:`_establish`): when the normal corridor walk leaves the target
    short of its value — a later sub-walk clobbered an earlier prerequisite
    (a side effect that broke a condition the target needs) — ask the
    projected causal oracle what still blocks it (:func:`_recheck_prereqs`)
    and walk those goals.  Bounded by ``_MAX_RECHECK_ITERS`` rounds.

    Nogood learning (Phase 4): each round records the cause()-named blocking
    assignment when a sub-walk fails or the round re-clobbers (makes no net
    progress).  A recorded blocker refines :func:`_explore`'s seen-key so a
    re-walk can re-enter the governing value under the cleared constraint
    (the corridor that the bare-value seen-key collapsed onto the start
    node), and a repeat of a proven-dead config trips :meth:`is_blocked`
    and bails immediately instead of burning another round.

    The blocking-tag source is :func:`_recheck_prereqs` (cause()-based).  On the
    cross-guard tripwire its ``cause(Latch_B, to=True)`` cleanly names
    ``Guard_A=False`` as the blocker, whereas the static SP-tree
    (``_unsatisfied_conditions``) returns nothing for the guarded arm — so
    cause() is the preferred source feeding both the nogood key and the
    projection.  If cause() returns only type-impossible blockers (for example
    a Bool value for a numeric tag), recovery falls back to the static writer
    prerequisites.  Blockers that are the walker's own held inputs are split
    off first (:func:`_classify_blockers`) and routed to the divest probe —
    the nogood key carries program facts only.

    Applies the recovery steps to *work* in place (recording them on *node*,
    the goal being recovered) and returns them, or ``None`` if the target
    cannot be recovered.  On ``None`` the caller fails the goal, so any
    partial mutation of *work* is discarded with it.
    """
    nogoods = ctx.nogoods
    recovered: list[_Action] = []
    for _ in range(_MAX_RECHECK_ITERS):
        if _values_match(work.state.tags.get(target_tag), target_value) or (
            monitors.active and monitors.landed(dict(work.state.tags))
        ):
            return recovered
        from_value = work.state.tags.get(target_tag)
        signal = _recovery_goals(ctx, work, target_tag, target_value, monitors)
        goals = signal.goals
        if goals and skip_goals:
            skipped = [g for g in goals if g in skip_goals]
            if skipped:
                goals = [g for g in goals if g not in skip_goals]
                logger.info(
                    "walk: recovery skip — %d goal(s) already failed via writer SP at depth %d",
                    len(skipped),
                    depth + 1,
                )
                if ctx.debug_sink is not None:
                    ctx.debug_sink.emit(
                        "recovery-skip",
                        tag=target_tag,
                        value=target_value,
                        depth=depth + 1,
                        detail=f"skipped={skipped}",
                    )
        if not goals:
            if recovered:
                steps = _apply_recovery_corridor(
                    ctx,
                    node,
                    work,
                    target_tag,
                    target_value,
                    monitors,
                )
                if steps is not None:
                    recovered.extend(steps)
                    if len(recovered) > budget:
                        return None
                    if _values_match(work.state.tags.get(target_tag), target_value) or (
                        monitors.active and monitors.landed(dict(work.state.tags))
                    ):
                        return recovered
                    continue
            temporal = _apply_temporal_recovery(
                ctx,
                node,
                work,
                target_tag,
                target_value,
                budget - len(recovered),
                monitors,
            )
            if temporal is not None:
                recovered.extend(temporal)
                if _values_match(work.state.tags.get(target_tag), target_value) or (
                    monitors.active and monitors.landed(dict(work.state.tags))
                ):
                    return recovered
                continue
            if not recovered:
                node.failure = node.failure or "no-recovery-goals"
            return None
        node.blockers = tuple(goals)
        nogoods.recovery_iters += 1
        _emit_recovery_snapshot(
            ctx,
            work=work,
            target_tag=target_tag,
            target_value=target_value,
            iteration=nogoods.recovery_iters,
            mined_goals=goals,
            depth=depth,
            visited=visited,
            budget=budget,
            recovered_len=len(recovered),
            provenance=node.provenance,
        )
        logger.info(
            "walk: recovery iter %d for %s -> %s (%d blocking goal(s))",
            nogoods.recovery_iters,
            target_tag,
            target_value,
            len(goals),
        )

        # Self-conflicts — blockers that are the walker's own held inputs —
        # are classified one layer up and routed to the divest probe; they
        # never enter the NoGoodStore (nogoods record program facts only).
        # A divest-approved hold is released (the re-explore below may then
        # steer it); a rejected one is a real conflict with a committed goal
        # and its blocker is dropped from this round.
        program_facts, self_conflicts = _classify_blockers(goals, ctx.holds)
        relation_facts = signal.facts
        if self_conflicts and ctx.holds is not None:
            rejected: set[tuple[str, Any]] = set()
            for name, needed in self_conflicts:
                held_goal = ctx.holds.goal_of(name)
                if _divest_blocker(ctx, work, name, needed, ctx.holds):
                    ctx.holds.release(name)
                    logger.info(
                        "walk: divest point — released %s for recovery blocker (was protecting %s)",
                        name,
                        held_goal[0] if held_goal is not None else "?",
                    )
                else:
                    rejected.add((name, needed))
                    logger.info(
                        "walk: self-conflict on %s — divest rejected (would break %s), "
                        "blocker dropped",
                        name,
                        held_goal[0] if held_goal is not None else "?",
                    )
            if rejected:
                goals = [g for g in goals if g not in rejected]
                relation_facts = frozenset()
                if not goals:
                    return None

        if program_facts or relation_facts:
            blocking = frozenset(program_facts) | relation_facts
            # Skip a proven-dead ordering: don't burn a round re-running it.
            if nogoods.is_blocked(from_value, target_value, blocking):
                logger.info(
                    "walk: skipping known-blocked config for %s -> %s", target_tag, target_value
                )
                return None
            # Record the cause()-named blocking assignment *before* re-exploring,
            # so the refined seen-key + blocker-clearing move (in
            # :func:`_explore`) can first clear a learned guard and then enter
            # the now-open corridor.
            nogoods.add(from_value, target_value, blocking)

        # Re-explore the governing tag with the refined seen-key.  This is the
        # forward-looking replacement for blindly re-walking the cause goals in
        # cause() order (which makes no progress — e.g. a scoped sub-walk holds
        # a non-retentive timer's input while the guard is still set, then
        # releases that input when it ends, dropping the timer; the later guard
        # clear then lands with the timer condition already gone).
        alphabet = _steer_alphabet(
            target_tag,
            ctx.pdg,
            ctx.known,
            ctx.program,
            target_value,
            nd_domains=ctx.nd_domains,
            advice=ctx.advice,
        )
        steps = _apply_recovery_corridor(
            ctx,
            node,
            work,
            target_tag,
            target_value,
            monitors,
            alphabet,
        )
        if steps is not None:
            recovered.extend(steps)
            if len(recovered) > budget:
                return None
            if _values_match(work.state.tags.get(target_tag), target_value) or (
                monitors.active and monitors.landed(dict(work.state.tags))
            ):
                return recovered
            continue

        # _explore still stuck: try independent-fork walk before serial fallback.
        if len(goals) >= 2:
            indep = _try_independent_walks(
                ctx,
                work,
                goals,
                target_tag,
                target_value,
                budget - len(recovered),
                depth,
                visited,
                monitors=monitors,
            )
            if indep is not None:
                _advance_work(ctx, work, indep)
                node.segments.append(list(indep))
                recovered.extend(indep)
                if len(recovered) > budget:
                    return None
                continue

        # Serial fallback: walk each cause goal one at a time.  Reference
        # constants and derived scratch stay last: both are sound fallbacks,
        # but goalpost-moving or implementation-detail detours should not
        # outrank state/input facts.  Probe the corridor before spending
        # anything on the deferred tail; the tail still runs if the probe
        # stays stuck, so this is ordering, never pruning.
        ordered_goals, deferred_at = _deprioritized_last(goals, _deprioritized_goal_tags(ctx))
        child_mon = (
            monitors
            if ctx.holds is None
            else _child_monitors(monitors, work, target_tag, target_value)
        )
        for gi, (rtag, rval) in enumerate(ordered_goals):
            if gi == deferred_at and 0 < deferred_at < len(ordered_goals):
                probe = _apply_recovery_corridor(
                    ctx,
                    node,
                    work,
                    target_tag,
                    target_value,
                    monitors,
                    alphabet,
                )
                if probe is not None:
                    recovered.extend(probe)
                    if len(recovered) > budget:
                        return None
                    break
            sub = yield _Request(
                runner=work,
                goal=(rtag, rval),
                depth=depth + 1,
                visited=visited,
                budget=budget - len(recovered),
                provenance="oracle-recheck",
                monitors=child_mon,
            )
            if sub is None:
                temporal = _apply_temporal_recovery(
                    ctx,
                    node,
                    work,
                    target_tag,
                    target_value,
                    budget - len(recovered),
                    monitors,
                )
                if temporal is not None:
                    recovered.extend(temporal)
                    return recovered
                return None
            recovered.extend(sub)
            if len(recovered) > budget:
                return None
            if _values_match(work.state.tags.get(target_tag), target_value) or (
                monitors.active and monitors.landed(dict(work.state.tags))
            ):
                return recovered
    if _values_match(work.state.tags.get(target_tag), target_value) or (
        monitors.active and monitors.landed(dict(work.state.tags))
    ):
        return recovered
    node.failure = node.failure or "recovery-exhausted"
    return None


def _why_regression_goals(
    ctx: _WalkContext,
    work: PLC,
    governing: str,
    visited: frozenset[tuple[str, Any]],
) -> list[tuple[str, Any]]:
    """Mine sub-goals from a frontier-terminated ``why()`` on the work fork.

    The frontier is "what the walker can already act on": external inputs
    (``ctx.ext_inputs`` / ``ctx.edge_ext``), tags with a multi-value
    pipeline domain, and goals already committed this walk (the *visited*
    tag names).  ``why_cause`` terminates its backward SP-tree attribution
    at those tags, so the conjunctive roots it returns are the *nearest
    actionable* sub-goals — and, being state-aware, it walks the live
    branch of Or-gates that the static extractor drops (the disjoint-tags
    Or-gate gap).

    Why-mode roots carry the *current* (load-bearing) value of each
    contact; the needed value is the flip for Bool contacts.  Non-Bool
    roots carry no statically-named needed value here and are skipped.
    Everything returned is a prior validated by the interpreted walk —
    a bad goal wastes budget, never produces a wrong plan.
    """
    try:
        from pyrung.core.analysis.causal.why import why_cause

        nd = ctx.nd_domains or {}
        ext = set(ctx.ext_inputs) | ctx.edge_ext
        committed = {t for t, _v in visited}

        def frontier(name: str) -> bool:
            if name == governing:
                return False
            return name in ext or len(nd.get(name, ())) > 1 or name in committed

        chain = why_cause(
            logic=work._logic,
            state=work.state,
            tags=[governing],
            pdg=ctx.pdg,
            program=ctx.program,
            frontier=frontier,
        )
    except Exception:  # noqa: BLE001 - goal source is best-effort; never break the walk
        logger.debug("walk: why(%s) raised; why-regression gets no goals", governing, exc_info=True)
        return []
    if ctx.debug_sink is not None:
        ctx.debug_sink.emit(
            "oracle-why",
            tag=governing,
            detail=f"roots={len(chain.conjunctive_roots)}, steps={len(chain.steps)}",
            chain_dump=_format_chain(chain),
        )

    tags = work.state.tags
    goals: list[tuple[str, Any]] = []
    seen: set[tuple[str, Any]] = set()
    for root in chain.conjunctive_roots:
        name = root.tag_name
        if name == governing:
            continue
        current = tags.get(name)
        if current is not None and not isinstance(current, bool):
            continue
        needed = not bool(current)
        key = (name, needed)
        if key in seen or key in visited:
            continue
        if _values_match(current, needed):
            continue
        seen.add(key)
        goals.append(key)
        if len(goals) >= _MAX_WHY_GOALS:
            break
    return goals


def _why_regression(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    governing: str,
    gov_value: Any,
    alphabet: list[Any],
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
    monitors: _StepMonitors,
) -> _Pipeline:
    """Fallback goal source: frontier-terminated why() when everything else
    came up empty.

    Runs only after the establish pipeline has exhausted its options
    (explore stuck, static prerequisite groups unusable, oracle recovery
    returned nothing).  Mines the nearest actionable sub-goals from a
    frontier-terminated ``why()`` (:func:`_why_regression_goals`), drives
    them through the normal agenda as ``"why-regression"`` requests —
    visited-set, depth bound, nogoods, and global budget all apply — then
    retries the governing corridor.  A goal source feeding the existing
    loop, never a new loop; returns the realized actions or ``None``.
    """
    if not _WHY_REGRESSION or ctx.budget.exhausted:
        return None
    goals = _why_regression_goals(ctx, work, governing, visited)
    if goals and ctx.holds is not None:
        goals = ctx.holds.filter_conflicting(goals)
    if not goals:
        return None
    logger.debug(
        "walk: why-regression for %s -> %r mined %d goal(s): %s",
        governing,
        gov_value,
        len(goals),
        ", ".join(f"{t}={v!r}" for t, v in goals),
    )
    realized: list[_Action] = []
    walked_any = False
    child_mon = (
        monitors if ctx.holds is None else _child_monitors(monitors, work, governing, gov_value)
    )
    for rtag, rval in goals:
        if ctx.budget.exhausted:
            return None
        sub = yield _Request(
            runner=work,
            goal=(rtag, rval),
            depth=depth + 1,
            visited=visited,
            budget=budget - len(realized),
            provenance="why-regression",
            monitors=child_mon,
        )
        if sub is None:
            continue
        walked_any = True
        realized.extend(sub)
        if len(realized) > budget:
            return None
        if _values_match(work.state.tags.get(governing), gov_value) or (
            monitors.active and monitors.landed(dict(work.state.tags))
        ):
            return realized
    if not walked_any:
        return None
    # Retry the corridor now that the why-named sub-goals are in.
    steps = _explore(
        ctx,
        work,
        governing,
        gov_value,
        alphabet,
        holds=ctx.holds,
        monitors=monitors,
    )
    if steps is None:
        return None
    _advance_work(ctx, work, steps)
    _commit_holds(ctx, steps, governing, gov_value)
    if steps:
        node.segments.append(list(steps))
    realized.extend(steps)
    if len(realized) > budget:
        return None
    if _values_match(work.state.tags.get(governing), gov_value) or (
        monitors.active and monitors.landed(dict(work.state.tags))
    ):
        logger.info(
            "walk: why-regression recovered %s -> %s in %d action(s)",
            governing,
            gov_value,
            len(realized),
        )
        return realized
    return None


def _backjump(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    best: Any,
    governing: str,
    gov_value: Any,
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
    monitors: _StepMonitors,
) -> _Pipeline:
    """Backjump resolver (Stage D4): re-enter from the diverged checkpoint.

    *best* is the deepest node a diverged corridor explore discovered — its
    live fork IS the checkpoint (a state the walker can actually produce
    from here).  Re-entry gets a *fresh* corridor search from that
    checkpoint, so long value corridors beyond one ``_explore``'s
    node/corridor caps are walked segment by segment (each diverged-again
    re-entry chains, bounded by ``_MAX_BACKJUMP_SEGMENTS``); when the
    re-entered search is stuck instead, the oracle recovery gets one
    attempt from the deepest checkpoint.

    The resolver is speculative end-to-end: every segment runs on forks,
    recovery uses a detached plan node, and holds are snapshotted — nothing
    touches *work* until a full re-entry has succeeded.  On success the
    adopted actions (checkpoint path + segments) are replayed onto *work*
    and accepted only if the governing value actually lands (*work* may
    have drifted since the corridor was explored — a failed recovery's
    residue stays, by the standing failure semantics — so the replay is
    checked, never assumed).  On any failure the hold store is rolled back
    and ``None`` is returned: the diverged subtree is dropped and the
    caller's failure path proceeds exactly as before — backjump only ever
    adds solutions.
    """
    if ctx.budget.exhausted:
        return None
    holds = ctx.holds
    snap = holds.snapshot() if holds is not None else None

    def _bail() -> None:
        if holds is not None and snap is not None:
            holds.restore(snap)

    def _adopt(actions: list[_Action], via: str) -> list[_Action] | None:
        _advance_work(ctx, work, actions)
        if not (
            _values_match(work.state.tags.get(governing), gov_value)
            or (monitors.active and monitors.landed(dict(work.state.tags)))
        ):
            _bail()
            return None
        _commit_holds(ctx, actions, governing, gov_value)
        node.segments.append(list(actions))
        logger.info(
            "walk: backjump — re-entered from diverged corridor and reached %s -> %s "
            "via %s (%d action(s) total)",
            governing,
            gov_value,
            via,
            len(actions),
        )
        return actions

    alphabet = _steer_alphabet(
        governing,
        ctx.pdg,
        ctx.known,
        ctx.program,
        gov_value,
        nd_domains=ctx.nd_domains,
        advice=ctx.advice,
    )
    adopted: list[_Action] = list(best.path)
    checkpoint = best.plc
    segments = 0
    for _ in range(_MAX_BACKJUMP_SEGMENTS):
        if ctx.budget.exhausted or len(adopted) > budget:
            _bail()
            return None
        trial = checkpoint.fork()
        ctx.budget.forks += 1
        res = _explore_corridor(
            ctx,
            trial,
            governing,
            gov_value,
            alphabet,
            holds=ctx.holds,
            monitors=monitors,
        )
        if res.steps is not None:
            adopted.extend(res.steps)
            if len(adopted) > budget:
                _bail()
                return None
            return _adopt(adopted, f"{segments + 1} corridor segment(s)")
        if res.outcome == "diverged" and res.best is not None and res.best.path:
            adopted.extend(res.best.path)
            checkpoint = res.best.plc
            segments += 1
            continue
        break  # stuck from here — give the oracle one shot below

    # Final attempt: oracle recovery from the deepest checkpoint reached.
    trial = checkpoint.fork()
    ctx.budget.forks += 1
    bj_node = _PlanNode(goal=(governing, gov_value), provenance="backjump", depth=depth)
    rec = yield from _recover(
        ctx,
        bj_node,
        trial,
        governing,
        gov_value,
        max(0, budget - len(adopted)),
        depth,
        visited,
        monitors,
    )
    if rec is None or not (
        _values_match(trial.state.tags.get(governing), gov_value)
        or (monitors.active and monitors.landed(dict(trial.state.tags)))
    ):
        _bail()
        return None
    adopted.extend(rec)
    if len(adopted) > budget:
        _bail()
        return None
    return _adopt(adopted, "checkpoint recovery")


def _extract_holds(
    actions: list[_Action],
    cone: frozenset[str],
    ext_set: set[str],
    *,
    strict: bool = True,
) -> dict[str, Any] | None:
    """Mine external-input holds from a realized action list.

    Filters to *cone* (the goal's upstream slice) so cross-cone steer
    releases are not captured as holds.  Two modes: *strict* returns
    ``None`` when the same input is patched to two different values — an
    incoherent set of *simultaneous* holds, the independent-fork merge
    case.  Non-strict keeps the last write: external inputs are sticky, so
    the final patched value IS the input's state at commit — the
    hold-registration case (a corridor may legitimately release and
    re-pulse the same input along the way).
    """
    holds: dict[str, Any] = {}
    for action, _scans in actions:
        for tag, val in action.items():
            if tag in ext_set and tag in cone:
                if strict and tag in holds and holds[tag] != val:
                    return None
                holds[tag] = val
    return holds


def _credit_progress(ctx: _WalkContext, tag: str, value: Any) -> bool:
    """Record one committed-work progress credit for ``(tag, value)``."""
    goal = (tag, value)
    if goal in ctx.progress_goals:
        return False
    ctx.progress_goals.add(goal)
    return True


def _commit_holds(
    ctx: _WalkContext,
    steps: list[_Action],
    tag: str,
    value: Any,
) -> None:
    """Reconcile divests in committed *steps*, then register the holds the
    committed ``(tag, value)`` goal depends on.

    Called at every corridor commit point — including the delegate-corridor
    path inside ``_walk_goal_inner`` and recovery's re-explore, which
    drive a governing tag via ``_explore`` without going through the
    ``_walk_goal`` wrapper.  Registering *before* residuals are walked is
    what protects the fresh corridor from the residual walk's releases.
    """
    if not steps:
        return
    _credit_progress(ctx, tag, value)
    ctx.committed_values[(tag, value)] = value
    if ctx.debug_sink is not None:
        ctx.debug_sink.diag.committed_values[(tag, value)] = value
    holds = ctx.holds
    if holds is None:
        return
    _reconcile_divests(steps, holds)
    mined = _extract_holds(
        steps, ctx.pdg.upstream_slice(tag), set(ctx.ext_inputs) | ctx.edge_ext, strict=False
    )
    if mined:
        for name, val in mined.items():
            holds.protect(name, val, (tag, value))
        if ctx.debug_sink is not None:
            held = [(n, v) for n, v in mined.items()]
            ctx.debug_sink.emit(
                "hold-protect",
                tag=tag,
                value=value,
                detail=f"held inputs: {held}",
            )


def _try_independent_walks(
    ctx: _WalkContext,
    work: PLC,
    prereqs: list[tuple[str, Any]],
    governing: str,
    gov_value: Any,
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
    *,
    all_goals: list[tuple[str, Any]] | None = None,
    monitors: _StepMonitors = _NO_MONITORS,
) -> list[_Action] | None:
    """Walk independent prerequisites on separate forks, merge holds.

    When two or more prerequisites need different external inputs held
    simultaneously (e.g. two SFC enables gating independent timers), serial
    walking clobbers earlier holds because the pulse steer releases all
    currently-held inputs.  If the prerequisites have disjoint upstream cones,
    each can be solved independently: walk each on a fresh fork, extract the
    external-input holds, and apply them simultaneously via a multi-steer on a
    trial fork.  The time-fold handles multi-timer convergence — after the
    first timer completes, the fold continues to the next crossing until the
    governing tag transitions.

    When *all_goals* is provided (compound targets), the fold continues until
    every ``(tag, value)`` in *all_goals* is satisfied, using sequential
    monitor iteration via :func:`_apply_steer_compound`.

    Returns the realized action list (trial fork verified) or ``None``.
    Does not modify *work* — the caller advances it on success.
    """
    if len(prereqs) < 2:
        return None

    holds = ctx.holds
    ptags = [t for t, _v in prereqs]
    exclude = {governing} | set(ptags)
    cones: list[frozenset[str]] = []
    for t in ptags:
        cones.append(ctx.pdg.upstream_slice(t))
    for i in range(len(cones)):
        for j in range(i + 1, len(cones)):
            if (cones[i] & cones[j]) - exclude:
                return None

    # The per-prereq walks below are speculative (separate forks; only the
    # merged multi-steer is committed) — roll back any holds they register
    # when the attempt fails.
    snap = holds.snapshot() if holds is not None else None
    progress_snap = set(ctx.progress_goals)

    def _bail() -> None:
        if holds is not None and snap is not None:
            holds.restore(snap)
        ctx.progress_goals = set(progress_snap)

    def _rollback_speculative_credits() -> None:
        ctx.progress_goals = set(progress_snap)

    ext_set = set(ctx.ext_inputs) | ctx.edge_ext
    required_holds: dict[str, Any] = {}
    hold_goals: dict[str, tuple[str, Any]] = {}
    for idx, (ptag, pval) in enumerate(prereqs):
        trial = work.fork()
        ctx.budget.forks += 1
        # Speculative sub-walk: a separate agenda run on the trial fork.  Its
        # plan node stays detached from the main tree — only the merged
        # multi-steer below is committed; the sub-walk exists to mine holds.
        sub_req = _Request(
            runner=trial,
            goal=(ptag, pval),
            depth=depth + 1,
            visited=visited,
            budget=budget,
            provenance="independent-probe",
            monitors=monitors,
        )
        sub_node = _PlanNode(goal=sub_req.goal, provenance=sub_req.provenance, depth=sub_req.depth)
        sub = _drive(ctx, _establish(ctx, sub_req, sub_node), sub_node, trial)
        if sub is None:
            _bail()
            return None
        mined = _extract_holds(sub, cones[idx], ext_set)
        if mined is None:
            _bail()
            return None
        for tag, val in mined.items():
            if tag in required_holds and required_holds[tag] != val:
                _bail()
                return None
            required_holds[tag] = val
            hold_goals.setdefault(tag, (ptag, pval))

    if len(required_holds) < 2:
        _bail()
        return None

    trial = work.fork()
    ctx.budget.forks += 1
    steer = _Steer("multi", patch=required_holds)
    prot = holds.protected_names() if holds is not None else frozenset()

    if all_goals is not None:
        realized = _apply_steer_compound(
            ctx, trial, steer, all_goals, _EMPTY_CAP, protected=prot, monitors=monitors
        )
        ok = realized is not None and all(
            _values_match(trial.state.tags.get(t), v) for t, v in all_goals
        )
    else:
        from_value = trial.state.tags.get(governing)
        realized = _apply_steer(
            ctx,
            trial,
            steer,
            governing,
            from_value,
            _EMPTY_CAP,
            protected=prot,
            monitors=monitors,
        )
        ok = realized is not None and _values_match(trial.state.tags.get(governing), gov_value)

    if ok and realized is not None:
        _rollback_speculative_credits()
        if realized:
            _credit_progress(ctx, governing, gov_value)
        if holds is not None:
            for tag, val in required_holds.items():
                holds.protect(tag, val, hold_goals.get(tag, (governing, gov_value)))
        logger.info(
            "walk: independent-fork hold for %s -> %s (%d action(s), holds: %s)",
            governing,
            gov_value,
            len(realized),
            ", ".join(sorted(str(k) for k in required_holds)),
        )
        return realized
    _bail()
    return None


def _walk_to_goal(
    work: PLC,
    target_tag: str,
    target_value: Any,
    pdg: ProgramGraph,
    program: Any,
    known: dict[str, Any],
    ext_inputs: list[str],
    edge_ext: set[str],
    budget: int,
    depth: int = 0,
    visited: frozenset[tuple[str, Any]] = frozenset(),
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    explore_context: Any = None,
    *,
    nogoods: NoGoodStore | None = None,
    holds: HoldStore | None = None,
    disabled_passes: frozenset[str] = frozenset(),
    fork_budget: int | None = None,
    wall_budget_s: float | None = None,
) -> list[_Action] | None:
    """Single-goal walk entry with explicit parameters.

    Builds the per-walk :class:`_WalkContext` (jump context, probe memo,
    budget counters, pass-registry advice) and drives one goal through the
    agenda.  ``plan_walk`` builds its context once and drives its own root
    pipeline; this wrapper serves direct single-goal callers (tests drive
    it as the walk entry).  Any harness must already be installed on *work*
    so the jump context sees the right profile-feedback tags.
    *disabled_passes* ablates registry advice (the matrix-test hook);
    *fork_budget*/*wall_budget_s* tighten the global budget caps (the
    honest-exhaustion test hook).
    """
    advice, journal = run_walk_passes(program, pdg, disabled=disabled_passes)
    walk_budget = _WalkBudget(max_wall_s=wall_budget_s)
    if fork_budget is not None:
        walk_budget.max_forks = fork_budget
    ctx = _WalkContext(
        pdg=pdg,
        program=program,
        known=known,
        ext_inputs=ext_inputs,
        edge_ext=edge_ext,
        jump_ctx=_build_jump_context(
            work,
            pdg,
            program,
            target_names=frozenset({target_tag}),
            advice=advice,
            journal=journal,
        ),
        nogoods=nogoods if nogoods is not None else NoGoodStore(),
        holds=holds,
        nd_domains=nd_domains,
        explore_context=explore_context,
        budget=walk_budget,
        ref_constants=(
            _reference_constants(pdg, program)
            if advice is None or advice.has("ref_constant_order")
            else frozenset()
        ),
        advice=advice,
        journal=journal,
    )
    req = _Request(
        runner=work,
        goal=(target_tag, target_value),
        depth=depth,
        visited=visited,
        budget=budget,
        provenance="goal",
    )
    node = _PlanNode(goal=req.goal, provenance=req.provenance, depth=req.depth)
    return _drive(ctx, _establish(ctx, req, node), node, work)


def _establish(ctx: _WalkContext, req: _Request, node: _PlanNode) -> _Pipeline:
    """The establish resolver: drive *req*'s goal, discovering prerequisites.

    The uniform strategy order: satisfied-check (stale items skip
    themselves) → independence gate + merge-holds
    (:func:`_try_independent_walks`) → corridor explore (:func:`_explore`)
    → sub-goal recursion (prerequisites yielded as ``writer-sp-tree``
    requests) → nogood + retry (:func:`_recover`), with the residual sweep
    (:func:`_residuals`) as the common tail.  Hold registration for the
    committed goal happens in the scheduler when this pipeline completes.

    ``ctx.nogoods`` (Phase 4) carries accumulated precondition-failure
    memory shared across the whole walk; ``ctx.holds`` carries the
    walk-wide protected-hold store.

    The goal's work fork (``req.runner``) is modified in place (advanced
    through every successful sub-corridor).  Returns the accumulated action
    list, or ``None``.
    """
    work = req.runner
    target_tag, target_value = req.goal
    budget = req.budget
    depth = req.depth
    visited = req.visited

    if _values_match(work.state.tags.get(target_tag), target_value) or (
        req.monitors.active and req.monitors.landed(dict(work.state.tags))
    ):
        return []
    goal_key = (target_tag, target_value)
    depth_limit = _progress_depth_limit(ctx)
    if goal_key in visited or depth > depth_limit or budget <= 0:
        logger.debug(
            "walk: goal (%s, %r) refused at entry: visited=%s depth=%d "
            "depth_limit=%d progress_credits=%d budget=%d",
            target_tag,
            target_value,
            goal_key in visited,
            depth,
            depth_limit,
            len(ctx.progress_goals),
            budget,
        )
        node.failure = "bounds"
        _emit_bounds_refusal(
            ctx,
            target_tag=target_tag,
            target_value=target_value,
            current_value=work.state.tags.get(target_tag),
            goal_in_visited=goal_key in visited,
            depth=depth,
            depth_limit=depth_limit,
            budget=budget,
            provenance=req.provenance,
        )
        return None
    visited = visited | {goal_key}

    governing, gov_value = _governing(
        target_tag,
        target_value,
        ctx.pdg,
        ctx.program,
        explore_context=ctx.explore_context,
        plc=work,
        probe_memo=ctx.probe_memo,
        advice=ctx.advice,
    )
    if ctx.debug_sink is not None:
        is_delegate = governing != target_tag
        ctx.debug_sink.emit(
            "governing-selected",
            tag=target_tag,
            value=target_value,
            detail=f"governing={governing}={gov_value!r}, delegate={is_delegate}",
        )

    # Independent-fork walk: when the governing tag is a delegate, the
    # governing corridor may succeed but residual conditions clobber it
    # (e.g. two independent enables that must be held simultaneously).
    # Before committing to the delegate corridor, check whether the
    # *target's* prerequisites are independent and can be merged.
    if governing != target_tag:
        target_prereqs = _unsatisfied_conditions(
            target_tag,
            target_value,
            dict(work.state.tags),
            ctx.pdg,
            ctx.program,
            nd_domains=ctx.nd_domains,
            known=ctx.known,
            func_deps=_functional_deps(ctx.explore_context),
        )
        if len(target_prereqs) >= 2:
            merged = _try_independent_walks(
                ctx,
                work,
                target_prereqs,
                target_tag,
                target_value,
                budget,
                depth,
                visited,
                monitors=req.monitors,
            )
            if merged is not None:
                _advance_work(ctx, work, merged)
                node.segments.append(list(merged))
                all_steps = list(merged)
                if len(all_steps) > budget:
                    return None
                if _values_match(work.state.tags.get(target_tag), target_value):
                    return all_steps
                return (
                    yield from _residuals(
                        ctx,
                        node,
                        work,
                        target_tag,
                        target_value,
                        target_tag,
                        budget - len(all_steps),
                        depth,
                        visited,
                        all_steps,
                        req.monitors,
                    )
                )

    alphabet = _steer_alphabet(
        governing,
        ctx.pdg,
        ctx.known,
        ctx.program,
        gov_value,
        nd_domains=ctx.nd_domains,
        advice=ctx.advice,
    )

    explore_res = _explore_corridor(
        ctx,
        work,
        governing,
        gov_value,
        alphabet,
        holds=ctx.holds,
        monitors=req.monitors,
    )
    steps = explore_res.steps

    if steps is None:
        context_groups = ctx.advice is None or ctx.advice.has("context_aware_groups")
        writer_candidates: list[_WriterCandidate] = []
        prereq_groups: list[list[tuple[str, Any]]] = []
        if context_groups:
            prereqs, writer_candidates = _writer_candidates(
                governing,
                gov_value,
                dict(work.state.tags),
                ctx.pdg,
                ctx.program,
                nd_domains=ctx.nd_domains,
                known=ctx.known,
                func_deps=_functional_deps(ctx.explore_context),
            )
        else:
            prereqs, prereq_groups = _unsatisfied_condition_groups(
                governing,
                gov_value,
                dict(work.state.tags),
                ctx.pdg,
                ctx.program,
                nd_domains=ctx.nd_domains,
                known=ctx.known,
                func_deps=_functional_deps(ctx.explore_context),
            )
        if not prereqs and not any(not c.unsatisfied for c in writer_candidates):
            # The static SP-tree sweep found nothing actionable, but the
            # projected causal oracle may still name a blocker the SP tree
            # can't surface (e.g. a guard gating a timer-done arm — cause()
            # reports ``Guard_A=False`` where ``_unsatisfied_conditions``
            # returns []).  Try the cause()-driven recovery (with nogood
            # learning) before giving up.
            rec = yield from _recover(
                ctx, node, work, governing, gov_value, budget, depth, visited, req.monitors
            )
            if rec is None and explore_res.outcome == "diverged" and explore_res.best is not None:
                # D4 backjump: the corridor moved but never landed, and
                # recovery from the pre-corridor state failed — re-enter
                # from the diverged checkpoint before giving up.
                rec = yield from _backjump(
                    ctx,
                    node,
                    work,
                    explore_res.best,
                    governing,
                    gov_value,
                    budget,
                    depth,
                    visited,
                    req.monitors,
                )
            if rec is None:
                rec = _apply_temporal_recovery(
                    ctx,
                    node,
                    work,
                    governing,
                    gov_value,
                    budget,
                    req.monitors,
                )
            if rec is None:
                # Last-ditch goal source: frontier-terminated why() on the
                # work fork names the nearest actionable sub-goals (walks
                # the live Or-gate branch the static extractor drops).
                rec = yield from _why_regression(
                    ctx,
                    node,
                    work,
                    governing,
                    gov_value,
                    alphabet,
                    budget,
                    depth,
                    visited,
                    req.monitors,
                )
            if rec is None:
                node.failure = node.failure or (
                    "explore-stuck" if explore_res.outcome == "stuck" else "diverged"
                )
                return None
            logger.info(
                "walk: recovered %s -> %s via oracle (no static prereqs) in %d action(s)",
                governing,
                gov_value,
                len(rec),
            )
            return (
                yield from _residuals(
                    ctx,
                    node,
                    work,
                    target_tag,
                    target_value,
                    governing,
                    budget - len(rec),
                    depth,
                    visited,
                    list(rec),
                    req.monitors,
                )
            )
        # Per-writer prerequisite groups (writer disjunction): each group is
        # one writer's own unsatisfied conditions — a genuine alternative,
        # since arming any single writer produces the value.  Walk the
        # smallest-unsatisfied group first, probing the corridor between
        # groups, so a nearly-satisfied writer is tried before another
        # writer's expensive chain ever spawns sub-goals.  Ordering only,
        # never pruning: any union pair not covered by a group rides in a
        # final remainder group, so ablation restores the serial union.
        groups_enabled = ctx.advice is None or ctx.advice.has("writer_prereq_groups")
        ordered_groups: list[tuple[list[tuple[str, Any]], _WriterCandidate | None]]
        if context_groups and groups_enabled and writer_candidates:
            covered = {p for c in writer_candidates for p in c.unsatisfied}
            # Reference constants and derived scratch sort behind every
            # other alternative: they are sound but indirect fallbacks.
            deprioritized = _deprioritized_goal_tags(ctx)
            ordered_candidates = [
                c
                for c in sorted(
                    writer_candidates,
                    key=lambda c: _candidate_sort_key(c, req.monitors, visited, deprioritized),
                )
                if _classify_disposition(c, req.monitors) is not _Disposition.REJECTED
            ]
            ordered_groups = [(list(c.unsatisfied), c) for c in ordered_candidates]
            remainder = [p for p in prereqs if p not in covered]
            if remainder:
                ordered_groups.append((remainder, None))
        else:
            use_groups = groups_enabled and len(prereq_groups) > 1
            if use_groups:
                covered = {p for g in prereq_groups for p in g}
                # Reference constants and derived scratch sort behind every
                # other alternative: they are sound but indirect fallbacks.
                deprioritized = _deprioritized_goal_tags(ctx)
                ordered_groups = [
                    (list(g), None)
                    for g in sorted(
                        prereq_groups,
                        key=lambda g: (any(t in deprioritized for t, _v in g), len(g)),
                    )
                ]
                remainder = [p for p in prereqs if p not in covered]
                if remainder:
                    ordered_groups.append((remainder, None))
            else:
                ordered_groups = [(prereqs, None)] if prereqs else []

        checkpoint: PLC | None = None
        all_steps: list[_Action] = []
        walked: set[tuple[str, Any]] = set()
        failed_writer_sp_goals: set[tuple[str, Any]] = set()
        probe_hit = None
        base_child_mon = (
            req.monitors
            if ctx.holds is None
            else _child_monitors(req.monitors, work, governing, gov_value)
        )
        for gi, (group, candidate) in enumerate(ordered_groups):
            child_mon = _candidate_monitors(base_child_mon, candidate, governing, gov_value)
            pending = [p for p in group if p not in walked]
            walked.update(pending)

            if candidate is not None and not candidate.unsatisfied:
                probe_res = _explore_corridor(
                    ctx,
                    work,
                    governing,
                    gov_value,
                    alphabet,
                    holds=ctx.holds,
                    monitors=child_mon,
                )
                if probe_res.steps is not None:
                    probe_hit = probe_res
                    break
                continue

            # Independent-fork walk: when a group's prerequisites each need
            # their own external input held, serial walking clobbers earlier
            # holds.  Walk each on an independent fork, merge holds, apply
            # simultaneously.
            if len(pending) >= 2:
                merged = _try_independent_walks(
                    ctx,
                    work,
                    pending,
                    governing,
                    gov_value,
                    budget - len(all_steps),
                    depth,
                    visited,
                    monitors=child_mon,
                )
                if merged is not None:
                    _advance_work(ctx, work, merged)
                    node.segments.append(list(merged))
                    all_steps.extend(merged)
                    if len(all_steps) > budget:
                        return None
                    return (
                        yield from _residuals(
                            ctx,
                            node,
                            work,
                            target_tag,
                            target_value,
                            governing,
                            budget - len(all_steps),
                            depth,
                            visited,
                            all_steps,
                            req.monitors,
                        )
                    )

            if checkpoint is None:
                # Snapshot the pre-clobber state before walking prerequisites
                # serially.  Tier 2 (force-and-solve) will fork from here to
                # solve interfering subsystems independently; for now it
                # anchors the diagnostic below.
                checkpoint = work.fork()
                ctx.budget.forks += 1

            for ptag, pval in pending:
                sub = yield _Request(
                    runner=work,
                    goal=(ptag, pval),
                    depth=depth + 1,
                    visited=visited,
                    budget=budget - len(all_steps),
                    provenance="writer-sp-tree",
                    monitors=child_mon,
                )
                if sub is None:
                    child = _last_child_node(node, (ptag, pval), "writer-sp-tree")
                    if _cacheable_writer_sp_failure(child):
                        failed_writer_sp_goals.add((ptag, pval))
                    continue
                all_steps.extend(sub)
                if _values_match(work.state.tags.get(governing), gov_value) or (
                    req.monitors.active and req.monitors.landed(dict(work.state.tags))
                ):
                    break

            if _values_match(work.state.tags.get(governing), gov_value) or (
                req.monitors.active and req.monitors.landed(dict(work.state.tags))
            ):
                break

            if gi < len(ordered_groups) - 1 and pending:
                # Between groups: probe whether this writer's group already
                # opened the corridor — if so, the remaining (more expensive)
                # alternatives are never walked.
                probe_res = _explore_corridor(
                    ctx,
                    work,
                    governing,
                    gov_value,
                    alphabet,
                    holds=ctx.holds,
                    monitors=child_mon,
                )
                if probe_res.steps is not None:
                    probe_hit = probe_res
                    break

        post_res = (
            probe_hit
            if probe_hit is not None
            else _explore_corridor(
                ctx,
                work,
                governing,
                gov_value,
                alphabet,
                holds=ctx.holds,
                monitors=req.monitors,
            )
        )
        steps = post_res.steps

        if steps is None:
            # Serial-clobber recovery: walking a later prerequisite may have
            # broken an earlier one.  Ask the oracle what still blocks the
            # governing value and walk those, then proceed as if _explore had
            # found a zero-action corridor (recovery already advanced *work*).
            rec = yield from _recover(
                ctx,
                node,
                work,
                governing,
                gov_value,
                budget - len(all_steps),
                depth,
                visited,
                req.monitors,
                skip_goals=frozenset(failed_writer_sp_goals),
            )
            if rec is None and post_res.outcome == "diverged" and post_res.best is not None:
                # D4 backjump: re-enter from the post-serial corridor's
                # diverged checkpoint before giving up.
                rec = yield from _backjump(
                    ctx,
                    node,
                    work,
                    post_res.best,
                    governing,
                    gov_value,
                    budget - len(all_steps),
                    depth,
                    visited,
                    req.monitors,
                )
            if rec is None:
                rec = _apply_temporal_recovery(
                    ctx,
                    node,
                    work,
                    governing,
                    gov_value,
                    budget - len(all_steps),
                    req.monitors,
                )
            if rec is None:
                # Fallback goal source after the serial walk + oracle both
                # came up short: frontier-terminated why() on the work fork.
                rec = yield from _why_regression(
                    ctx,
                    node,
                    work,
                    governing,
                    gov_value,
                    alphabet,
                    budget - len(all_steps),
                    depth,
                    visited,
                    req.monitors,
                )
            if rec is None:
                node.failure = node.failure or (
                    "explore-stuck" if post_res.outcome == "stuck" else "diverged"
                )
                _log_decomposition_hint(
                    target_tag,
                    prereqs,
                    ctx.pdg,
                    checkpoint,
                    nogoods=ctx.nogoods,
                    transition=(work.state.tags.get(governing), gov_value),
                )
                return None
            logger.info(
                "walk: recovered %s -> %s via oracle re-check in %d action(s)",
                governing,
                gov_value,
                len(rec),
            )
            all_steps.extend(rec)
            steps = []  # recovery already advanced *work*

        logger.info(
            "walk: corridor on %s reached %s in %d action(s)",
            governing,
            gov_value,
            len(steps),
        )
        _advance_work(ctx, work, steps)
        _commit_holds(ctx, steps, governing, gov_value)
        if steps:
            node.segments.append(list(steps))
        all_steps.extend(steps)
        if len(all_steps) > budget:
            return None
        return (
            yield from _residuals(
                ctx,
                node,
                work,
                target_tag,
                target_value,
                governing,
                budget - len(all_steps),
                depth,
                visited,
                all_steps,
                req.monitors,
            )
        )

    logger.info(
        "walk: corridor on %s reached %s in %d action(s)",
        governing,
        gov_value,
        len(steps),
    )
    _advance_work(ctx, work, steps)
    # Register the delegate corridor's commitments before residual walking —
    # this corridor was driven by _explore directly (not its own agenda
    # goal), so the scheduler never sees it as a completed goal frame.
    _commit_holds(ctx, steps, governing, gov_value)
    if steps:
        node.segments.append(list(steps))
    all_steps = list(steps)
    if len(all_steps) > budget:
        return None
    return (
        yield from _residuals(
            ctx,
            node,
            work,
            target_tag,
            target_value,
            governing,
            budget - len(all_steps),
            depth,
            visited,
            all_steps,
            req.monitors,
        )
    )


def _residuals(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    target_tag: str,
    target_value: Any,
    governing: str,
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
    all_steps: list[_Action],
    monitors: _StepMonitors,
) -> _Pipeline:
    """After driving the governing tag, walk any residual conditions.

    The common tail of the establish pipeline: uses the oracle-driven
    re-check loop (:func:`_recover`) to walk the target's still-unsatisfied
    conditions.  This subsumes the older static ``_unsatisfied_conditions``
    residual sweep: walking residuals serially can clobber the governing
    corridor (a side effect of a later condition breaking an earlier one),
    and the oracle loop both walks the residuals and recovers from such
    clobbers in a single bounded loop.
    """
    if _values_match(work.state.tags.get(target_tag), target_value) or (
        monitors.active and monitors.landed(dict(work.state.tags))
    ):
        return all_steps

    if target_tag != governing:
        rec = yield from _recover(
            ctx,
            node,
            work,
            target_tag,
            target_value,
            budget - len(all_steps),
            depth,
            visited,
            monitors,
        )
        if rec is not None:
            all_steps.extend(rec)
        else:
            temporal = _apply_temporal_recovery(
                ctx,
                node,
                work,
                target_tag,
                target_value,
                budget - len(all_steps),
                monitors,
            )
            if temporal is not None:
                all_steps.extend(temporal)

    if _values_match(work.state.tags.get(target_tag), target_value) or (
        monitors.active and monitors.landed(dict(work.state.tags))
    ):
        return all_steps

    # Unrecoverable: log a Tier 2 hint if the target's conditions couple.
    coupling = _unsatisfied_conditions(
        target_tag,
        target_value,
        dict(work.state.tags),
        ctx.pdg,
        ctx.program,
        nd_domains=ctx.nd_domains,
        known=ctx.known,
        func_deps=_functional_deps(ctx.explore_context),
    )
    _log_decomposition_hint(
        target_tag,
        [(governing, True), *coupling],
        ctx.pdg,
        nogoods=ctx.nogoods,
        transition=(work.state.tags.get(target_tag), target_value),
    )
    return None
