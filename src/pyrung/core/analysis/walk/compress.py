"""Post-plan compression: greedy step drop.

After ``_flatten_plan`` produces the raw action list and before the verify
replay in ``plan_walk``, this pass tries dropping each non-empty-action step.
If the goal still holds on a trial fork without that step, the step was
discovery overhead and is removed.  The verify replay that follows is the
final correctness arbiter — compression can never produce a wrong plan.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import _Action
from pyrung.core.analysis.walk.physical import _install_replay_harness

if TYPE_CHECKING:
    from pyrung.core.analysis.walk.passes import _WalkJournal
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)


def _trial_replay(
    candidate: list[_Action],
    plc: PLC,
    expr: Any,
    avoid_pred: Any | None,
    has_harness: bool,
    unlink: list[str] | None,
) -> bool:
    """Replay *candidate* on a fresh fork; return whether the goal holds."""
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state

    fork = plc.fork()
    if has_harness:
        _install_replay_harness(fork, unlink)
    for action, scans in candidate:
        if action:
            fork.patch(action)
        for _ in range(scans):
            fork.step()
            if avoid_pred is not None and avoid_pred(dict(fork.state.tags)):
                return False
    return _eval_expr_from_state(expr, dict(fork.state.tags)) is True


def _compress_plan(
    all_steps: list[_Action],
    plc: PLC,
    expr: Any,
    *,
    avoid_pred: Any | None = None,
    has_harness: bool = False,
    unlink: list[str] | None = None,
    journal: _WalkJournal | None = None,
) -> list[_Action]:
    """Compress *all_steps* by dropping steps the program doesn't need.

    Single forward pass: for each step with a non-empty action, try removing
    it.  If the goal still holds, the step was discovery overhead — drop it.
    Empty-action steps (timing waits) are never candidates.

    Returns the compressed plan (may be the original if nothing was droppable).
    """
    original_count = len(all_steps)
    if original_count <= 1:
        return all_steps

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "compress: original %d steps: %s",
            original_count,
            [(a, s) for a, s in all_steps],
        )

    current = list(all_steps)
    i = 0
    while i < len(current):
        action, _scans = current[i]
        if not action:
            i += 1
            continue
        candidate = current[:i] + current[i + 1 :]
        if _trial_replay(candidate, plc, expr, avoid_pred, has_harness, unlink):
            current = candidate
        else:
            i += 1

    dropped = original_count - len(current)
    if dropped:
        logger.info("compress: %d → %d steps (%d dropped)", original_count, len(current), dropped)
    if journal is not None and dropped:
        journal.add_note(f"compress: {original_count} → {len(current)} steps ({dropped} dropped)")

    return current
