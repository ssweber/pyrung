"""Tests for PLC.debug.rung_trace retention API."""

from __future__ import annotations

import pytest

from pyrung.core import PLC, Bool, Program, Rung, out


def _runner_with_single_rung() -> PLC:
    light = Bool("Light")
    with Program(strict=False) as logic:
        with Rung():
            out(light)
    return PLC(logic)


def _run_debug_scan(runner: PLC) -> int:
    for _ in runner.debug.scan_steps_debug():
        pass
    return runner.current_state.scan_id


def test_rung_trace_returns_trace_for_fully_consumed_debug_scan() -> None:
    runner = _runner_with_single_rung()
    scan_id = _run_debug_scan(runner)

    trace = runner.debug.rung_trace(rung_id=0)
    assert trace.scan_id == scan_id
    assert trace.rung_id == 0
    assert isinstance(trace.events, tuple)
    assert [event.kind for event in trace.events] == ["rung", "instruction"]
    assert trace.events[-1].trace is not None


def test_rung_trace_overwrites_previous_debug_scan() -> None:
    runner = _runner_with_single_rung()
    _run_debug_scan(runner)  # scan 1
    scan_id = _run_debug_scan(runner)  # scan 2

    trace = runner.debug.rung_trace(rung_id=0)
    assert trace.scan_id == scan_id == 2


def test_rung_trace_raises_when_no_debug_scan_has_committed() -> None:
    runner = _runner_with_single_rung()

    with pytest.raises(KeyError) as exc:
        runner.debug.rung_trace(rung_id=0)
    assert exc.value.args == (0,)


def test_rung_trace_cleared_after_non_debug_step() -> None:
    runner = _runner_with_single_rung()
    _run_debug_scan(runner)  # scan 1 (debug commit)

    runner.step()  # scan 2 (non-debug commit wipes slot)

    with pytest.raises(KeyError) as exc:
        runner.debug.rung_trace(rung_id=0)
    assert exc.value.args == (0,)


def test_partial_debug_scan_does_not_store_trace() -> None:
    runner = _runner_with_single_rung()
    scan_gen = runner.debug.scan_steps_debug()
    first_step = next(scan_gen)

    assert first_step.kind == "rung"
    assert runner.current_state.scan_id == 0

    with pytest.raises(KeyError):
        runner.debug.rung_trace(rung_id=0)

    for _ in scan_gen:
        pass

    trace = runner.debug.rung_trace(rung_id=0)
    assert trace.scan_id == 1


def test_last_event_returns_inflight_step_during_partial_debug_scan() -> None:
    runner = _runner_with_single_rung()
    scan_gen = runner.debug.scan_steps_debug()
    first_step = next(scan_gen)

    assert first_step.kind == "rung"
    assert runner.current_state.scan_id == 0

    event_result = runner.debug.last_event()
    assert event_result is not None
    scan_id, rung_id, event = event_result
    assert scan_id == 1
    assert rung_id == 0
    assert event.kind == "rung"
    assert event.trace is not None


def test_last_event_returns_committed_rung_event_after_debug_scan() -> None:
    runner = _runner_with_single_rung()
    scan_gen = runner.debug.scan_steps_debug()
    next(scan_gen)
    for _ in scan_gen:
        pass

    event_result = runner.debug.last_event()
    assert event_result is not None
    scan_id, rung_id, event = event_result
    assert scan_id == 1
    assert rung_id == 0
    assert event.kind == "instruction"

    retained = runner.debug.rung_trace(rung_id=0)
    assert retained.events[-1] == event


def test_last_event_falls_back_to_last_committed_after_aborted_scan() -> None:
    runner = _runner_with_single_rung()
    _run_debug_scan(runner)  # scan 1 committed

    committed = runner.debug.last_event()
    assert committed is not None
    assert committed[0] == 1

    scan_gen = runner.debug.scan_steps_debug()
    next(scan_gen)
    inflight = runner.debug.last_event()
    assert inflight is not None
    assert inflight[0] == 2
    assert runner.current_state.scan_id == 1

    scan_gen.close()

    fallback = runner.debug.last_event()
    assert fallback is not None
    assert fallback[0] == 1
    assert runner.current_state.scan_id == 1


def test_last_event_is_none_after_aborted_scan_without_prior_committed_trace() -> None:
    runner = _runner_with_single_rung()
    scan_gen = runner.debug.scan_steps_debug()
    next(scan_gen)
    assert runner.debug.last_event() is not None

    scan_gen.close()
    assert runner.debug.last_event() is None


def test_last_event_clears_after_non_debug_step() -> None:
    runner = _runner_with_single_rung()
    _run_debug_scan(runner)

    committed = runner.debug.last_event()
    assert committed is not None
    assert committed[0] == 1

    runner.step()  # non-debug commit wipes trace slot

    assert runner.debug.last_event() is None
