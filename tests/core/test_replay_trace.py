"""Historical rung-trace reconstruction via ``PLC.replay_trace_at``.

``replay_trace_at`` regenerates per-rung traces for a historical scan
by replaying to ``target_scan_id - 1`` on the plain path and driving
``_scan_steps_debug`` for the final scan.  The ``_replay_mode`` guards
(monitors, breakpoints, in-scan RTC setter) suppress side effects on
both paths — commit is shared between ``_scan_steps`` and
``_scan_steps_debug``.

Also covers the one-slot cache (``_cached_replay_trace``), which hits
on repeat queries for the same ``target_scan_id`` and invalidates on
any tip advance.
"""

from __future__ import annotations

import pytest

from pyrung import Rung, program
from pyrung.core import PLC, Bool, Dint, calc, out
from pyrung.core.debug_trace import RungTrace


def _build_source(cycles: int = 6) -> PLC:
    """Construct a PLC with a small ladder and run ``cycles`` scans."""

    a = Bool("input_a")
    b = Bool("output_b")
    c = Dint("counter", default=0)

    @program
    def ladder() -> None:
        with Rung(a):
            out(b)
        with Rung(b):
            calc(c + 1, c)

    plc = PLC(logic=ladder, dt=0.01)
    plc.patch({"input_a": True})
    for _ in range(cycles):
        plc.step()
    return plc


def test_replay_trace_at_returns_traces_for_historical_scan() -> None:
    source = _build_source(cycles=6)
    target = 3

    traces = source.replay_trace_at(target)

    assert traces, "replay_trace_at returned no traces for a rung-driven program"
    for rung_id, trace in traces.items():
        assert isinstance(rung_id, int)
        assert isinstance(trace, RungTrace)
        assert trace.scan_id == target, (
            f"trace.scan_id {trace.scan_id} does not match target {target}"
        )
        assert trace.rung_id == rung_id
        assert trace.events, f"trace for rung {rung_id} has no events"


def test_replay_trace_at_does_not_disturb_live_state() -> None:
    source = _build_source(cycles=6)

    before_state = source.current_state
    before_playhead = source.playhead
    before_cache_ids = list(source._recent_state_cache.keys())
    before_snapshot = source._scan_log.snapshot()
    before_traces = dict(source._current_rung_traces)
    before_traces_scan_id = source._current_rung_traces_scan_id

    source.replay_trace_at(3)

    assert source.current_state is before_state
    assert source.playhead == before_playhead
    assert list(source._recent_state_cache.keys()) == before_cache_ids
    after_snapshot = source._scan_log.snapshot()
    assert after_snapshot.base_scan == before_snapshot.base_scan
    assert dict(after_snapshot.patches_by_scan) == dict(before_snapshot.patches_by_scan)
    assert dict(after_snapshot.force_changes_by_scan) == dict(before_snapshot.force_changes_by_scan)
    assert dict(after_snapshot.rtc_base_changes) == dict(before_snapshot.rtc_base_changes)
    assert after_snapshot.lifecycle_events == before_snapshot.lifecycle_events
    assert source._current_rung_traces == before_traces
    assert source._current_rung_traces_scan_id == before_traces_scan_id


def test_replay_trace_at_validates_target_scan_id() -> None:
    source = _build_source(cycles=4)

    # Initial scan has no traces — the fork anchor was never stepped.
    with pytest.raises(ValueError, match="must be >"):
        source.replay_trace_at(source._initial_scan_id)

    with pytest.raises(ValueError, match="must be >"):
        source.replay_trace_at(source._initial_scan_id - 1)

    with pytest.raises(ValueError, match="beyond current tip"):
        source.replay_trace_at(source.current_state.scan_id + 1)


def test_replay_trace_at_one_slot_cache_hits_on_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source(cycles=6)

    call_count = 0
    original = source._build_replay_fork

    def counting(anchor: int | None):
        nonlocal call_count
        call_count += 1
        return original(anchor)

    monkeypatch.setattr(source, "_build_replay_fork", counting)

    first = source.replay_trace_at(3)
    assert call_count == 1

    second = source.replay_trace_at(3)
    assert call_count == 1, "cache hit should skip the replay fork"

    # The cache must hand out independent dicts so caller mutation
    # doesn't corrupt a subsequent hit.
    assert first is not second
    assert first == second


def test_replay_trace_at_cache_invalidates_on_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _build_source(cycles=6)

    call_count = 0
    original = source._build_replay_fork

    def counting(anchor: int | None):
        nonlocal call_count
        call_count += 1
        return original(anchor)

    monkeypatch.setattr(source, "_build_replay_fork", counting)

    source.replay_trace_at(3)
    assert call_count == 1
    assert source._cached_replay_trace is not None
    assert source._cached_replay_trace[0] == 3

    source.step()
    assert source._cached_replay_trace is None, "step must invalidate cache"

    source.replay_trace_at(3)
    assert call_count == 2, "post-step query must re-fork"
