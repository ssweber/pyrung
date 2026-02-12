"""Core system points and runtime behavior."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Bool, Int, Tag

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.state import SystemState
    from pyrung.core.time_mode import TimeMode


@dataclass(frozen=True)
class SysNamespace:
    always_on: Tag
    first_scan: Tag
    scan_clock_toggle: Tag
    clock_10ms: Tag
    clock_100ms: Tag
    clock_500ms: Tag
    clock_1s: Tag
    clock_1m: Tag
    clock_1h: Tag
    mode_switch_run: Tag
    mode_run: Tag
    cmd_mode_stop: Tag
    cmd_watchdog_reset: Tag
    fixed_scan_mode: Tag
    scan_counter: Tag
    scan_time_current_ms: Tag
    scan_time_min_ms: Tag
    scan_time_max_ms: Tag
    scan_time_fixed_setup_ms: Tag
    interrupt_scan_time_ms: Tag


@dataclass(frozen=True)
class RtcNamespace:
    year4: Tag
    year2: Tag
    month: Tag
    day: Tag
    weekday: Tag
    hour: Tag
    minute: Tag
    second: Tag
    new_year4: Tag
    new_month: Tag
    new_day: Tag
    new_hour: Tag
    new_minute: Tag
    new_second: Tag
    apply_date: Tag
    apply_date_error: Tag
    apply_time: Tag
    apply_time_error: Tag


@dataclass(frozen=True)
class FaultNamespace:
    plc_error: Tag
    division_error: Tag
    out_of_range: Tag
    address_error: Tag
    math_operation_error: Tag
    code: Tag


@dataclass(frozen=True)
class FirmwareNamespace:
    main_ver_low: Tag
    main_ver_high: Tag
    sub_ver_low: Tag
    sub_ver_high: Tag


@dataclass(frozen=True)
class SystemNamespaces:
    sys: SysNamespace
    rtc: RtcNamespace
    fault: FaultNamespace
    firmware: FirmwareNamespace


def _iter_namespace_tags(namespace: Any) -> tuple[Tag, ...]:
    return tuple(value for value in vars(namespace).values() if isinstance(value, Tag))


system = SystemNamespaces(
    sys=SysNamespace(
        always_on=Bool("sys.always_on"),
        first_scan=Bool("sys.first_scan"),
        scan_clock_toggle=Bool("sys.scan_clock_toggle"),
        clock_10ms=Bool("sys.clock_10ms"),
        clock_100ms=Bool("sys.clock_100ms"),
        clock_500ms=Bool("sys.clock_500ms"),
        clock_1s=Bool("sys.clock_1s"),
        clock_1m=Bool("sys.clock_1m"),
        clock_1h=Bool("sys.clock_1h"),
        mode_switch_run=Bool("sys.mode_switch_run"),
        mode_run=Bool("sys.mode_run"),
        cmd_mode_stop=Bool("sys.cmd_mode_stop"),
        cmd_watchdog_reset=Bool("sys.cmd_watchdog_reset"),
        fixed_scan_mode=Bool("sys.fixed_scan_mode"),
        scan_counter=Int("sys.scan_counter", retentive=False),
        scan_time_current_ms=Int("sys.scan_time_current_ms", retentive=False),
        scan_time_min_ms=Int("sys.scan_time_min_ms", retentive=False),
        scan_time_max_ms=Int("sys.scan_time_max_ms", retentive=False),
        scan_time_fixed_setup_ms=Int("sys.scan_time_fixed_setup_ms", retentive=False),
        interrupt_scan_time_ms=Int("sys.interrupt_scan_time_ms", retentive=False),
    ),
    rtc=RtcNamespace(
        year4=Int("rtc.year4", retentive=False),
        year2=Int("rtc.year2", retentive=False),
        month=Int("rtc.month", retentive=False),
        day=Int("rtc.day", retentive=False),
        weekday=Int("rtc.weekday", retentive=False),
        hour=Int("rtc.hour", retentive=False),
        minute=Int("rtc.minute", retentive=False),
        second=Int("rtc.second", retentive=False),
        new_year4=Int("rtc.new_year4", retentive=False),
        new_month=Int("rtc.new_month", retentive=False),
        new_day=Int("rtc.new_day", retentive=False),
        new_hour=Int("rtc.new_hour", retentive=False),
        new_minute=Int("rtc.new_minute", retentive=False),
        new_second=Int("rtc.new_second", retentive=False),
        apply_date=Bool("rtc.apply_date"),
        apply_date_error=Bool("rtc.apply_date_error"),
        apply_time=Bool("rtc.apply_time"),
        apply_time_error=Bool("rtc.apply_time_error"),
    ),
    fault=FaultNamespace(
        plc_error=Bool("fault.plc_error"),
        division_error=Bool("fault.division_error"),
        out_of_range=Bool("fault.out_of_range"),
        address_error=Bool("fault.address_error"),
        math_operation_error=Bool("fault.math_operation_error"),
        code=Int("fault.code", retentive=False),
    ),
    firmware=FirmwareNamespace(
        main_ver_low=Int("firmware.main_ver_low", retentive=False),
        main_ver_high=Int("firmware.main_ver_high", retentive=False),
        sub_ver_low=Int("firmware.sub_ver_low", retentive=False),
        sub_ver_high=Int("firmware.sub_ver_high", retentive=False),
    ),
)

_ALL_SYSTEM_TAGS = (
    *_iter_namespace_tags(system.sys),
    *_iter_namespace_tags(system.rtc),
    *_iter_namespace_tags(system.fault),
    *_iter_namespace_tags(system.firmware),
)
SYSTEM_TAGS_BY_NAME = {tag.name: tag for tag in _ALL_SYSTEM_TAGS}

WRITABLE_SYSTEM_TAG_NAMES = frozenset(
    {
        system.rtc.new_year4.name,
        system.rtc.new_month.name,
        system.rtc.new_day.name,
        system.rtc.new_hour.name,
        system.rtc.new_minute.name,
        system.rtc.new_second.name,
        system.rtc.apply_date.name,
        system.rtc.apply_time.name,
        system.sys.cmd_mode_stop.name,
        system.sys.cmd_watchdog_reset.name,
    }
)
READ_ONLY_SYSTEM_TAG_NAMES = frozenset(
    name for name in SYSTEM_TAGS_BY_NAME if name not in WRITABLE_SYSTEM_TAG_NAMES
)

_DERIVED_TAG_NAMES = frozenset(
    {
        system.sys.always_on.name,
        system.sys.first_scan.name,
        system.sys.scan_clock_toggle.name,
        system.sys.clock_10ms.name,
        system.sys.clock_100ms.name,
        system.sys.clock_500ms.name,
        system.sys.clock_1s.name,
        system.sys.clock_1m.name,
        system.sys.clock_1h.name,
        system.sys.mode_switch_run.name,
        system.sys.mode_run.name,
        system.sys.fixed_scan_mode.name,
        system.sys.scan_time_current_ms.name,
        system.sys.scan_time_fixed_setup_ms.name,
        system.sys.interrupt_scan_time_ms.name,
        system.rtc.year4.name,
        system.rtc.year2.name,
        system.rtc.month.name,
        system.rtc.day.name,
        system.rtc.weekday.name,
        system.rtc.hour.name,
        system.rtc.minute.name,
        system.rtc.second.name,
        system.firmware.main_ver_low.name,
        system.firmware.main_ver_high.name,
        system.firmware.sub_ver_low.name,
        system.firmware.sub_ver_high.name,
    }
)

_CLOCK_HALF_PERIODS = {
    system.sys.clock_10ms.name: 0.005,
    system.sys.clock_100ms.name: 0.050,
    system.sys.clock_500ms.name: 0.250,
    system.sys.clock_1s.name: 0.500,
    system.sys.clock_1m.name: 30.0,
    system.sys.clock_1h.name: 1800.0,
}
_RTC_OFFSET_KEY = "_sys.rtc.offset"
_MODE_RUN_KEY = "_sys.mode.run"


def _click_weekday(value: datetime) -> int:
    # Click returns Sunday=1 ... Saturday=7.
    return ((value.weekday() + 1) % 7) + 1


def _raw_get_tag(ctx_or_state: ScanContext | SystemState, name: str, default: Any) -> Any:
    getter = getattr(ctx_or_state, "_get_tag_internal", None)
    if callable(getter):
        return getter(name, default)
    return ctx_or_state.tags.get(name, default)


def _raw_has_tag(ctx_or_state: ScanContext | SystemState, name: str) -> bool:
    checker = getattr(ctx_or_state, "_has_tag_internal", None)
    if callable(checker):
        return checker(name)
    return name in ctx_or_state.tags


def _raw_get_memory(ctx_or_state: ScanContext | SystemState, key: str, default: Any) -> Any:
    getter = getattr(ctx_or_state, "_get_memory_internal", None)
    if callable(getter):
        return getter(key, default)
    return ctx_or_state.memory.get(key, default)


def _raw_has_memory(ctx_or_state: ScanContext | SystemState, key: str) -> bool:
    checker = getattr(ctx_or_state, "_has_memory_internal", None)
    if callable(checker):
        return checker(key)
    return key in ctx_or_state.memory


class SystemPointRuntime:
    """Runtime resolver and lifecycle hooks for core system points."""

    def __init__(
        self,
        *,
        time_mode_getter: Callable[[], TimeMode],
        fixed_step_dt_getter: Callable[[], float],
    ) -> None:
        self._time_mode_getter = time_mode_getter
        self._fixed_step_dt_getter = fixed_step_dt_getter

    @property
    def read_only_tags(self) -> frozenset[str]:
        return READ_ONLY_SYSTEM_TAG_NAMES

    def is_system_tag(self, name: str) -> bool:
        return name in SYSTEM_TAGS_BY_NAME

    def is_read_only(self, name: str) -> bool:
        return name in READ_ONLY_SYSTEM_TAG_NAMES

    def resolve(self, name: str, ctx_or_state: ScanContext | SystemState) -> tuple[bool, Any]:
        if name not in SYSTEM_TAGS_BY_NAME:
            return False, None

        if name not in _DERIVED_TAG_NAMES:
            tag = SYSTEM_TAGS_BY_NAME[name]
            return True, _raw_get_tag(ctx_or_state, tag.name, tag.default)

        if name == system.sys.always_on.name:
            return True, True
        if name == system.sys.first_scan.name:
            return True, ctx_or_state.scan_id == 0
        if name == system.sys.scan_clock_toggle.name:
            counter = int(_raw_get_tag(ctx_or_state, system.sys.scan_counter.name, 0))
            return True, (counter % 2) == 1

        half_period = _CLOCK_HALF_PERIODS.get(name)
        if half_period is not None:
            phase = int(ctx_or_state.timestamp / half_period)
            return True, (phase % 2) == 1

        if name == system.sys.mode_switch_run.name or name == system.sys.mode_run.name:
            return True, bool(_raw_get_memory(ctx_or_state, _MODE_RUN_KEY, True))
        if name == system.sys.fixed_scan_mode.name:
            from pyrung.core.time_mode import TimeMode

            return True, self._time_mode_getter() == TimeMode.FIXED_STEP
        if name == system.sys.scan_time_current_ms.name:
            return True, self._scan_time_current_ms(ctx_or_state)
        if name == system.sys.scan_time_fixed_setup_ms.name:
            from pyrung.core.time_mode import TimeMode

            if self._time_mode_getter() == TimeMode.FIXED_STEP:
                return True, int(round(self._fixed_step_dt_getter() * 1000))
            return True, 0
        if name == system.sys.interrupt_scan_time_ms.name:
            return True, 0

        rtc_now = self._rtc_now(ctx_or_state)
        if name == system.rtc.year4.name:
            return True, rtc_now.year
        if name == system.rtc.year2.name:
            return True, rtc_now.year % 100
        if name == system.rtc.month.name:
            return True, rtc_now.month
        if name == system.rtc.day.name:
            return True, rtc_now.day
        if name == system.rtc.weekday.name:
            return True, _click_weekday(rtc_now)
        if name == system.rtc.hour.name:
            return True, rtc_now.hour
        if name == system.rtc.minute.name:
            return True, rtc_now.minute
        if name == system.rtc.second.name:
            return True, rtc_now.second

        return True, 0

    def on_scan_start(self, ctx: ScanContext) -> None:
        self._ensure_memory_defaults(ctx)
        self._clear_transient_status(ctx)
        self._process_rtc_apply(ctx)
        self._process_mode_commands(ctx)

    def on_scan_end(self, ctx: ScanContext) -> None:
        current_ms = self._scan_time_current_ms(ctx)
        next_counter = int(_raw_get_tag(ctx, system.sys.scan_counter.name, 0)) + 1
        ctx._set_tag_internal(system.sys.scan_counter.name, next_counter)

        min_name = system.sys.scan_time_min_ms.name
        max_name = system.sys.scan_time_max_ms.name
        min_value = (
            current_ms
            if not _raw_has_tag(ctx, min_name)
            else min(int(_raw_get_tag(ctx, min_name, current_ms)), current_ms)
        )
        max_value = (
            current_ms
            if not _raw_has_tag(ctx, max_name)
            else max(int(_raw_get_tag(ctx, max_name, current_ms)), current_ms)
        )
        ctx._set_tags_internal(
            {
                min_name: min_value,
                max_name: max_value,
            }
        )

    def _ensure_memory_defaults(self, ctx: ScanContext) -> None:
        if not _raw_has_memory(ctx, _RTC_OFFSET_KEY):
            ctx.set_memory(_RTC_OFFSET_KEY, timedelta())
        if not _raw_has_memory(ctx, _MODE_RUN_KEY):
            ctx.set_memory(_MODE_RUN_KEY, True)

    def _clear_transient_status(self, ctx: ScanContext) -> None:
        ctx._set_tags_internal(
            {
                system.fault.division_error.name: False,
                system.fault.out_of_range.name: False,
                system.fault.address_error.name: False,
                system.rtc.apply_date_error.name: False,
                system.rtc.apply_time_error.name: False,
            }
        )

    def _process_rtc_apply(self, ctx: ScanContext) -> None:
        if bool(_raw_get_tag(ctx, system.rtc.apply_date.name, False)):
            self._apply_rtc_date(ctx)
        if bool(_raw_get_tag(ctx, system.rtc.apply_time.name, False)):
            self._apply_rtc_time(ctx)

        ctx._set_tags_internal(
            {
                system.rtc.apply_date.name: False,
                system.rtc.apply_time.name: False,
            }
        )

    def _apply_rtc_date(self, ctx: ScanContext) -> None:
        now = datetime.now()
        rtc_now = self._rtc_now(ctx)
        try:
            target = datetime(
                int(_raw_get_tag(ctx, system.rtc.new_year4.name, 0)),
                int(_raw_get_tag(ctx, system.rtc.new_month.name, 1)),
                int(_raw_get_tag(ctx, system.rtc.new_day.name, 1)),
                rtc_now.hour,
                rtc_now.minute,
                rtc_now.second,
                rtc_now.microsecond,
            )
        except ValueError:
            ctx._set_tag_internal(system.rtc.apply_date_error.name, True)
            return

        ctx.set_memory(_RTC_OFFSET_KEY, target - now)

    def _apply_rtc_time(self, ctx: ScanContext) -> None:
        now = datetime.now()
        rtc_now = self._rtc_now(ctx)
        try:
            target = datetime(
                rtc_now.year,
                rtc_now.month,
                rtc_now.day,
                int(_raw_get_tag(ctx, system.rtc.new_hour.name, 0)),
                int(_raw_get_tag(ctx, system.rtc.new_minute.name, 0)),
                int(_raw_get_tag(ctx, system.rtc.new_second.name, 0)),
                rtc_now.microsecond,
            )
        except ValueError:
            ctx._set_tag_internal(system.rtc.apply_time_error.name, True)
            return

        ctx.set_memory(_RTC_OFFSET_KEY, target - now)

    def _process_mode_commands(self, ctx: ScanContext) -> None:
        mode_run = bool(_raw_get_memory(ctx, _MODE_RUN_KEY, True))
        if bool(_raw_get_tag(ctx, system.fault.math_operation_error.name, False)):
            mode_run = False
        if bool(_raw_get_tag(ctx, system.sys.cmd_mode_stop.name, False)):
            mode_run = False

        ctx.set_memory(_MODE_RUN_KEY, mode_run)
        ctx._set_tags_internal(
            {
                system.sys.cmd_mode_stop.name: False,
                system.sys.cmd_watchdog_reset.name: False,
            }
        )

    def _scan_time_current_ms(self, ctx_or_state: ScanContext | SystemState) -> int:
        dt = float(_raw_get_memory(ctx_or_state, "_dt", self._fixed_step_dt_getter()))
        return int(round(dt * 1000))

    def _rtc_now(self, ctx_or_state: ScanContext | SystemState) -> datetime:
        offset = _raw_get_memory(ctx_or_state, _RTC_OFFSET_KEY, timedelta())
        if not isinstance(offset, timedelta):
            offset = timedelta()
        return datetime.now() + offset
