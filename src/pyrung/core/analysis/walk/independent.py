"""Independent-fork walks and the single-goal entry point.

``_try_independent_walks`` solves disjoint-cone prerequisites on separate
forks and merges their holds; ``_walk_to_goal`` is the direct single-goal
entry (used by tests).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _EMPTY_CAP,
    _NO_MONITORS,
    HoldStore,
    NoGoodStore,
    _Action,
    _Steer,
    _StepMonitors,
    _values_match,
    _WalkBudget,
    _WalkContext,
)
from pyrung.core.analysis.walk.fold import _build_fold_context
from pyrung.core.analysis.walk.passes import run_walk_passes
from pyrung.core.analysis.walk.priors import _reference_constants
from pyrung.core.analysis.walk.scheduler import (
    _credit_progress,
    _drive,
    _extract_holds,
    _PlanNode,
    _Request,
)
from pyrung.core.analysis.walk.steer import _apply_steer, _apply_steer_compound

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC


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
    from pyrung.core.analysis.walk.establish import _establish

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
    from pyrung.core.analysis.walk.establish import _establish

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
        fold_ctx=_build_fold_context(
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
