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
    the scan-time readings, ``sys.dt``, the SD status the core runtime always
    reports, and ``mode_run``/``mode_switch_run`` when the program never
    writes ``cmd_mode_stop``.  Written into the power-on kernel.
``first_scan``
    True on scan 0 only.  The power-on kernel holds True and every scan
    leaves False behind for the next one, so the snapshot value is the value
    the next scan reads.  The tag is a stateful dimension so the power-on
    state stays distinct from every later state.
``free``
    Values that follow the scan count, the timestamp or the wall clock:
    the clocks, ``scan_clock_toggle``, ``scan_counter``, the RTC fields,
    ``scan_time_min_ms``/``scan_time_max_ms`` (0 on scan 0 only), and the
    environment bit ``battery_present``.  The state key carries none of
    these, so they become nondeterministic inputs: any value on any scan.
    That over-approximates (a counterexample may need a timing the PLC never
    produces, see ``FREE_SYSTEM_CAVEAT``) but never loses a reachable state.
    ``mode_run``, ``sd.write_status`` and the RTC apply errors are free only
    when the program writes the command that changes them.
``scan_start``
    Status bits and command bits the runtime resets before each scan
    (transient fault flags, RTC apply errors, mode/watchdog/SD commands,
    RTC apply requests).  They are reset before every step function call.
    Their end-of-scan value is not what the next scan reads, so they are
    hidden from partial evaluation (``hidden``).

``reachable`` / ``always`` only.  The ``how()`` domain prior
(``domains_only``) keeps the old classification: PILOT drives the real
runner, where system tags are exact, and must not treat clocks as levers.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from pyrung.core.system_points import system

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
        _sys.scan_time_min_ms.name,
        _sys.scan_time_max_ms.name,
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

# Fallback domains for free system tags that no comparison partitions.
_FREE_DOMAINS: dict[str, tuple[Any, ...]] = {
    _rtc.year2.name: tuple(range(0, 100)),
    _rtc.month.name: tuple(range(1, 13)),
    _rtc.day.name: tuple(range(1, 32)),
    _rtc.weekday.name: tuple(range(1, 8)),
    _rtc.hour.name: tuple(range(0, 24)),
    _rtc.minute.name: tuple(range(0, 60)),
    _rtc.second.name: tuple(range(0, 60)),
}


def _constants(dt: float) -> dict[str, Any]:
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
    """Fallback finite domain for a free system tag, or None."""
    return _FREE_DOMAINS.get(name)


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
    free: Collection[str],
    dt: float,
) -> SystemPointModel | None:
    """Model for the system tags in *referenced* (kernel tag names).

    *written* is the PDG writer index and *free* the tags classification
    turned into nondeterministic inputs (BFS assigns those itself).
    Returns None when no referenced system tag needs a kernel hook.
    """
    constants = _constants(dt)
    power_on: list[tuple[str, Any]] = []
    for name in sorted(referenced):
        if name in free or name in written:
            continue
        if name in constants:
            power_on.append((name, constants[name]))
    after_scan: list[tuple[str, Any]] = []
    if FIRST_SCAN in referenced:
        power_on.append((FIRST_SCAN, True))
        after_scan.append((FIRST_SCAN, False))
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
    "FREE_SYSTEM_CAVEAT",
    "SCAN_START_RESETS",
    "SystemPointModel",
    "build_system_point_model",
    "free_system_domain",
    "free_system_tags",
]
