"""Autoharness: automatic feedback synthesis from Physical + link= declarations."""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from math import ceil
from typing import TYPE_CHECKING, Any

from pyrung.core.physical import Physical
from pyrung.core.tag import TagType

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

_profile_registry: dict[str, Callable[..., Any]] = {}


def profile(name: str) -> Callable[..., Any]:
    """Register an analog feedback profile function.

    The decorated function is called once per scan tick for each active
    analog coupling::

        @profile("generic_thermal")
        def generic_thermal(cur, en, dt):
            if en:
                return cur + 0.5 * dt
            return cur
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _profile_registry[name] = fn
        return fn

    return decorator


@dataclass
class _ScheduledPatch:
    target_scan: int
    tag_name: str
    value: bool | int | float | str
    _seq: int = 0

    def __lt__(self, other: _ScheduledPatch) -> bool:
        if self.target_scan != other.target_scan:
            return self.target_scan < other.target_scan
        return self._seq < other._seq


@dataclass(frozen=True)
class Coupling:
    """Public view of one enable→feedback coupling discovered by the harness."""

    en_name: str
    fb_name: str
    physical: Physical
    trigger_value: int | str | None = None


@dataclass
class _BoolCoupling:
    en_name: str
    fb_name: str
    on_delay_ms: int
    off_delay_ms: int
    physical: Physical
    trigger_value: int | str | None = None


@dataclass
class _ProfileCoupling:
    en_name: str
    fb_name: str
    profile_name: str
    physical: Physical
    active: bool = False
    trigger_value: int | str | None = None


def _parse_link_spec(link: str) -> tuple[str, str | None]:
    name, _, trigger = link.partition(":")
    return (name, trigger or None)


def _resolve_trigger_value(trigger_raw: str, en_tag: Any) -> int | str:
    try:
        return int(trigger_raw)
    except ValueError:
        pass
    choices = getattr(en_tag, "choices", None)
    if choices is not None:
        for key, label in choices.items():
            if label == trigger_raw:
                return int(key) if isinstance(key, (int, float)) else key
    if getattr(en_tag, "type", None) == TagType.CHAR:
        return trigger_raw
    if choices is None:
        raise ValueError(
            f"Trigger value {trigger_raw!r} is not an int literal and "
            f"enable tag {en_tag.name!r} has no choices map."
        )
    raise ValueError(
        f"Trigger label {trigger_raw!r} not found in choices for "
        f"{en_tag.name!r}. Available: {list(choices.values())}."
    )


@dataclass
class Harness:
    """Automatic feedback harness driven by Physical + link= declarations.

    Walks all known tags to find link= couplings, installs edge monitors
    on En tags, and schedules Fb patches using declared timing (bool) or
    profile functions (analog).

    Usage::

        plc = PLC(logic, dt=0.010)
        harness = Harness(plc)
        harness.install()
        plc.run_for(0.5)  # Fb patches synthesized automatically
    """

    _plc: PLC = field(repr=False)
    _heap: list[_ScheduledPatch] = field(default_factory=list, init=False)
    _seq: int = field(default=0, init=False)
    _bool_couplings: list[_BoolCoupling] = field(default_factory=list, init=False)
    _profile_couplings: list[_ProfileCoupling] = field(default_factory=list, init=False)
    _monitors: list[Any] = field(default_factory=list, init=False)
    _installed: bool = field(default=False, init=False)

    def __init__(self, plc: PLC) -> None:
        self._plc = plc
        self._heap: list[_ScheduledPatch] = []
        self._seq = 0
        self._bool_couplings: list[_BoolCoupling] = []
        self._profile_couplings: list[_ProfileCoupling] = []
        self._monitors: list[Any] = []
        self._installed = False
        self.on_patches_applied: Callable[[list[tuple[str, Any, str]]], None] | None = None

    def install(self) -> None:
        if self._installed:
            return
        self._installed = True
        self._discover_couplings()
        self._install_monitors()
        self._plc._pre_scan_callbacks.append(self._on_pre_scan)

    def uninstall(self) -> None:
        if not self._installed:
            return
        self._installed = False
        for handle in self._monitors:
            handle.remove()
        self._monitors.clear()
        try:
            self._plc._pre_scan_callbacks.remove(self._on_pre_scan)
        except ValueError:
            pass
        self._heap.clear()
        self._bool_couplings.clear()
        self._profile_couplings.clear()

    @property
    def pending_count(self) -> int:
        return len(self._heap)

    def couplings(self) -> Iterator[Coupling]:
        """Iterate over all discovered couplings (bool and profile)."""
        for c in self._bool_couplings:
            yield Coupling(c.en_name, c.fb_name, c.physical, c.trigger_value)
        for c in self._profile_couplings:
            yield Coupling(c.en_name, c.fb_name, c.physical, c.trigger_value)

    def _schedule(self, target_scan: int, tag_name: str, value: Any) -> None:
        entry = _ScheduledPatch(target_scan, tag_name, value, self._seq)
        self._seq += 1
        heapq.heappush(self._heap, entry)

    def _drain_due(self) -> dict[str, Any]:
        next_scan = self._plc.current_state.scan_id + 1
        patches: dict[str, Any] = {}
        while self._heap and self._heap[0].target_scan <= next_scan:
            entry = heapq.heappop(self._heap)
            patches[entry.tag_name] = entry.value
        return patches

    def _on_pre_scan(self) -> None:
        bool_patches = self._drain_due()
        analog_details = self._tick_analog_with_provenance()

        all_patches = dict(bool_patches)
        for tag_name, value, _profile in analog_details:
            all_patches[tag_name] = value

        if all_patches:
            self._plc.patch(all_patches)

        if self.on_patches_applied is not None and all_patches:
            notifications: list[tuple[str, Any, str]] = [
                (n, v, "harness:nominal") for n, v in bool_patches.items()
            ]
            notifications.extend((n, v, f"harness:analog:{p}") for n, v, p in analog_details)
            self.on_patches_applied(notifications)

    def _tick_analog_with_provenance(self) -> list[tuple[str, Any, str]]:
        results: list[tuple[str, Any, str]] = []
        for coupling in self._profile_couplings:
            if not coupling.active:
                continue
            fn = _profile_registry.get(coupling.profile_name)
            if fn is None:
                continue
            state = self._plc.current_state
            cur = state.tags.get(coupling.fb_name, 0.0)
            en_raw = state.tags.get(coupling.en_name, False)
            if coupling.trigger_value is not None:
                en = en_raw == coupling.trigger_value
            else:
                en = bool(en_raw)
            dt = state.memory.get("_dt", self._plc._dt)
            results.append((coupling.fb_name, fn(cur, en, dt), coupling.profile_name))
        return results

    def _discover_couplings(self) -> None:
        seen_runtimes: set[int] = set()
        for tag in list(self._plc._known_tags_by_name.values()):
            runtime = getattr(tag, "_pyrung_structure_runtime", None)
            if runtime is None:
                self._try_add_flat_coupling(tag)
                continue
            rt_id = id(runtime)
            if rt_id in seen_runtimes:
                continue
            seen_runtimes.add(rt_id)
            self._discover_structure_couplings(runtime)

    def _try_add_flat_coupling(self, tag: Any) -> None:
        if tag.link is None or tag.physical is None:
            return
        en_name, trigger_raw = _parse_link_spec(tag.link)
        if en_name not in self._plc._known_tags_by_name:
            return
        trigger_value = None
        if trigger_raw is not None:
            en_tag = self._plc._known_tags_by_name[en_name]
            trigger_value = _resolve_trigger_value(trigger_raw, en_tag)
        self._add_coupling(en_name, tag.name, tag.physical, trigger_value=trigger_value)

    def _discover_structure_couplings(self, runtime: Any) -> None:
        field_specs = runtime._field_specs
        blocks = runtime._blocks
        count = getattr(runtime, "count", 1)
        for spec in field_specs.values():
            if spec.link is None or spec.physical is None:
                continue
            en_field_name, trigger_raw = _parse_link_spec(spec.link)
            en_block = blocks.get(en_field_name)
            fb_block = blocks.get(spec.name)
            if en_block is None or fb_block is None:
                continue
            for idx in range(1, count + 1):
                try:
                    en_tag = en_block[idx]
                    fb_tag = fb_block[idx]
                except (KeyError, IndexError):
                    continue
                self._plc._register_known_tag(en_tag)
                self._plc._register_known_tag(fb_tag)
                trigger_value = None
                if trigger_raw is not None:
                    trigger_value = _resolve_trigger_value(trigger_raw, en_tag)
                self._add_coupling(
                    en_tag.name, fb_tag.name, spec.physical, trigger_value=trigger_value
                )

    def _add_coupling(
        self,
        en_name: str,
        fb_name: str,
        physical: Any,
        *,
        trigger_value: int | str | None = None,
    ) -> None:
        if physical.feedback_type == "bool":
            on_ms = physical.on_delay_ms or 0
            off_ms = physical.off_delay_ms or 0
            self._bool_couplings.append(
                _BoolCoupling(
                    en_name, fb_name, on_ms, off_ms, physical, trigger_value=trigger_value
                )
            )
        elif physical.feedback_type == "analog" and physical.profile is not None:
            self._profile_couplings.append(
                _ProfileCoupling(
                    en_name, fb_name, physical.profile, physical, trigger_value=trigger_value
                )
            )

    def _install_monitors(self) -> None:
        en_to_bool: dict[str, list[_BoolCoupling]] = {}
        for coupling in self._bool_couplings:
            en_to_bool.setdefault(coupling.en_name, []).append(coupling)

        en_to_analog: dict[str, list[_ProfileCoupling]] = {}
        for coupling in self._profile_couplings:
            en_to_analog.setdefault(coupling.en_name, []).append(coupling)

        all_en_names = set(en_to_bool) | set(en_to_analog)
        for en_name in all_en_names:
            bool_couplings = en_to_bool.get(en_name, [])
            analog_couplings = en_to_analog.get(en_name, [])
            handle = self._plc.monitor(
                en_name,
                self._make_en_callback(bool_couplings, analog_couplings),
            )
            self._monitors.append(handle)

    def _make_en_callback(
        self,
        bool_couplings: list[_BoolCoupling],
        analog_couplings: list[_ProfileCoupling],
    ) -> Callable[[Any, Any], None]:
        dt_ms = self._plc._dt * 1000
        plain_bool = [c for c in bool_couplings if c.trigger_value is None]
        trigger_bool = [c for c in bool_couplings if c.trigger_value is not None]
        plain_analog = [c for c in analog_couplings if c.trigger_value is None]
        trigger_analog = [c for c in analog_couplings if c.trigger_value is not None]

        def on_en_change(current: Any, previous: Any) -> None:
            scan_id = self._plc.current_state.scan_id

            cur_bool = bool(current)
            prev_bool = bool(previous)
            if cur_bool != prev_bool:
                rising = cur_bool and not prev_bool
                for coupling in plain_bool:
                    delay_ms = coupling.on_delay_ms if rising else coupling.off_delay_ms
                    delay_scans = max(1, ceil(delay_ms / dt_ms))
                    target = scan_id + delay_scans
                    self._schedule(target, coupling.fb_name, rising)
                for coupling in plain_analog:
                    coupling.active = True

            for coupling in trigger_bool:
                was_match = previous == coupling.trigger_value
                is_match = current == coupling.trigger_value
                if was_match == is_match:
                    continue
                on_edge = is_match
                delay_ms = coupling.on_delay_ms if on_edge else coupling.off_delay_ms
                delay_scans = max(1, ceil(delay_ms / dt_ms))
                target = scan_id + delay_scans
                self._schedule(target, coupling.fb_name, on_edge)

            for coupling in trigger_analog:
                was_match = previous == coupling.trigger_value
                is_match = current == coupling.trigger_value
                if was_match == is_match:
                    continue
                coupling.active = True

        return on_en_change

    def _delay_scans(self, delay_ms: int) -> int:
        dt_ms = self._plc._dt * 1000
        return max(1, ceil(delay_ms / dt_ms))

    def coupling_summary(self) -> dict[str, Any]:
        return {
            "installed": self._installed,
            "bool_couplings": [
                {
                    "en": c.en_name,
                    "fb": c.fb_name,
                    "on_delay_ms": c.on_delay_ms,
                    "off_delay_ms": c.off_delay_ms,
                    "trigger_value": c.trigger_value,
                }
                for c in self._bool_couplings
            ],
            "profile_couplings": [
                {
                    "en": c.en_name,
                    "fb": c.fb_name,
                    "profile": c.profile_name,
                    "active": c.active,
                    "trigger_value": c.trigger_value,
                }
                for c in self._profile_couplings
            ],
            "pending_patches": len(self._heap),
        }
