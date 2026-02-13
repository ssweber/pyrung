"""Synchronous custom() callback example for multi-step math."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pyrung.core.tag import Tag

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext


def weighted_average(*, inputs: list[Tag], weights: list[float], output: Tag) -> Callable[[ScanContext], None]:
    """Build a callback that computes a weighted average into ``output``."""
    if len(inputs) != len(weights):
        raise ValueError(
            f"weighted_average() inputs/weights length mismatch: {len(inputs)} != {len(weights)}"
        )

    def _execute(ctx: ScanContext) -> None:
        total = sum(
            float(ctx.get_tag(tag.name, tag.default)) * weight
            for tag, weight in zip(inputs, weights, strict=True)
        )
        weight_sum = float(sum(weights))
        result = total / weight_sum if weight_sum else 0
        ctx.set_tag(output.name, result)

    return _execute
