"""Click-specific system point mappings to SC/SD addresses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


SYSTEM_CLICK_SLOTS = (
    SystemClickSlot(system.sys.always_on, _hardware_tag("SC1"), "_Always_ON", True),
    SystemClickSlot(system.sys.first_scan, _hardware_tag("SC2"), "_1st_SCAN", True),
    SystemClickSlot(system.sys.scan_clock_toggle, _hardware_tag("SC3"), "_SCAN_Clock", True),
    SystemClickSlot(system.sys.clock_10ms, _hardware_tag("SC4"), "_10ms_Clock", True),
    SystemClickSlot(system.sys.clock_100ms, _hardware_tag("SC5"), "_100ms_Clock", True),
    SystemClickSlot(system.sys.clock_500ms, _hardware_tag("SC6"), "_500ms_Clock", True),
    SystemClickSlot(system.sys.clock_1s, _hardware_tag("SC7"), "_1sec_Clock", True),
    SystemClickSlot(system.sys.clock_1m, _hardware_tag("SC8"), "_1min_Clock", True),
    SystemClickSlot(system.sys.clock_1h, _hardware_tag("SC9"), "_1hour_Clock", True),
    SystemClickSlot(system.sys.mode_switch_run, _hardware_tag("SC10"), "_Mode_Switch", True),
    SystemClickSlot(system.sys.mode_run, _hardware_tag("SC11"), "_PLC_Mode", True),
    SystemClickSlot(
        system.sys.cmd_mode_stop, _hardware_tag("SC50"), "_PLC_Mode_Change_to_STOP", False
    ),
    SystemClickSlot(
        system.sys.cmd_watchdog_reset, _hardware_tag("SC51"), "_Watchdog_Timer_Reset", False
    ),
    SystemClickSlot(system.sys.fixed_scan_mode, _hardware_tag("SC202"), "_Fixed_Scan_Mode", True),
    SystemClickSlot(system.sys.scan_counter, _hardware_tag("SD9"), "_Scan_Counter", True),
    SystemClickSlot(
        system.sys.scan_time_current_ms, _hardware_tag("SD10"), "_Current_Scan_Time", True
    ),
    SystemClickSlot(system.sys.scan_time_min_ms, _hardware_tag("SD11"), "_Minimum_Scan_Time", True),
    SystemClickSlot(system.sys.scan_time_max_ms, _hardware_tag("SD12"), "_Maximum_Scan_Time", True),
    SystemClickSlot(
        system.sys.scan_time_fixed_setup_ms,
        _hardware_tag("SD13"),
        "_Fixed_Scan_Time_Setup",
        True,
    ),
    SystemClickSlot(
        system.sys.interrupt_scan_time_ms, _hardware_tag("SD14"), "_Interrupt_Scan_Time", True
    ),
    SystemClickSlot(system.fault.plc_error, _hardware_tag("SC19"), "_PLC_Error", True),
    SystemClickSlot(system.fault.division_error, _hardware_tag("SC40"), "_Division_Error", True),
    SystemClickSlot(system.fault.out_of_range, _hardware_tag("SC43"), "_Out_of_Range", True),
    SystemClickSlot(system.fault.address_error, _hardware_tag("SC44"), "_Address_Error", True),
    SystemClickSlot(
        system.fault.math_operation_error, _hardware_tag("SC46"), "_Math_Operation_Error", True
    ),
    SystemClickSlot(system.fault.code, _hardware_tag("SD1"), "_PLC_Error_Code", True),
    SystemClickSlot(system.rtc.year4, _hardware_tag("SD19"), "_RTC_Year (4 digits)", True),
    SystemClickSlot(system.rtc.year2, _hardware_tag("SD20"), "_RTC_Year (2 digits)", True),
    SystemClickSlot(system.rtc.month, _hardware_tag("SD21"), "_RTC_Month", True),
    SystemClickSlot(system.rtc.day, _hardware_tag("SD22"), "_RTC_Day", True),
    SystemClickSlot(system.rtc.weekday, _hardware_tag("SD23"), "_RTC_Day_of_the_Week", True),
    SystemClickSlot(system.rtc.hour, _hardware_tag("SD24"), "_RTC_Hour", True),
    SystemClickSlot(system.rtc.minute, _hardware_tag("SD25"), "_RTC_Minute", True),
    SystemClickSlot(system.rtc.second, _hardware_tag("SD26"), "_RTC_Second", True),
    SystemClickSlot(system.rtc.new_year4, _hardware_tag("SD29"), "_RTC_New_Year(4 digits)", False),
    SystemClickSlot(system.rtc.new_month, _hardware_tag("SD31"), "_RTC_New_Month", False),
    SystemClickSlot(system.rtc.new_day, _hardware_tag("SD32"), "_RTC_New_Day", False),
    SystemClickSlot(system.rtc.new_hour, _hardware_tag("SD34"), "_RTC_New_Hour", False),
    SystemClickSlot(system.rtc.new_minute, _hardware_tag("SD35"), "_RTC_New_Minute", False),
    SystemClickSlot(system.rtc.new_second, _hardware_tag("SD36"), "_RTC_New_Second", False),
    SystemClickSlot(system.rtc.apply_date, _hardware_tag("SC53"), "_RTC_Date_Change", False),
    SystemClickSlot(
        system.rtc.apply_date_error, _hardware_tag("SC54"), "_RTC_Date_Change_Error", True
    ),
    SystemClickSlot(system.rtc.apply_time, _hardware_tag("SC55"), "_RTC_Time_Change", False),
    SystemClickSlot(
        system.rtc.apply_time_error, _hardware_tag("SC56"), "_RTC_Time_Change_Error", True
    ),
    SystemClickSlot(
        system.firmware.main_ver_low, _hardware_tag("SD5"), "_Firmware_Version_L", True
    ),
    SystemClickSlot(
        system.firmware.main_ver_high, _hardware_tag("SD6"), "_Firmware_Version_H", True
    ),
    SystemClickSlot(
        system.firmware.sub_ver_low, _hardware_tag("SD7"), "_Sub_Firmware_Version_L", True
    ),
    SystemClickSlot(
        system.firmware.sub_ver_high, _hardware_tag("SD8"), "_Sub_Firmware_Version_H", True
    ),
)
SYSTEM_TAG_NAMES_BY_HARDWARE = {
    slot.hardware.name: slot.logical.name for slot in SYSTEM_CLICK_SLOTS
}
