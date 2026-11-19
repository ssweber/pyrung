"""The Timer family: on_delay() and off_delay().

laddercodec's Timer checks them.
"""

from __future__ import annotations

from typing import Any, cast

from laddercodec import Timer, TimeUnit

from ..fields import Family, refusals_of, render_operand
from ..resolve import _ResolutionCache

# The argument of the pyrung call that holds each field of laddercodec's Timer. The timer's
# UDT holds the Done bit and the Acc. A `.reset()` makes the timer retentive and
# adds its pin.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Timer.done": 0,
    "Timer.accumulator": 0,
    "Timer.preset": "preset",
    "Timer.unit": "unit",
    "Timer.retained": "reset",
    "Timer.pin_rows": "reset",
}

# pyrung's advice for a refusal of a field, where the advice names pyrung's own arguments.
_ADVICE: dict[tuple[str, str], str] = {
    ("constant-out-of-range", "Timer.preset"): (
        "Use a larger time unit to keep the preset within 32767."
    ),
    ("timer-pair-mismatch", "Timer.accumulator"): (
        "Map Done and Acc to the same number, e.g. T5 with TD5."
    ),
}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    return _ADVICE.get((code, field))


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Timer that the export writes."""

    def render(attribute: str) -> str:
        return render_operand(exporter, getattr(instruction, attribute), attribute, instruction)

    kind = "on_delay" if type(instruction).__name__ == "OnDelayInstruction" else "off_delay"
    done = render("done_bit")
    accumulator = render("accumulator")
    preset = render("preset")
    unit = render("unit")
    retained = instruction.reset_condition is not None if kind == "on_delay" else False
    return refusals_of(
        lambda: Timer(
            kind=kind,
            done=done,
            accumulator=accumulator,
            preset=preset,
            unit=cast(TimeUnit, unit),
            retained=retained,
        )
    )


_OFF_DELAY_WORDS = {"done_bit": "timer", "accumulator": "timer", "preset": "preset"}
_ON_DELAY_WORDS = {**_OFF_DELAY_WORDS, "reset_condition": "reset condition"}

FAMILY = Family(
    name="timers",
    classes={"OnDelayInstruction": _ON_DELAY_WORDS, "OffDelayInstruction": _OFF_DELAY_WORDS},
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
)
