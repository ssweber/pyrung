"""Live route readings for ORIENT.

This instrument reads the current compass graph under the captain's active
constraints. It returns a bearing for *this* world only; callers never carry
the returned suffix through later observations.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.core.analysis.pilot.charts import CompassEdge, CompassGraph, CompassPlan


def live_compass_plan(
    needed_tag: str,
    needed_value: Any,
    snapshot: dict[str, Any],
    graphs: tuple[CompassGraph, ...],
    *,
    edge_allowed: Callable[[CompassEdge], bool],
) -> CompassPlan | None:
    """Read the best currently allowed compass bearing.

    ``edge_allowed`` filters BEFORE path selection — avoided actions and
    world-keyed wait nogoods are removed, including edges later in a
    prospective suffix, so BFS returns the surviving route.  The next ORIENT
    performs a fresh query against its newly observed world.
    """
    from pyrung.core.analysis.pilot.charts import best_compass_plan

    return best_compass_plan(
        needed_tag,
        needed_value,
        snapshot,
        graphs,
        edge_allowed=edge_allowed,
    )
