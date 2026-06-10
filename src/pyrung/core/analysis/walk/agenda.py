"""The agenda: one deepest-first loop and the resolvers it drives.

``_drive`` is the scheduler; ``_establish``/``_recover``/``_residuals``
are the resolver pipelines feeding it; the plan tree (``_PlanNode``) is
born here and flattened once at Path-build time.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _EMPTY_CAP,
    _MAX_PREREQ_DEPTH,
    _MAX_RECHECK_ITERS,
    _PULSE_REACT_CAP,
    HoldStore,
    NoGoodStore,
    _Action,
    _Steer,
    _values_match,
    _WalkContext,
)
from pyrung.core.analysis.walk.explore import _explore
from pyrung.core.analysis.walk.fold import _build_jump_context
from pyrung.core.analysis.walk.passes import run_walk_passes
from pyrung.core.analysis.walk.priors import (
    _governing,
    _log_decomposition_hint,
    _steer_alphabet,
    _unsatisfied_conditions,
)
from pyrung.core.analysis.walk.steer import _apply_steer, _apply_steer_compound

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.runner import PLC

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
    ``"independent-probe"`` (a speculative per-fork sub-walk), or
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
    reads the best partial plan from them — but contribute nothing to the
    flattened plan: their committed effects remain on the work fork, exactly
    as the old recursion dropped a failed child's actions, and replay
    verification decides whether the plan stands without them.
    """

    goal: tuple[str, Any] | None  # None for the walk root
    provenance: str
    depth: int
    segments: list[Any] = field(default_factory=list)
    status: str = "open"  # open | solved | failed


def _flatten_plan(node: _PlanNode) -> list[_Action]:
    """Flatten the plan tree into execution-ordered actions (solved nodes only)."""
    out: list[_Action] = []
    for seg in node.segments:
        if isinstance(seg, _PlanNode):
            if seg.status == "solved":
                out.extend(_flatten_plan(seg))
        else:
            out.extend(seg)
    return out


# A pipeline yields sub-goal requests and finally returns its realized
# actions (None = the goal could not be established).
_Pipeline = Generator["_Request", "list[_Action] | None", "list[_Action] | None"]


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
            fgen.close()
            if fnode.status == "open":
                fnode.status = "failed"
            frames.pop()
            to_send = None
            continue
        try:
            request = fgen.send(to_send)
        except StopIteration as stop:
            result: list[_Action] | None = stop.value
            fnode.status = "solved" if result is not None else "failed"
            if (
                fnode.goal is not None
                and result
                and ctx.holds is not None
                and _values_match(frunner.state.tags.get(fnode.goal[0]), fnode.goal[1])
            ):
                _commit_holds(ctx, result, fnode.goal[0], fnode.goal[1])
            frames.pop()
            to_send = result
            continue
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
    work: PLC,
    target_tag: str,
    target_value: Any,
) -> list[tuple[str, Any]]:
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
        chain = work.cause(target_tag, to=target_value)
    except Exception:  # noqa: BLE001 - oracle is best-effort; never break the walk
        return []
    if chain is None:
        return []

    tags = work.state.tags
    goals: list[tuple[str, Any]] = []
    seen: set[tuple[str, Any]] = set()

    def _add(name: str, value: Any) -> None:
        key = (name, value)
        if key in seen or name == target_tag:
            return
        if _values_match(tags.get(name), value):
            return
        seen.add(key)
        goals.append(key)

    for step in chain.steps:
        for trig in step.triggers:
            _add(trig.tag_name, trig.to_value)
    for blocker in getattr(chain, "blockers", ()):  # unreachable mode
        _add(blocker.blocked_tag, blocker.needed_value)
        for sub in getattr(blocker, "sub_blockers", ()):
            _add(sub.blocked_tag, sub.needed_value)
    return goals


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


def _recover(
    ctx: _WalkContext,
    node: _PlanNode,
    work: PLC,
    target_tag: str,
    target_value: Any,
    budget: int,
    depth: int,
    visited: frozenset[tuple[str, Any]],
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
    cause() is the single source feeding both the nogood key and the projection.
    Blockers that are the walker's own held inputs are split off first
    (:func:`_classify_blockers`) and routed to the divest probe — the nogood
    key carries program facts only.

    Applies the recovery steps to *work* in place (recording them on *node*,
    the goal being recovered) and returns them, or ``None`` if the target
    cannot be recovered.  On ``None`` the caller fails the goal, so any
    partial mutation of *work* is discarded with it.
    """
    nogoods = ctx.nogoods
    recovered: list[_Action] = []
    for _ in range(_MAX_RECHECK_ITERS):
        if _values_match(work.state.tags.get(target_tag), target_value):
            return recovered
        from_value = work.state.tags.get(target_tag)
        goals = _recheck_prereqs(work, target_tag, target_value)
        if not goals:
            return None
        nogoods.recovery_iters += 1
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
                if not goals:
                    return None

        if program_facts:
            blocking = frozenset(program_facts)
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
        steps = _explore(ctx, work, target_tag, target_value, alphabet, holds=ctx.holds)
        if steps is not None:
            _advance_work(ctx, work, steps)
            _commit_holds(ctx, steps, target_tag, target_value)
            if steps:
                node.segments.append(list(steps))
            recovered.extend(steps)
            if len(recovered) > budget:
                return None
            if _values_match(work.state.tags.get(target_tag), target_value):
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
            )
            if indep is not None:
                _advance_work(ctx, work, indep)
                node.segments.append(list(indep))
                recovered.extend(indep)
                if len(recovered) > budget:
                    return None
                continue

        # Serial fallback: walk each cause goal one at a time.
        for rtag, rval in goals:
            sub = yield _Request(
                runner=work,
                goal=(rtag, rval),
                depth=depth + 1,
                visited=visited,
                budget=budget - len(recovered),
                provenance="oracle-recheck",
            )
            if sub is None:
                return None
            recovered.extend(sub)
            if len(recovered) > budget:
                return None
    return recovered if _values_match(work.state.tags.get(target_tag), target_value) else None


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
    holds = ctx.holds
    if holds is None or not steps:
        return
    _reconcile_divests(steps, holds)
    mined = _extract_holds(
        steps, ctx.pdg.upstream_slice(tag), set(ctx.ext_inputs) | ctx.edge_ext, strict=False
    )
    if mined:
        for name, val in mined.items():
            holds.protect(name, val, (tag, value))


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

    def _bail() -> None:
        if holds is not None and snap is not None:
            holds.restore(snap)

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
        realized = _apply_steer_compound(ctx, trial, steer, all_goals, _EMPTY_CAP, protected=prot)
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
        )
        ok = realized is not None and _values_match(trial.state.tags.get(governing), gov_value)

    if ok and realized is not None:
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
) -> list[_Action] | None:
    """Single-goal walk entry with explicit parameters.

    Builds the per-walk :class:`_WalkContext` (jump context, probe memo,
    budget counters, pass-registry advice) and drives one goal through the
    agenda.  ``plan_walk`` builds its context once and drives its own root
    pipeline; this wrapper serves direct single-goal callers (tests drive
    it as the walk entry).  Any harness must already be installed on *work*
    so the jump context sees the right profile-feedback tags.
    *disabled_passes* ablates registry advice (the matrix-test hook).
    """
    advice, journal = run_walk_passes(program, pdg, disabled=disabled_passes)
    ctx = _WalkContext(
        pdg=pdg,
        program=program,
        known=known,
        ext_inputs=ext_inputs,
        edge_ext=edge_ext,
        jump_ctx=_build_jump_context(work, pdg, program),
        nogoods=nogoods if nogoods is not None else NoGoodStore(),
        holds=holds,
        nd_domains=nd_domains,
        explore_context=explore_context,
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

    if _values_match(work.state.tags.get(target_tag), target_value):
        return []
    goal_key = (target_tag, target_value)
    if goal_key in visited or depth > _MAX_PREREQ_DEPTH or budget <= 0:
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

    steps = _explore(ctx, work, governing, gov_value, alphabet, holds=ctx.holds)

    if steps is None:
        prereqs = _unsatisfied_conditions(
            governing,
            gov_value,
            dict(work.state.tags),
            ctx.pdg,
            ctx.program,
            nd_domains=ctx.nd_domains,
        )
        if not prereqs:
            # The static SP-tree sweep found nothing actionable, but the
            # projected causal oracle may still name a blocker the SP tree
            # can't surface (e.g. a guard gating a timer-done arm — cause()
            # reports ``Guard_A=False`` where ``_unsatisfied_conditions``
            # returns []).  Try the cause()-driven recovery (with nogood
            # learning) before giving up.
            rec = yield from _recover(ctx, node, work, governing, gov_value, budget, depth, visited)
            if rec is None:
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
                )
            )
        # Independent-fork walk: when prerequisites each need their own
        # external input held, serial walking clobbers earlier holds.  Walk
        # each on an independent fork, merge holds, apply simultaneously.
        if len(prereqs) >= 2:
            merged = _try_independent_walks(
                ctx,
                work,
                prereqs,
                governing,
                gov_value,
                budget,
                depth,
                visited,
            )
            if merged is not None:
                _advance_work(ctx, work, merged)
                node.segments.append(list(merged))
                all_steps = list(merged)
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
                    )
                )

        # Snapshot the pre-clobber state before walking prerequisites serially.
        # Tier 2 (force-and-solve) will fork from here to solve interfering
        # subsystems independently; for now it anchors the diagnostic below.
        checkpoint = work.fork()
        ctx.budget.forks += 1
        all_steps: list[_Action] = []
        for ptag, pval in prereqs:
            sub = yield _Request(
                runner=work,
                goal=(ptag, pval),
                depth=depth + 1,
                visited=visited,
                budget=budget - len(all_steps),
                provenance="writer-sp-tree",
            )
            if sub is None:
                continue
            all_steps.extend(sub)

        alphabet = _steer_alphabet(
            governing,
            ctx.pdg,
            ctx.known,
            ctx.program,
            gov_value,
            nd_domains=ctx.nd_domains,
            advice=ctx.advice,
        )
        # NOTE: this re-explore historically runs hold-blind (holds was not
        # passed here pre-consolidation) — preserved bit-identically; the
        # commit below still registers the corridor's holds.
        steps = _explore(ctx, work, governing, gov_value, alphabet, holds=None)

        if steps is None:
            # Serial-clobber recovery: walking a later prerequisite may have
            # broken an earlier one.  Ask the oracle what still blocks the
            # governing value and walk those, then proceed as if _explore had
            # found a zero-action corridor (recovery already advanced *work*).
            rec = yield from _recover(
                ctx, node, work, governing, gov_value, budget - len(all_steps), depth, visited
            )
            if rec is None:
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
    if _values_match(work.state.tags.get(target_tag), target_value):
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
        )
        if rec is not None:
            all_steps.extend(rec)

    if _values_match(work.state.tags.get(target_tag), target_value):
        return all_steps

    # Unrecoverable: log a Tier 2 hint if the target's conditions couple.
    coupling = _unsatisfied_conditions(
        target_tag,
        target_value,
        dict(work.state.tags),
        ctx.pdg,
        ctx.program,
        nd_domains=ctx.nd_domains,
    )
    _log_decomposition_hint(
        target_tag,
        [(governing, True), *coupling],
        ctx.pdg,
        nogoods=ctx.nogoods,
        transition=(work.state.tags.get(target_tag), target_value),
    )
    return None
