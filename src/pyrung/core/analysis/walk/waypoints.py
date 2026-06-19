"""Waypoint planning for the corridor walker.

Builds the governing tag's transition graph from static analysis (zero
simulation cost), computes the shortest waypoint sequence, and provides
it to ``_establish`` so each intermediate transition is driven with a
known target value rather than discovered reactively.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import _WalkContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Waypoint:
    from_value: Any
    to_value: Any


def _static_transition_graph(
    ctx: _WalkContext,
    governing: str,
) -> dict[Any, list[Any]]:
    """Build a transition graph from static analysis (zero simulation cost).

    Uses ``_written_value_for_tag`` to classify each writer's output and
    ``_extract_condition_values`` on the rung SP-tree for from-values.

    Returns ``{from_value: [to_value, ...], ...}``.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _extract_condition_values,
        _written_value_for_tag,
    )

    graph: dict[Any, list[Any]] = {}
    pdg = ctx.pdg
    if pdg is None:
        return graph

    for rung_idx in pdg.writers_of.get(governing, frozenset()):
        node = pdg.rung_nodes[rung_idx]
        rung_obj = _resolve_rung(ctx.program, node)
        if rung_obj is None:
            continue

        wv = _written_value_for_tag(rung_obj, governing)
        if wv is None:
            continue

        sp = rung_obj.sp_tree()
        if sp is None:
            continue
        cond_values = _extract_condition_values(_sp_to_expr(sp))
        from_vals = cond_values.get(governing, frozenset())

        kind = wv[0]
        if kind == "literal":
            to_value = wv[1]
            for fv in from_vals:
                if fv != to_value:
                    graph.setdefault(fv, []).append(to_value)
            if not from_vals:
                graph.setdefault(None, []).append(to_value)
        elif kind == "increment":
            step = wv[1]
            for fv in from_vals:
                graph.setdefault(fv, []).append(fv + step)
        elif kind == "decrement":
            step = wv[1]
            for fv in from_vals:
                graph.setdefault(fv, []).append(fv - step)

    return graph


def _compute_waypoint_sequence(
    graph: dict[Any, list[Any]],
    start_value: Any,
    target_value: Any,
) -> list[_Waypoint] | None:
    """BFS on the transition graph for the shortest waypoint path.

    Returns the sequence as ``_Waypoint`` objects, or ``None`` if unreachable.
    """
    if start_value == target_value:
        return []

    queue: deque[tuple[Any, list[_Waypoint]]] = deque([(start_value, [])])
    visited: set[Any] = {start_value}

    while queue:
        current, path = queue.popleft()
        for to_val in graph.get(current, []):
            if to_val in visited:
                continue
            wp = _Waypoint(from_value=current, to_value=to_val)
            new_path = path + [wp]
            if to_val == target_value:
                return new_path
            visited.add(to_val)
            queue.append((to_val, new_path))

    return None
