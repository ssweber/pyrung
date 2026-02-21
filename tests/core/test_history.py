"""Tests for PLCRunner history retention and queries."""

from __future__ import annotations

import pytest

from pyrung.core import PLCRunner, TimeMode
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


def test_diff_returns_only_changed_tags_sorted_and_treats_missing_as_none() -> None:
    initial = SystemState().with_tags({"B": 0, "A": 0})
    runner = PLCRunner(logic=[], initial_state=initial)

    runner.patch({"A": 1, "B": 2, "C": 3})
    runner.step()

    forward = runner.diff(0, 1)
    assert list(forward) == sorted(forward)
    assert forward["A"] == (0, 1)
    assert forward["B"] == (0, 2)
    assert forward["C"] == (None, 3)

    reverse = runner.diff(1, 0)
    assert list(reverse) == sorted(reverse)
    assert reverse["C"] == (3, None)


def test_diff_returns_empty_for_same_scan() -> None:
    runner = PLCRunner(logic=[])
    runner.step()
    runner.step()

    assert runner.diff(2, 2) == {}


def test_diff_reflects_system_tag_changes_between_scans() -> None:
    runner = PLCRunner(logic=[])
    runner.step()
    runner.step()

    assert runner.diff(1, 2) == {"sys.scan_counter": (1, 2)}


def test_diff_raises_for_missing_scan() -> None:
    runner = PLCRunner(logic=[])

    with pytest.raises(KeyError):
        runner.diff(0, 99)


def test_fork_from_starts_from_exact_snapshot_and_preserves_time_config() -> None:
    initial = SystemState().with_tags({"A": 1}).with_memory({"m": 7})
    runner = PLCRunner(logic=[], initial_state=initial)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.25)
    runner.patch({"A": 2})
    runner.step()

    snapshot = runner.history.at(1)
    fork = runner.fork_from(1)

    assert fork.current_state == snapshot
    assert fork.current_state.scan_id == 1
    assert fork.current_state.timestamp == pytest.approx(0.25)
    assert dict(fork.current_state.tags) == dict(snapshot.tags)
    assert dict(fork.current_state.memory) == dict(snapshot.memory)
    assert [state.scan_id for state in fork.history.latest(10)] == [1]
    assert fork.time_mode == TimeMode.FIXED_STEP

    fork.step()
    assert fork.current_state.scan_id == 2
    assert fork.current_state.timestamp == pytest.approx(0.5)


def test_fork_from_inherits_history_limit_and_evicts_oldest() -> None:
    runner = PLCRunner(logic=[], history_limit=3)
    runner.run(cycles=5)

    fork = runner.fork_from(4)
    assert _scan_ids(fork) == [4]

    fork.step()
    fork.step()
    fork.step()

    assert _scan_ids(fork) == [5, 6, 7]
    with pytest.raises(KeyError):
        fork.history.at(4)


def test_fork_from_starts_clean_and_parent_fork_evolve_independently() -> None:
    runner = PLCRunner(logic=[])
    runner.patch({"X": 1})
    runner.step()
    runner.add_force("X", 5)
    runner.patch({"Y": 2})  # pending only in parent runtime state

    fork = runner.fork_from(1)
    assert dict(fork.forces) == {}
    assert fork._pending_patches == {}

    runner.clear_forces()
    runner.patch({"X": 2})
    runner.step()

    fork.patch({"X": 99})
    fork.step()

    assert runner.current_state.tags["X"] == 2
    assert fork.current_state.tags["X"] == 99
    assert _scan_ids(runner) == [0, 1, 2]
    assert _scan_ids(fork) == [1, 2]


def test_fork_from_raises_for_missing_scan() -> None:
    runner = PLCRunner(logic=[])

    with pytest.raises(KeyError):
        runner.fork_from(999)
