"""Per-channel determinism tests routed through ``PLC.replay_to``.

Every nondeterminism channel is exercised in isolation so a regression
in one surfaces as a specific failing test.  The ``reboot()``
lifecycle is covered: reboot is treated as a destructive log reset
(Option B), so post-reboot replay anchors on the fresh log and the
pre-reboot era is not replay-addressable.

Mutation verification — each mutation confirmed to surface as the
listed failing test(s) against a deliberately broken ``replay_to``
(reverted after verifying):

- Skip applying forces → ``forces_add_remove`` +
  ``lifecycle_clear_forces`` + ``across_multiple_checkpoints`` +
  ``forces_across_checkpoint`` fail.
  (``forces_interact_with_patches`` passes because the force map is
  cleared by the test's end — redundant coverage from the others.)
- Drop the final patch (skip when ``scan_id == target_scan_id``) →
  ``patches`` + ``forces_interact_with_patches`` +
  ``rtc_changes_via_apply_tags`` + ``across_multiple_checkpoints``
  fail.
- Ignore ``rtc_base_changes`` → both ``rtc_changes_via_set_rtc`` and
  ``rtc_changes_via_apply_tags`` fail via the historical-RTC checks
  below.  The final effective-RTC equality alone is insensitive —
  ``fork()``'s RTC rebase at the anchor is mathematically equivalent
  to applying source's whole trajectory — so each RTC test also
  captures a mid-run RTC base and compares against
  ``replay_to(intermediate_scan)``.  The replay-mode guard blocks the
  in-scan RTC setter from reconstructing the base independently,
  making the per-channel RTC test load-bearing.
- Skip a lifecycle event → ``lifecycle_stop`` +
  ``lifecycle_battery_present_toggle`` fail.
  (``lifecycle_clear_forces`` passes on this mutation because the
  empty force map is also captured in ``force_changes_by_scan`` —
  redundant capture.)
- Ignore ``dts`` in REALTIME → ``realtime_dt`` fails.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from pyrung import Rung, program
from pyrung.core import PLC, Bool, Dint, calc, out

# --------------------------------------------------------------------------- #
# Equality helper — explicit field coverage for every replay channel.
# --------------------------------------------------------------------------- #


def assert_plc_state_equal(live: PLC, replayed: PLC, *, context: str = "") -> None:
    """Assert two PLCs have equivalent observable state.

    Covers every field replay is responsible for reproducing.  A bare
    ``==`` would silently pass if replay forgets ``_battery_present``
    or the live force map; this helper forces every channel to show
    up.

    RTC is compared via the effective "now" at the shared
    ``state.timestamp`` rather than raw ``_rtc_base`` /
    ``_rtc_base_sim_time``.  ``PLC.fork()`` rebases the internal
    representation when anchoring at a non-zero snapshot, which is
    semantically equivalent but not bit-identical.  What matters for
    correctness is what ``rtc_now`` returns.
    """
    prefix = f"[{context}] " if context else ""
    live_state = live.current_state
    replay_state = replayed.current_state

    assert live_state.scan_id == replay_state.scan_id, (
        f"{prefix}scan_id mismatch: live={live_state.scan_id} replay={replay_state.scan_id}"
    )
    assert live_state.timestamp == replay_state.timestamp, (
        f"{prefix}timestamp mismatch: live={live_state.timestamp} replay={replay_state.timestamp}"
    )
    assert dict(live_state.tags) == dict(replay_state.tags), (
        f"{prefix}tags mismatch\n  live:   {dict(live_state.tags)}\n"
        f"  replay: {dict(replay_state.tags)}"
    )
    assert dict(live_state.memory) == dict(replay_state.memory), (
        f"{prefix}memory mismatch\n  live:   {dict(live_state.memory)}\n"
        f"  replay: {dict(replay_state.memory)}"
    )
    live_rtc_now = live._rtc_at_sim_time(live_state.timestamp)
    replay_rtc_now = replayed._rtc_at_sim_time(replay_state.timestamp)
    assert live_rtc_now == replay_rtc_now, (
        f"{prefix}effective RTC mismatch: live={live_rtc_now} replay={replay_rtc_now}"
    )
    assert live._time_mode == replayed._time_mode, f"{prefix}_time_mode mismatch"
    assert live._dt == replayed._dt, f"{prefix}_dt mismatch"
    assert dict(live._input_overrides.forces_mutable) == dict(
        replayed._input_overrides.forces_mutable
    ), f"{prefix}force map mismatch"
    assert dict(live._input_overrides.pending_patches) == {}, (
        f"{prefix}live pending_patches should be empty post-commit"
    )
    assert dict(replayed._input_overrides.pending_patches) == {}, (
        f"{prefix}replay pending_patches should be empty post-commit"
    )
    assert live._running == replayed._running, f"{prefix}_running mismatch"
    assert live._battery_present == replayed._battery_present, f"{prefix}_battery_present mismatch"


# --------------------------------------------------------------------------- #
# Tests — one per nondeterminism channel, routed through replay_to.
# --------------------------------------------------------------------------- #


def test_replay_idle_scans() -> None:
    """N idle scans with no patches, forces, or events — zero-bytes log."""
    source = PLC(dt=0.01)
    for _ in range(100):
        source.step()

    assert source._scan_log.bytes_estimate() == 0  # sparse-by-field invariant
    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_patches() -> None:
    """Patches applied at varied scans — including a final-scan patch."""
    source = PLC(dt=0.01)

    source.step()  # scan 1, no patch
    source.patch({"A": True, "B": 42})
    source.step()  # scan 2
    source.step()  # scan 3, no patch
    source.patch({"C": 7})
    source.step()  # scan 4
    source.patch({"A": False})
    source.step()  # scan 5 — final-scan patch

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_forces_add_remove() -> None:
    """Forces added, removed, and cleared across multiple scans."""
    source = PLC(dt=0.01)

    source.force(Bool("X"), True)
    source.step()  # scan 1: force map {X:True}
    source.step()  # scan 2: unchanged
    source.force(Bool("Y"), False)
    source.step()  # scan 3: {X:True, Y:False}
    source.unforce("X")
    source.step()  # scan 4: {Y:False}
    source.step()  # scan 5: unchanged

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_forces_interact_with_patches() -> None:
    """Patch and force applied on the same scan — force wins at pre-logic."""
    source = PLC(dt=0.01)

    source.step()  # scan 1
    source.force(Bool("Z"), True)
    source.patch({"Z": False})  # force will overwrite at pre-logic
    source.step()  # scan 2
    source.step()  # scan 3
    source.patch({"Z": False})  # force still wins
    source.step()  # scan 4
    source.unforce("Z")
    source.patch({"Z": False})  # now the patch sticks
    source.step()  # scan 5

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)
    # Sanity: final Z value is False (last patch, no force)
    assert source.current_state.tags["Z"] is False


def test_replay_rtc_changes_via_set_rtc() -> None:
    """User-initiated ``set_rtc()`` between scans is captured and replayed.

    A final-state effective-RTC check alone is insensitive to mutation
    3 (ignore ``rtc_base_changes``) because fork()'s RTC rebase at the
    anchor is mathematically equivalent to applying source's whole
    RTC trajectory.  To catch mutation 3 we also verify the
    *intermediate* historical RTC at scan 2: source's current
    ``_rtc_base`` has since been overwritten, so the historical value
    can only be reconstructed by applying the recorded base change.
    """
    source = PLC(dt=0.01)

    source.step()  # scan 1
    source.set_rtc(datetime(2030, 6, 15, 10, 30, 0))
    source.step()  # scan 2 — rtc effective here (base set at sim_time 0.01)
    source.step()  # scan 3
    source.set_rtc(datetime(2035, 1, 1, 0, 0, 0))
    source.step()  # scan 4

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)

    # Historical RTC at scan 2: base was 2030-06-15 10:30:00 set at
    # sim_time 0.01; scan 2's state.timestamp is 0.02, so the
    # effective RTC is 10ms after the base.
    replay_2 = source.replay_to(2)
    expected_at_2 = datetime(2030, 6, 15, 10, 30, 0, 10_000)
    assert replay_2._rtc_at_sim_time(replay_2.current_state.timestamp) == expected_at_2


def test_replay_rtc_changes_via_apply_tags() -> None:
    """In-scan ``rtc.new_*`` + ``rtc.apply_*`` path — downstream of patch.

    The ``_replay_mode`` guard on ``_set_rtc_and_record`` is
    load-bearing here: the in-scan ``_apply_rtc_date/time`` path no
    longer reconstructs the base independently — the log's
    ``rtc_base_changes`` entry is authoritative.
    """
    source = PLC(logic=[], dt=0.01)

    source.step()  # scan 1
    # Patch the "new date" tags and the apply flag; the in-scan logic
    # in SystemPointRuntime will call _rtc_setter -> _set_rtc_and_record.
    source.patch(
        {
            "rtc.new_year4": 2040,
            "rtc.new_month": 3,
            "rtc.new_day": 15,
            "rtc.apply_date": True,
        }
    )
    # The patch drains at scan 2's commit, so ``apply_date=True`` is
    # visible to scan 3's ``on_scan_start`` — that's where
    # ``_rtc_setter`` actually fires (not scan 2).
    source.step()  # scan 2 — patch commits; apply_date now visible for scan 3
    source.step()  # scan 3 — rtc_setter fires in on_scan_start of this scan
    # Capture post-apply-date RTC base for the historical check below
    # (before the scan-5 apply_time overwrites hour/minute/second).
    rtc_base_after_scan_3 = source._rtc_base
    source.patch(
        {
            "rtc.new_hour": 14,
            "rtc.new_minute": 45,
            "rtc.new_second": 30,
            "rtc.apply_time": True,
        }
    )
    source.step()  # scan 4 — apply_time patch commits
    source.step()  # scan 5 — apply_time fires in on_scan_start

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)

    # Historical check: at scan 3, source's ``_rtc_base`` had 2040-03-15
    # as the date but construction-time H/M/S.  After scan 5 the H/M/S
    # were overwritten to 14/45/30 — so asking source.rtc_now at scan 3
    # today yields the wrong answer.  Replay must reconstruct the
    # historical base; mutation 3 (ignore rtc_base_changes) would leave
    # replay with only fork()'s rebase of source's *current* base, whose
    # hour/minute/second differ from the scan-3 snapshot.
    replay_3 = source.replay_to(3)
    # replay_3._rtc_base at scan 3 should equal what source had then.
    # _rtc_base_sim_time == 0.02 (ctx.timestamp at scan 3's on_scan_start).
    assert replay_3._rtc_base == rtc_base_after_scan_3
    assert replay_3._rtc_base_sim_time == 0.02


def test_replay_lifecycle_stop() -> None:
    """Trailing ``stop()`` — _running flag and state.memory flag match."""
    source = PLC(dt=0.01)

    for _ in range(3):
        source.step()
    source.stop()

    assert source._running is False
    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_lifecycle_battery_present_toggle() -> None:
    """Battery present toggled across scans."""
    source = PLC(dt=0.01)

    source.step()
    source.battery_present = False
    source.step()
    source.battery_present = True
    source.step()
    source.battery_present = False

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_lifecycle_clear_forces() -> None:
    """``clear_forces()`` between scans clears the force map."""
    source = PLC(dt=0.01)

    source.force(Bool("X"), True)
    source.force(Bool("Y"), False)
    source.step()
    source.step()
    source.clear_forces()
    source.step()

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_lifecycle_reboot() -> None:
    """Reboot resets the log; replay anchors on the fresh post-reboot era.

    Pre-reboot scans are not replay-addressable (the log is cleared).
    Post-reboot replay works because ``reboot()`` sets up a fresh
    recording session rooted at the new scan 0, and ``replay_to`` on
    any post-reboot scan_id falls back to ``fork(scan_id=0)`` against
    the fresh history when no checkpoint yet exists.
    """
    source = PLC(dt=0.01)

    for _ in range(5):
        source.step()
    assert source.current_state.scan_id == 5

    source.reboot()
    # Option B semantics: log and checkpoints reset with the reboot.
    assert source._scan_log.bytes_estimate() == 0
    assert source._checkpoints == {}
    assert source.current_state.scan_id == 0

    for _ in range(3):
        source.step()
    assert source.current_state.scan_id == 3

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_realtime_dt() -> None:
    """REALTIME mode — varying dts captured and replayed."""
    source = PLC(realtime=True)

    for _ in range(4):
        source.step()
        time.sleep(0.002)  # force measurable dt variation between scans
    source.step()

    snap = source._scan_log.snapshot()
    assert snap.dts is not None
    dts_captured = list(snap.dts)
    assert any(dt > 0 for dt in dts_captured[1:])  # sanity: real values

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_with_logic_present() -> None:
    """Replay works when rungs drive scan behavior, not just idle."""

    a = Bool("input_a")
    b = Bool("output_b")
    c = Dint("counter", default=0)

    @program
    def ladder() -> None:
        with Rung(a):
            out(b)
        with Rung(b):
            calc(c + 1, c)

    source = PLC(logic=ladder, dt=0.01)

    source.patch({"input_a": True})
    source.step()  # scan 1: b becomes True, c increments
    source.step()  # scan 2: b still True, c increments
    source.patch({"input_a": False})
    source.step()  # scan 3: b False, c stops
    source.step()  # scan 4: steady

    replay = source.replay_to(source.current_state.scan_id)
    assert_plc_state_equal(source, replay)
    assert source.current_state.tags["counter"] >= 1


# --------------------------------------------------------------------------- #
# Smoke tests for the helper itself.
# --------------------------------------------------------------------------- #


def test_assert_plc_state_equal_catches_scan_id_mismatch() -> None:
    source = PLC(dt=0.01)
    source.step()  # scan 1
    replay = source.replay_to(0)  # anchor at scan 0 — different scan_id
    with pytest.raises(AssertionError, match="scan_id"):
        assert_plc_state_equal(source, replay)


def test_assert_plc_state_equal_catches_forces_mismatch() -> None:
    """Build a replay that disagrees on forces — helper must flag it."""
    source = PLC(dt=0.01)
    source.force(Bool("X"), True)
    source.step()
    replay = source.replay_to(source.current_state.scan_id)
    # Mutate the replay's force map to synthesize a mismatch — the
    # helper must notice either via the force map itself or via tags.
    replay._input_overrides._forces.clear()
    with pytest.raises(AssertionError):
        assert_plc_state_equal(source, replay)


# --------------------------------------------------------------------------- #
# replay_to with multi-checkpoint anchoring.
# --------------------------------------------------------------------------- #


def test_replay_across_multiple_checkpoints() -> None:
    """Exercise replay_to anchoring on real checkpoints (not just scan 0).

    Runs past three checkpoint boundaries with a steady force and a
    patch applied at scan 13, then verifies both a full replay_to(tip)
    and a mid-range replay_to(13) reconstruct correctly.
    """
    source = PLC(dt=0.01, checkpoint_interval=5)
    source.force(Bool("X"), True)
    for _ in range(12):
        source.step()  # scan 1..12
    source.patch({"A": 1})
    source.step()  # scan 13 with a patch, post-second checkpoint
    for _ in range(5):
        source.step()  # scan 14..18

    assert source.current_state.scan_id == 18
    assert set(source._checkpoints.keys()) == {5, 10, 15}

    # Full-tip replay anchors on checkpoint 15 (nearest <= 18).
    replay = source.replay_to(18)
    assert replay.current_state.scan_id == 18
    assert_plc_state_equal(source, replay, context="replay_to(18)")

    # Mid-range replay to scan 13 anchors on checkpoint 10 and walks
    # 11, 12, 13 — including the patch at scan 13.  Compared against
    # a fresh source run to exactly scan 13; fresh's RTC is seeded
    # from source so the two trajectories are observationally
    # identical (avoiding the construction-time datetime.now() drift).
    replay_13 = source.replay_to(13)
    assert replay_13.current_state.scan_id == 13
    fresh = PLC(dt=0.01, checkpoint_interval=5)
    fresh._set_rtc_internal(source._rtc_base, source._rtc_base_sim_time)
    fresh.force(Bool("X"), True)
    for _ in range(12):
        fresh.step()
    fresh.patch({"A": 1})
    fresh.step()
    assert_plc_state_equal(fresh, replay_13, context="replay_to(13)")


def test_replay_to_rejects_invalid_target() -> None:
    source = PLC(dt=0.01)
    for _ in range(3):
        source.step()

    with pytest.raises(ValueError, match="must be >= 0"):
        source.replay_to(-1)
    with pytest.raises(ValueError, match="beyond current tip"):
        source.replay_to(source.current_state.scan_id + 1)


def test_replay_fork_is_in_replay_mode() -> None:
    """The returned fork has ``_replay_mode=True`` so further steps
    would still suppress monitors/breakpoints unless cleared."""
    source = PLC(dt=0.01)
    source.step()
    replay = source.replay_to(1)
    assert replay._replay_mode is True
    assert source._replay_mode is False  # parent unaffected


# --------------------------------------------------------------------------- #
# Checkpoints + force-map bypass invariant (replay correctness invariant).
# --------------------------------------------------------------------------- #


def test_replay_forces_across_checkpoint() -> None:
    """Checkpoints unconditionally write the full force map to the log.

    Protects the replay correctness invariant: at a checkpoint scan the
    force diff-guard must be bypassed, otherwise a replay that starts at
    that checkpoint has no force entry to read and silently loses the
    force map.  Exercised with a force set at scan 1 and held steady
    past two checkpoint boundaries (5 and 10) — the diff-guard would
    elide scans 5 and 10 without the bypass.
    """
    source = PLC(dt=0.01, checkpoint_interval=5)

    source.force(Bool("X"), True)
    for _ in range(12):  # scan 1..12
        source.step()

    assert source.current_state.scan_id == 12

    # Checkpoints retained, each carrying the correct SystemState.
    assert set(source._checkpoints.keys()) == {5, 10}
    assert source._checkpoints[5].scan_id == 5
    assert source._checkpoints[10].scan_id == 10

    # Helper lookup.
    assert source._nearest_checkpoint_at_or_before(4) is None
    assert source._nearest_checkpoint_at_or_before(5) == 5
    assert source._nearest_checkpoint_at_or_before(7) == 5
    assert source._nearest_checkpoint_at_or_before(10) == 10
    assert source._nearest_checkpoint_at_or_before(999) == 10

    # The invariant: force_changes_by_scan has entries at both
    # checkpoint scans even though the force map never changed after
    # scan 1.  A reintroduced diff-guard at checkpoints would drop 5
    # and 10 from this dict.
    snap = source._scan_log.snapshot()
    assert set(snap.force_changes_by_scan.keys()) == {1, 5, 10}
    assert dict(snap.force_changes_by_scan[1]) == {"X": True}
    assert dict(snap.force_changes_by_scan[5]) == {"X": True}
    assert dict(snap.force_changes_by_scan[10]) == {"X": True}

    # Route the invariant check through the public replay_to API.  Without
    # the unconditional checkpoint write, anchor=10 would not find {"X": True}
    # in ``force_changes_by_scan[10]`` and the force map would be lost.
    anchor = source._nearest_checkpoint_at_or_before(12)
    assert anchor == 10
    replay = source.replay_to(12)
    assert dict(replay._input_overrides.forces_mutable) == {"X": True}
    assert dict(replay.current_state.tags) == dict(source.current_state.tags)
    assert replay.current_state.scan_id == source.current_state.scan_id


def test_checkpoint_interval_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="checkpoint_interval"):
        PLC(checkpoint_interval=0)
    with pytest.raises(ValueError, match="checkpoint_interval"):
        PLC(checkpoint_interval=-1)


def test_checkpoints_cleared_on_fork() -> None:
    """Fork resets the log and checkpoints together — a fork is a fresh
    recording session rooted at the chosen scan."""
    source = PLC(dt=0.01, checkpoint_interval=5)
    for _ in range(12):
        source.step()
    assert set(source._checkpoints.keys()) == {5, 10}

    fork = source.fork(scan_id=10)
    assert fork._checkpoints == {}
    assert fork._checkpoint_interval == 5  # propagates through fork
    # Forked PLC accumulates its own checkpoints starting fresh.
    for _ in range(6):  # scan 11..16
        fork.step()
    assert set(fork._checkpoints.keys()) == {15}


# ---------------------------------------------------------------------------
# _trim_history_before coordinator tests
# ---------------------------------------------------------------------------


def test_trim_history_before_rejects_replay_below_horizon() -> None:
    """After trimming, replay_to for scans below the horizon must raise."""
    plc = PLC(dt=0.01, checkpoint_interval=5)
    for _ in range(10):  # scans 1–10; checkpoints at 5, 10
        plc.step()

    plc._trim_history_before(5)

    # scan 4 is below the horizon — initial_scan_id advanced to 5
    with pytest.raises(ValueError, match="must be >= 5"):
        plc.replay_to(4)


def test_trim_history_before_allows_replay_at_horizon() -> None:
    """replay_to at exactly the horizon scan must succeed when a checkpoint
    at that scan survived the trim."""
    plc = PLC(dt=0.01, checkpoint_interval=5)
    for _ in range(10):  # scans 1–10; checkpoints at 5, 10
        plc.step()

    plc._trim_history_before(5)

    # scan 5 is the horizon AND a checkpoint — must succeed
    assert plc.replay_to(5).current_state.scan_id == 5
    # scan 7 is above the horizon
    assert plc.replay_to(7).current_state.scan_id == 7


def test_trim_history_before_prunes_checkpoints() -> None:
    plc = PLC(dt=0.01, checkpoint_interval=5)
    for _ in range(15):  # checkpoints at 5, 10, 15
        plc.step()

    plc._trim_history_before(10)

    assert 5 not in plc._checkpoints
    assert 10 in plc._checkpoints
    assert 15 in plc._checkpoints


def test_trim_history_before_trims_firings_in_lockstep() -> None:
    """The rung-firing timelines must trim alongside the log."""
    plc = PLC(logic=[], dt=0.01, checkpoint_interval=20)
    for _ in range(10):
        plc.step()

    assert plc._scan_log.base_scan == 0
    plc._trim_history_before(5)

    assert plc._scan_log.base_scan == 5
