"""Runner reboot and battery-aware retention behavior."""

from __future__ import annotations

from pyrung.core import Block, Bool, Int, PLCRunner, Program, Rung, TagType, copy, system


def _resolved(runner: PLCRunner, tag_name: str):
    found, value = runner.system_runtime.resolve(tag_name, runner.current_state)
    assert found is True
    return value


def test_reboot_with_battery_preserves_all_known_tags():
    retentive_tag = Int("RebootRet")
    non_retentive_tag = Bool("RebootNonRet", retentive=False)

    runner = PLCRunner(logic=[])
    runner.patch({retentive_tag: 11, non_retentive_tag: True, "UnknownAdHoc": 99})
    runner.step()

    runner.patch({retentive_tag: 123})
    runner.add_force(non_retentive_tag, False)
    runner._state = runner._state.with_memory({"user.custom.memory": 42})

    rebooted = runner.reboot()

    assert rebooted is runner.current_state
    assert _resolved(runner, system.sys.mode_run.name) is True
    assert runner.current_state.scan_id == 0
    assert runner.current_state.timestamp == 0.0
    assert runner.current_state.tags[retentive_tag.name] == 11
    assert runner.current_state.tags[non_retentive_tag.name] is True
    assert "UnknownAdHoc" not in runner.current_state.tags
    assert "user.custom.memory" not in runner.current_state.memory
    assert runner._pending_patches == {}
    assert runner._forces == {}
    assert runner.history.oldest_scan_id == 0
    assert runner.history.newest_scan_id == 0
    assert runner.playhead == 0


def test_reboot_without_battery_resets_all_known_tags_to_defaults():
    retentive_tag = Int("RebootNoBatteryRet")
    non_retentive_tag = Bool("RebootNoBatteryNonRet", retentive=False)

    runner = PLCRunner(logic=[])
    runner.patch({retentive_tag: 77, non_retentive_tag: True})
    runner.step()

    runner.set_battery_present(False)
    runner.reboot()

    assert _resolved(runner, system.sys.mode_run.name) is True
    assert runner.current_state.scan_id == 0
    assert runner.current_state.tags[retentive_tag.name] == 0
    assert runner.current_state.tags[non_retentive_tag.name] is False


def test_reboot_keeps_runner_in_run_mode_and_first_scan_is_true_on_next_scan():
    first_scan_latched = Bool("RebootFirstScanLatched")
    with Program() as program:
        with Rung():
            copy(system.sys.first_scan, first_scan_latched)

    runner = PLCRunner(logic=program)
    runner.step()
    runner.reboot()

    assert _resolved(runner, system.sys.mode_run.name) is True

    runner.step()
    assert runner.current_state.tags[first_scan_latched.name] is True


def test_battery_default_is_present():
    runner = PLCRunner(logic=[])
    assert _resolved(runner, system.sys.battery_present.name) is True


def test_reboot_without_battery_uses_per_slot_defaults_regardless_of_retentive():
    regs = Block("RebootSlot", TagType.INT, 1, 2, retentive=False)
    regs.configure_slot(1, retentive=False, default=10)
    regs.configure_slot(2, retentive=True, default=20)

    runner = PLCRunner(logic=[])
    runner.patch({regs[1]: 101, regs[2]: 202})
    runner.step()

    runner.set_battery_present(False)
    runner.reboot()

    assert runner.current_state.tags["RebootSlot1"] == 10
    assert runner.current_state.tags["RebootSlot2"] == 20
