"""Tests for predicate breakpoints and snapshot labels."""

from __future__ import annotations

import pytest

from pyrung.core import PLCRunner, TimeMode
from pyrung.core.state import SystemState


def _scan_ids(states: list[SystemState]) -> list[int]:
    return [state.scan_id for state in states]


def test_pause_breakpoint_halts_run_after_triggering_commit() -> None:
    runner = PLCRunner(logic=[])
    runner.when(lambda state: state.scan_id >= 3).pause()

    runner.run(cycles=10)

    assert runner.current_state.scan_id == 3


def test_pause_breakpoint_halts_run_for() -> None:
    runner = PLCRunner(logic=[])
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
    runner.when(lambda state: state.scan_id >= 3).pause()

    runner.run_for(seconds=10.0)

    assert runner.current_state.scan_id == 3
    assert runner.simulation_time == pytest.approx(0.3)


def test_pause_breakpoint_halts_run_until_even_when_predicate_is_false() -> None:
    runner = PLCRunner(logic=[])
    runner.when(lambda state: state.scan_id >= 2).pause()

    runner.run_until(lambda state: state.scan_id >= 10, max_cycles=20)

    assert runner.current_state.scan_id == 2


def test_snapshot_breakpoint_labels_history_without_halting() -> None:
    runner = PLCRunner(logic=[])
    runner.when(lambda state: state.scan_id > 0 and state.scan_id % 2 == 0).snapshot("even")

    runner.run(cycles=5)

    assert runner.current_state.scan_id == 5
    latest_even = runner.history.find("even")
    assert latest_even is not None
    assert latest_even.scan_id == 4
    assert _scan_ids(runner.history.find_all("even")) == [2, 4]


def test_snapshot_and_pause_can_fire_together_on_same_scan() -> None:
    runner = PLCRunner(logic=[])
    runner.when(lambda state: state.scan_id == 2).snapshot("hit")
    runner.when(lambda state: state.scan_id == 2).pause()

    runner.run(cycles=10)

    assert runner.current_state.scan_id == 2
    latest_hit = runner.history.find("hit")
    assert latest_hit is not None
    assert latest_hit.scan_id == 2
    assert _scan_ids(runner.history.find_all("hit")) == [2]


def test_snapshot_labels_deduplicate_same_label_on_same_scan() -> None:
    runner = PLCRunner(logic=[])
    runner.when(lambda state: state.scan_id == 2).snapshot("dup")
    runner.when(lambda state: state.scan_id == 2).snapshot("dup")

    runner.run(cycles=3)

    assert _scan_ids(runner.history.find_all("dup")) == [2]


def test_snapshot_labels_are_evicted_with_history() -> None:
    runner = PLCRunner(logic=[], history_limit=3)
    runner.when(lambda state: state.scan_id in {1, 3}).snapshot("milestone")

    runner.run(cycles=4)  # retained scans [2, 3, 4]

    assert _scan_ids(runner.history.find_all("milestone")) == [3]
    latest_milestone = runner.history.find("milestone")
    assert latest_milestone is not None
    assert latest_milestone.scan_id == 3


def test_breakpoint_handle_disable_enable_and_remove() -> None:
    runner = PLCRunner(logic=[])
    handle = runner.when(lambda state: state.scan_id >= 2).pause()

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

    runner.when(_boom).pause()
    with pytest.raises(RuntimeError, match="predicate boom"):
        runner.step()


def test_step_consumes_pause_request_without_persisting_it() -> None:
    runner = PLCRunner(logic=[])
    runner.when(lambda state: state.scan_id == 1).pause()

    runner.step()
    runner.step()

    assert runner.current_state.scan_id == 2
