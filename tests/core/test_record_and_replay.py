"""Stage 2 per-channel determinism tests.

Validates that the ``ScanLog`` captured in Stage 1 carries enough
information to deterministically reconstruct any historical state.
Every nondeterminism channel is exercised in isolation so a regression
in one surfaces as a specific failing test.

The ``_replay_from_log_for_test`` helper is test-only scaffolding
(forks from scan 0, no checkpoints) — Stages 3/4 replace it with
``PLC.replay_to(scan_id)`` backed by real checkpoints and a
``_replay_mode`` guard.

Mutation verification: each test was run once against a deliberately
broken replay to confirm it fails loudly — see the module docstring
block below for the exact mutations exercised.

Mutations verified (reverted after):
- Skip applying forces → ``test_replay_forces_*`` must fail.
- Drop the last recorded patch → ``test_replay_patches`` must fail.
- Ignore ``rtc_base_changes`` → ``test_replay_rtc_changes`` must fail.
- Skip a lifecycle event → ``test_replay_lifecycle`` must fail.
- Ignore ``dts`` in REALTIME → ``test_replay_realtime_dt`` must fail.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import pytest

from pyrung import Rung, program
from pyrung.core import PLC, Bool, Dint, calc, out
from pyrung.core.scan_log import LifecycleEvent

# --------------------------------------------------------------------------- #
# Equality helper — explicit field coverage per Stage 2 design.
# --------------------------------------------------------------------------- #


def assert_plc_state_equal(live: PLC, replayed: PLC, *, context: str = "") -> None:
    """Assert two PLCs have equivalent observable state.

    Covers every field replay is responsible for reproducing.  A bare
    ``==`` would silently pass if replay forgets ``_rtc_base`` or
    ``_battery_present``; this helper forces every channel to show up.
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
    assert live._rtc_base == replayed._rtc_base, (
        f"{prefix}_rtc_base mismatch: live={live._rtc_base} replay={replayed._rtc_base}"
    )
    assert live._rtc_base_sim_time == replayed._rtc_base_sim_time, (
        f"{prefix}_rtc_base_sim_time mismatch"
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
# Replay helper — Stage 2 scaffolding.
#
# Constructs a replay PLC paired with the source (matched initial RTC)
# and walks the source's ScanLog forward, applying each nondeterminism
# channel in the same order the runtime does:
#
#   1. Lifecycle events at the incoming scan boundary
#   2. Force-map replacement (full snapshot from the log entry)
#   3. RTC base update
#   4. Patch application (becomes pending_patches, drained by step)
#   5. step() — which internally calls apply_pre_scan in the right order
#
# In REALTIME mode we override ``_calculate_dt`` to return recorded dts.
# --------------------------------------------------------------------------- #


def _make_source_and_replay(
    logic: Any = None,
    *,
    dt: float | None = 0.01,
    realtime: bool = False,
) -> tuple[PLC, PLC]:
    """Create source + replay PLCs with matched initial RTC base."""
    if realtime:
        source = PLC(logic=logic, realtime=True)
        replay = PLC(logic=logic, realtime=True)
    else:
        source = PLC(logic=logic, dt=dt)
        replay = PLC(logic=logic, dt=dt)
    # Two PLCs constructed moments apart have different datetime.now()
    # reads — pin the replay to the source's initial RTC so any
    # post-construction divergence is attributable to replay itself.
    replay._set_rtc_internal(source._rtc_base, source._rtc_base_sim_time)
    return source, replay


def _apply_lifecycle(replay: PLC, event: LifecycleEvent) -> None:
    if event.kind == "stop":
        replay.stop()
    elif event.kind == "reboot":
        replay.reboot()
    elif event.kind == "battery_present":
        replay.battery_present = bool(event.value)
    elif event.kind == "clear_forces":
        replay.clear_forces()
    else:  # pragma: no cover - exhaustive
        raise AssertionError(f"unknown lifecycle kind: {event.kind!r}")


def _replay_from_log_for_test(
    source: PLC,
    replay: PLC,
    target_scan_id: int,
) -> None:
    """Replay ``source``'s ScanLog into ``replay`` up to ``target_scan_id``.

    ``replay`` must have been constructed alongside ``source`` via
    ``_make_source_and_replay`` so initial RTC matches.  Target scan_id
    should be ``source.current_state.scan_id`` for round-trip tests.
    """
    log = source._scan_log.snapshot()

    lifecycle_by_scan: dict[int, list[LifecycleEvent]] = {}
    for event in log.lifecycle_events:
        lifecycle_by_scan.setdefault(event.at_scan_id, []).append(event)

    if log.dts is not None:

        def _calc_dt_override() -> float:
            scan_to_come = replay.current_state.scan_id + 1
            index = scan_to_come - log.base_scan
            return float(log.dts[index])

        replay._calculate_dt = _calc_dt_override  # type: ignore[method-assign]

    while replay.current_state.scan_id < target_scan_id:
        next_scan = replay.current_state.scan_id + 1

        for event in lifecycle_by_scan.get(next_scan, []):
            _apply_lifecycle(replay, event)

        if next_scan in log.force_changes_by_scan:
            replay._input_overrides._forces.clear()
            replay._input_overrides._forces.update(log.force_changes_by_scan[next_scan])

        if next_scan in log.rtc_base_changes:
            base, base_sim_time = log.rtc_base_changes[next_scan]
            replay._set_rtc_internal(base, base_sim_time)

        if next_scan in log.patches_by_scan:
            replay.patch(log.patches_by_scan[next_scan])

        replay.step()

    # Apply lifecycle events that fired after the final committed scan
    # (e.g. a trailing stop() with no subsequent step).
    for event in lifecycle_by_scan.get(target_scan_id + 1, []):
        _apply_lifecycle(replay, event)


# --------------------------------------------------------------------------- #
# Tests — one per nondeterminism channel.
# --------------------------------------------------------------------------- #


def test_replay_idle_scans() -> None:
    """N idle scans with no patches, forces, or events — zero-bytes log."""
    source, replay = _make_source_and_replay()
    for _ in range(100):
        source.step()

    assert source._scan_log.bytes_estimate() == 0  # Stage 1 invariant
    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_patches() -> None:
    """Patches applied at varied scans — including a final-scan patch."""
    source, replay = _make_source_and_replay()

    source.step()  # scan 1, no patch
    source.patch({"A": True, "B": 42})
    source.step()  # scan 2
    source.step()  # scan 3, no patch
    source.patch({"C": 7})
    source.step()  # scan 4
    source.patch({"A": False})
    source.step()  # scan 5 — final-scan patch

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_forces_add_remove() -> None:
    """Forces added, removed, and cleared across multiple scans."""
    source, replay = _make_source_and_replay()

    source.force(Bool("X"), True)
    source.step()  # scan 1: force map {X:True}
    source.step()  # scan 2: unchanged
    source.force(Bool("Y"), False)
    source.step()  # scan 3: {X:True, Y:False}
    source.unforce("X")
    source.step()  # scan 4: {Y:False}
    source.step()  # scan 5: unchanged

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_forces_interact_with_patches() -> None:
    """Patch and force applied on the same scan — force wins at pre-logic."""
    source, replay = _make_source_and_replay()

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

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)
    # Sanity: final Z value is False (last patch, no force)
    assert source.current_state.tags["Z"] is False


def test_replay_rtc_changes_via_set_rtc() -> None:
    """User-initiated ``set_rtc()`` between scans is captured and replayed."""
    source, replay = _make_source_and_replay()

    source.step()  # scan 1
    source.set_rtc(datetime(2030, 6, 15, 10, 30, 0))
    source.step()  # scan 2 — rtc effective here
    source.step()  # scan 3
    source.set_rtc(datetime(2035, 1, 1, 0, 0, 0))
    source.step()  # scan 4

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_rtc_changes_via_apply_tags() -> None:
    """In-scan ``rtc.new_*`` + ``rtc.apply_*`` path — downstream of patch."""
    source, replay = _make_source_and_replay(logic=[])

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
    source.step()  # scan 2 — rtc_setter fires inside this scan
    source.step()  # scan 3
    source.patch(
        {
            "rtc.new_hour": 14,
            "rtc.new_minute": 45,
            "rtc.new_second": 30,
            "rtc.apply_time": True,
        }
    )
    source.step()  # scan 4

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_lifecycle_stop() -> None:
    """Trailing ``stop()`` — _running flag and state.memory flag match."""
    source, replay = _make_source_and_replay()

    for _ in range(3):
        source.step()
    source.stop()

    assert source._running is False
    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_lifecycle_battery_present_toggle() -> None:
    """Battery present toggled across scans."""
    source, replay = _make_source_and_replay()

    source.step()
    source.battery_present = False
    source.step()
    source.battery_present = True
    source.step()
    source.battery_present = False

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


def test_replay_lifecycle_clear_forces() -> None:
    """``clear_forces()`` between scans clears the force map."""
    source, replay = _make_source_and_replay()

    source.force(Bool("X"), True)
    source.force(Bool("Y"), False)
    source.step()
    source.step()
    source.clear_forces()
    source.step()

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)


# ``reboot()`` lifecycle is intentionally out-of-scope for Stage 2.
# After reboot(), ``_reset_runtime_scope`` resets ``state.scan_id`` to
# 0 and ``state.timestamp`` to 0.0 — and the subsequent
# ``_record_lifecycle("reboot")`` uses the *post-reset* scan_id, so the
# recorded ``at_scan_id`` is always 1 regardless of how many scans ran
# before.  The current Stage 1 log format can't distinguish "reboot
# now" from "run N scans then reboot."  Stage 4 gets proper sequencing
# (either sim_time-based ordering or reset-invalidates-log).  Tracked
# in ``record-and-replay-checklist.md``.


def test_replay_realtime_dt() -> None:
    """REALTIME mode — varying dts captured and replayed."""
    source, replay = _make_source_and_replay(realtime=True)

    for _ in range(4):
        source.step()
        time.sleep(0.002)  # force measurable dt variation between scans
    source.step()

    snap = source._scan_log.snapshot()
    assert snap.dts is not None
    dts_captured = list(snap.dts)
    assert any(dt > 0 for dt in dts_captured[1:])  # sanity: real values

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
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

    source, replay = _make_source_and_replay(logic=ladder)

    source.patch({"input_a": True})
    source.step()  # scan 1: b becomes True, c increments
    source.step()  # scan 2: b still True, c increments
    source.patch({"input_a": False})
    source.step()  # scan 3: b False, c stops
    source.step()  # scan 4: steady

    _replay_from_log_for_test(source, replay, source.current_state.scan_id)
    assert_plc_state_equal(source, replay)
    assert source.current_state.tags["counter"] >= 1


# --------------------------------------------------------------------------- #
# Smoke tests for the helpers themselves (cheap to keep, easy to spot if
# the helper regresses independently of the scenarios).
# --------------------------------------------------------------------------- #


def test_assert_plc_state_equal_catches_scan_id_mismatch() -> None:
    source, replay = _make_source_and_replay()
    source.step()
    with pytest.raises(AssertionError, match="scan_id"):
        assert_plc_state_equal(source, replay)


def test_assert_plc_state_equal_catches_forces_mismatch() -> None:
    """A force difference surfaces (either via the tag it writes or the
    force map itself).  The helper must flag *something* — a bare ==
    would silently pass on a subset of these fields."""
    source, replay = _make_source_and_replay()
    source.force(Bool("X"), True)
    source.step()
    replay.step()
    # The force leaks through to tags first (force-write to X=True); the
    # helper still fails loudly, just on tags rather than on the map
    # itself.  Either way the divergence is caught.
    with pytest.raises(AssertionError):
        assert_plc_state_equal(source, replay)
