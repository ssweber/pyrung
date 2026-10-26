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
    adapter._pause_executor()
    with adapter._state_lock:
        adapter._assert_can_step_locked()
        adapter._step_until(
            lambda step: step is None or step.kind == "rung",
        )
    return {}, [("stopped", adapter._stopped_body("step"))]


def on_step_in(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    adapter._pause_executor()
    with adapter._state_lock:
        adapter._assert_can_step_locked()
        adapter._step_until(
            lambda step: (
                step is None or step.kind not in {"branch", "rung", "subroutine", "loop_body"}
            ),
        )
    return {}, [("stopped", adapter._stopped_body("step"))]


def on_step_out(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    adapter._pause_executor()
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
    adapter._pause_executor()
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

        from pyrung.dap.bounds_console import emit_bounds_violations

        emit_bounds_violations(adapter)

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


#: Furthest real-time pacing may fall behind before it stops catching up.
PACE_MAX_LAG_SECONDS = 0.1

#: Minimum wall time between two continue-mode scan frames.
FRAME_INTERVAL_SECONDS = 0.033


class ScanPacer:
    """Keep simulation time ~= wall time x speed while continue runs.

    ``delay()`` is called once per committed scan and returns how long to wait
    before the next one.  The scan period (dt) is never changed; pacing only
    inserts waits.  When the simulation falls more than
    ``PACE_MAX_LAG_SECONDS`` behind, the baseline is reset instead of
    bursting scans to catch up.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._speed = 0.0
        self._wall0: float | None = None
        self._sim0 = 0.0

    def delay(self, speed: float, sim_time: float) -> float:
        now = self._clock()
        if speed <= 0:
            self._wall0 = None
            return 0.0
        if self._wall0 is None or speed != self._speed or sim_time < self._sim0:
            self._reset(now, sim_time, speed)
            return 0.0
        ahead = self._wall0 + (sim_time - self._sim0) / speed - now
        if ahead > 0:
            return ahead
        if -ahead > PACE_MAX_LAG_SECONDS:
            self._reset(now, sim_time, speed)
        return 0.0

    def _reset(self, now: float, sim_time: float, speed: float) -> None:
        self._wall0 = now
        self._sim0 = sim_time
        self._speed = speed


class SpeedMeter:
    """Measured simulation-time / wall-time ratio while continue runs.

    Updated at most every ``WINDOW_SECONDS`` of wall time so the reading is
    steady enough to display ("6.3x" when the PLC cannot keep up).
    """

    WINDOW_SECONDS = 0.5

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._wall0: float | None = None
        self._sim0 = 0.0
        self.value: float | None = None

    def sample(self, sim_time: float) -> float | None:
        now = self._clock()
        if self._wall0 is None or sim_time < self._sim0:
            self._wall0, self._sim0 = now, sim_time
            return self.value
        elapsed = now - self._wall0
        if elapsed >= self.WINDOW_SECONDS:
            self.value = (sim_time - self._sim0) / elapsed
            self._wall0, self._sim0 = now, sim_time
        return self.value


#: ``source`` of frames emitted by the continue worker.
WORKER_SOURCE: dict[str, Any] = {"origin": "worker", "command": "continue"}


def continue_worker(adapter: Any) -> None:
    from pyrung.dap.bounds_console import emit_bounds_violations
    from pyrung.dap.handlers.simulation import enter_stop_locked, frame_body_locked
    from pyrung.dap.session import ScanFrameBuffer

    clock = adapter._clock
    pacer = ScanPacer(clock)
    meter = SpeedMeter(clock)
    last_emit_time: float | None = None

    def _emit_scan_frame(*, force: bool = False, running: bool = True) -> None:
        nonlocal last_emit_time
        if not adapter._configuration_done:
            return
        now = clock()
        if (
            not force
            and last_emit_time is not None
            and (now - last_emit_time) < FRAME_INTERVAL_SECONDS
        ):
            return

        buffer = adapter._session.scan_frame_buffer
        if buffer is None:
            return

        with adapter._state_lock:
            if adapter._runner is None:
                return
            # A program with no rungs has no trace; its frames still carry the scan.
            no_rungs = not adapter._top_level_rungs(adapter._runner)
            trace_body = adapter._live_trace_body_locked(allow_empty=force or no_rungs)
            if trace_body is None:
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

            adapter._last_trace_was_step = False
            frame_body = frame_body_locked(
                adapter,
                running=running,
                trace_body=trace_body,
                changes=changes,
                monitors=buffer.monitors,
                snapshots=buffer.snapshots,
                outputs=buffer.outputs,
                source=WORKER_SOURCE,
            )

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
                with adapter._state_lock:
                    if adapter._pending_stop and adapter._runner is not None:
                        enter_stop_locked(adapter)
                _emit_scan_frame(force=True, running=False)
                adapter._enqueue_internal_event("stopped", adapter._stopped_body("pause"))
                return

            with adapter._state_lock:
                if adapter._runner is None:
                    return
                old_scan_id = adapter._current_scan_id
                old_state = adapter._runner.current_state
                advanced = adapter._advance_one_step_locked()
                adapter._flush_pending_snapshots_locked()
                hit_breakpoint = adapter._current_rung_hits_breakpoint_locked()
                adapter._flush_pending_snapshots_locked()
                hit_data_breakpoint = adapter._pending_predicate_pause
                if hit_data_breakpoint:
                    adapter._pending_predicate_pause = False
                committed = adapter._runner is not None and (
                    adapter._runner.current_state is not old_state
                )
                # With no rungs to step through, each advance is a whole committed scan.
                scan_completed = (
                    advanced and old_scan_id is not None and adapter._current_scan_id != old_scan_id
                ) or (not advanced and committed)
                speed = adapter._speed
                sim_time = adapter._runner.current_state.timestamp if adapter._runner else 0.0

            if scan_completed:
                _emit_scan_frame()
                emit_bounds_violations(adapter)

            if hit_breakpoint:
                _emit_scan_frame(force=True, running=False)
                adapter._enqueue_internal_event("stopped", adapter._stopped_body("breakpoint"))
                return

            if hit_data_breakpoint:
                _emit_scan_frame(force=True, running=False)
                adapter._enqueue_internal_event("stopped", adapter._stopped_body("data breakpoint"))
                return

            if committed:
                adapter._actual_speed = meter.sample(sim_time)
            wait = pacer.delay(speed, sim_time) if committed else 0.0
            if wait > 0:
                adapter._pace_wait(wait)
            elif not advanced and speed <= 0:
                time.sleep(0.005)

        _emit_scan_frame(force=True, running=False)
    finally:
        with adapter._state_lock:
            adapter._continue_thread = None
            adapter._pending_predicate_pause = False
            adapter._pending_stop = False
            adapter._actual_speed = None
            adapter._session.scan_frame_buffer = None
        adapter._pause_event.clear()


def invalidate_mid_scan(adapter: Any) -> None:
    """Discard a partially-advanced scan so the next advance starts fresh.

    Called when force/patch/unforce/clear_forces modify overrides while
    the scan generator is paused mid-scan (e.g. after a DAP ``next``).
    Without this, the override misses the current scan's ``prepare_scan``
    and only takes effect on the *following* scan — a 1-scan lag that
    diverges from pure ``PLC.force()`` + ``step()`` behaviour.

    The abandoned scan already drained the pending patches into its context
    (``prepare_scan``), so they are queued again: without that they would
    vanish without ever being committed.
    """
    if adapter._scan_gen is not None:
        if adapter._runner is not None:
            adapter._runner.debug.abandon_scan()
        adapter._scan_gen = None
        adapter._current_step = None
        adapter._current_rung_index = None
        adapter._current_rung = None
        adapter._current_ctx = None


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
