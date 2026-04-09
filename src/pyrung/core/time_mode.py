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
    """Timer time units for Click PLC.

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


TimeUnitStr = Literal["Tms", "Ts", "Tm", "Th", "Td"]

_VALID_UNITS: dict[str, TimeUnit] = {m.name: m for m in TimeUnit}


def _parse_time_unit(value: str) -> TimeUnit:
    """Convert a string unit name to a TimeUnit enum member."""
    try:
        return _VALID_UNITS[value]
    except KeyError:
        valid = ", ".join(f"'{n}'" for n in _VALID_UNITS)
        raise ValueError(f"unknown unit '{value}'; expected one of {valid}") from None
