"""Tests for core system points runtime and namespaces."""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    PLCRunner,
    Program,
    Rung,
    TagType,
    TimeMode,
    as_binary,
    as_value,
    calc,
    copy,
    out,
    system,
)


class _FrozenDateTime(datetime):
    fixed_now = datetime(2026, 1, 15, 10, 20, 30)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls.fixed_now


def _resolved(runner: PLCRunner, tag_name: str):
    found, value = runner.system_runtime.resolve(tag_name, runner.current_state)
    assert found is True
    return value


def test_system_namespace_shape_and_names():
    assert system.sys.first_scan.name == "sys.first_scan"
    assert system.sys.scan_counter.name == "sys.scan_counter"
    assert system.fault.division_error.name == "fault.division_error"
    assert system.rtc.apply_time.name == "rtc.apply_time"
    assert system.firmware.main_ver_low.name == "firmware.main_ver_low"


def test_derived_points_always_on_first_scan_scan_clock_and_fixed_mode():
    first_scan_latched = Bool("FirstScanLatched")
    clock_value = Bool("ScanClockValue")
    fixed_mode_value = Bool("FixedModeValue")

    with Program() as program:
        with Rung(system.sys.first_scan):
            out(first_scan_latched)
        with Rung():
            copy(system.sys.scan_clock_toggle, clock_value)
        with Rung():
            copy(system.sys.fixed_scan_mode, fixed_mode_value)

    runner = PLCRunner(logic=program)

    runner.step()
    assert runner.current_state.tags["FirstScanLatched"] is True
    assert runner.current_state.tags["ScanClockValue"] is False
    assert runner.current_state.tags["FixedModeValue"] is True

    runner.step()
    assert runner.current_state.tags["FirstScanLatched"] is False
    assert runner.current_state.tags["ScanClockValue"] is True


def test_scan_counter_and_scan_min_max_stats_update():
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    runner.step()
    assert runner.current_state.tags["sys.scan_counter"] == 1
    assert runner.current_state.tags["sys.scan_time_min_ms"] == 100
    assert runner.current_state.tags["sys.scan_time_max_ms"] == 100

    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.25)
    runner.step()
    assert runner.current_state.tags["sys.scan_counter"] == 2
    assert runner.current_state.tags["sys.scan_time_min_ms"] == 100
    assert runner.current_state.tags["sys.scan_time_max_ms"] == 250
    assert _resolved(runner, system.sys.scan_time_current_ms.name) == 250


def test_rtc_wall_clock_derived_fields_follow_datetime_now(monkeypatch):
    _FrozenDateTime.fixed_now = datetime(2026, 3, 5, 6, 7, 8)
    monkeypatch.setattr("pyrung.core.system_points.datetime", _FrozenDateTime)

    runner = PLCRunner(logic=[])
    runner.step()

    assert _resolved(runner, system.rtc.year4.name) == 2026
    assert _resolved(runner, system.rtc.year2.name) == 26
    assert _resolved(runner, system.rtc.month.name) == 3
    assert _resolved(runner, system.rtc.day.name) == 5
    assert _resolved(runner, system.rtc.hour.name) == 6
    assert _resolved(runner, system.rtc.minute.name) == 7
    assert _resolved(runner, system.rtc.second.name) == 8


def test_rtc_apply_date_updates_offset_and_current_date(monkeypatch):
    _FrozenDateTime.fixed_now = datetime(2026, 1, 15, 10, 20, 30)
    monkeypatch.setattr("pyrung.core.system_points.datetime", _FrozenDateTime)

    runner = PLCRunner(logic=[])
    runner.patch(
        {
            system.rtc.new_year4.name: 2030,
            system.rtc.new_month.name: 4,
            system.rtc.new_day.name: 10,
            system.rtc.apply_date.name: True,
        }
    )
    runner.step()
    runner.step()

    assert _resolved(runner, system.rtc.year4.name) == 2030
    assert _resolved(runner, system.rtc.month.name) == 4
    assert _resolved(runner, system.rtc.day.name) == 10
    assert runner.current_state.tags[system.rtc.apply_date.name] is False
    assert runner.current_state.tags[system.rtc.apply_date_error.name] is False


def test_rtc_apply_time_updates_offset_and_current_time(monkeypatch):
    _FrozenDateTime.fixed_now = datetime(2026, 1, 15, 10, 20, 30)
    monkeypatch.setattr("pyrung.core.system_points.datetime", _FrozenDateTime)

    runner = PLCRunner(logic=[])
    runner.patch(
        {
            system.rtc.new_hour.name: 23,
            system.rtc.new_minute.name: 59,
            system.rtc.new_second.name: 58,
            system.rtc.apply_time.name: True,
        }
    )
    runner.step()
    runner.step()

    assert _resolved(runner, system.rtc.hour.name) == 23
    assert _resolved(runner, system.rtc.minute.name) == 59
    assert _resolved(runner, system.rtc.second.name) == 58
    assert runner.current_state.tags[system.rtc.apply_time.name] is False
    assert runner.current_state.tags[system.rtc.apply_time_error.name] is False


def test_rtc_invalid_date_sets_error_status_for_single_scan(monkeypatch):
    _FrozenDateTime.fixed_now = datetime(2026, 1, 15, 10, 20, 30)
    monkeypatch.setattr("pyrung.core.system_points.datetime", _FrozenDateTime)

    runner = PLCRunner(logic=[])
    runner.patch(
        {
            system.rtc.new_year4.name: 2030,
            system.rtc.new_month.name: 13,
            system.rtc.new_day.name: 10,
            system.rtc.apply_date.name: True,
        }
    )
    runner.step()
    runner.step()
    assert runner.current_state.tags[system.rtc.apply_date_error.name] is True
    assert runner.current_state.tags[system.rtc.apply_date.name] is False

    runner.step()
    assert runner.current_state.tags[system.rtc.apply_date_error.name] is False


def test_read_only_system_points_reject_logic_and_patch_writes():
    with Program() as program:
        with Rung():
            out(system.sys.always_on)
    runner = PLCRunner(logic=program)

    with pytest.raises(ValueError, match="read-only system point"):
        runner.step()

    with pytest.raises(ValueError, match="read-only system point"):
        runner.patch({system.sys.always_on.name: False})


def test_fault_division_error_auto_clears_next_scan_when_not_retriggered():
    A = Int("A")
    B = Int("B")
    Result = Int("Result")
    Enable = Bool("Enable")

    with Program() as program:
        with Rung(Enable):
            calc(A / B, Result, oneshot=True)

    runner = PLCRunner(logic=program)
    runner.patch({"Enable": True, "A": 100, "B": 0})
    runner.step()
    assert runner.current_state.tags["Result"] == 0
    assert runner.current_state.tags[system.fault.division_error.name] is True

    runner.step()
    assert runner.current_state.tags[system.fault.division_error.name] is False


def test_fault_out_of_range_from_math_auto_clears_next_scan_when_not_retriggered():
    A = Int("A")
    B = Int("B")
    Result = Int("Result")
    Enable = Bool("Enable")

    with Program() as program:
        with Rung(Enable):
            calc(A + B, Result, oneshot=True)

    runner = PLCRunner(logic=program)
    runner.patch({"Enable": True, "A": 30000, "B": 30000})
    runner.step()
    assert runner.current_state.tags["Result"] == -5536
    assert runner.current_state.tags[system.fault.out_of_range.name] is True

    runner.step()
    assert runner.current_state.tags[system.fault.out_of_range.name] is False


def test_fault_out_of_range_auto_clears_next_scan_when_not_retriggered():
    CH = Block("CH", TagType.CHAR, 1, 10)
    Dest = Int("Dest")
    Enable = Bool("Enable")

    with Program() as program:
        with Rung(Enable):
            copy(as_value(CH[1]), Dest, oneshot=True)

    runner = PLCRunner(logic=program)
    runner.patch({"Enable": True, "CH1": "A"})
    runner.step()
    assert runner.current_state.tags[system.fault.out_of_range.name] is True

    runner.step()
    assert runner.current_state.tags[system.fault.out_of_range.name] is False


def test_fault_address_error_auto_clears_next_scan_when_not_retriggered():
    DS = Block("DS", TagType.INT, 1, 10)
    CH = Block("CH", TagType.CHAR, 1, 10)
    Pointer = Int("Pointer")
    Enable = Bool("Enable")

    with Program() as program:
        with Rung(Enable):
            copy(as_binary(DS[Pointer]), CH[1], oneshot=True)

    runner = PLCRunner(logic=program)
    runner.patch({"Enable": True, "Pointer": 999})
    runner.step()
    assert runner.current_state.tags[system.fault.address_error.name] is True

    runner.step()
    assert runner.current_state.tags[system.fault.address_error.name] is False
