"""System points in the prover kernel.

The compiled step function reads system tags (``sys.*``, ``rtc.*``,
``fault.*``, ...) from ``kernel.tags`` but never sets them.  The runner does
that around the step function (``SystemPointRuntime.on_scan_start``, the
per-scan system-tag materialization, ``on_scan_end``).  BFS replays only the
step function, so without a model every system tag would sit at its ``Tag``
default for the whole search: ``always_on`` would read False,
``first_scan`` would never fire and the clocks would never toggle.

The model reproduces the runner at the prover's fixed ``dt``, one rule per
referenced system tag:

``constant``
    Resolved identically on every scan: ``always_on``, ``fixed_scan_mode``,
    ``scan_time_current_ms`` and the fixed-scan setup, ``sys.dt``, the SD
    status the core runtime always reports, and ``mode_run``/
    ``mode_switch_run`` when the program never writes ``cmd_mode_stop`` (a
    write from outside the program, over Modbus or an HMI, is not modeled).
    Written into the power-on kernel.
``first_scan``
    True on scan 0 only.  The power-on kernel holds True and every scan
    leaves False behind for the next one, so the snapshot value is the value
    the next scan reads.  The tag is a stateful dimension so the power-on
    state stays distinct from every later state.  ``scan_time_min_ms`` and
    ``scan_time_max_ms`` ride on the same bit: 0 on scan 0 (the runner sets
    them at the end of the first scan), the fixed scan time after.
``free``
    Values that follow the scan count, the timestamp or the wall clock: the
    clocks, ``scan_clock_toggle``, ``scan_counter`` and the RTC fields, plus
    ``battery_present``, which the environment sets (``plc.battery_present``)
    like any external input.  The state key carries none of these, so they
    become nondeterministic inputs: any value on any scan, within the RTC
    fields' calendar ranges.  That over-approximates (a counterexample may
    need a timing the PLC never produces, see ``FREE_SYSTEM_CAVEAT``) but never
    loses a reachable state.  ``mode_run``, ``sd.write_status`` and the RTC
    apply errors are free only when the program writes the command that
    changes them.
``scan_start``
    Status bits and command bits the runtime resets before each scan
    (transient fault flags, RTC apply errors, mode/watchdog/SD commands,
    RTC apply requests).  They are reset before every step function call.
    Their end-of-scan value is not what the next scan reads, so they are
    hidden from partial evaluation (``hidden``).

The model covers the system tags the rungs read and the ones the property,
scope or projection names.  A callable property can read anything, so its
state view (``guarded_state``) answers the constants and refuses
(``UnmodeledSystemRead``) a free or scan-dependent tag the search did not
explore.

``reachable`` / ``always`` only.  The ``how()`` domain prior
(``domains_only``) keeps the old classification: PILOT drives the real
runner, where system tags are exact, and must not treat clocks as levers.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast

from pyrung.core.system_points import system
from pyrung.core.tag import Tag

_sys = system.sys
_rtc = system.rtc
_fault = system.fault
_sd = system.storage.sd

FIRST_SCAN = _sys.first_scan.name

FREE_SYSTEM_CAVEAT = (
    "System tags [{names}] follow the scan count, timestamp or wall clock and "
    "were explored as free inputs; the counterexample may rely on a sequence "
    "of their values the PLC never produces."
)

# Free regardless of what the program writes.
_ALWAYS_FREE: frozenset[str] = frozenset(
    {
        _sys.clock_10ms.name,
        _sys.clock_100ms.name,
        _sys.clock_500ms.name,
        _sys.clock_1s.name,
        _sys.clock_1m.name,
        _sys.clock_1h.name,
        _sys.scan_clock_toggle.name,
        _sys.scan_counter.name,
        _sys.battery_present.name,
        _rtc.year4.name,
        _rtc.year2.name,
        _rtc.month.name,
        _rtc.day.name,
        _rtc.weekday.name,
        _rtc.hour.name,
        _rtc.minute.name,
        _rtc.second.name,
    }
)

# Free when the program writes any of the listed command tags; otherwise the
# runtime holds them at the constant in ``_constants``.
_FREE_WHEN_WRITTEN: dict[str, frozenset[str]] = {
    _sys.mode_run.name: frozenset({_sys.cmd_mode_stop.name}),
    _sys.mode_switch_run.name: frozenset({_sys.cmd_mode_stop.name}),
    _sd.write_status.name: frozenset({_sd.eject_cmd.name, _sd.delete_all_cmd.name}),
    _rtc.apply_date_error.name: frozenset({_rtc.apply_date.name}),
    _rtc.apply_time_error.name: frozenset({_rtc.apply_time.name}),
}

# Reset by ``SystemPointRuntime.on_scan_start`` before the logic runs.
SCAN_START_RESETS: dict[str, Any] = {
    _fault.division_error.name: False,
    _fault.out_of_range.name: False,
    _fault.address_error.name: False,
    _rtc.apply_date_error.name: False,
    _rtc.apply_time_error.name: False,
    _rtc.apply_date.name: False,
    _rtc.apply_time.name: False,
    _sys.cmd_mode_stop.name: False,
    _sys.cmd_watchdog_reset.name: False,
    _sd.eject_cmd.name: False,
    _sd.delete_all_cmd.name: False,
}

# Fault flags instructions set implicitly (``Instruction._fault_writes``).
FAULT_FLAGS: frozenset[str] = frozenset(
    {_fault.division_error.name, _fault.out_of_range.name, _fault.address_error.name}
)

# 0 on scan 0, the fixed scan time after: tracked through the first_scan bit.
FIRST_SCAN_DEPENDENTS: frozenset[str] = frozenset(
    {_sys.scan_time_min_ms.name, _sys.scan_time_max_ms.name}
)

# Calendar ranges of the RTC fields (``datetime`` bounds; CLICK weekday 1..7).
_RANGES: dict[str, tuple[int, int]] = {
    _rtc.year4.name: (1, 9999),
    _rtc.year2.name: (0, 99),
    _rtc.month.name: (1, 12),
    _rtc.day.name: (1, 31),
    _rtc.weekday.name: (1, 7),
    _rtc.hour.name: (0, 23),
    _rtc.minute.name: (0, 59),
    _rtc.second.name: (0, 59),
}
_FULL_RANGE_CAP = 1000


def system_constants(dt: float) -> dict[str, Any]:
    """Values the runner resolves identically on every scan at fixed *dt*."""
    dt_ms = int(round(dt * 1000))
    return {
        _sys.always_on.name: True,
        _sys.fixed_scan_mode.name: True,
        _sys.mode_run.name: True,
        _sys.mode_switch_run.name: True,
        _sys.scan_time_current_ms.name: dt_ms,
        _sys.scan_time_fixed_setup_ms.name: dt_ms,
        _sys.interrupt_scan_time_ms.name: 0,
        _sys.dt.name: float(dt),
        _sd.ready.name: True,
        _sd.write_status.name: False,
        _sd.error.name: False,
        _sd.error_code.name: 0,
    }


def free_system_tags(names: Iterable[str], written: Collection[str]) -> frozenset[str]:
    """System tags among *names* that BFS must explore as free inputs."""
    free: set[str] = set()
    for name in names:
        if name in _ALWAYS_FREE:
            free.add(name)
            continue
        commands = _FREE_WHEN_WRITTEN.get(name)
        if commands is not None and any(cmd in written for cmd in commands):
            free.add(name)
    return frozenset(free)


def free_system_domain(name: str) -> tuple[Any, ...] | None:
    """Every value of a free system tag with a small calendar range, or None."""
    bounds = _RANGES.get(name)
    if bounds is None or bounds[1] - bounds[0] + 1 > _FULL_RANGE_CAP:
        return None
    return tuple(range(bounds[0], bounds[1] + 1))


def bounded_system_tag(tag: Tag) -> Tag:
    """*tag* with its calendar range as ``min``/``max``, for domain inference.

    The declared system tags carry no bounds and default to 0, which is not a
    month, so the comparison split would otherwise keep impossible values
    such as ``rtc.month == 0``.
    """
    bounds = _RANGES.get(tag.name)
    if bounds is None or tag.min is not None or tag.max is not None:
        return tag
    return replace(cast(Any, tag), min=bounds[0], max=bounds[1], default=bounds[0])


def clip_to_range(name: str, domain: tuple[Any, ...]) -> tuple[Any, ...]:
    """Keep a free system tag's domain inside its calendar range.

    Values outside the range are unreachable (``rtc.month == 0``).  The range
    bounds join as representatives, so a comparison class whose only
    representative fell outside (``rtc.hour < 30`` split at 29/30/31) is still
    covered by the bound it contains.
    """
    bounds = _RANGES.get(name)
    if bounds is None:
        return domain
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in domain):
        return domain
    lo, hi = bounds
    return tuple(sorted({v for v in domain if lo <= v <= hi} | {lo, hi}))


class UnmodeledSystemRead(Exception):
    """A callable property read a system tag the search did not explore."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class _GuardedState(Mapping[str, Any]):
    """State view for callable properties (see ``guarded_state``)."""

    __slots__ = ("_constants", "_refused", "_state")

    def __init__(
        self,
        state: dict[str, Any],
        constants: dict[str, Any],
        refused: frozenset[str],
    ) -> None:
        self._state = dict(state)
        self._constants = constants
        self._refused = refused

    def __getitem__(self, name: str) -> Any:
        if name in self._refused:
            raise UnmodeledSystemRead(name)
        if name in self._state:
            return self._state[name]
        return self._constants[name]

    def __iter__(self) -> Iterator[str]:
        yield from self._state
        yield from (name for name in self._constants if name not in self._state)

    def __len__(self) -> int:
        return len(self._state) + sum(1 for name in self._constants if name not in self._state)


def callable_refusals(
    nondeterministic: Collection[str],
    stateful: Collection[str],
    written: Collection[str],
    read_by_rungs: Collection[str],
    uncounted_faults: Collection[str],
) -> frozenset[str]:
    """System tags a callable property may not read in this search.

    Free tags the search did not explore: not an input, or an input no rung
    reads.  Such an input is explored only when the callable's ``scope=``
    names it; the refusal does not consult the scope, so it stays
    conservative.  Also ``first_scan`` (and the scan-time min/max
    riding on it) when it is not a dimension, and fault flags whose
    instruction writers the graph does not count.
    """
    refused = {
        name
        for name in free_system_tags((*_ALWAYS_FREE, *_FREE_WHEN_WRITTEN), written)
        if name not in nondeterministic or name not in read_by_rungs
    }
    if FIRST_SCAN not in stateful:
        refused.add(FIRST_SCAN)
        refused.update(FIRST_SCAN_DEPENDENTS)
    refused.update(uncounted_faults)
    return frozenset(refused)


def guarded_state(
    state: dict[str, Any],
    constants: dict[str, Any],
    refused: frozenset[str],
) -> Mapping[str, Any]:
    """Copy of *state* that answers constants and refuses unexplored tags."""
    return _GuardedState(state, constants, refused)


@dataclass(frozen=True, slots=True)
class SystemPointModel:
    """Kernel-side system point values (see the module docstring).

    ``power_on`` overrides the ``Tag``-default template of a fresh kernel.
    ``scan_start`` is applied before each step function call and
    ``after_scan`` after it.  ``hidden`` lists the tags whose end-of-scan
    value is not the next scan's entry value.
    """

    power_on: tuple[tuple[str, Any], ...]
    scan_start: tuple[tuple[str, Any], ...]
    after_scan: tuple[tuple[str, Any], ...]
    hidden: frozenset[str]

    def seed(self, tags: dict[str, Any]) -> None:
        for name, value in self.power_on:
            tags[name] = value


def build_system_point_model(
    referenced: Collection[str],
    written: Collection[str],
    free: Mapping[str, tuple[Any, ...]],
    dt: float,
) -> SystemPointModel | None:
    """Model for the system tags in *referenced* (kernel and property names).

    *written* is the PDG writer index and *free* maps the tags classification
    turned into nondeterministic inputs to their domains.  BFS assigns those
    on every step; the power-on kernel holds the first value of the domain,
    since the ``Tag`` default (0) need not be one (``rtc.month``).  Returns
    None when no referenced system tag needs a kernel hook.
    """
    constants = system_constants(dt)
    power_on: list[tuple[str, Any]] = []
    for name in sorted(referenced):
        if name in free:
            if free[name]:
                power_on.append((name, free[name][0]))
            continue
        if name in written:
            continue
        if name in constants:
            power_on.append((name, constants[name]))
    after_scan: list[tuple[str, Any]] = []
    if FIRST_SCAN in referenced or any(name in referenced for name in FIRST_SCAN_DEPENDENTS):
        dt_ms = int(round(dt * 1000))
        power_on.append((FIRST_SCAN, True))
        after_scan.append((FIRST_SCAN, False))
        for name in sorted(FIRST_SCAN_DEPENDENTS):
            if name in referenced:
                power_on.append((name, 0))
                after_scan.append((name, dt_ms))
    scan_start = tuple(
        (name, value)
        for name, value in sorted(SCAN_START_RESETS.items())
        if name in referenced and name not in free
    )
    if not (power_on or scan_start or after_scan):
        return None
    return SystemPointModel(
        power_on=tuple(power_on),
        scan_start=scan_start,
        after_scan=tuple(after_scan),
        hidden=frozenset(name for name, _ in scan_start),
    )


__all__ = [
    "FAULT_FLAGS",
    "FIRST_SCAN",
    "FIRST_SCAN_DEPENDENTS",
    "FREE_SYSTEM_CAVEAT",
    "SCAN_START_RESETS",
    "SystemPointModel",
    "UnmodeledSystemRead",
    "bounded_system_tag",
    "build_system_point_model",
    "callable_refusals",
    "clip_to_range",
    "free_system_domain",
    "free_system_tags",
    "guarded_state",
    "system_constants",
]
