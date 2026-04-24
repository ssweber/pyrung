"""ScanLog recorder-shim tests.

These tests exercise the capture surface of ``ScanLog`` without any
replay consumer.  The goal here is to confirm:

- Every nondeterminism channel (patches, force changes, RTC base,
  dt in REALTIME, lifecycle) lands in the log under the right scan_id.
- Idle scans add zero bytes to the log.
- The log ``snapshot()`` is truly decoupled from subsequent writes.
- ``trim_before`` correctly advances the replay horizon.
"""

from __future__ import annotations

import pytest

from pyrung.core import PLC, Bool
from pyrung.core.scan_log import ScanLog
from pyrung.core.time_mode import TimeMode


def _idle_plc(*, checkpoint_interval: int | None = None) -> PLC:
    return PLC(logic=[], dt=0.01, checkpoint_interval=checkpoint_interval)


def test_fresh_log_is_empty():
    plc = _idle_plc()
    assert plc._scan_log.bytes_estimate() == 0
    snap = plc._scan_log.snapshot()
    assert snap.patches_by_scan == {}
    assert snap.force_changes_by_scan == {}
    assert snap.rtc_base_changes == {}
    assert snap.lifecycle_events == ()
    assert snap.dts is None  # FIXED_STEP


def test_idle_scans_cost_zero_bytes():
    # Checkpoints force-write the current force map every K scans as a
    # replay correctness invariant — that cost lives in a separate
    # budget line, not the per-scan log growth this test
    # pins down.  Disable checkpoints for this run so the log-level
    # "idle scans contribute zero bytes" claim stays testable.
    plc = _idle_plc(checkpoint_interval=10_001)
    for _ in range(10_000):
        plc.step()
    assert plc._scan_log.bytes_estimate() == 0


def test_patches_recorded_at_the_scan_they_apply_to():
    plc = _idle_plc()
    plc.step()  # scan_id 1
    plc.patch({"X": True})
    plc.step()  # scan_id 2 — patches apply here
    plc.step()  # scan_id 3 — no patches

    snap = plc._scan_log.snapshot()
    assert snap.patches_by_scan == {2: {"X": True}}


def test_force_changes_recorded_only_when_map_changes():
    plc = _idle_plc()
    plc.force(Bool("X"), True)
    plc.step()  # scan_id 1 — force map changed from {} to {X: True}
    plc.step()  # scan_id 2 — no change
    plc.step()  # scan_id 3 — no change
    plc.force(Bool("Y"), False)
    plc.step()  # scan_id 4 — force map changed to {X: True, Y: False}
    plc.unforce("X")
    plc.step()  # scan_id 5 — force map changed to {Y: False}

    snap = plc._scan_log.snapshot()
    assert snap.force_changes_by_scan == {
        1: {"X": True},
        4: {"X": True, "Y": False},
        5: {"Y": False},
    }


def test_clear_forces_records_lifecycle_and_force_change():
    plc = _idle_plc()
    plc.force(Bool("X"), True)
    plc.step()  # scan_id 1, force_changes[1]={X:True}
    plc.clear_forces()
    plc.step()  # scan_id 2 — force map now {} (changed)

    snap = plc._scan_log.snapshot()
    assert snap.force_changes_by_scan == {1: {"X": True}, 2: {}}
    kinds = [e.kind for e in snap.lifecycle_events]
    assert kinds == ["clear_forces"]
    assert snap.lifecycle_events[0].at_scan_id == 2


def test_clear_forces_with_empty_map_is_noop():
    plc = _idle_plc()
    plc.clear_forces()
    plc.step()
    snap = plc._scan_log.snapshot()
    assert snap.lifecycle_events == ()


def test_rtc_set_records_at_next_scan_id():
    plc = _idle_plc()
    plc.step()  # scan_id 1
    from datetime import datetime

    plc.set_rtc(datetime(2026, 3, 5, 12, 0, 0))
    plc.step()  # scan_id 2 — rtc change was for this scan

    snap = plc._scan_log.snapshot()
    assert 2 in snap.rtc_base_changes
    base, base_sim_time = snap.rtc_base_changes[2]
    assert base == datetime(2026, 3, 5, 12, 0, 0)
    assert base_sim_time == pytest.approx(plc.current_state.timestamp - 0.01)


def test_stop_records_lifecycle_event():
    plc = _idle_plc()
    plc.step()
    plc.stop()
    snap = plc._scan_log.snapshot()
    kinds = [e.kind for e in snap.lifecycle_events]
    assert kinds == ["stop"]
    assert snap.lifecycle_events[0].at_scan_id == 2


def test_stop_when_already_stopped_is_noop():
    plc = _idle_plc()
    plc.stop()
    plc.stop()
    snap = plc._scan_log.snapshot()
    assert [e.kind for e in snap.lifecycle_events] == ["stop"]


def test_reboot_resets_scan_log_and_checkpoints():
    """Reboot is destructive: post-reboot scan_ids would alias pre-reboot
    entries in every sparse channel, so the log and checkpoints reset to
    a fresh recording session rooted at the post-reboot scan 0.  No
    ghost ``reboot`` lifecycle event is retained — the log starts clean.
    """
    plc = _idle_plc(checkpoint_interval=2)
    for _ in range(3):
        plc.step()
    assert plc._scan_log.bytes_estimate() > 0
    assert plc._checkpoints  # checkpoint at scan 2

    plc.reboot()
    snap = plc._scan_log.snapshot()
    assert snap.lifecycle_events == ()
    assert snap.patches_by_scan == {}
    assert snap.force_changes_by_scan == {}
    assert snap.rtc_base_changes == {}
    assert plc._checkpoints == {}


def test_battery_present_toggle_records_with_value():
    plc = _idle_plc()
    plc.battery_present = False
    plc.battery_present = True
    snap = plc._scan_log.snapshot()
    events = [(e.kind, e.value) for e in snap.lifecycle_events]
    assert events == [("battery_present", False), ("battery_present", True)]


def test_battery_present_same_value_is_noop():
    plc = _idle_plc()
    plc.battery_present = True  # same as init default
    snap = plc._scan_log.snapshot()
    assert snap.lifecycle_events == ()


def test_realtime_mode_records_dt_per_scan():
    plc = PLC(logic=[], realtime=True)
    for _ in range(5):
        plc.step()
    snap = plc._scan_log.snapshot()
    assert snap.dts is not None
    # Indexed as dts[scan_id - base_scan]; scan 0 is the initial state
    # (never executed), so dts[0] is a placeholder 0.0 and recorded
    # scans 1..5 land at indices 1..5.  Length = max_scan_id + 1.
    assert len(snap.dts) == 6
    assert snap.dts[0] == 0.0
    assert all(dt > 0 for dt in snap.dts[1:])


def test_fixed_step_mode_elides_dts():
    plc = _idle_plc()
    for _ in range(5):
        plc.step()
    snap = plc._scan_log.snapshot()
    assert snap.dts is None


def test_snapshot_is_decoupled_from_subsequent_writes():
    plc = _idle_plc()
    plc.patch({"X": 1})
    plc.step()

    snap = plc._scan_log.snapshot()
    assert snap.patches_by_scan == {1: {"X": 1}}

    plc.patch({"Y": 2})
    plc.step()

    # Snapshot taken earlier must not see the new write.
    assert snap.patches_by_scan == {1: {"X": 1}}
    # Live log sees both.
    live = plc._scan_log.snapshot()
    assert live.patches_by_scan == {1: {"X": 1}, 2: {"Y": 2}}


def test_fork_has_independent_fresh_scan_log():
    plc = _idle_plc()
    plc.patch({"X": 1})
    plc.step()
    plc.step()

    fork = plc.fork()
    assert fork._scan_log.bytes_estimate() == 0
    assert plc._scan_log.bytes_estimate() > 0


def test_scan_log_direct_construction():
    log = ScanLog(time_mode=TimeMode.FIXED_STEP)
    assert log.base_scan == 0
    assert log.records_dt is False

    log = ScanLog(time_mode=TimeMode.REALTIME, base_scan=42)
    assert log.base_scan == 42
    assert log.records_dt is True


# ---------------------------------------------------------------------------
# trim_before tests
# ---------------------------------------------------------------------------


def test_trim_before_drops_sparse_dict_entries():
    plc = _idle_plc(checkpoint_interval=1000)
    plc.patch({"X": 1})
    plc.step()  # scan 1 — patch lands here
    plc.patch({"Y": 2})
    plc.step()  # scan 2
    plc.patch({"Z": 3})
    plc.step()  # scan 3

    plc._scan_log.trim_before(2)  # drop scan 1

    snap = plc._scan_log.snapshot()
    assert 1 not in snap.patches_by_scan
    assert 2 in snap.patches_by_scan
    assert 3 in snap.patches_by_scan
    assert plc._scan_log.base_scan == 2


def test_trim_before_noop_when_at_or_below_base():
    log = ScanLog(time_mode=TimeMode.FIXED_STEP)
    log.record_patches(5, {"A": 1})
    log.trim_before(0)  # no-op
    assert log.base_scan == 0
    snap = log.snapshot()
    assert 5 in snap.patches_by_scan


def test_trim_before_idle_tail_still_zero_bytes():
    plc = _idle_plc(checkpoint_interval=1000)
    plc.patch({"X": 1})
    plc.step()  # scan 1
    # 10 idle scans
    for _ in range(10):
        plc.step()

    plc._scan_log.trim_before(2)  # trim the one patch entry

    assert plc._scan_log.bytes_estimate() == 0


def test_trim_before_realtime_rebases_dts():
    plc = PLC(logic=[], realtime=True)
    for _ in range(5):
        plc.step()  # scans 1–5

    log = plc._scan_log
    before_base = log.base_scan
    assert before_base == 0

    log.trim_before(3)

    assert log.base_scan == 3
    # The dts array should now start at index 0 == scan 3
    snap = log.snapshot()
    assert snap.base_scan == 3
    assert len(snap.dts) == 3  # scans 3, 4, 5

    # Appending a new scan after trim must use the new base
    log.record_dt(6, 0.012)
    assert len(log._dts) == 4  # index 6-3=3 → slot 3


def test_trim_before_drops_lifecycle_events():
    plc = _idle_plc()
    plc.step()  # scan 1
    plc.stop()  # lifecycle at_scan_id=2
    plc.step()  # scan 2

    snap_before = plc._scan_log.snapshot()
    assert len(snap_before.lifecycle_events) == 1
    assert snap_before.lifecycle_events[0].at_scan_id == 2

    plc._scan_log.trim_before(3)  # drop scan 2 lifecycle

    snap_after = plc._scan_log.snapshot()
    assert snap_after.lifecycle_events == ()


def test_trim_before_keeps_lifecycle_at_horizon():
    plc = _idle_plc()
    plc.step()  # scan 1
    plc.stop()  # lifecycle at_scan_id=2
    plc.step()  # scan 2

    plc._scan_log.trim_before(2)  # horizon = 2, event at_scan_id=2 — keep

    snap = plc._scan_log.snapshot()
    assert len(snap.lifecycle_events) == 1
