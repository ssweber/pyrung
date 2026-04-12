"""Time modes for PLC simulation."""

from __future__ import annotations

from enum import Enum
from typing import Literal


class TimeMode(Enum):
    """Simulation time modes.

    FIXED_STEP: Each scan advances by a fixed dt, regardless of wall clock.
                Use for unit tests and deterministic simulations.

    REALTIME: Simulation clock tracks actual elapsed time.
              Use for integration tests and hardware-in-loop.
    """

    FIXED_STEP = "fixed_step"
    REALTIME = "realtime"


class TimeUnit(Enum):
    """Timer time units.

    The accumulator stores integer values in the specified unit.
    Conversion from dt (seconds) uses appropriate scaling.
    """

    Tms = "milliseconds"  # 1 unit = 1 ms, multiply dt by 1000
    Ts = "seconds"  # 1 unit = 1 second
    Tm = "minutes"  # 1 unit = 1 minute, divide dt by 60
    Th = "hours"  # 1 unit = 1 hour, divide dt by 3600
    Td = "days"  # 1 unit = 1 day, divide dt by 86400

    def dt_to_units(self, dt_seconds: float) -> float:
        """Convert dt in seconds to timer units (with fractional part)."""
        match self:
            case TimeUnit.Tms:
                return dt_seconds * 1000
            case TimeUnit.Ts:
                return dt_seconds
            case TimeUnit.Tm:
                return dt_seconds / 60
            case TimeUnit.Th:
                return dt_seconds / 3600
            case TimeUnit.Td:
                return dt_seconds / 86400


# fmt: off
TimeUnitStr = Literal[
    # canonical
    "Tms", "Ts", "Tm", "Th", "Td",
    # short
    "ms", "s", "min", "m", "h", "d",
    # long
    "milliseconds", "millisecond", "msec",
    "seconds", "second", "sec",
    "minutes", "minute",
    "hours", "hour", "hr",
    "days", "day",
]
# fmt: on

_VALID_UNITS: dict[str, TimeUnit] = {m.name: m for m in TimeUnit}

UNIT_MAP: dict[str, str] = {
    "days": "Td",
    "day": "Td",
    "d": "Td",
    "hours": "Th",
    "hour": "Th",
    "hr": "Th",
    "h": "Th",
    "minutes": "Tm",
    "minute": "Tm",
    "min": "Tm",
    "m": "Tm",
    "seconds": "Ts",
    "second": "Ts",
    "sec": "Ts",
    "s": "Ts",
    "milliseconds": "Tms",
    "millisecond": "Tms",
    "msec": "Tms",
    "ms": "Tms",
}


def normalize_unit(unit: str) -> str:
    """Normalize a time unit string to its canonical form.

    ``"ms"``, ``"sec"``, ``"min"``, ``"hour"``, ``"day"`` (and plurals,
    abbreviations, T-prefixed forms) → ``"Tms"``/``"Ts"``/``"Tm"``/``"Th"``/``"Td"``.
    """
    key = unit.lower().strip()
    if key == "t":
        raise ValueError("ambiguous time unit 'T' — use one of: ms, s, min, h, d")
    # Strip leading 't' so "tms" → "ms", "ts" → "s", etc.
    stripped = key.lstrip("t")
    if stripped in UNIT_MAP:
        return UNIT_MAP[stripped]
    if key in UNIT_MAP:
        return UNIT_MAP[key]
    raise ValueError(f"unknown time unit '{unit}' — use one of: ms, s, min, h, d")


def _parse_time_unit(value: str) -> TimeUnit:
    """Convert a string unit name to a TimeUnit enum member."""
    return _VALID_UNITS[normalize_unit(value)]
