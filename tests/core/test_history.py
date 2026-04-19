"""Tests for PLC history retention and queries.

Stage 5 (record-and-replay) replaced ``History``'s deque/dict storage
with a thin facade over the PLC's byte-bounded recent-state cache and
``replay_to``.  Every scan from ``0`` to the current tip is
addressable; recent scans are served from the cache, older scans are
reconstructed via ``replay_to`` from the nearest checkpoint.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung.core import PLC, TimeMode
from pyrung.core.state import SystemState


def _scan_ids(runner: PLC, n: int = 100) -> list[int]:
    return [state.scan_id for state in runner.history.latest(n)]


def test_history_includes_initial_state_on_creation() -> None:
    runner = PLC(logic=[])

    snapshots = runner.history.latest(10)
    assert len(snapshots) == 1
    assert snapshots[0].scan_id == 0
    assert snapshots[0] is runner.current_state


def test_history_appends_one_snapshot_per_step() -> None:
    runner = PLC(logic=[])

    runner.step()
    runner.step()

    assert _scan_ids(runner) == [0, 1, 2]
    assert runner.history.at(2) is runner.current_state


def test_history_at_returns_state_and_raises_for_unknown_scan() -> None:
    runner = PLC(logic=[])
    runner.run(cycles=3)

    assert runner.history.at(1).scan_id == 1

    with pytest.raises(KeyError):
        runner.history.at(99)


def test_history_range_is_start_inclusive_end_exclusive() -> None:
    runner = PLC(logic=[])
    runner.run(cycles=5)

    subset = runner.history.range(1, 4)
    assert [state.scan_id for state in subset] == [1, 2, 3]
    assert runner.history.range(3, 3) == []
    assert runner.history.range(9, 12) == []


def test_history_latest_returns_chronological_window_with_bounds() -> None:
    runner = PLC(logic=[])
    runner.run(cycles=4)

    assert [state.scan_id for state in runner.history.latest(2)] == [3, 4]
    assert [state.scan_id for state in runner.history.latest(50)] == [0, 1, 2, 3, 4]
    assert runner.history.latest(0) == []
    assert runner.history.latest(-3) == []


def test_unbounded_history_retains_all_scans() -> None:
    runner = PLC(logic=[])
    runner.run(cycles=6)

    assert _scan_ids(runner) == [0, 1, 2, 3, 4, 5, 6]


def test_history_cache_validation_rejects_below_1mb() -> None:
    with pytest.raises(ValueError, match="history_cache must be >= 1 MB"):
        PLC(logic=[], history_cache=0)

    with pytest.raises(ValueError, match="history_cache must be >= 1 MB"):
        PLC(logic=[], history_cache=500_000)


def test_playhead_starts_at_tip_and_tracks_new_scans() -> None:
    runner = PLC(logic=[])

    assert runner.playhead == 0
    runner.step()
    assert runner.playhead == 1
    runner.step()
    assert runner.playhead == 2


def test_seek_repositions_playhead_without_advancing_tip() -> None:
    runner = PLC(logic=[])
    runner.run(cycles=3)

    state = runner.seek(1)
    assert state.scan_id == 1
    assert runner.playhead == 1
    assert runner.current_state.scan_id == 3


def test_seek_raises_for_unknown_scan() -> None:
    runner = PLC(logic=[])

    with pytest.raises(KeyError):
        runner.seek(99)


def test_rewind_selects_latest_scan_not_after_target_time() -> None:
    runner = PLC(logic=[], dt=0.5)
    runner.run(cycles=5)  # scan 5 @ 2.5s

    runner.seek(5)
    state = runner.rewind(0.9)  # target: 1.6s -> scan 3 @ 1.5s

    assert state.scan_id == 3
    assert runner.playhead == 3


def test_rewind_clamps_to_oldest_addressable_scan_for_early_target_time() -> None:
    runner = PLC(logic=[], dt=1.0)
    runner.run(cycles=5)

    runner.seek(5)
    state = runner.rewind(100.0)

    # Facade addresses scan 0 onward, so a far-back rewind lands at 0.
    assert state.scan_id == 0
    assert runner.playhead == 0


def test_rewind_rejects_negative_seconds() -> None:
    runner = PLC(logic=[])

    with pytest.raises(ValueError, match="seconds must be >= 0"):
        runner.rewind(-0.1)


def test_step_appends_at_tip_even_when_playhead_is_in_the_past() -> None:
    runner = PLC(logic=[])
    runner.run(cycles=3)
    runner.seek(1)

    runner.step()

    assert runner.current_state.scan_id == 4
    assert runner.playhead == 1
    assert _scan_ids(runner) == [0, 1, 2, 3, 4]


def test_playhead_stays_put_when_window_rotates_past_it() -> None:
    """Playhead is no longer pinned to a retained-state window — every
    scan ``>= 0`` is addressable, so the playhead survives window
    rotation untouched."""
    runner = PLC(logic=[])

    runner.run(cycles=4)
    runner.seek(2)

    runner.step()

    assert runner.playhead == 2
    assert runner.history.at(2).scan_id == 2


def test_diff_sorts_keys_and_represents_absent_tags_as_none() -> None:
    initial = SystemState().with_tags({"B": 0, "A": 0})
    runner = PLC(logic=[], initial_state=initial)

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
    runner = PLC(logic=[])
    runner.step()
    runner.step()

    assert runner.diff(2, 2) == {}


def test_diff_is_empty_for_idle_scans() -> None:
    runner = PLC(logic=[])
    runner.step()
    runner.step()

    assert runner.diff(1, 2) == {}


def test_diff_raises_for_unknown_scan() -> None:
    runner = PLC(logic=[])

    with pytest.raises(KeyError):
        runner.diff(0, 99)


def test_fork_defaults_to_current_tip_even_if_playhead_is_in_the_past() -> None:
    runner = PLC(logic=[])
    runner.run(cycles=3)
    runner.seek(1)

    fork = runner.fork()

    assert fork.current_state.scan_id == 3
    assert fork.current_state.timestamp == pytest.approx(runner.current_state.timestamp)
    assert _scan_ids(fork) == [3]
    assert fork.playhead == 3


def test_fork_with_scan_id_starts_from_exact_snapshot_and_preserves_time_config() -> None:
    initial = SystemState().with_tags({"A": 1}).with_memory({"m": 7})
    runner = PLC(logic=[], initial_state=initial, dt=0.25)
    runner.patch({"A": 2})
    runner.step()

    snapshot = runner.history.at(1)
    fork = runner.fork(scan_id=1)

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


def test_fork_starts_with_same_rtc_as_parent_at_fork_point() -> None:
    runner = PLC(logic=[], dt=0.25)
    runner.set_rtc(datetime(2026, 3, 5, 6, 59, 50))
    runner.run(cycles=6)

    expected_parent_rtc = runner.debug.system_runtime._rtc_now(runner.current_state)
    fork = runner.fork()

    assert fork.debug.system_runtime._rtc_now(fork.current_state) == expected_parent_rtc


def test_fork_starts_clean_and_parent_fork_evolve_independently() -> None:
    runner = PLC(logic=[])
    runner.patch({"X": 1})
    runner.step()
    runner.force("X", 5)
    runner.patch({"Y": 2})  # pending only in parent runtime state

    fork = runner.fork()
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


def test_fork_raises_for_unknown_scan() -> None:
    runner = PLC(logic=[])

    with pytest.raises(KeyError):
        runner.fork(scan_id=999)


def test_fork_from_starts_from_exact_snapshot_and_preserves_time_config() -> None:
    initial = SystemState().with_tags({"A": 1}).with_memory({"m": 7})
    runner = PLC(logic=[], initial_state=initial, dt=0.25)
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


def test_fork_from_starts_with_same_rtc_as_parent_at_selected_scan() -> None:
    runner = PLC(logic=[], dt=0.25)
    runner.set_rtc(datetime(2026, 3, 5, 6, 59, 50))
    runner.run(cycles=6)

    snapshot = runner.history.at(3)
    expected_parent_rtc = runner.debug.system_runtime._rtc_now(snapshot)
    fork = runner.fork_from(3)

    assert fork.debug.system_runtime._rtc_now(fork.current_state) == expected_parent_rtc


def test_fork_from_inherits_history_cache_budget() -> None:
    """``history_cache`` is propagated across fork."""
    budget = 2 * 1024 * 1024  # 2 MB
    runner = PLC(logic=[], history_cache=budget)
    runner.run(cycles=5)

    fork = runner.fork_from(4)
    assert fork._recent_state_cache_budget == budget
    assert _scan_ids(fork) == [4]

    fork.step()
    fork.step()
    fork.step()

    # Fork starts at scan 4; subsequent scans 5, 6, 7 stay addressable.
    assert _scan_ids(fork) == [4, 5, 6, 7]
    assert fork.history.at(4).scan_id == 4


def test_fork_from_starts_clean_and_parent_fork_evolve_independently() -> None:
    runner = PLC(logic=[])
    runner.patch({"X": 1})
    runner.step()
    runner.force("X", 5)
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


def test_fork_from_raises_for_unknown_scan() -> None:
    runner = PLC(logic=[])

    with pytest.raises(KeyError):
        runner.fork_from(999)


def test_history_find_apis_return_empty_results_for_unknown_label() -> None:
    runner = PLC(logic=[])

    assert runner.history.find("missing") is None
    assert runner.history.find_all("missing") == []
    assert runner.history.find_labeled("missing") is None
    assert runner.history.find_all_labeled("missing") == []


def test_history_label_scan_supports_find_find_all_and_dedup_per_scan() -> None:
    runner = PLC(logic=[], dt=0.1)
    runner.step()
    runner.step()

    history = runner.history
    history._label_scan("fault", 1)
    history._label_scan("fault", 1)
    history._label_scan("fault", 2)

    assert history.find("fault").scan_id == 2
    assert [state.scan_id for state in history.find_all("fault")] == [1, 2]


def test_history_labeled_snapshot_includes_metadata_when_provided() -> None:
    runner = PLC(logic=[], dt=0.1)
    runner.step()

    history = runner.history
    history._label_scan(
        "fault",
        1,
        metadata={"rtc_iso": "2026-02-24T12:34:56", "rtc_offset_seconds": 30.0},
    )

    labeled = history.find_labeled("fault")
    assert labeled is not None
    assert labeled.label == "fault"
    assert labeled.scan_id == 1
    assert labeled.timestamp == pytest.approx(0.1)
    assert labeled.rtc_iso == "2026-02-24T12:34:56"
    assert labeled.rtc_offset_seconds == pytest.approx(30.0)


def test_history_label_scan_raises_for_unknown_scan() -> None:
    runner = PLC(logic=[])

    with pytest.raises(KeyError):
        runner.history._label_scan("fault", 99)


def test_history_labels_survive_window_rotation() -> None:
    """Labels are decoupled from state storage — labeling an early
    scan stays valid after the recent-state window has rotated past it."""
    runner = PLC(logic=[], dt=0.1)
    runner.step()
    runner.history._label_scan(
        "boot",
        0,
        metadata={"rtc_iso": "2026-02-24T12:00:00", "rtc_offset_seconds": 0.0},
    )

    # Spin past the 20-scan window so scan 0 is replay-only.
    for _ in range(40):
        runner.step()

    found = runner.history.find("boot")
    assert found is not None and found.scan_id == 0
    assert [s.scan_id for s in runner.history.find_all("boot")] == [0]
    labeled = runner.history.find_labeled("boot")
    assert labeled is not None and labeled.scan_id == 0
    assert labeled.rtc_iso == "2026-02-24T12:00:00"
