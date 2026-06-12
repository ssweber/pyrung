"""Corridor exploration: best-first interpreted search over governing values.

Edges are discovered by forking and stepping the real interpreter; holds
are enforced by construction (steer-conflict detection plus the empirical
divest probe), and learned nogoods refine the seen-key so re-walks can
re-enter a value under cleared constraints.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _MAX_ADVANCE_ITERS,
    _MAX_CORRIDOR,
    _MAX_NODES,
    _PULSE_REACT_CAP,
    HoldStore,
    _Action,
    _must_stay_landed,
    _must_stay_violation,
    _MustStay,
    _Steer,
    _values_match,
    _WalkContext,
)
from pyrung.core.analysis.walk.steer import _apply_steer, _steer_prefix

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Best-first interpreted search over the governing value graph
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    value: Any
    plc: PLC
    path: list[_Action]
    # Holds divest-approved on this branch (per-branch overlay; the shared
    # HoldStore is only mutated at commit time — see _reconcile_divests).
    released: frozenset[str] = frozenset()


@dataclass
class _ExploreResult:
    """Three-exit corridor search result (Stage D4).

    - ``found`` — *steps* reach the target value (the classic success).
    - ``stuck`` — no steer ever moved the governing value off the start node
      and no blocker-clearing move fired: structural deadness at this state
      (feeds the ``Unsolvable`` side of Diagnosis).
    - ``diverged`` — the governing moved (a corridor exists) but the target
      value was never reached before the frontier/node budget ran out.
      *best* is the deepest node discovered; its live fork is the backjump
      checkpoint (re-entry point) and the state where ``cause()`` names what
      still blocks — the backtracking trigger.
    """

    steps: list[_Action] | None
    outcome: str  # "found" | "stuck" | "diverged"
    best: _Node | None = None


def _steer_conflicts(
    steer: _Steer,
    held: dict[str, Any],
    effective: frozenset[str],
) -> frozenset[str]:
    """Protected names whose held value *steer* intends to change.

    Implicit prefix writes (releases, edge blips, edge blasts) are already
    filtered by :func:`_steer_prefix`, so only the steer's *intended* writes
    can conflict: a pulse on a protected input (drive-high against a held
    low, or the release dip a clean rising edge needs against a held high),
    a low against a held high, and set/multi values differing from the held
    value.  Conflicts go to the divest probe, not straight to the prefix.
    """
    out: set[str] = set()
    if steer.kind == "pulse" and steer.input in effective:
        out.add(steer.input)
    elif steer.kind == "low" and steer.input in effective:
        if held.get(steer.input):
            out.add(steer.input)
    elif steer.kind == "set" and steer.input in effective:
        if not _values_match(steer.value, held.get(steer.input)):
            out.add(steer.input)
    elif steer.kind == "multi" and steer.patch:
        for inp, val in steer.patch.items():
            if inp in effective and not _values_match(val, held.get(inp)):
                out.add(inp)
    return frozenset(out)


def _divest_probe(
    ctx: _WalkContext,
    node: _Node,
    steer: _Steer,
    conflicts: frozenset[str],
    holds: HoldStore,
    effective: frozenset[str],
    must_stay: tuple[_MustStay, ...],
) -> bool:
    """Empirically check whether the *conflicts* holds are releasable here.

    Forks the node, applies the steer prefix with the conflicting names
    unprotected, settles a few scans, and checks that every conflicting
    hold's recorded goal is still satisfied — the seal-in case, where the
    input established a latch and is no longer load-bearing.  ``True`` means
    the steer may proceed and the branch records the divest (a divest
    point); ``False`` means the steer would break a committed goal and is
    skipped.  One fork + a handful of scans per conflicting steer per node,
    bounded by ``_MAX_NODES x len(alphabet)``.
    """
    probe = node.plc.fork()
    ctx.budget.forks += 1
    context_prot = _must_stay_context_protected(ctx, dict(probe.state.tags), steer, must_stay)
    for action, scans in _steer_prefix(
        steer,
        dict(probe.state.tags),
        ctx.ext_inputs,
        ctx.edge_ext,
        (effective - conflicts) | context_prot,
    ):
        if action:
            probe.patch(action)
        for _ in range(scans):
            probe.step()
            if _must_stay_violation(must_stay, dict(probe.state.tags)) is not None:
                return False
        ctx.budget.scans += scans
    for _ in range(_PULSE_REACT_CAP):
        probe.step()
        if _must_stay_violation(must_stay, dict(probe.state.tags)) is not None:
            return False
    ctx.budget.scans += _PULSE_REACT_CAP
    for name in conflicts:
        goal = holds.goal_of(name)
        if goal is None:
            continue
        if not _values_match(probe.state.tags.get(goal[0]), goal[1]):
            return False
    return True


def _must_stay_context_protected(
    ctx: _WalkContext,
    tags: dict[str, Any],
    steer: _Steer,
    must_stay: tuple[_MustStay, ...],
) -> frozenset[str]:
    """Inputs whose implicit release should be skipped under must-stay.

    A pulse normally drops every high external input to create a clean edge.
    While a stateful ancestor must stay true, that global release can break the
    ancestor even though the child steer does not intend to write that input
    (fill's ``HMI_tare`` pulse must keep ``HMI_on`` high).  Preserve current
    high external inputs from implicit releases, but leave intended writes to
    the normal guard path.
    """
    if not must_stay:
        return frozenset()
    intended = set(steer.patch) if steer.kind == "multi" and steer.patch else set()
    if steer.input is not None:
        intended.add(steer.input)
    return frozenset(
        name
        for name in set(ctx.ext_inputs) | ctx.edge_ext
        if name not in intended and bool(tags.get(name))
    )


def _explore(
    ctx: _WalkContext,
    start_plc: PLC,
    governing: str,
    target_value: Any,
    alphabet: list[_Steer],
    *,
    holds: HoldStore | None,
    must_stay: tuple[_MustStay, ...] = (),
) -> list[_Action] | None:
    """Steps-or-None corridor search (the classic two-exit surface).

    Thin wrapper over :func:`_explore_corridor` for call sites that don't
    consume the third exit.
    """
    return _explore_corridor(
        ctx, start_plc, governing, target_value, alphabet, holds=holds, must_stay=must_stay
    ).steps


def _explore_corridor(
    ctx: _WalkContext,
    start_plc: PLC,
    governing: str,
    target_value: Any,
    alphabet: list[_Steer],
    *,
    holds: HoldStore | None,
    must_stay: tuple[_MustStay, ...] = (),
) -> _ExploreResult:
    """BFS over governing values; edges discovered by interpreted stepping.

    Returns an :class:`_ExploreResult` — found (with the realized action
    sequence), stuck, or diverged (with the deepest node as the backjump
    checkpoint).

    The ``seen`` set is keyed on ``(governing_value, nogoods.project(snapshot))``
    rather than the bare governing value.  An empty store projects to ``()`` so
    the key partitions identically to today (bit-identical, behavior-preserving).
    After a nogood is learned, distinct learned-blocking-tag configs at the same
    governing value become distinct keys — letting a re-walk re-enter a value
    under different constraints (the Phase-4 requirement: e.g. re-entering
    ``Latch_B=False`` after a ``Reset`` cleared the ``Guard_A`` blocker).

    *holds* protects committed external-input commitments: steer prefixes skip
    protected names (prevention), and a steer that *intends* to change a
    protected input goes through the divest probe — allowed only when every
    conflicting hold's goal survives the change (a divest point, recorded on
    the branch's ``released`` overlay; the shared store is reconciled at
    commit time).  An empty/absent store is today's behavior exactly.

    *holds* stays an explicit (keyword-only) parameter rather than reading
    ``ctx.holds`` so every call site declares its mode.  All agenda sites
    are hold-aware today — the post-serial-prereq re-explore in
    ``_establish``, hold-blind from before holds existed, was switched at
    Stage D4 with a suite-level A/B showing zero behavioral shift (the
    decision is pinned by ``test_post_serial_reexplore_is_hold_aware``).

    *must_stay* carries ancestor transition context.  Branches that break
    any must-stay comparison before its ``until`` comparison lands are
    skipped, just like a rejected hold conflict: safe refusal, never a
    manufactured plan.
    """
    nogoods = ctx.nogoods
    protected_base = holds.protected_names() if holds is not None else frozenset()
    held_values = holds.protected() if holds is not None else {}
    start_val = start_plc.state.tags.get(governing)
    if start_val == target_value or _must_stay_landed(must_stay, dict(start_plc.state.tags)):
        return _ExploreResult(steps=[], outcome="found")
    start_key = (start_val, nogoods.project(dict(start_plc.state.tags)))
    seen: set[Any] = {start_key}
    frontier: deque[_Node] = deque([_Node(start_val, start_plc.fork(), [])])
    ctx.budget.forks += 1
    nodes = 0
    # Deepest child discovered — the diverged exit's backjump checkpoint.
    best: _Node | None = None

    # The budget is re-checked per steer trial, not just at agenda yield
    # boundaries: one explore on a wide program pays |alphabet| forks per
    # node, so checking only between resolver steps lets a single establish
    # blow arbitrarily far past the caps (and makes a wall-clock cap
    # meaningless).  An exhausted explore exits through the normal stuck/
    # diverged paths; _diagnose already refuses the "unsolvable" verdict
    # when the budget was hit.
    while frontier and nodes < _MAX_NODES and not ctx.budget.exhausted:
        node = frontier.popleft()
        nodes += 1
        for steer in alphabet:
            if ctx.budget.exhausted:
                break
            # Holds: a steer that intends to change a protected input must
            # pass the divest probe; approved names join the branch's
            # released overlay so this branch's prefixes stop protecting
            # them.  Probe-rejected steers are skipped (they would break a
            # committed goal).
            effective = protected_base - node.released
            divested: frozenset[str] = frozenset()
            if effective:
                conflicts = _steer_conflicts(steer, held_values, effective)
                if conflicts:
                    if holds is None or not _divest_probe(
                        ctx, node, steer, conflicts, holds, effective, must_stay
                    ):
                        continue
                    divested = conflicts
            prot = effective - divested
            prot = prot | _must_stay_context_protected(
                ctx, dict(node.plc.state.tags), steer, must_stay
            )
            trial = node.plc.fork()
            ctx.budget.forks += 1
            # Both steers fold productive dwells to _EMPTY_CAP; what is passed
            # here is a *reaction* budget that bails a steer only while it churns
            # without reaching an accumulation plateau.  A pulse that merely
            # starts a dwell settles within a few scans then folds, so it needs
            # only a small budget; the empty steer's long waits are plateaus,
            # not churn, so it is effectively unbounded.
            react_cap = _MAX_ADVANCE_ITERS if steer.kind == "empty" else _PULSE_REACT_CAP
            realized = _apply_steer(
                ctx,
                trial,
                steer,
                governing,
                node.value,
                react_cap,
                protected=prot,
                must_stay=must_stay,
            )
            child_released = node.released | divested
            if realized is None:
                # Blocker-clearing move (Phase 4): a steer that does NOT change
                # the governing value is normally dropped, but once a nogood is
                # learned a learned-blocking tag may need clearing first (e.g.
                # pulse Reset clears Guard_A without changing Latch_B).  If the
                # steer's prefix changes a learned blocking-tag projection and
                # the resulting key is unseen, enqueue it so the cleared
                # corridor can be entered on a later expansion.
                realized = _blocker_clearing_move(
                    ctx, node, steer, governing, seen, prot, must_stay
                )
                if realized is None:
                    continue
                cleared_trial, cleared_actions = realized
                ck = (node.value, nogoods.project(dict(cleared_trial.state.tags)))
                new_path = node.path + cleared_actions
                if len(new_path) > _MAX_CORRIDOR:
                    continue
                seen.add(ck)
                child = _Node(node.value, cleared_trial, new_path, child_released)
                frontier.append(child)
                if best is None or len(child.path) > len(best.path):
                    best = child
                continue
            nv = trial.state.tags.get(governing)
            nkey = (nv, nogoods.project(dict(trial.state.tags)))
            if nkey in seen:
                continue
            new_path = node.path + realized
            if nv == target_value or _must_stay_landed(must_stay, dict(trial.state.tags)):
                return _ExploreResult(steps=new_path, outcome="found")
            if len(new_path) > _MAX_CORRIDOR:
                continue
            seen.add(nkey)
            child = _Node(nv, trial, new_path, child_released)
            frontier.append(child)
            if best is None or len(child.path) > len(best.path):
                best = child
    if best is None:
        return _ExploreResult(steps=None, outcome="stuck")
    return _ExploreResult(steps=None, outcome="diverged", best=best)


def _blocker_clearing_move(
    ctx: _WalkContext,
    node: _Node,
    steer: _Steer,
    governing: str,
    seen: set[Any],
    protected: frozenset[str] = frozenset(),
    must_stay: tuple[_MustStay, ...] = (),
) -> tuple[PLC, list[_Action]] | None:
    """A non-governing steer that clears a learned blocking tag.

    Returns ``(trial, realized)`` when applying *steer*'s prefix (a single
    bounded action, no time-fold) changes a learned blocking-tag projection to
    an unseen key without changing the governing value; otherwise ``None``.

    This is what lets a re-walk first clear a guard (a learned blocker) and then
    enter the now-open corridor on a subsequent expansion — the Phase-4
    requirement.  Gated on a non-empty store, so it never fires for a fresh
    walk (behavior-preserving).
    """
    nogoods = ctx.nogoods
    if not nogoods.blocking_tag_names():
        return None
    before = nogoods.project(dict(node.plc.state.tags))
    trial = node.plc.fork()
    ctx.budget.forks += 1
    realized: list[_Action] = []
    context_prot = _must_stay_context_protected(ctx, dict(trial.state.tags), steer, must_stay)
    for action, scans in _steer_prefix(
        steer, dict(trial.state.tags), ctx.ext_inputs, ctx.edge_ext, protected | context_prot
    ):
        if action:
            trial.patch(action)
        for _ in range(scans):
            trial.step()
            if _must_stay_violation(must_stay, dict(trial.state.tags)) is not None:
                return None
        ctx.budget.scans += scans
        realized.append((action, scans))
    # Governing value must be unchanged (else _apply_steer would have kept it).
    if trial.state.tags.get(governing) != node.value:
        return None
    after = nogoods.project(dict(trial.state.tags))
    if after == before:
        return None
    key = (node.value, after)
    if key in seen:
        return None
    return trial, realized
