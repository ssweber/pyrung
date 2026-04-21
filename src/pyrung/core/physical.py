"""Physical feedback declarations.

A ``Physical`` describes the real-world response characteristics of a
feedback signal — how long a bool feedback takes to assert/deassert, or
which profile function drives an analog response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|min|s|m|h|d)")

_UNIT_TO_MS: dict[str, float] = {
    "ms": 1.0,
    "s": 1_000.0,
    "min": 60_000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
    "d": 86_400_000.0,
}


def parse_duration(text: str) -> int:
    """Parse a compound duration string into milliseconds.

    Accepts strings like ``"2s"``, ``"500ms"``, ``"2s50ms"``,
    ``"1h30min"``.  Tokens are summed left-to-right.

    Raises ``ValueError`` for empty or unparseable strings.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty duration string")

    total = 0.0
    pos = 0
    found = False

    for match in _DURATION_TOKEN.finditer(stripped):
        if match.start() != pos:
            bad = stripped[pos : match.start()].strip()
            if bad:
                raise ValueError(
                    f"unexpected '{bad}' in duration '{text}'"
                )
        value = float(match.group(1))
        unit = match.group(2)
        total += value * _UNIT_TO_MS[unit]
        pos = match.end()
        found = True

    if not found:
        raise ValueError(f"no duration tokens in '{text}'")

    trailing = stripped[pos:].strip()
    if trailing:
        raise ValueError(f"unexpected '{trailing}' in duration '{text}'")

    return int(total)


FeedbackType = Literal["bool", "analog"]


@dataclass(frozen=True)
class Physical:
    """Declares physical feedback characteristics for a tag or field.

    Bool feedback (has timing)::

        motor_fb = Physical("MotorFb", on_delay="2s", off_delay="500ms")

    Analog feedback (has profile)::

        temp = Physical("TempSensor", profile="first_order")

    The ``system`` field groups related feedback for reporting.
    """

    name: str
    on_delay: str | None = None
    off_delay: str | None = None
    profile: str | None = None
    system: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Physical name must be non-empty")

        has_timing = self.on_delay is not None or self.off_delay is not None
        has_profile = self.profile is not None

        if has_timing and has_profile:
            raise ValueError(
                f"Physical '{self.name}' has both timing and profile — "
                f"a feedback is either bool (on_delay/off_delay) or "
                f"analog (profile), not both"
            )

        if not has_timing and not has_profile:
            raise ValueError(
                f"Physical '{self.name}' has neither timing nor profile — "
                f"provide on_delay/off_delay for bool feedback or "
                f"profile for analog feedback"
            )

        if self.on_delay is not None:
            parse_duration(self.on_delay)
        if self.off_delay is not None:
            parse_duration(self.off_delay)

    @property
    def feedback_type(self) -> FeedbackType:
        if self.on_delay is not None or self.off_delay is not None:
            return "bool"
        return "analog"

    @property
    def on_delay_ms(self) -> int | None:
        if self.on_delay is None:
            return None
        return parse_duration(self.on_delay)

    @property
    def off_delay_ms(self) -> int | None:
        if self.off_delay is None:
            return None
        return parse_duration(self.off_delay)
