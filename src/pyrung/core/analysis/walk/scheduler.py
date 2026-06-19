"""The frame-stack scheduler and shared helpers.

``_drive`` pushes ``_establish`` frames and pops results, deepest-first.
The plan tree (``_PlanNode``) is born here and flattened at Path-build time.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _NO_MONITORS,
    HoldStore,
    _Action,
    _MustStay,
    _StepMonitors,
    _values_match,
    _WalkContext,
)
from pyrung.core.analysis.walk.explore import _counterfactual_hold_sweep
from pyrung.core.analysis.walk.rules import (
    _last_committed_scan,
    mine_regression_holds,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


# The spin guard is loop machinery (a termination guard, not registry
# advice); this switch exists for the directional A/B in tests only.
_SPIN_GUARD = True


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
    from pyrung.core.analysis.walk.establish import _establish

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
