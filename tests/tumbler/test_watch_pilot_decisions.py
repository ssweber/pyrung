"""Fast contracts for the process-isolated PILOT performance watcher."""

from __future__ import annotations

import multiprocessing as mp
import time
from types import SimpleNamespace
from typing import Any

from devtools.watch_pilot_decisions import (
    EXIT_TIMEOUT,
    _decision_lines,
    _target,
    _target_condition,
    watch_worker,
)
from pyrung import Bool, Int


def _silent_worker(messages: Any) -> None:
    messages.put({"type": "started", "elapsed": 0.01, "scan": 1})
    time.sleep(10.0)


def _responsive_worker(messages: Any) -> None:
    messages.put({"type": "started", "elapsed": 0.01, "scan": 1})
    messages.put(
        {
            "type": "event",
            "elapsed": 0.02,
            "index": 1,
            "kind": "started",
            "scan": 1,
            "rendered": "Finding a way...\n",
            "decision_lines": (),
            "context": "state=1 step=0",
        }
    )
    messages.put(
        {
            "type": "result",
            "status": "reached",
            "elapsed": 0.03,
            "events": 1,
            "scan": 1,
            "reason": None,
        }
    )


def _internally_busy_but_invisible_worker(messages: Any) -> None:
    messages.put({"type": "started", "elapsed": 0.01, "scan": 1})
    started = time.monotonic()
    index = 0
    while time.monotonic() - started < 1.0:
        index += 1
        messages.put(
            {
                "type": "event",
                "elapsed": time.monotonic() - started,
                "index": index,
                "kind": "iteration",
                "scan": index,
                "rendered": None,
                "decision_lines": (),
                "context": "state=6 step=101",
            }
        )
        time.sleep(0.02)


def _watch(
    process_target: Any,
    *,
    stall_budget_s: float,
    output_budget_s: float = 1.0,
    memory_budget_mb: int | None = None,
) -> int:
    context = mp.get_context("spawn")
    messages = context.Queue()
    dump_request = context.Event()
    dump_ready = context.Event()
    worker_args = (
        (messages,)
        if process_target
        in {_silent_worker, _responsive_worker, _internally_busy_but_invisible_worker}
        else ()
    )
    process = context.Process(target=process_target, args=worker_args)
    process.start()
    try:
        return watch_worker(
            process,
            messages,
            dump_request,
            dump_ready,
            wall_budget_s=2.0,
            stall_budget_s=stall_budget_s,
            output_budget_s=output_budget_s,
            memory_budget_mb=memory_budget_mb,
            dump_grace_s=0.01,
        )
    finally:
        messages.close()
        messages.join_thread()


def test_target_accepts_dap_style_boolean_and_explicit_value() -> None:
    assert _target("y_BurnerLoop") == ("y_BurnerLoop", True)
    assert _target("Sts_StateCurrent=17") == ("Sts_StateCurrent", 17)


def test_target_condition_preserves_boolean_tag_semantics() -> None:
    ready = Bool("WatchReady")
    state = Int("WatchState")

    assert _target_condition(ready, True) is ready
    assert repr(_target_condition(ready, False)) == "~WatchReady"
    assert repr(_target_condition(state, 17)) == "WatchState == 17"


def test_decision_receipt_names_state_step_and_tripwire() -> None:
    event = SimpleNamespace(
        kind="candidates_built",
        scan=42,
        data={
            "candidates": ({"tag": "Danger", "value": True},),
            "trace_actions": (),
            "route_candidates": (),
            "prerequisite_pilot_rungs": (),
            "trace_action_details": (),
        },
    )

    lines, stopped = _decision_lines(
        event,
        {"Sts_StateCurrent": 6, "Internal__Step": 101},
        ("Danger", True),
    )

    assert stopped is True
    assert "scan=42 state=6 step=101" in lines[0]
    assert lines[-1] == "[stop] candidate construction surfaced ('Danger', True)"


def test_parent_kills_a_worker_that_withholds_the_next_event(capsys: Any) -> None:
    code = _watch(_silent_worker, stall_budget_s=0.1)

    assert code == EXIT_TIMEOUT
    error = capsys.readouterr().err
    assert "no structured PILOT event for 0.1s" in error
    assert "no PILOT event was received" in error


def test_parent_accepts_a_responsive_reached_worker(capsys: Any) -> None:
    code = _watch(_responsive_worker, stall_budget_s=1.0)

    assert code == 0
    output = capsys.readouterr().out
    assert "Finding a way..." in output
    assert "[watch] reached" in output
    assert "max_event_gap=" in output


def test_parent_bounds_user_visible_silence_even_with_internal_events(capsys: Any) -> None:
    code = _watch(
        _internally_busy_but_invisible_worker,
        stall_budget_s=0.2,
        output_budget_s=0.1,
    )

    assert code == EXIT_TIMEOUT
    error = capsys.readouterr().err
    assert "no DAP-visible progress for 0.1s" in error
    assert "last event:" in error


def test_parent_bounds_worker_memory(capsys: Any) -> None:
    code = _watch(
        _silent_worker,
        stall_budget_s=1.0,
        output_budget_s=1.0,
        memory_budget_mb=1,
    )

    assert code == EXIT_TIMEOUT
    error = capsys.readouterr().err
    assert "exceeded the 1 MB memory budget" in error
    assert "worker RSS: current=" in error
