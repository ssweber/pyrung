"""Live route readings for ORIENT.

This instrument reads the current compass graph under the captain's active
constraints. It returns a bearing for *this* world only; callers never carry
the returned suffix through later observations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.core.analysis.pilot.charts import CompassEdge, CompassGraph, CompassPlan

ActionPair = tuple[str, Any]


def live_compass_plan(
    needed_tag: str,
    needed_value: Any,
    snapshot: dict[str, Any],
    graphs: tuple[CompassGraph, ...],
    route_allowed: Callable[[ActionPair], bool],
) -> CompassPlan | None:
    """Read the best currently allowed compass bearing.

    Avoided actions are removed before path selection, including actions later
    in a prospective suffix. The next ORIENT performs a fresh query against its
    newly observed world.
    """
    from pyrung.core.analysis.pilot.charts import best_compass_plan

    return best_compass_plan(
        needed_tag,
        needed_value,
        snapshot,
        graphs,
        edge_allowed=lambda edge: _edge_allowed(edge, route_allowed),
    )


def _edge_allowed(
    edge: CompassEdge,
    route_allowed: Callable[[ActionPair], bool],
) -> bool:
    return edge.action is None or route_allowed(edge.action)
