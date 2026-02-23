"""Synchronous run_function() example for multi-step math."""

from __future__ import annotations

from typing import Any


def weighted_average(
    sensor1: float,
    sensor2: float,
    sensor3: float,
    weight1: float = 0.5,
    weight2: float = 0.3,
    weight3: float = 0.2,
) -> dict[str, Any]:
    """Compute a weighted average and return PLC output mapping."""
    weight_sum = float(weight1 + weight2 + weight3)
    if weight_sum == 0:
        return {"result": 0.0}

    total = (
        float(sensor1) * float(weight1)
        + float(sensor2) * float(weight2)
        + float(sensor3) * float(weight3)
    )
    return {"result": total / weight_sum}
