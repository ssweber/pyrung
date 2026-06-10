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
    for action, scans in _steer_prefix(
        steer, dict(probe.state.tags), ctx.ext_inputs, ctx.edge_ext, effective - conflicts
    ):
        if action:
            probe.patch(action)
        for _ in range(scans):
            probe.step()
        ctx.budget.scans += scans
    for _ in range(_PULSE_REACT_CAP):
        probe.step()
    ctx.budget.scans += _PULSE_REACT_CAP
    for name in conflicts:
        goal = holds.goal_of(name)
        if goal is None:
            continue
        if not _values_match(probe.state.tags.get(goal[0]), goal[1]):
            return False
    return True


def _explore(
    ctx: _WalkContext,
    start_plc: PLC,
    governing: str,
    target_value: Any,
    alphabet: list[_Steer],
    *,
    holds: HoldStore | None,
) -> list[_Action] | None:
    """BFS over governing values; edges discovered by interpreted stepping.

    Returns the realized action sequence reaching *target_value*, or ``None``.

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
    ``ctx.holds``: the post-serial-prereq re-explore in
    :func:`_walk_goal_inner` historically runs hold-blind (``holds=None``),
    and every call site must say which mode it wants.
    """
    nogoods = ctx.nogoods
    protected_base = holds.protected_names() if holds is not None else frozenset()
    held_values = holds.protected() if holds is not None else {}
    start_val = start_plc.state.tags.get(governing)
    if start_val == target_value:
        return []
    start_key = (start_val, nogoods.project(dict(start_plc.state.tags)))
    seen: set[Any] = {start_key}
    frontier: deque[_Node] = deque([_Node(start_val, start_plc.fork(), [])])
    ctx.budget.forks += 1
    nodes = 0

    while frontier and nodes < _MAX_NODES:
        node = frontier.popleft()
        nodes += 1
        for steer in alphabet:
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
                        ctx, node, steer, conflicts, holds, effective
                    ):
                        continue
                    divested = conflicts
            prot = effective - divested
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
                realized = _blocker_clearing_move(ctx, node, steer, governing, seen, prot)
                if realized is None:
                    continue
                cleared_trial, cleared_actions = realized
                ck = (node.value, nogoods.project(dict(cleared_trial.state.tags)))
                new_path = node.path + cleared_actions
                if len(new_path) > _MAX_CORRIDOR:
                    continue
                seen.add(ck)
                frontier.append(_Node(node.value, cleared_trial, new_path, child_released))
                continue
            nv = trial.state.tags.get(governing)
            nkey = (nv, nogoods.project(dict(trial.state.tags)))
            if nkey in seen:
                continue
            new_path = node.path + realized
            if nv == target_value:
                return new_path
            if len(new_path) > _MAX_CORRIDOR:
                continue
            seen.add(nkey)
            frontier.append(_Node(nv, trial, new_path, child_released))
    return None


def _blocker_clearing_move(
    ctx: _WalkContext,
    node: _Node,
    steer: _Steer,
    governing: str,
    seen: set[Any],
    protected: frozenset[str] = frozenset(),
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
    for action, scans in _steer_prefix(
        steer, dict(trial.state.tags), ctx.ext_inputs, ctx.edge_ext, protected
    ):
        if action:
            trial.patch(action)
        for _ in range(scans):
            trial.step()
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
