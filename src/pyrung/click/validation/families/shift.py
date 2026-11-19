"""The Shift family: shift().

laddercodec's Shift checks it.
"""

from __future__ import annotations

from typing import Any

from laddercodec import Shift

from ..fields import Family, Unbuildable, refusals_of, split_range
from ..resolve import _ResolutionCache

# The argument of the pyrung call that holds each field of laddercodec's Shift.
# Shift.pin_rows covers the clock and the reset, so it marks `.reset()`.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Shift.start_bit": 0,
    "Shift.end_bit": 0,
    "Shift.pin_rows": "reset",
}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    return None


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Shift that the export writes."""
    from pyrung.click.ladder.types import _RenderError

    try:
        text = exporter._render_shift_range(
            instruction.bit_range, path="instruction.bit_range", source=instruction
        )
    except _RenderError as exc:
        raise Unbuildable from exc
    start, end = split_range(text)
    return refusals_of(lambda: Shift(start_bit=start, end_bit=end))


FAMILY = Family(
    name="shift",
    classes={
        "ShiftInstruction": {
            "bit_range": "range",
            "clock_condition": "clock",
            "reset_condition": "reset",
        }
    },
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    ranges={"ShiftInstruction": ("bit_range",)},
)
