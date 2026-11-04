"""Time modes for PLC simulation."""

from __future__ import annotations

from enum import Enum
from fractions import Fraction
from functools import lru_cache
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

    The accumulator stores integer values in the specified unit.  Elapsed time
    counts in whole nanoseconds, ``ns`` to a unit (see :func:`dt_to_ns`), so the
    scan a timer is done on does not depend on the unit it counts in.
    """

    Tms = "milliseconds"  # 1 unit = 1 ms, multiply dt by 1000
    Ts = "seconds"  # 1 unit = 1 second
    Tm = "minutes"  # 1 unit = 1 minute, divide dt by 60
    Th = "hours"  # 1 unit = 1 hour, divide dt by 3600
    Td = "days"  # 1 unit = 1 day, divide dt by 86400

    ns: int  # nanoseconds to one unit

    def dt_to_units(self, dt_seconds: float) -> float:
        """Convert dt in seconds to timer units, as an estimate of a timer's rate.

        Timers themselves count through :func:`dt_to_ns`: this float loses time
        when summed.
        """
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


TimeUnit.Tms.ns = 1_000_000
TimeUnit.Ts.ns = 1_000_000_000
TimeUnit.Tm.ns = 60_000_000_000
TimeUnit.Th.ns = 3_600_000_000_000
TimeUnit.Td.ns = 86_400_000_000_000


# How far a dt may move to become whole nanoseconds.  A clock read as a float carries
# noise of a fraction of a nanosecond, which at a scan of a millisecond is well under this;
# 2**-20 s, 953.67 ns, moves by 3 parts in 10000, and that would be time lost.
_WHOLE_NS_WITHIN = 1e-6


@lru_cache(maxsize=64)
def dt_to_ns(dt_seconds: float) -> int | Fraction:
    """The time a scan of *dt_seconds* takes, in nanoseconds, with nothing lost in a sum.

    A dt written as a decimal is that many whole nanoseconds, the float's own rounding
    gone: 0.1 is 100_000_000.  One too fine for that which is a plain ratio (1/3, 1/1024)
    stays that ratio exactly, as a ``Fraction``.  A measured dt, whose last digits are
    float noise, is the nearest whole nanosecond where that barely moves it.  Anything
    else is the float's own value, exactly, as a ``Fraction``.
    """
    exact = Fraction(dt_seconds).limit_denominator(1_000_000)
    if float(exact) == dt_seconds:
        ns = exact * 1_000_000_000
        return ns.numerator if ns.denominator == 1 else ns
    scaled = dt_seconds * 1e9
    nearest = round(scaled)
    if abs(scaled - nearest) <= scaled * _WHOLE_NS_WITHIN:
        return nearest
    return Fraction(dt_seconds) * 1_000_000_000


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
        raise ValueError("ambiguous time unit 'T'; use one of: ms, s, min, h, d")
    # Strip leading 't' so "tms" → "ms", "ts" → "s", etc.
    stripped = key.lstrip("t")
    if stripped in UNIT_MAP:
        return UNIT_MAP[stripped]
    if key in UNIT_MAP:
        return UNIT_MAP[key]
    raise ValueError(f"unknown time unit '{unit}'; use one of: ms, s, min, h, d")


def _parse_time_unit(value: str) -> TimeUnit:
    """Convert a string unit name to a TimeUnit enum member."""
    return _VALID_UNITS[normalize_unit(value)]
