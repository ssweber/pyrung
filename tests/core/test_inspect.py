"""Tests for PLCRunner.inspect rung-trace retention API."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, PLCRunner, Program, Rung, out


def _runner_with_single_rung(*, history_limit: int | None = None) -> PLCRunner:
    light = Bool("Light")
    with Program(strict=False) as logic:
        with Rung():
            out(light)
    return PLCRunner(logic, history_limit=history_limit)


def _run_debug_scan(runner: PLCRunner) -> int:
    for _ in runner.scan_steps_debug():
        pass
    return runner.current_state.scan_id


def test_inspect_returns_trace_for_fully_consumed_debug_scan() -> None:
    runner = _runner_with_single_rung()
    scan_id = _run_debug_scan(runner)

    trace = runner.inspect(rung_id=0)
    assert trace.scan_id == scan_id
    assert trace.rung_id == 0
    assert isinstance(trace.events, tuple)
    assert [event.kind for event in trace.events] == ["instruction", "rung"]
    assert trace.events[-1].trace is not None


def test_inspect_defaults_to_playhead_when_scan_id_omitted() -> None:
    runner = _runner_with_single_rung()
    _run_debug_scan(runner)  # scan 1
    _run_debug_scan(runner)  # scan 2

    runner.seek(1)
    trace = runner.inspect(rung_id=0)

    assert trace.scan_id == 1
    assert runner.current_state.scan_id == 2


def test_inspect_accepts_explicit_scan_id_lookup() -> None:
    runner = _runner_with_single_rung()
    _run_debug_scan(runner)  # scan 1
    scan_id = _run_debug_scan(runner)  # scan 2

    trace = runner.inspect(rung_id=0, scan_id=scan_id)
    assert trace.scan_id == 2
    assert trace.rung_id == 0


def test_inspect_raises_key_error_for_missing_scan() -> None:
    runner = _runner_with_single_rung()
    _run_debug_scan(runner)

    with pytest.raises(KeyError) as exc:
        runner.inspect(rung_id=0, scan_id=99)
    assert exc.value.args == (99,)


def test_inspect_raises_rung_key_error_when_scan_has_no_debug_trace() -> None:
    runner = _runner_with_single_rung()
    runner.step()

    with pytest.raises(KeyError) as exc:
        runner.inspect(rung_id=0, scan_id=1)
    assert exc.value.args == (0,)


def test_inspect_prunes_trace_data_when_history_eviction_occurs() -> None:
    runner = _runner_with_single_rung(history_limit=3)

    for _ in range(4):
        _run_debug_scan(runner)

    with pytest.raises(KeyError) as exc:
        runner.inspect(rung_id=0, scan_id=1)
    assert exc.value.args == (1,)

    retained = runner.inspect(rung_id=0, scan_id=4)
    assert retained.scan_id == 4
    assert set(runner._rung_traces_by_scan) == {2, 3, 4}


def test_scan_steps_debug_partial_consumption_does_not_store_inspect_trace() -> None:
    runner = _runner_with_single_rung()
    scan_gen = runner.scan_steps_debug()
    first_step = next(scan_gen)

    assert first_step.kind == "instruction"
    assert runner.current_state.scan_id == 0

    with pytest.raises(KeyError) as exc:
        runner.inspect(rung_id=0, scan_id=1)
    assert exc.value.args == (1,)

    for _ in scan_gen:
        pass

    trace = runner.inspect(rung_id=0, scan_id=1)
    assert trace.scan_id == 1
