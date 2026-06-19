"""Waypoint planning for the corridor walker.

Builds the governing tag's transition graph from static analysis (zero
simulation cost), annotates edges with prerequisite cost and fragility,
and computes the cheapest waypoint sequence via Dijkstra.
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.walk.base import _MAX_PREREQ_DEPTH, _WalkContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_INF_COST = 1_000_000


@dataclass(frozen=True)
class _Waypoint:
    from_value: Any
    to_value: Any
    fragile: bool = False


def _prerequisite_cost(
    tag: str,
    value: Any,
    snapshot: dict[str, Any],
    pdg: Any,
    program: Any,
    governing: str,
    known: dict[str, Any] | None = None,
    nd_domains: dict[str, tuple[Any, ...]] | None = None,
    _visited: frozenset[tuple[str, Any]] | None = None,
    _depth: int = 0,
) -> tuple[int, bool]:
    """Static cost estimate and fragility for establishing ``(tag, value)``.

    Returns ``(cost, fragile)``:
    - *cost*: 0 = already satisfied, 1 = external input, N = recursive
      prerequisite depth.  ``_INF_COST`` = structurally unreachable (prune).
    - *fragile*: ``True`` when the prerequisite chain depends on the
      governing tag (order-dependent, one-shot); ``False`` when
      order-independent (retryable at any corridor position).
    """
    from pyrung.core.analysis.pdg import TagRole
    from pyrung.core.analysis.walk.base import _values_match
    from pyrung.core.analysis.walk.priors import _writer_candidates

    if _values_match(snapshot.get(tag), value):
        return 0, False

    goal = (tag, value)
    if _visited is None:
        _visited = frozenset()
    if goal in _visited or _depth >= _MAX_PREREQ_DEPTH:
        return _INF_COST, True

    _visited = _visited | {goal}

    if pdg.tag_roles.get(tag) == TagRole.INPUT:
        return 1, False

    writers_of = pdg.writers_of.get(tag, frozenset())
    if not writers_of:
        return _INF_COST, True

    _prereqs, candidates = _writer_candidates(
        tag,
        value,
        snapshot,
        pdg,
        program,
        nd_domains=nd_domains,
        known=known,
    )

    if not candidates:
        if _prereqs:
            candidates_unsatisfied = [
                [(t, v) for t, v in _prereqs if not _values_match(snapshot.get(t), v)]
            ]
        else:
            return _INF_COST, True
    else:
        candidates_unsatisfied = [
            [(t, v) for t, v in c.unsatisfied if not _values_match(snapshot.get(t), v)]
            for c in candidates
        ]

    best_cost = _INF_COST
    best_fragile = True

    for unsatisfied in candidates_unsatisfied:
        if not unsatisfied:
            best_cost = 1
            best_fragile = False
            break

        writer_cost = 1
        writer_fragile = False
        for ptag, pval in unsatisfied:
            sub_cost, sub_fragile = _prerequisite_cost(
                ptag,
                pval,
                snapshot,
                pdg,
                program,
                governing,
                known=known,
                nd_domains=nd_domains,
                _visited=_visited,
                _depth=_depth + 1,
            )
            writer_cost += sub_cost
            if sub_fragile:
                writer_fragile = True
            if writer_cost >= _INF_COST:
                break

        if not writer_fragile:
            upstream = pdg.upstream_slice(tag)
            if governing in upstream:
                writer_fragile = True

        if writer_cost < best_cost or (writer_cost == best_cost and not writer_fragile):
            best_cost = writer_cost
            best_fragile = writer_fragile

    return best_cost, best_fragile


def _static_transition_graph(
    ctx: _WalkContext,
    governing: str,
) -> dict[Any, list[Any]]:
    """Build a transition graph from static analysis (zero simulation cost).

    Uses the crossing registry's ``forward()`` to classify each writer's
    output and ``_extract_condition_values`` on the rung SP-tree for
    from-values.

    Returns ``{from_value: [to_value, ...], ...}``.
    """
    from pyrung.core.analysis.pdg import resolve_rung as _resolve_rung
    from pyrung.core.analysis.simplified import _sp_to_expr
    from pyrung.core.analysis.sp_values import (
        _extract_condition_values,
        _written_value_for_tag,
    )
    from pyrung.core.crossing import UNKNOWN, Affine, Literal

    graph: dict[Any, list[Any]] = {}
    pdg = ctx.pdg
    if pdg is None:
        return graph

    for rung_idx in pdg.writers_of.get(governing, frozenset()):
        node = pdg.rung_nodes[rung_idx]
        rung_obj = _resolve_rung(ctx.program, node)
        if rung_obj is None:
            continue

        fwd = _written_value_for_tag(rung_obj, governing)
        if fwd is UNKNOWN:
            continue

        sp = rung_obj.sp_tree()
        if sp is None:
            continue
        cond_values = _extract_condition_values(_sp_to_expr(sp))
        from_vals = cond_values.get(governing, frozenset())

        if isinstance(fwd, Literal):
            to_value = fwd.value
            for fv in from_vals:
                if fv != to_value:
                    graph.setdefault(fv, []).append(to_value)
            if not from_vals:
                graph.setdefault(None, []).append(to_value)
        elif isinstance(fwd, Affine):
            for fv in from_vals:
                graph.setdefault(fv, []).append(fv * fwd.scale + fwd.offset)

    return graph


def _weighted_transition_graph(
    ctx: _WalkContext,
    governing: str,
    snapshot: dict[str, Any],
) -> dict[Any, list[tuple[Any, int, bool]]]:
    """Annotated transition graph: ``{from_val: [(to_val, cost, fragile), ...]}``.

    Each edge carries the total static prerequisite cost and a fragility
    flag.  Edges with ``_INF_COST`` are pruned.
    """
    from pyrung.core.analysis.walk.priors import _writer_candidates

    raw = _static_transition_graph(ctx, governing)
    pdg = ctx.pdg
    if pdg is None:
        return {}

    weighted: dict[Any, list[tuple[Any, int, bool]]] = {}

    for from_val, to_vals in raw.items():
        projected = dict(snapshot)
        if from_val is not None:
            projected[governing] = from_val

        for to_val in to_vals:
            _prereqs, candidates = _writer_candidates(
                governing,
                to_val,
                projected,
                pdg,
                ctx.program,
                nd_domains=ctx.nd_domains,
                known=ctx.known,
            )

            if not candidates and not _prereqs:
                weighted.setdefault(from_val, []).append((to_val, 1, False))
                continue

            unsatisfied = []
            if candidates:
                best_c = min(candidates, key=lambda c: len(c.unsatisfied))
                unsatisfied = list(best_c.unsatisfied)
            else:
                from pyrung.core.analysis.walk.base import _values_match

                unsatisfied = [
                    (t, v) for t, v in _prereqs if not _values_match(projected.get(t), v)
                ]

            if not unsatisfied:
                weighted.setdefault(from_val, []).append((to_val, 1, False))
                continue

            edge_cost = 1
            edge_fragile = False
            for ptag, pval in unsatisfied:
                sub_cost, sub_fragile = _prerequisite_cost(
                    ptag,
                    pval,
                    projected,
                    pdg,
                    ctx.program,
                    governing,
                    known=ctx.known,
                    nd_domains=ctx.nd_domains,
                )
                edge_cost += sub_cost
                if sub_fragile:
                    edge_fragile = True
                if edge_cost >= _INF_COST:
                    break

            if edge_cost < _INF_COST:
                weighted.setdefault(from_val, []).append((to_val, edge_cost, edge_fragile))

    return weighted


def _compute_waypoint_sequence(
    graph: dict[Any, list[tuple[Any, int, bool]]],
    start_value: Any,
    target_value: Any,
) -> list[_Waypoint] | None:
    """Dijkstra on the weighted transition graph for the cheapest waypoint path.

    Returns the sequence as ``_Waypoint`` objects, or ``None`` if unreachable.
    Breaks ties by hop count (fewer hops preferred at equal cost).
    """
    if start_value == target_value:
        return []

    # (total_cost, hop_count, counter, current_value, path)
    counter = 0
    heap: list[tuple[int, int, int, Any, list[_Waypoint]]] = [(0, 0, counter, start_value, [])]
    best: dict[Any, int] = {}

    while heap:
        cost, hops, _cnt, current, path = heapq.heappop(heap)

        if current in best and best[current] <= cost:
            continue
        best[current] = cost

        if current == target_value:
            return path

        for to_val, edge_cost, fragile in graph.get(current, []):
            new_cost = cost + edge_cost
            if to_val in best and best[to_val] <= new_cost:
                continue
            wp = _Waypoint(from_value=current, to_value=to_val, fragile=fragile)
            counter += 1
            heapq.heappush(heap, (new_cost, hops + 1, counter, to_val, path + [wp]))

    return None
