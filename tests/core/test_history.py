"""Tests for PLCRunner history retention and queries."""

from __future__ import annotations

import pytest

from pyrung.core import PLCRunner
from pyrung.core.history import History
from pyrung.core.state import SystemState


def _scan_ids(runner: PLCRunner, n: int = 100) -> list[int]:
    return [state.scan_id for state in runner.history.latest(n)]


def test_history_includes_initial_state_on_creation() -> None:
    runner = PLCRunner(logic=[])

    snapshots = runner.history.latest(10)
    assert len(snapshots) == 1
    assert snapshots[0].scan_id == 0
    assert snapshots[0] is runner.current_state


def test_history_appends_one_snapshot_per_step() -> None:
    runner = PLCRunner(logic=[])

    runner.step()
    runner.step()

    assert _scan_ids(runner) == [0, 1, 2]
    assert runner.history.at(2) is runner.current_state


def test_history_at_returns_state_and_raises_for_missing_scan() -> None:
    runner = PLCRunner(logic=[])
    runner.run(cycles=3)

    assert runner.history.at(1).scan_id == 1

    with pytest.raises(KeyError):
        runner.history.at(99)


def test_history_range_is_start_inclusive_end_exclusive() -> None:
    runner = PLCRunner(logic=[])
    runner.run(cycles=5)

    subset = runner.history.range(1, 4)
    assert [state.scan_id for state in subset] == [1, 2, 3]
    assert runner.history.range(3, 3) == []
    assert runner.history.range(9, 12) == []


def test_history_latest_orders_oldest_to_newest_and_handles_bounds() -> None:
    runner = PLCRunner(logic=[])
    runner.run(cycles=4)

    assert [state.scan_id for state in runner.history.latest(2)] == [3, 4]
    assert [state.scan_id for state in runner.history.latest(50)] == [0, 1, 2, 3, 4]
    assert runner.history.latest(0) == []
    assert runner.history.latest(-3) == []


def test_unbounded_history_retains_all_scans() -> None:
    runner = PLCRunner(logic=[], history_limit=None)
    runner.run(cycles=6)

    assert _scan_ids(runner) == [0, 1, 2, 3, 4, 5, 6]


def test_bounded_history_evicts_oldest_first() -> None:
    runner = PLCRunner(logic=[], history_limit=3)

    runner.step()  # [0, 1]
    runner.step()  # [0, 1, 2]
    runner.step()  # [1, 2, 3]
    runner.step()  # [2, 3, 4]

    assert _scan_ids(runner) == [2, 3, 4]
    assert runner.history.at(2).scan_id == 2
    with pytest.raises(KeyError):
        runner.history.at(1)


def test_history_limit_validation_rejects_zero_or_negative() -> None:
    with pytest.raises(ValueError, match="history_limit must be >= 1 or None"):
        PLCRunner(logic=[], history_limit=0)

    with pytest.raises(ValueError, match="history_limit must be >= 1 or None"):
        PLCRunner(logic=[], history_limit=-5)


def test_history_enforces_monotonic_scan_order_when_appending() -> None:
    history = History(SystemState())

    with pytest.raises(ValueError, match="strictly increasing"):
        history._append(SystemState())
