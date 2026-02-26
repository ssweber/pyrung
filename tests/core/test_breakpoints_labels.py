"""Tests for predicate breakpoints and snapshot labels."""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung.core import Bool, Int, PLCRunner, TimeMode
from pyrung.core.state import SystemState


def _scan_ids(states: list[SystemState]) -> list[int]:
    return [state.scan_id for state in states]


def test_pause_breakpoint_stops_run_on_trigger_scan() -> None:
    runner = PLCRunner(logic=[])
    runner.when_fn(lambda state: state.scan_id >= 3).pause()

    runner.run(cycles=10)

    assert runner.current_state.scan_id == 3


def test_pause_breakpoint_halts_run_for() -> None:
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    runner.when_fn(lambda state: state.scan_id >= 3).pause()

    runner.run_for(seconds=10.0)

    assert runner.current_state.scan_id == 3
    assert runner.simulation_time == pytest.approx(0.3)


def test_pause_breakpoint_halts_run_until_even_when_predicate_is_false() -> None:
    runner = PLCRunner(logic=[])
    runner.when_fn(lambda state: state.scan_id >= 2).pause()

    runner.run_until_fn(lambda state: state.scan_id >= 10, max_cycles=20)

    assert runner.current_state.scan_id == 2


def test_snapshot_breakpoint_labels_history_and_run_continues() -> None:
    runner = PLCRunner(logic=[])
    runner.when_fn(lambda state: state.scan_id > 0 and state.scan_id % 2 == 0).snapshot("even")

    runner.run(cycles=5)

    assert runner.current_state.scan_id == 5
    latest_even = runner.history.find("even")
    assert latest_even is not None
    assert latest_even.scan_id == 4
    assert _scan_ids(runner.history.find_all("even")) == [2, 4]


def test_snapshot_and_pause_can_fire_together_on_same_scan() -> None:
    runner = PLCRunner(logic=[])
    runner.when_fn(lambda state: state.scan_id == 2).snapshot("hit")
    runner.when_fn(lambda state: state.scan_id == 2).pause()

    runner.run(cycles=10)

    assert runner.current_state.scan_id == 2
    latest_hit = runner.history.find("hit")
    assert latest_hit is not None
    assert latest_hit.scan_id == 2
    assert _scan_ids(runner.history.find_all("hit")) == [2]


def test_snapshot_labels_deduplicate_same_label_on_same_scan() -> None:
    runner = PLCRunner(logic=[])
    runner.when_fn(lambda state: state.scan_id == 2).snapshot("dup")
    runner.when_fn(lambda state: state.scan_id == 2).snapshot("dup")

    runner.run(cycles=3)

    assert _scan_ids(runner.history.find_all("dup")) == [2]


def test_snapshot_labels_are_evicted_with_history() -> None:
    runner = PLCRunner(logic=[], history_limit=3)
    runner.when_fn(lambda state: state.scan_id in {1, 3}).snapshot("milestone")

    runner.run(cycles=4)  # retained scans [2, 3, 4]

    assert _scan_ids(runner.history.find_all("milestone")) == [3]
    latest_milestone = runner.history.find("milestone")
    assert latest_milestone is not None
    assert latest_milestone.scan_id == 3


def test_snapshot_breakpoint_captures_rtc_metadata() -> None:
    runner = PLCRunner(logic=[])
    runner.set_rtc(datetime(2026, 2, 24, 12, 34, 56))
    runner.when_fn(lambda state: state.scan_id == 1).snapshot("tick")

    runner.run(cycles=1)

    labeled = runner.history.find_labeled("tick")
    assert labeled is not None
    assert labeled.scan_id == 1
    assert labeled.rtc_iso == "2026-02-24T12:34:56.100000"
    assert isinstance(labeled.rtc_offset_seconds, float)


def test_breakpoint_handle_disable_enable_and_remove() -> None:
    runner = PLCRunner(logic=[])
    handle = runner.when_fn(lambda state: state.scan_id >= 2).pause()

    handle.disable()
    runner.run(cycles=3)
    assert runner.current_state.scan_id == 3

    handle.enable()
    runner.run(cycles=5)
    assert runner.current_state.scan_id == 4

    handle.remove()
    handle.remove()
    handle.disable()
    handle.enable()
    runner.run(cycles=3)
    assert runner.current_state.scan_id == 7


def test_breakpoint_predicate_exceptions_propagate() -> None:
    runner = PLCRunner(logic=[])

    def _boom(_state: SystemState) -> bool:
        raise RuntimeError("predicate boom")

    runner.when_fn(_boom).pause()
    with pytest.raises(RuntimeError, match="predicate boom"):
        runner.step()


def test_pause_request_is_consumed_by_single_step() -> None:
    runner = PLCRunner(logic=[])
    runner.when_fn(lambda state: state.scan_id == 1).pause()

    runner.step()
    runner.step()

    assert runner.current_state.scan_id == 2


def test_expression_pause_breakpoint_accepts_tag_conditions() -> None:
    fault = Bool("Fault")
    runner = PLCRunner(logic=[])
    runner.when(fault).pause()

    runner.patch({"Fault": True})
    runner.run(cycles=10)

    assert runner.current_state.scan_id == 1


def test_expression_snapshot_breakpoint_accepts_inverted_tag_conditions() -> None:
    fault = Bool("Fault")
    runner = PLCRunner(logic=[])
    runner.when(~fault).snapshot("fault_clear")

    runner.step()

    latest = runner.history.find("fault_clear")
    assert latest is not None
    assert latest.scan_id == 1


def test_run_until_expression_supports_inverted_bool_tag() -> None:
    motor = Bool("Motor")
    runner = PLCRunner(logic=[])

    result = runner.run_until(~motor, max_cycles=10)

    assert result.scan_id == 1


def test_run_until_expression_supports_comparisons() -> None:
    step = Int("Step")
    runner = PLCRunner(logic=[])
    runner.patch({"Step": 2})

    result = runner.run_until(step >= 2, max_cycles=10)

    assert result.scan_id == 1


def test_when_rejects_callable_with_migration_hint() -> None:
    runner = PLCRunner(logic=[])

    with pytest.raises(TypeError, match="when_fn\\(\\.\\.\\.\\)"):
        runner.when(lambda state: state.scan_id >= 1).pause()


def test_run_until_rejects_callable_with_migration_hint() -> None:
    runner = PLCRunner(logic=[])

    with pytest.raises(TypeError, match="run_until_fn\\(\\.\\.\\.\\)"):
        runner.run_until(lambda state: state.scan_id >= 1, max_cycles=1)
