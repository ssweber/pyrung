"""Apply current constraints to a static transition-graph query.

``live_compass_plan`` removes disallowed edges before path selection, then
delegates to ``charts.best_compass_plan``. Its result is valid for the supplied
snapshot and constraints only; callers query again after the world changes.
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
    """Return the best static graph path allowed in the current world.

    ``edge_allowed`` filters BEFORE path selection — avoided actions and
    world-keyed wait nogoods are removed, including edges later in a
    prospective suffix, so BFS returns the surviving route. Callers query again
    after observing a new world.
    """
    from pyrung.core.analysis.pilot.charts import best_compass_plan

    return best_compass_plan(
        needed_tag,
        needed_value,
        snapshot,
        graphs,
        edge_allowed=edge_allowed,
    )
