"""Click-specific system point mappings to SC/SD addresses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pyclickplc.system import AUTOMATIONDIRECT_SYSTEM_NICKNAMES

from pyrung.core.system_points import system
from pyrung.core.tag import Tag, TagType


@dataclass(frozen=True)
class SystemClickSlot:
    logical: Tag
    hardware: Tag
    click_nickname: str
    read_only: bool
    source: Literal["system"] = "system"


def _hardware_tag(address: str) -> Tag:
    if address.startswith("SC"):
        return Tag(address, TagType.BOOL, default=False)
    if address.startswith("SD"):
        return Tag(address, TagType.INT, default=0)
    raise ValueError(f"Unsupported system hardware address: {address!r}")


def _slot(
    logical: Tag, address: str, read_only: bool, *, nickname: str | None = None
) -> SystemClickSlot:
    """Build a slot whose nickname is pyclickplc's catalog entry for the address.

    ``nickname`` overrides the catalog where CLICK writes a shorter name than the catalog's
    help-file label (the catalog says ``_RTC_Month (1-12)``, CLICK writes ``_RTC_Month``).
    """
    if nickname is None:
        nickname = AUTOMATIONDIRECT_SYSTEM_NICKNAMES[(address[:2], int(address[2:]))].nickname
    return SystemClickSlot(logical, _hardware_tag(address), nickname, read_only)


SYSTEM_CLICK_SLOTS = (
    _slot(system.sys.always_on, "SC1", True),
    _slot(system.sys.first_scan, "SC2", True),
    _slot(system.sys.scan_clock_toggle, "SC3", True),
    _slot(system.sys.clock_10ms, "SC4", True),
    _slot(system.sys.clock_100ms, "SC5", True),
    _slot(system.sys.clock_500ms, "SC6", True),
    _slot(system.sys.clock_1s, "SC7", True),
    _slot(system.sys.clock_1m, "SC8", True),
    _slot(system.sys.clock_1h, "SC9", True),
    _slot(system.sys.mode_switch_run, "SC10", True),
    _slot(system.sys.mode_run, "SC11", True),
    _slot(system.sys.cmd_mode_stop, "SC50", False),
    _slot(system.sys.cmd_watchdog_reset, "SC51", False),
    _slot(system.sys.fixed_scan_mode, "SC202", True),
    _slot(system.sys.battery_present, "SC203", True),
    _slot(system.sys.scan_counter, "SD9", True),
    _slot(system.sys.scan_time_current_ms, "SD10", True),
    _slot(system.sys.scan_time_min_ms, "SD11", True),
    _slot(system.sys.scan_time_max_ms, "SD12", True),
    _slot(system.sys.scan_time_fixed_setup_ms, "SD13", True),
    _slot(system.sys.interrupt_scan_time_ms, "SD14", True),
    _slot(system.fault.plc_error, "SC19", True),
    _slot(system.fault.division_error, "SC40", True),
    _slot(system.fault.out_of_range, "SC43", True),
    _slot(system.fault.address_error, "SC44", True),
    _slot(system.fault.math_operation_error, "SC46", True),
    _slot(system.fault.code, "SD1", True),
    _slot(system.rtc.year4, "SD19", True),
    _slot(system.rtc.year2, "SD20", True),
    _slot(system.rtc.month, "SD21", True, nickname="_RTC_Month"),
    _slot(system.rtc.day, "SD22", True, nickname="_RTC_Day"),
    _slot(system.rtc.weekday, "SD23", True),
    _slot(system.rtc.hour, "SD24", True, nickname="_RTC_Hour"),
    _slot(system.rtc.minute, "SD25", True, nickname="_RTC_Minute"),
    _slot(system.rtc.second, "SD26", True, nickname="_RTC_Second"),
    _slot(system.rtc.new_year4, "SD29", False),
    _slot(system.rtc.new_month, "SD31", False),
    _slot(system.rtc.new_day, "SD32", False),
    _slot(system.rtc.new_hour, "SD34", False),
    _slot(system.rtc.new_minute, "SD35", False),
    _slot(system.rtc.new_second, "SD36", False),
    _slot(system.rtc.apply_date, "SC53", False),
    _slot(system.rtc.apply_date_error, "SC54", True),
    _slot(system.rtc.apply_time, "SC55", False),
    _slot(system.rtc.apply_time_error, "SC56", True),
    _slot(system.storage.sd.eject_cmd, "SC65", False),
    _slot(system.storage.sd.delete_all_cmd, "SC66", False),
    _slot(system.storage.sd.ready, "SC68", True),
    _slot(system.storage.sd.write_status, "SC69", True),
    _slot(system.storage.sd.error, "SC70", True),
    _slot(system.storage.sd.error_code, "SD69", True),
    _slot(system.firmware.main_ver_low, "SD5", True),
    _slot(system.firmware.main_ver_high, "SD6", True),
    _slot(system.firmware.sub_ver_low, "SD7", True),
    _slot(system.firmware.sub_ver_high, "SD8", True),
)
SYSTEM_TAG_NAMES_BY_HARDWARE = {
    slot.hardware.name: slot.logical.name for slot in SYSTEM_CLICK_SLOTS
}

# Hardware operand → Python expression for codegen (e.g. "SC5" → "system.sys.clock_100ms")
SYSTEM_OPERAND_PATHS: dict[str, str] = {
    slot.hardware.name: f"system.{slot.logical.name}" for slot in SYSTEM_CLICK_SLOTS
}
