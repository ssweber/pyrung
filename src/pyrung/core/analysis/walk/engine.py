"""Corridor walker for how().

A sequential-simulation planner that runs on the PLC runner instead of the
BFS infrastructure.

The principle: the target reduces to driving one **governing** stateful tag
to a value.  The walker discovers that tag's value-transition graph by
*interpreted simulation* — from a state at value ``from``, it applies a
candidate *steer* on a forked runner and observes the value ``to`` that
results.  Every edge is therefore something the real interpreter produced
(sound by construction; immune to copy/calc/indirect-addressing blindness
that defeats static writer inversion).

Static analysis is only a **prior**, never correctness-bearing:
  1. it picks the governing tag (a derived coil delegates to the state tag
     that gates it),
  2. it narrows the steer alphabet to the governing tag's input cone, and
  3. it sets the search horizon (short for command machines, long when a
     timer/counter gates the tag — a held wait advances time to the crossing,
     so "advance time" is just an empty steer with a longer horizon).

Best-first search runs over the governing tag's value space (tiny — mode
values or a counter's range), not the full state space, so it stays "mostly
no search".  Goals flow through one deepest-first agenda (:func:`_drive`)
whose pipelines resolve flaws by establishing corridors, recursing on
prerequisites, and learning nogoods; the walker is the sole ``how()`` path,
so anything it cannot reach is reported as not reachable (or, when the
global budget runs out, as an honest "budget exhausted").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

# The walk engine spans the package modules (base, physical, priors, fold,
# steer, explore, agenda); tests and callers historically reach internals
# through this module, so the load-bearing names are re-exported here.
from pyrung.core.analysis.walk.agenda import (
    _advance_work,
    _drive,
    _flatten_plan,
    _Pipeline,
    _PlanNode,
    _Request,
    _try_independent_walks,
)
from pyrung.core.analysis.walk.agenda import (
    _walk_to_goal as _walk_to_goal,
)
from pyrung.core.analysis.walk.base import (
    _MAX_ADVANCE_ITERS as _MAX_ADVANCE_ITERS,
)
from pyrung.core.analysis.walk.base import (
    _PULSE_REACT_CAP as _PULSE_REACT_CAP,
)
from pyrung.core.analysis.walk.base import (
    HoldStore,
    NoGoodStore,
    _Action,
    _values_match,
    _WalkBudget,
    _WalkContext,
)
from pyrung.core.analysis.walk.base import (
    NoGoodFact as NoGoodFact,
)
from pyrung.core.analysis.walk.fold import (
    _advance_time as _advance_time,
)
from pyrung.core.analysis.walk.fold import (
    _build_jump_context,
)
from pyrung.core.analysis.walk.fold import (
    _scans_to_cross as _scans_to_cross,
)
from pyrung.core.analysis.walk.fold import (
    _scans_to_uncross as _scans_to_uncross,
)
from pyrung.core.analysis.walk.passes import (
    WALK_PASSES as WALK_PASSES,
)
from pyrung.core.analysis.walk.passes import (
    run_walk_passes,
)
from pyrung.core.analysis.walk.physical import (
    _harness_nearest_scan as _harness_nearest_scan,
)
from pyrung.core.analysis.walk.physical import (
    _install_replay_harness,
    _install_walk_harness,
)
from pyrung.core.analysis.walk.priors import (
    _edge_tags,
    _external_bool_inputs,
    _extract_goals,
    _reference_constants,
)
from pyrung.core.analysis.walk.priors import (
    _governing as _governing,
)
from pyrung.core.analysis.walk.priors import (
    _needs_decomposition as _needs_decomposition,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.graph import Path
    from pyrung.core.runner import PLC


# ---------------------------------------------------------------------------
# Diagnosis (Stage D4): a consumer of tree + holds + nogoods + journal
# ---------------------------------------------------------------------------

# Failure kinds that certify structural deadness: no steer moved the
# governing tag, or the causal oracle named nothing actionable.  Everything
# else (diverged corridors, exhausted recovery rounds, bounds, budget) means
# the search was limited — the weaker, honest "not-found".
_STRUCTURAL_FAILURES = frozenset({"explore-stuck", "no-recovery-goals"})


def _failed_leaves(node: _PlanNode) -> list[_PlanNode]:
    """Failed nodes with no failed child — the root causes, in plan order."""
    out: list[_PlanNode] = []
    for seg in node.segments:
        if isinstance(seg, _PlanNode):
            out.extend(_failed_leaves(seg))
    if node.status == "failed" and not out:
        return [node]
    return out


def _diagnose(root: _PlanNode, ctx: _WalkContext) -> Any:
    """Build the failure :class:`~pyrung.core.analysis.graph.Diagnosis`."""
    from pyrung.core.analysis.graph import Diagnosis

    leaves = _failed_leaves(root)
    first = next((n for n in leaves if n.goal is not None), leaves[0] if leaves else None)
    budget_hit = ctx.budget.exhausted

    structural = bool(leaves) and all(n.failure in _STRUCTURAL_FAILURES for n in leaves)
    verdict = "unsolvable" if structural and not budget_hit else "not-found"

    if budget_hit:
        reason = ctx.budget.describe_exhaustion()
    elif first is not None and first.goal is not None:
        tag, value = first.goal
        reason = f"goal {tag} -> {value!r} failed ({first.failure or 'unresolved'})"
    else:
        reason = "no goal could be established"

    def _fmt_nogood_fact(fact: Any) -> str:
        if isinstance(fact, tuple) and len(fact) == 2:
            return f"{fact[0]}={fact[1]!r}"
        return str(fact)

    nogood_lines = tuple(
        f"{frm!r} -> {to!r} blocked by " + ", ".join(_fmt_nogood_fact(f) for f in blocking)
        for frm, to, blocking in ctx.nogoods.entries()
    )

    notes: list[str] = []
    if ctx.holds is not None and len(ctx.holds):
        held = ", ".join(
            f"{h.name}={h.value!r} (for {h.goal[0]})"
            for h in sorted(ctx.holds, key=lambda h: h.name)
        )
        notes.append(f"holds at failure: {held}")
    if ctx.journal is not None:
        notes.extend(ctx.journal.notes)
        disabled = [d.pass_name for d in ctx.journal.decisions if d.outcome == "disabled"]
        if disabled:
            notes.append("passes disabled: " + ", ".join(disabled))

    return Diagnosis(
        verdict=verdict,
        reason=reason,
        failing_goal=first.goal if first is not None else None,
        failure_kind=first.failure if first is not None else None,
        blockers=first.blockers if first is not None else (),
        nogoods=nogood_lines,
        partial_steps=len(_flatten_plan(root)),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _solve_targets(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    resolved_goals: list[tuple[str, Any]],
    max_steps: int,
    regressions: list[tuple[tuple[str, Any], tuple[str, Any]]],
) -> _Pipeline:
    """The walk root: feed the target-decomposition goals to the agenda.

    Tries the compound independent-fork walk first (≥2 unsatisfied goals
    walked serially can clobber earlier results), then yields each goal in
    order.  A failed target goal fails the walk.

    Committed conjuncts are must-stays: a goal is committed at its turn
    (skipped-as-satisfied or walked), and after every later goal's walk the
    committed list is re-checked on the work fork.  A regression fails the
    attempt with the ``(clobbered, clobbering)`` pair appended to
    *regressions* — :func:`plan_walk`'s reorder loop is the resolver.
    """
    all_steps: list[_Action] = []
    unsatisfied = [
        (t, v) for t, v in resolved_goals if not _values_match(work.state.tags.get(t), v)
    ]
    if len(unsatisfied) >= 2:
        merged = _try_independent_walks(
            ctx,
            work,
            unsatisfied,
            unsatisfied[0][0],
            unsatisfied[0][1],
            max_steps,
            0,
            frozenset(),
            all_goals=unsatisfied,
        )
        if merged is not None:
            _advance_work(ctx, work, merged)
            node.segments.append(list(merged))
            all_steps.extend(merged)

    committed: list[tuple[str, Any]] = []
    for target_tag, target_value in resolved_goals:
        if _values_match(work.state.tags.get(target_tag), target_value):
            committed.append((target_tag, target_value))
            # A conjunct satisfied at its turn is a commitment like any
            # other: when it is an external input, hold it so later goals'
            # walks can't steer it away (prevention — without the hold the
            # only backstop is the must-stay re-check below, and the
            # reorder loop just replays the same clobber).
            if ctx.holds is not None and (
                target_tag in ctx.edge_ext or target_tag in ctx.ext_inputs
            ):
                ctx.holds.protect(target_tag, target_value, (target_tag, target_value))
            continue

        steps = yield _Request(
            runner=work,
            goal=(target_tag, target_value),
            depth=0,
            visited=frozenset(),
            budget=max_steps - len(all_steps),
            provenance="target-decomposition",
        )
        if steps is None or not steps:
            return None
        all_steps.extend(steps)
        if len(all_steps) > max_steps:
            return None

        regressed = next(
            (g for g in committed if not _values_match(work.state.tags.get(g[0]), g[1])),
            None,
        )
        if regressed is not None:
            regressions.append((regressed, (target_tag, target_value)))
            child = _PlanNode(goal=regressed, provenance="must-stay", depth=0)
            child.status = "failed"
            child.failure = "goal-regressed"
            child.blockers = ((target_tag, target_value),)
            node.segments.append(child)
            logger.info(
                "walk: must-stay regression — committed goal %s=%r broken by walking %s=%r",
                regressed[0],
                regressed[1],
                target_tag,
                target_value,
            )
            return None
        committed.append((target_tag, target_value))

    if not all_steps:
        return None
    return all_steps


def plan_walk(
    plc: PLC,
    snapshot: dict[str, Any],
    expr: Any,
    max_steps: int,
    avoid_pred: Any = None,
    *,
    explore_context: Any = None,
    atom_index: dict[str, list[Any]] | None = None,
    domain_sources: dict[str, str] | None = None,
    unlink: list[str] | None = None,
    wall_budget_s: float | None = None,
) -> Path | None:
    """Try to reach the target by walking a governing-tag value corridor.

    Returns a :class:`~pyrung.core.analysis.graph.Path` on success, a
    ``Path(reachable=False)`` whose ``reason`` says "budget exhausted" when
    the global fork/scan budget ran out (honest NotFound — distinct from
    structurally unreachable), or ``None`` when the walker cannot solve it.

    When *explore_context* (an ``_ExploreContext`` from the prover pipeline)
    is provided, the walker uses its ``nondeterministic_dims`` for non-Bool
    input steers and inequality prerequisite resolution.

    *wall_budget_s* caps the walk's wall-clock time (``None`` = no cap);
    exhaustion returns the honest budget-exhausted Path, same as the
    fork/scan caps.
    """
    from pyrung.core.analysis.graph import (
        Diagnosis,
        Path,
        ReachabilityStep,
        _build_triangle_table,
    )
    from pyrung.core.analysis.pdg import build_program_graph
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state

    program = getattr(plc, "_program", None)
    if program is None:
        return None

    goals = _extract_goals(expr, snapshot)
    if goals is None:
        return None

    known = plc._known_tags_by_name
    tag_defaults = {t.name: t.default for t in known.values()}

    nd_domains: dict[str, tuple[Any, ...]] | None = None
    if explore_context is not None:
        nd_domains = getattr(explore_context, "nondeterministic_dims", None)

    # Resolve choice names ("IDLE") to underlying values.
    resolved_goals: list[tuple[str, Any]] = []
    for target_tag, target_value in goals:
        if isinstance(target_value, str):
            t = known.get(target_tag)
            choices = getattr(t, "choices", None) if t is not None else None
            if choices:
                inv = {name: val for val, name in choices.items()}
                if target_value in inv:
                    target_value = inv[target_value]
        resolved_goals.append((target_tag, target_value))

    # Already satisfied?
    if all(snapshot.get(tag) == val for tag, val in resolved_goals):
        return Path(
            reachable=True, steps=(), total_changes=0, total_scans=0, tag_defaults=tag_defaults
        )

    pdg = build_program_graph(program)

    # Install harness on the work fork so feedback timing is respected
    # during folded jumps.  fork() propagates the harness to trial forks.
    work = plc.fork()
    _install_walk_harness(work)

    # The pass registry runs once, against (program, pdg) only, and freezes
    # its advice; the journal records what applied for diagnosis.  It runs
    # before input collection so the ack_cleared_inputs advice applies.
    advice, journal = run_walk_passes(program, pdg)

    ext_inputs = _external_bool_inputs(pdg, known, program, advice=advice)
    edge_ext = _edge_tags(pdg, program) & set(ext_inputs)

    # Linked Fb tags are driven by the Harness, not steered directly.
    if work._harness is not None:
        if unlink:
            work._harness.unlink(unlink)
        linked_fbs = {c.fb_name for c in work._harness.couplings()}
        excluded = sorted(set(ext_inputs) & linked_fbs)
        ext_inputs = [i for i in ext_inputs if i not in linked_fbs]
        edge_ext -= linked_fbs
        if excluded:
            journal.add_note(
                "harness: linked feedback tags excluded from steers: " + ", ".join(excluded)
            )

    # The per-walk context, built once: jump context (after harness install +
    # unlink so profile-feedback names are right), probe memo, budget counters,
    # and one nogood store + one hold store shared across compound-goal walks
    # so precondition-failure learning and committed holds carry between goals.
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
            target_names=frozenset(t for t, _v in resolved_goals),
            advice=advice,
            journal=journal,
        ),
        nogoods=NoGoodStore(),
        holds=HoldStore(),
        nd_domains=nd_domains,
        explore_context=explore_context,
        atom_index=atom_index,
        domain_sources=domain_sources,
        budget=_WalkBudget(max_wall_s=wall_budget_s),
        ref_constants=(
            _reference_constants(pdg, program) if advice.has("ref_constant_order") else frozenset()
        ),
        advice=advice,
        journal=journal,
    )
    if ctx.ref_constants:
        journal.add_note(
            "ref_constant_order: deprioritized reference constants: "
            + ", ".join(sorted(ctx.ref_constants))
        )

    # Drive the walk through the agenda: the root pipeline feeds the
    # target-decomposition goals (compound independent-fork pre-pass, then
    # each goal in order); the plan tree is born here and flattened once
    # below for the Path build.
    #
    # The must-stay reorder loop: a committed conjunct broken by a later
    # goal's walk fails the attempt, and the walk retries with the
    # clobbering goal moved ahead of the goal it broke (mode-before-state
    # for state machines a mode change resets).  Each attempt runs on a
    # fresh work fork with the holds rolled back; nogoods (program facts)
    # and the global budget carry across attempts.
    holds_clean = ctx.holds.snapshot() if ctx.holds is not None else None
    order = list(resolved_goals)
    tried: set[tuple[tuple[str, Any], ...]] = {tuple(order)}
    for _attempt in range(1 + len(resolved_goals)):
        root = _PlanNode(goal=None, provenance="walk-root", depth=-1)
        regressions: list[tuple[tuple[str, Any], tuple[str, Any]]] = []
        result = _drive(
            ctx, _solve_targets(ctx, root, work, order, max_steps, regressions), root, work
        )
        if result is not None or not regressions or ctx.budget.exhausted:
            break
        clobbered, clobbering = regressions[-1]
        new_order = [g for g in order if g != clobbering]
        new_order.insert(new_order.index(clobbered), clobbering)
        if tuple(new_order) in tried:
            break
        order = new_order
        tried.add(tuple(order))
        if ctx.holds is not None and holds_clean is not None:
            ctx.holds.restore(holds_clean)
        journal.add_note(
            f"compound goals: must-stay regression — retrying with "
            f"{clobbering[0]}=={clobbering[1]!r} before {clobbered[0]}=={clobbered[1]!r}"
        )
        work = plc.fork()
        _install_walk_harness(work)
        if work._harness is not None and unlink:
            work._harness.unlink(unlink)
    if result is None:
        if ctx.budget.exhausted:
            # Honest NotFound: the search ran out of budget — distinct from
            # a structurally unreachable target.
            return Path(
                reachable=False,
                steps=(),
                total_changes=0,
                total_scans=0,
                tag_defaults=tag_defaults,
                reason=f"walker: {ctx.budget.describe_exhaustion()}",
                diagnosis=_diagnose(root, ctx),
            )
        # The walk root failed: report the diagnosis (tree + holds + nogoods
        # + journal) alongside the unreachable verdict.
        return Path(
            reachable=False,
            steps=(),
            total_changes=0,
            total_scans=0,
            tag_defaults=tag_defaults,
            reason="walker: target not reachable",
            diagnosis=_diagnose(root, ctx),
        )
    all_steps = _flatten_plan(root)
    if not all_steps:
        return None

    # Verify on a fresh fork against the *full* original expression.
    # The verify fork uses the real Harness (step-by-step, no folding) so
    # that feedback timing is validated at full fidelity.
    verify = plc.fork()
    if work._harness is not None:
        _install_replay_harness(verify, unlink)
    for action, scans in all_steps:
        if action:
            verify.patch(action)
        for _ in range(scans):
            verify.step()
            if avoid_pred is not None and avoid_pred(dict(verify.state.tags)):
                logger.info("walk: path passes through avoided state")
                return None
    if _eval_expr_from_state(expr, dict(verify.state.tags)) is not True:
        logger.info("walk: replay verification failed for compound target")
        final = dict(verify.state.tags)
        unmet = [(t, v) for t, v in order if not _values_match(final.get(t), v)]
        first_unmet = unmet[0] if unmet else None
        return Path(
            reachable=False,
            steps=(),
            total_changes=0,
            total_scans=0,
            tag_defaults=tag_defaults,
            reason="walker: replay verification failed",
            diagnosis=Diagnosis(
                verdict="not-found",
                reason=(
                    f"replay verification failed: goal {first_unmet[0]} -> "
                    f"{first_unmet[1]!r} did not hold at path end"
                    if first_unmet is not None
                    else "replay verification failed: target expression false at path end"
                ),
                failing_goal=first_unmet,
                failure_kind="verify-failed",
                partial_steps=len(all_steps),
                notes=tuple(journal.notes),
            ),
        )

    # Build annotated steps: replay on a second fork to collect per-step state
    # for semantic constraint annotations.
    rsteps: list[ReachabilityStep] = []
    if atom_index is not None and domain_sources is not None:
        from pyrung.core.analysis.graph import _classify_step_inputs

        annotate_fork = plc.fork()
        if work._harness is not None:
            _install_replay_harness(annotate_fork, unlink)
        for action, scans in all_steps:
            if action:
                annotate_fork.patch(action)
            for _ in range(scans):
                annotate_fork.step()
            constraints = (
                _classify_step_inputs(
                    action, atom_index, domain_sources, dict(annotate_fork.state.tags)
                )
                if action
                else None
            ) or None
            rsteps.append(
                ReachabilityStep(
                    action=action,
                    source_key=(),
                    dest_key=(),
                    scans=scans,
                    constraints=constraints,
                )
            )
    else:
        rsteps = [
            ReachabilityStep(action=action, source_key=(), dest_key=(), scans=scans)
            for action, scans in all_steps
        ]
    from pyrung.core.runner import _count_visible_changes

    total_changes = _count_visible_changes(rsteps, tag_defaults)
    total_scans = sum(scans for _action, scans in all_steps)
    walk_holds = ctx.holds if ctx.holds is not None else HoldStore()
    holds_out = tuple(
        sorted(((h.name, h.value, h.goal[0]) for h in walk_holds), key=lambda t: t[0])
    )
    released_out = tuple((h.name, h.value, h.goal[0]) for h in walk_holds.released())
    triangle = _build_triangle_table(tuple(all_steps), holds_out, released_out)
    logger.info("walk: reached compound target in %d step(s)", len(rsteps))
    return Path(
        reachable=True,
        steps=tuple(rsteps),
        total_changes=total_changes,
        total_scans=total_scans,
        tag_defaults=tag_defaults,
        holds=holds_out or None,
        triangle=triangle,
    )
