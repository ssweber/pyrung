"""Execution-flow ownership for DAP stepping/continue behavior.

Owns step, continue, and pause execution control paths.
Must preserve stop reasons, queue timing, and thread cleanup semantics.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from pyrung.core.runner import ScanStep

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


def on_next(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        adapter._assert_can_step_locked()
        adapter._step_until(
            lambda step: step is None or step.kind == "rung",
        )
    return {}, [("stopped", adapter._stopped_body("step"))]


def on_step_in(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        adapter._assert_can_step_locked()
        adapter._step_until(
            lambda step: step is None or step.kind not in {"branch", "rung", "subroutine"},
        )
    return {}, [("stopped", adapter._stopped_body("step"))]


def on_step_out(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        adapter._assert_can_step_locked()
        origin_step = adapter._current_step
        origin_ctx = adapter._current_ctx
        if origin_step is None:
            adapter._step_until(lambda _step: True)
        else:
            origin_depth = origin_step.depth
            origin_stack_len = len(origin_step.call_stack)
            adapter._step_until(
                lambda step: (
                    step is None
                    or len(step.call_stack) < origin_stack_len
                    or step.depth < origin_depth
                    or adapter._current_ctx is not origin_ctx
                ),
            )
    return {}, [("stopped", adapter._stopped_body("step"))]


def on_pyrung_step_scan(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        adapter._assert_can_step_locked()
        origin_ctx = adapter._current_ctx

        if origin_ctx is None:
            if not adapter._advance_with_step_logpoints_locked():
                return {}, [("stopped", adapter._stopped_body("step"))]
            origin_ctx = adapter._current_ctx

        while adapter._current_ctx is origin_ctx:
            if not adapter._advance_with_step_logpoints_locked():
                break

    return {}, [("stopped", adapter._stopped_body("step"))]


def on_continue(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        adapter._require_runner_locked()
        if not adapter._thread_running_locked():
            adapter._pause_event.clear()
            thread = threading.Thread(
                target=adapter._continue_worker,
                daemon=True,
                name="pyrung-dap-continue",
            )
            adapter._continue_thread = thread
            thread.start()
    return {"allThreadsContinued": True}, []


def on_pause(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    adapter._pause_event.set()
    return {}, []


def continue_worker(adapter: Any) -> None:
    from pyrung.dap.session import ScanFrameBuffer

    last_emit_time = 0.0

    def _emit_scan_frame(*, force: bool = False) -> None:
        nonlocal last_emit_time
        if not adapter._configuration_done:
            return
        now = time.monotonic()
        if not force and (now - last_emit_time) < 0.033:
            return

        buffer = adapter._session.scan_frame_buffer
        if buffer is None:
            return

        with adapter._state_lock:
            trace_body = adapter._live_trace_body_locked()
            if trace_body is None and not force:
                return
            current_tags = dict(adapter._runner.current_state.tags)

        changes: list[dict[str, Any]] = []
        prev = buffer.previous_tags
        for tag_name in set(prev.keys()) | set(current_tags.keys()):
            old_val = prev.get(tag_name)
            new_val = current_tags.get(tag_name)
            if old_val != new_val:
                changes.append(
                    {
                        "tag": tag_name,
                        "previous": adapter._format_value(old_val),
                        "current": adapter._format_value(new_val),
                    }
                )

        frame_body: dict[str, Any] = {
            "scanId": trace_body.get("scanId") if trace_body else None,
            "trace": trace_body,
            "monitors": buffer.monitors,
            "changes": changes,
            "snapshots": buffer.snapshots,
            "outputs": buffer.outputs,
        }

        adapter._enqueue_internal_event("pyrungScanFrame", frame_body)
        last_emit_time = now

        buffer.monitors = []
        buffer.snapshots = []
        buffer.outputs = []
        buffer.previous_tags = current_tags

    try:
        adapter._pending_predicate_pause = False
        with adapter._state_lock:
            runner = adapter._runner
            if runner is None:
                return
            adapter._session.scan_frame_buffer = ScanFrameBuffer(
                previous_tags=dict(runner.current_state.tags),
            )

        while not adapter._stop_event.is_set():
            if adapter._pause_event.is_set():
                _emit_scan_frame(force=True)
                adapter._enqueue_internal_event("stopped", adapter._stopped_body("pause"))
                return

            with adapter._state_lock:
                if adapter._runner is None:
                    return
                old_scan_id = adapter._current_scan_id
                advanced = adapter._advance_one_step_locked()
                adapter._flush_pending_snapshots_locked()
                hit_breakpoint = adapter._current_rung_hits_breakpoint_locked()
                adapter._flush_pending_snapshots_locked()
                hit_data_breakpoint = adapter._pending_predicate_pause
                if hit_data_breakpoint:
                    adapter._pending_predicate_pause = False
                scan_completed = (
                    advanced and old_scan_id is not None and adapter._current_scan_id != old_scan_id
                )

            if scan_completed:
                _emit_scan_frame()

            if hit_breakpoint:
                _emit_scan_frame(force=True)
                adapter._enqueue_internal_event("stopped", adapter._stopped_body("breakpoint"))
                return

            if hit_data_breakpoint:
                _emit_scan_frame(force=True)
                adapter._enqueue_internal_event("stopped", adapter._stopped_body("data breakpoint"))
                return

            if not advanced:
                time.sleep(0.005)

        _emit_scan_frame(force=True)
    finally:
        with adapter._state_lock:
            adapter._continue_thread = None
            adapter._pending_predicate_pause = False
            adapter._session.scan_frame_buffer = None
        adapter._pause_event.clear()


def advance_one_step_locked(adapter: Any) -> bool:
    runner = adapter._require_runner_locked()
    if not adapter._top_level_rungs(runner):
        runner.step()
        adapter._scan_gen = None
        adapter._current_scan_id = None
        adapter._current_step = None
        adapter._current_rung_index = None
        adapter._current_rung = None
        adapter._current_ctx = None
        return False

    if adapter._scan_gen is None:
        adapter._scan_gen = runner.debug.scan_steps_debug()
        adapter._current_scan_id = runner.current_state.scan_id + 1

    try:
        step = next(adapter._scan_gen)
    except StopIteration:
        adapter._scan_gen = runner.debug.scan_steps_debug()
        adapter._current_scan_id = runner.current_state.scan_id + 1
        step = next(adapter._scan_gen)

    adapter._current_step = step
    adapter._current_rung_index = step.rung_index
    adapter._current_rung = step.rung
    adapter._current_ctx = step.ctx
    return True


def advance_with_step_logpoints_locked(adapter: Any) -> bool:
    advanced = adapter._advance_one_step_locked()
    adapter._flush_pending_snapshots_locked()
    adapter._process_logpoints_for_current_rung_locked()
    adapter._flush_pending_snapshots_locked()
    return advanced


def step_until(adapter: Any, should_stop: Callable[[ScanStep | None], bool]) -> None:
    if not adapter._advance_with_step_logpoints_locked():
        return
    while not should_stop(adapter._current_step):
        if not adapter._advance_with_step_logpoints_locked():
            return


def assert_can_step_locked(adapter: Any) -> None:
    adapter._require_runner_locked()
    if adapter._thread_running_locked():
        raise adapter.DAPAdapterError("Cannot step while continue is running")


def thread_running_locked(adapter: Any) -> bool:
    return adapter._continue_thread is not None and adapter._continue_thread.is_alive()


def current_rung_hits_breakpoint_locked(adapter: Any) -> bool:
    return adapter._breakpoints.current_rung_hits_breakpoint(
        current_rung=adapter._current_rung,
        current_scan_id=adapter._current_scan_id,
        runner=adapter._runner,
        on_logpoint_hit=lambda breakpoint, state, scan_id: adapter._handle_logpoint_hit_locked(
            breakpoint,
            state,
            active_scan_id=scan_id,
        ),
    )


def process_logpoints_for_current_rung_locked(adapter: Any) -> None:
    adapter._breakpoints.process_logpoints_for_current_rung(
        current_rung=adapter._current_rung,
        current_scan_id=adapter._current_scan_id,
        runner=adapter._runner,
        on_logpoint_hit=lambda breakpoint, state, scan_id: adapter._handle_logpoint_hit_locked(
            breakpoint,
            state,
            active_scan_id=scan_id,
        ),
    )
