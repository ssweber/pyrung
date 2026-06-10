"""Interpreted steer application: the prefix builder and the one fold.

``_apply_steer_fold`` is the single execution-monitoring seam — both the
single-governing watch and the sequential goal-list iteration are this
function with a different ``done``/``monitor`` pair.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import (
    _EMPTY_CAP,
    _Action,
    _Steer,
    _values_match,
    _WalkContext,
)
from pyrung.core.analysis.walk.fold import _advance_time

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

# ---------------------------------------------------------------------------
# Interpreted steer application
# ---------------------------------------------------------------------------


def _steer_prefix(
    steer: _Steer,
    work_tags: dict[str, Any],
    ext_inputs: list[str],
    edge_ext: set[str],
    protected: frozenset[str] = frozenset(),
) -> list[_Action]:
    """Action prefix for *steer*: empty → none; pulse → release then high; low → drive low; set → patch value; multi → simultaneous patch.

    *protected* names (holds) are excluded from every implicit write the
    prefix inserts — the global release, the edge-release, and the edge
    blast — so a steer no longer breaks what an earlier sub-walk
    established.  Intended writes to a protected input never reach here:
    ``_explore`` resolves them first (divest probe), removing approved
    names from *protected* before building the prefix.  An empty set is
    today's behavior exactly.
    """
    if steer.kind == "empty" or (steer.input is None and steer.kind != "multi"):
        return []
    if steer.kind == "multi" and steer.patch:
        release: dict[str, Any] = {}
        pulse: dict[str, Any] = {}
        for inp, val in steer.patch.items():
            if val:
                if work_tags.get(inp) and inp not in protected:
                    release[inp] = False
                pulse[inp] = True
            else:
                if inp in edge_ext and not work_tags.get(inp) and inp not in protected:
                    release[inp] = True
                pulse[inp] = False
        prefix: list[_Action] = []
        if release:
            prefix.append((release, 1))
        prefix.append((pulse, 1))
        return prefix
    # set / low / pulse all require a concrete input tag; the empty/None guard
    # above guarantees steer.input is set for these kinds.
    assert steer.input is not None
    inp = steer.input
    if steer.kind == "set":
        return [({inp: steer.value}, 1)]
    if steer.kind == "low":
        prefix: list[_Action] = []
        if inp in edge_ext and not work_tags.get(inp) and inp not in protected:
            prefix.append(({inp: True}, 1))
        prefix.append(({inp: False}, 1))
        return prefix
    # pulse: release all (unprotected) highs for a clean rising edge, then
    # drive high.
    release: dict[str, Any] = {
        c: False for c in ext_inputs if work_tags.get(c) and c not in protected
    }
    for e in edge_ext:
        if work_tags.get(e) and e not in protected:
            release[e] = False
    pulse: dict[str, Any] = {inp: True}
    for e in edge_ext:
        if e not in protected:
            pulse[e] = True
    prefix: list[_Action] = []
    if release:
        prefix.append((release, 1))
    prefix.append((pulse, 1))
    return prefix


def _apply_steer_fold(
    ctx: _WalkContext,
    runner: PLC,
    steer: _Steer,
    done: Callable[[Any], bool],
    monitor: Callable[[Any], tuple[str, Any] | None],
    react_cap: int,
    cap: int,
    protected: frozenset[str] = frozenset(),
) -> list[_Action] | None:
    """The one fold: apply *steer* on *runner* and fold time until *done*.

    This is the single execution-monitoring seam — both the single-governing
    watch (:func:`_apply_steer`) and the sequential goal-list iteration
    (:func:`_apply_steer_compound`) are this function with a different
    ``done``/``monitor`` pair, and future monitors (path-sequence divergence,
    must-stay violation, deadline race) plug in here rather than growing new
    code paths.

    ``done(state)`` decides completion after every prefix segment and every
    fold round.  ``monitor(state)`` picks the next ``(tag, from_value)`` for
    :func:`_advance_time` to watch — returning ``None`` means no further
    progress is possible (the fold fails).  Each round's reaction budget is
    ``min(react_cap, cap - total_used)``; *cap* bounds the total folded
    scans.  Returns the realized ``(action, scans)`` list (folded runs are
    emitted as one ``({}, scans)`` entry so a plain normal-dt replay
    reproduces them), or ``None`` when *done* is never reached.  *protected*
    hold names are excluded from the steer prefix's implicit releases (see
    :func:`_steer_prefix`).
    """
    realized: list[_Action] = []
    for action, scans in _steer_prefix(
        steer, dict(runner.state.tags), ctx.ext_inputs, ctx.edge_ext, protected
    ):
        if action:
            runner.patch(action)
        for _ in range(scans):
            runner.step()
        ctx.budget.scans += scans
        realized.append((action, scans))
        if done(runner.state):
            return realized

    total_used = 0
    while total_used < cap:
        if done(runner.state):
            break
        sel = monitor(runner.state)
        if sel is None:
            return None
        gov, from_value = sel
        used = _advance_time(
            runner, gov, from_value, ctx.jump_ctx, min(react_cap, cap - total_used)
        )
        if used is None:
            return None
        ctx.budget.scans += used
        total_used += used

    if not done(runner.state):
        return None
    if total_used:
        realized.append(({}, total_used))
    return realized


def _apply_steer(
    ctx: _WalkContext,
    runner: PLC,
    steer: _Steer,
    governing: str,
    from_value: Any,
    react_cap: int,
    protected: frozenset[str] = frozenset(),
) -> list[_Action] | None:
    """Apply *steer* on *runner* and step until the governing value changes.

    :func:`_apply_steer_fold` with the single-governing monitor: done when
    *governing* leaves *from_value*; the fold always watches that one pair,
    so it runs at most one :func:`_advance_time` round.
    """

    def done(state: Any) -> bool:
        return state.tags.get(governing) != from_value

    return _apply_steer_fold(
        ctx,
        runner,
        steer,
        done,
        lambda _state: (governing, from_value),
        react_cap,
        _EMPTY_CAP,
        protected,
    )


def _apply_steer_compound(
    ctx: _WalkContext,
    runner: PLC,
    steer: _Steer,
    goals: list[tuple[str, Any]],
    cap: int,
    protected: frozenset[str] = frozenset(),
) -> list[_Action] | None:
    """Apply *steer* and fold until ALL *goals* are satisfied.

    :func:`_apply_steer_fold` with the sequential goal-list monitor: each
    round watches the first unsatisfied goal's tag from its current value,
    and a round that doesn't shrink the unsatisfied set stops the fold (the
    anti-stall guard lives in the monitor closure).  Converges because
    accumulation is monotone and each fold advances past at least one
    crossing.
    """

    def done(state: Any) -> bool:
        return all(_values_match(state.tags.get(t), v) for t, v in goals)

    prev_remaining = len(goals) + 1

    def monitor(state: Any) -> tuple[str, Any] | None:
        nonlocal prev_remaining
        remaining = [(t, v) for t, v in goals if not _values_match(state.tags.get(t), v)]
        if len(remaining) >= prev_remaining:
            return None
        prev_remaining = len(remaining)
        gov, _ = remaining[0]
        return gov, state.tags.get(gov)

    return _apply_steer_fold(ctx, runner, steer, done, monitor, cap, cap, protected)
