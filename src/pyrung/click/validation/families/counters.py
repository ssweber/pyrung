"""The Counter family: count_up() and count_down().

laddercodec's Counter checks them.
"""

from __future__ import annotations

from typing import Any

from laddercodec import Counter

from ..fields import Family, refusals_of, render_operand, render_preset
from ..resolve import _ResolutionCache

# The argument of the pyrung call that holds each field of laddercodec's Counter. The
# counter's UDT holds the Done bit and the Acc. The `.down()` and `.reset()` calls
# turn on their inputs and add their pins.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Counter.done": 0,
    "Counter.accumulator": 0,
    "Counter.preset": "preset",
    "Counter.down_enabled": "down",
    "Counter.reset_enabled": "reset",
    "Counter.pin_rows": "reset",
}


# pyrung's advice for a refusal of a field, where the advice names pyrung's own arguments.
_ADVICE: dict[tuple[str, str], str] = {
    ("timer-pair-mismatch", "Counter.accumulator"): (
        "Map Done and Acc to the same number, e.g. CT5 with CTD5."
    ),
}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    return _ADVICE.get((code, field))


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Counter that the export writes."""

    is_up = type(instruction).__name__ == "CountUpInstruction"
    done = render_operand(exporter, instruction.done_bit.tag, "done_bit", instruction)
    accumulator = render_operand(exporter, instruction.accumulator.tag, "accumulator", instruction)
    preset = render_preset(exporter, instruction.preset, "preset", instruction)
    down_enabled = is_up and instruction.down_condition is not None
    return refusals_of(
        lambda: Counter(
            kind="count_up" if is_up else "count_down",
            done=done,
            accumulator=accumulator,
            preset=preset,
            down_enabled=down_enabled,
        )
    )


_COUNT_DOWN_WORDS = {
    "done_bit": "counter",
    "accumulator": "counter",
    "preset": "preset",
    "reset_condition": "reset condition",
}
_COUNT_UP_WORDS = {**_COUNT_DOWN_WORDS, "down_condition": "down condition"}

FAMILY = Family(
    name="counters",
    classes={"CountUpInstruction": _COUNT_UP_WORDS, "CountDownInstruction": _COUNT_DOWN_WORDS},
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
)
