"""The establish resolver pipeline.

``_establish`` is the generator that ``_drive`` pushes — it discovers
prerequisites, probes corridors, and delegates to recovery.
"""

from __future__ import annotations

import logging
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _Action,
    _MustStay,
    _progress_depth_limit,
    _StepMonitors,
    _values_match,
    _WalkContext,
)
from pyrung.core.analysis.walk.explore import _explore_corridor
from pyrung.core.analysis.walk.priors import (
    _functional_deps,
    _governing,
    _log_decomposition_hint,
    _steer_alphabet,
    _unsatisfied_condition_groups,
    _unsatisfied_conditions,
    _writer_candidates,
    _WriterCandidate,
)
from pyrung.core.analysis.walk.recovery import (
    _apply_temporal_recovery,
    _backjump,
    _cacheable_writer_sp_failure,
    _last_child_node,
    _recover,
    _why_regression,
)
from pyrung.core.analysis.walk.scheduler import (
    _advance_work,
    _child_monitors,
    _commit_holds,
    _deprioritized_goal_tags,
    _emit_bounds_refusal,
    _Pipeline,
    _PlanNode,
    _Request,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


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
    from pyrung.core.analysis.walk.independent import _try_independent_walks

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
