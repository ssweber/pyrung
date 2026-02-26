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


def _resolved(runner: PLCRunner, tag_name: str):
    found, value = runner.system_runtime.resolve(tag_name, runner.current_state)
    assert found is True
    return value


def test_system_namespace_shape_and_names():
    assert system.sys.first_scan.name == "sys.first_scan"
    assert system.sys.battery_present.name == "sys.battery_present"
    assert system.sys.scan_counter.name == "sys.scan_counter"
    assert system.fault.division_error.name == "fault.division_error"
    assert system.rtc.apply_time.name == "rtc.apply_time"
    assert system.firmware.main_ver_low.name == "firmware.main_ver_low"
    assert system.storage.sd.save_cmd.name == "storage.sd.save_cmd"
    assert system.storage.sd.eject_cmd.name == "storage.sd.eject_cmd"
    assert system.storage.sd.delete_all_cmd.name == "storage.sd.delete_all_cmd"
    assert system.storage.sd.ready.name == "storage.sd.ready"
    assert system.storage.sd.write_status.name == "storage.sd.write_status"
    assert system.storage.sd.error.name == "storage.sd.error"
    assert system.storage.sd.error_code.name == "storage.sd.error_code"


def test_storage_sd_status_defaults():
    runner = PLCRunner(logic=[])

    assert _resolved(runner, system.storage.sd.ready.name) is True
    assert _resolved(runner, system.storage.sd.write_status.name) is False
    assert _resolved(runner, system.storage.sd.error.name) is False
    assert _resolved(runner, system.storage.sd.error_code.name) == 0

    runner.step()
    assert _resolved(runner, system.storage.sd.ready.name) is True
    assert _resolved(runner, system.storage.sd.write_status.name) is False
    assert _resolved(runner, system.storage.sd.error.name) is False
    assert _resolved(runner, system.storage.sd.error_code.name) == 0


def test_battery_present_is_read_only_and_defaults_true():
    runner = PLCRunner(logic=[])

    assert _resolved(runner, system.sys.battery_present.name) is True

    with pytest.raises(ValueError, match="read-only system point"):
        runner.patch({system.sys.battery_present.name: False})


def test_set_battery_present_updates_resolved_value():
    runner = PLCRunner(logic=[])

    runner.set_battery_present(False)
    assert _resolved(runner, system.sys.battery_present.name) is False

    runner.set_battery_present(True)
    assert _resolved(runner, system.sys.battery_present.name) is True


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


def test_rtc_fields_derive_from_set_rtc_anchor():
    runner = PLCRunner(logic=[])
    runner.set_rtc(datetime(2026, 3, 5, 6, 7, 8))

    assert _resolved(runner, system.rtc.year4.name) == 2026
    assert _resolved(runner, system.rtc.year2.name) == 26
    assert _resolved(runner, system.rtc.month.name) == 3
    assert _resolved(runner, system.rtc.day.name) == 5
    assert _resolved(runner, system.rtc.hour.name) == 6
    assert _resolved(runner, system.rtc.minute.name) == 7
    assert _resolved(runner, system.rtc.second.name) == 8


def test_rtc_fixed_step_advances_deterministically_with_simulation_time():
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.001)
    base = datetime(2026, 3, 5, 6, 7, 8)
    runner.set_rtc(base)

    runner.run(cycles=1000)

    rtc_now = runner.system_runtime._rtc_now(runner.current_state)
    assert (rtc_now - base).total_seconds() == pytest.approx(1.0)


@pytest.mark.parametrize("mode", [TimeMode.FIXED_STEP, TimeMode.REALTIME])
def test_rtc_apply_date_updates_current_date_in_all_time_modes(mode: TimeMode):
    runner = PLCRunner(logic=[])
    runner.set_time_mode(mode, dt=0.1)
    runner.set_rtc(datetime(2026, 1, 15, 10, 20, 30))

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


@pytest.mark.parametrize("mode", [TimeMode.FIXED_STEP, TimeMode.REALTIME])
def test_rtc_apply_time_updates_current_time_in_all_time_modes(mode: TimeMode):
    runner = PLCRunner(logic=[])
    runner.set_time_mode(mode, dt=0.1)
    runner.set_rtc(datetime(2026, 1, 15, 10, 20, 30))

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


def test_rtc_invalid_date_sets_error_status_for_single_scan():
    runner = PLCRunner(logic=[])
    runner.set_rtc(datetime(2026, 1, 15, 10, 20, 30))
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


def test_rtc_shift_changeover_example_at_fixed_step():
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    runner.set_rtc(datetime(2026, 3, 5, 6, 59, 50))

    runner.run(cycles=100)

    assert _resolved(runner, system.rtc.hour.name) == 7
    assert _resolved(runner, system.rtc.minute.name) == 0
    assert _resolved(runner, system.rtc.second.name) == 0


def test_rtc_values_are_not_stored_in_state_memory():
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    runner.set_rtc(datetime(2026, 1, 15, 10, 20, 30))
    runner.patch(
        {
            system.rtc.new_hour.name: 8,
            system.rtc.new_minute.name: 0,
            system.rtc.new_second.name: 0,
            system.rtc.apply_time.name: True,
        }
    )
    runner.step()
    runner.step()

    assert "_sys.rtc.offset" not in runner.current_state.memory


def test_read_only_system_points_reject_logic_and_patch_writes():
    with Program() as program:
        with Rung():
            out(system.sys.always_on)
    runner = PLCRunner(logic=program)

    with pytest.raises(ValueError, match="read-only system point"):
        runner.step()

    with pytest.raises(ValueError, match="read-only system point"):
        runner.patch({system.sys.always_on.name: False})


def test_storage_sd_read_only_status_points_reject_logic_patch_and_force():
    with Program() as program:
        with Rung():
            out(system.storage.sd.ready)
    runner = PLCRunner(logic=program)

    with pytest.raises(ValueError, match="read-only system point"):
        runner.step()

    with pytest.raises(ValueError, match="read-only system point"):
        runner.patch({system.storage.sd.ready.name: False})

    with pytest.raises(ValueError, match="read-only system point"):
        runner.patch({system.storage.sd.write_status.name: True})

    with pytest.raises(ValueError, match="read-only system point"):
        runner.patch({system.storage.sd.error.name: True})

    with pytest.raises(ValueError, match="read-only system point"):
        runner.patch({system.storage.sd.error_code.name: 1})

    with pytest.raises(ValueError, match="read-only system point"):
        runner.add_force(system.storage.sd.error, True)


def test_storage_sd_commands_auto_clear_and_pulse_write_status():
    runner = PLCRunner(logic=[])
    runner.patch(
        {
            system.storage.sd.save_cmd.name: True,
            system.storage.sd.eject_cmd.name: True,
            system.storage.sd.delete_all_cmd.name: True,
        }
    )

    runner.step()
    assert runner.current_state.tags[system.storage.sd.save_cmd.name] is True
    assert runner.current_state.tags[system.storage.sd.eject_cmd.name] is True
    assert runner.current_state.tags[system.storage.sd.delete_all_cmd.name] is True
    assert _resolved(runner, system.storage.sd.write_status.name) is False

    runner.step()
    assert runner.current_state.tags[system.storage.sd.save_cmd.name] is False
    assert runner.current_state.tags[system.storage.sd.eject_cmd.name] is False
    assert runner.current_state.tags[system.storage.sd.delete_all_cmd.name] is False
    assert _resolved(runner, system.storage.sd.write_status.name) is True

    runner.step()
    assert _resolved(runner, system.storage.sd.write_status.name) is False


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
