"""Runner STOP/RUN transition behavior."""

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
    Tag,
    TagType,
    TimeMode,
    copy,
    out,
    system,
)


def _resolved(runner: PLCRunner, tag_name: str):
    found, value = runner.system_runtime.resolve(tag_name, runner.current_state)
    assert found is True
    return value


def test_stop_sets_mode_off_and_is_idempotent():
    runner = PLCRunner(logic=[])
    assert _resolved(runner, system.sys.mode_run.name) is True

    runner.stop()
    assert _resolved(runner, system.sys.mode_run.name) is False

    stopped_state = runner.current_state
    runner.stop()
    assert runner.current_state is stopped_state
    assert _resolved(runner, system.sys.mode_run.name) is False


def test_step_after_stop_performs_stop_to_run_transition_and_resets_runtime_scope():
    retentive_tag = Int("RetentiveValue")
    non_retentive_tag = Bool("NonRetentiveValue", retentive=False)
    first_scan_latched = Bool("FirstScanLatched")

    with Program() as program:
        with Rung():
            copy(system.sys.first_scan, first_scan_latched)

    runner = PLCRunner(logic=program)
    runner.patch({retentive_tag: 7, non_retentive_tag: True, "UnknownAdHoc": 1})
    runner.step()

    runner.patch({retentive_tag: 9})
    runner.add_force(non_retentive_tag, False)
    runner._state = runner._state.with_memory({"user.custom.memory": 123})

    runner.stop()
    runner.step()

    assert _resolved(runner, system.sys.mode_run.name) is True
    assert runner.current_state.scan_id == 1
    assert runner.current_state.timestamp == pytest.approx(0.1)
    assert runner.current_state.tags[retentive_tag.name] == 7
    assert runner.current_state.tags[non_retentive_tag.name] is False
    assert runner.current_state.tags[first_scan_latched.name] is True
    assert "UnknownAdHoc" not in runner.current_state.tags
    assert "user.custom.memory" not in runner.current_state.memory
    assert runner._pending_patches == {}
    assert runner._forces == {}
    assert runner.history.oldest_scan_id == 0
    assert runner.history.newest_scan_id == 1
    assert runner.playhead == 1


@pytest.mark.parametrize(
    ("method_name", "invoke"),
    [
        ("step", lambda r: r.step()),
        ("run", lambda r: r.run(1)),
        ("run_for", lambda r: r.run_for(0.01)),
        ("run_until", lambda r: r.run_until(lambda s: s.scan_id >= 1, max_cycles=1)),
        ("scan_steps", lambda r: list(r.scan_steps())),
    ],
)
def test_all_execution_methods_auto_restart_from_stop(
    method_name: str,
    invoke,
):
    _ = method_name
    retentive_tag = Int("AutoRestartRet")
    non_retentive_tag = Bool("AutoRestartNonRet", retentive=False)

    runner = PLCRunner(logic=[])
    runner.patch({retentive_tag: 21, non_retentive_tag: True})
    runner.step()
    runner.stop()

    invoke(runner)

    assert _resolved(runner, system.sys.mode_run.name) is True
    assert runner.current_state.scan_id >= 1
    assert runner.current_state.tags[retentive_tag.name] == 21
    assert runner.current_state.tags[non_retentive_tag.name] is False


def test_stop_restart_preserves_time_mode_and_debug_registrations():
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.25)

    monitor = runner.monitor("WatchedTag", lambda current, previous: None)
    breakpoint_handle = runner.when(lambda _state: False).pause()

    runner.stop()
    runner.step()

    assert runner.time_mode == TimeMode.FIXED_STEP
    assert runner.current_state.timestamp == pytest.approx(0.25)
    assert monitor.id in runner._monitors_by_id
    assert breakpoint_handle.id in runner._breakpoints_by_id


def test_stop_restart_preserves_rtc_continuity():
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.25)
    runner.set_rtc(datetime(2026, 3, 5, 6, 59, 50))
    runner.run(cycles=4)
    before_stop_rtc = runner.system_runtime._rtc_now(runner.current_state)

    runner.stop()
    runner.step()
    after_restart_rtc = runner.system_runtime._rtc_now(runner.current_state)

    assert (after_restart_rtc - before_stop_rtc).total_seconds() == pytest.approx(0.25)


def test_cmd_mode_stop_written_in_scan_transitions_runner_to_stop():
    with Program() as program:
        with Rung(system.sys.first_scan):
            out(system.sys.cmd_mode_stop)

    runner = PLCRunner(logic=program)

    runner.step()
    assert _resolved(runner, system.sys.mode_run.name) is True
    runner.step()
    assert _resolved(runner, system.sys.mode_run.name) is False
    runner.step()
    assert _resolved(runner, system.sys.mode_run.name) is True


def test_stop_restart_mixed_block_slot_retentive_and_default_policy():
    regs = Block("Reg", TagType.INT, 1, 2, retentive=False)
    regs.configure_slot(1, retentive=False, default=111)
    regs.configure_slot(2, retentive=True, default=222)

    runner = PLCRunner(logic=[])
    runner.patch({regs[1]: 5, regs[2]: 6})
    runner.step()

    runner.stop()
    runner.step()

    assert runner.current_state.tags["Reg1"] == 111
    assert runner.current_state.tags["Reg2"] == 6


def test_register_known_tag_conflict_on_same_name_raises():
    runner = PLCRunner(logic=[])
    first = Tag("DupMeta", TagType.INT, retentive=True, default=0)
    second = Tag("DupMeta", TagType.INT, retentive=False, default=0)

    runner.patch({first: 1})

    with pytest.raises(ValueError, match="Conflicting tag metadata"):
        runner.patch({second: 2})
