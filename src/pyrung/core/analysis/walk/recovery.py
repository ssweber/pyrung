"""Fallback resolvers: oracle-driven recovery, backjump, why-regression.

``_recover`` is the nogood-and-retry stage of the establish pipeline;
``_backjump`` re-enters from diverged corridor checkpoints; ``_why_regression``
mines frontier-terminated ``why()`` sub-goals as a last-ditch source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _MAX_BACKJUMP_SEGMENTS,
    _MAX_RECHECK_ITERS,
    _NO_MONITORS,
    _PULSE_REACT_CAP,
    HoldStore,
    NoGoodFact,
    _Action,
    _progress_depth_limit,
    _Steer,
    _StepMonitors,
    _values_match,
    _WalkContext,
)
from pyrung.core.analysis.walk.explore import _explore, _explore_corridor
from pyrung.core.analysis.walk.priors import (
    _functional_deps,
    _steer_alphabet,
    _unsatisfied_conditions,
)
from pyrung.core.analysis.walk.rules import (
    recursive_cause_evidence,
    temporal_cycle_recovery,
)
from pyrung.core.analysis.walk.scheduler import (
    _advance_work,
    _child_monitors,
    _commit_holds,
    _deprioritized_goal_tags,
    _deprioritized_last,
    _holds_snapshot,
    _Pipeline,
    _PlanNode,
    _Request,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


# Cap on goals mined per frontier-terminated why() regression (the fallback
# goal source when explore, static prereqs, and oracle recovery all came up
# empty).  Goals are priors validated by the interpreted walk — the cap only
# bounds wasted budget, never correctness.
_MAX_WHY_GOALS = 6

# Test-only ablation switch for the why-regression fallback goal source
# (mirrors _SPIN_GUARD): directional pins disable it to show the walk
# honestly fails without the source.
_WHY_REGRESSION = True


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


@dataclass(frozen=True)
class _RecoverySignal:
    """Actionable goals plus the richer facts they came from."""

    goals: list[tuple[str, Any]]
    facts: frozenset[NoGoodFact]


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
    from pyrung.core.analysis.walk.independent import _try_independent_walks

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
            if nogoods.is_blocked(target_tag, from_value, target_value, blocking):
                logger.info(
                    "walk: skipping known-blocked config for %s -> %s", target_tag, target_value
                )
                return None
            # Record the cause()-named blocking assignment *before* re-exploring,
            # so the refined seen-key + blocker-clearing move (in
            # :func:`_explore`) can first clear a learned guard and then enter
            # the now-open corridor.
            nogoods.add(target_tag, from_value, target_value, blocking)

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
