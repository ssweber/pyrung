"""Debug Adapter Protocol server for pyrung."""

from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable, Generator
from typing import Any, BinaryIO, TypeVar

from pyrung.core import PLCRunner
from pyrung.core.context import ScanContext
from pyrung.core.rung import Rung
from pyrung.core.runner import ScanStep
from pyrung.core.state import SystemState
from pyrung.dap import execution_flow
from pyrung.dap.args import parse_args
from pyrung.dap.breakpoints import BreakpointManager, SourceBreakpoint
from pyrung.dap.formatter import DAPFormatter
from pyrung.dap.handlers import (
    breakpoint_requests,
    lifecycle_launch,
    monitor_data_breakpoints,
    stack_variables_evaluate,
)
from pyrung.dap.protocol import (
    MessageSequencer,
    make_event,
    make_response,
    read_message,
    write_message,
)
from pyrung.dap.session import DebugSession


class DAPAdapterError(Exception):
    """Adapter-level protocol/usage error."""


HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]
ParsedArgs = TypeVar("ParsedArgs")


class DAPAdapter:
    """DAP orchestrator that routes protocol commands to internal handler modules."""

    DAPAdapterError = DAPAdapterError

    TRACE_VERSION = 1
    THREAD_ID = 1
    TAGS_SCOPE_REF = 1
    FORCES_SCOPE_REF = 2
    MEMORY_SCOPE_REF = 3
    MONITORS_SCOPE_REF = 4

    def __init__(
        self,
        *,
        in_stream: BinaryIO | None = None,
        out_stream: BinaryIO | None = None,
    ) -> None:
        self._in_stream = in_stream if in_stream is not None else sys.stdin.buffer
        self._out_stream = out_stream if out_stream is not None else sys.stdout.buffer
        self._seq = MessageSequencer()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._continue_thread: threading.Thread | None = None
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._formatter = DAPFormatter()
        self._session = DebugSession()

        self._breakpoints = BreakpointManager()
        self._monitor_handles: dict[int, Any] = {}
        self._monitor_meta: dict[int, monitor_data_breakpoints.MonitorMeta] = {}
        self._monitor_scope_ref: int = self.MONITORS_SCOPE_REF
        self._monitor_values: dict[int, str] = {}
        self._data_bp_handles: dict[str, Any] = {}
        self._data_bp_meta: dict[str, monitor_data_breakpoints.DataBreakpointMeta] = {}
        self._pending_snapshot_labels_by_scan: dict[int, set[str]] = {}

    @property
    def _runner(self) -> PLCRunner | None:
        return self._session.runner

    @_runner.setter
    def _runner(self, value: PLCRunner | None) -> None:
        self._session.runner = value

    @property
    def _scan_gen(self) -> Generator[ScanStep, None, None] | None:
        return self._session.scan_gen

    @_scan_gen.setter
    def _scan_gen(self, value: Generator[ScanStep, None, None] | None) -> None:
        self._session.scan_gen = value

    @property
    def _current_scan_id(self) -> int | None:
        return self._session.current_scan_id

    @_current_scan_id.setter
    def _current_scan_id(self, value: int | None) -> None:
        self._session.current_scan_id = value

    @property
    def _current_step(self) -> ScanStep | None:
        return self._session.current_step

    @_current_step.setter
    def _current_step(self, value: ScanStep | None) -> None:
        self._session.current_step = value

    @property
    def _current_rung_index(self) -> int | None:
        return self._session.current_rung_index

    @_current_rung_index.setter
    def _current_rung_index(self, value: int | None) -> None:
        self._session.current_rung_index = value

    @property
    def _current_rung(self) -> Rung | None:
        return self._session.current_rung

    @_current_rung.setter
    def _current_rung(self, value: Rung | None) -> None:
        self._session.current_rung = value

    @property
    def _current_ctx(self) -> ScanContext | None:
        return self._session.current_ctx

    @_current_ctx.setter
    def _current_ctx(self, value: ScanContext | None) -> None:
        self._session.current_ctx = value

    @property
    def _program_path(self) -> str | None:
        return self._session.program_path

    @_program_path.setter
    def _program_path(self, value: str | None) -> None:
        self._session.program_path = value

    @property
    def _pending_predicate_pause(self) -> bool:
        return self._session.pending_predicate_pause

    @_pending_predicate_pause.setter
    def _pending_predicate_pause(self, value: bool) -> None:
        self._session.pending_predicate_pause = value

    def run(self) -> None:
        """Run the adapter loop until EOF or disconnect."""
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="pyrung-dap-reader"
        )
        self._reader_thread.start()

        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            kind = item.get("kind")
            if kind == "request":
                self.handle_request(item["message"])
                continue
            if kind == "internal_event":
                self._send_event(item["event"], item.get("body"))
                if item.get("event") == "stopped":
                    self._emit_trace_event()
                continue
            if kind == "eof":
                self._stop_event.set()

        self._pause_event.set()
        thread = self._continue_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = read_message(self._in_stream)
            except Exception:
                self._queue.put({"kind": "eof"})
                return
            if message is None:
                self._queue.put({"kind": "eof"})
                return
            if message.get("type") == "request":
                self._queue.put({"kind": "request", "message": message})

    def handle_request(self, request: dict[str, Any]) -> None:
        """Handle one already-parsed DAP request object."""
        command = request.get("command")
        if not isinstance(command, str):
            self._send_response(request, success=False, message="Invalid request command")
            return

        handler = getattr(self, f"_on_{command}", None)
        if handler is None:
            self._send_response(request, success=False, message=f"Unsupported command: {command}")
            return

        try:
            body, events = handler(request.get("arguments") or {})
            self._send_response(request, success=True, body=body)
            for event_name, event_body in events:
                self._send_event(event_name, event_body)
                if event_name == "stopped":
                    self._emit_trace_event()
        except DAPAdapterError as exc:
            self._send_response(request, success=False, message=str(exc))
        except Exception as exc:  # pragma: no cover - defensive fail-safe path
            self._send_response(request, success=False, message=f"Internal adapter error: {exc}")

    def _send_response(
        self,
        request: dict[str, Any],
        *,
        success: bool,
        body: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        envelope = make_response(
            seq=self._seq.next(),
            request=request,
            success=success,
            body=body,
            message=message,
        )
        write_message(self._out_stream, envelope)

    def _send_event(self, event: str, body: dict[str, Any] | None = None) -> None:
        envelope = make_event(seq=self._seq.next(), event=event, body=body)
        write_message(self._out_stream, envelope)

    def _enqueue_internal_event(self, event: str, body: dict[str, Any] | None = None) -> None:
        self._queue.put({"kind": "internal_event", "event": event, "body": body})

    def _emit_trace_event(self) -> None:
        with self._state_lock:
            body = self._current_trace_body_locked()
        if body is None:
            return
        self._send_event("pyrungTrace", body)

    def _parse_request_args(self, model: type[ParsedArgs], args: Any) -> ParsedArgs:
        """Parse handler arguments while preserving legacy non-object failures."""
        if not isinstance(args, dict):
            typename = type(args).__name__
            raise AttributeError(f"'{typename}' object has no attribute 'get'")
        return parse_args(model, args, error=DAPAdapterError)

    def _on_initialize(self, _args: dict[str, Any]) -> HandlerResult:
        return lifecycle_launch.on_initialize(self, _args)

    def _on_configurationDone(self, _args: dict[str, Any]) -> HandlerResult:
        return lifecycle_launch.on_configuration_done(self, _args)

    def _on_disconnect(self, _args: dict[str, Any]) -> HandlerResult:
        return lifecycle_launch.on_disconnect(self, _args)

    def _on_terminate(self, _args: dict[str, Any]) -> HandlerResult:
        return lifecycle_launch.on_terminate(self, _args)

    def _on_threads(self, _args: dict[str, Any]) -> HandlerResult:
        return lifecycle_launch.on_threads(self, _args)

    def _on_launch(self, args: dict[str, Any]) -> HandlerResult:
        return lifecycle_launch.on_launch(self, args)

    def _discover_runner(self, namespace: dict[str, Any]) -> PLCRunner:
        return lifecycle_launch.discover_runner(self, namespace)

    def _on_stackTrace(self, args: dict[str, Any]) -> HandlerResult:
        return stack_variables_evaluate.on_stack_trace(self, args)

    def _on_scopes(self, _args: dict[str, Any]) -> HandlerResult:
        return stack_variables_evaluate.on_scopes(self, _args)

    def _on_variables(self, args: dict[str, Any]) -> HandlerResult:
        return stack_variables_evaluate.on_variables(self, args)

    def _on_next(self, _args: dict[str, Any]) -> HandlerResult:
        return execution_flow.on_next(self, _args)

    def _on_stepIn(self, _args: dict[str, Any]) -> HandlerResult:
        return execution_flow.on_step_in(self, _args)

    def _on_stepOut(self, _args: dict[str, Any]) -> HandlerResult:
        return execution_flow.on_step_out(self, _args)

    def _on_pyrungStepScan(self, _args: dict[str, Any]) -> HandlerResult:
        return execution_flow.on_pyrung_step_scan(self, _args)

    def _on_continue(self, _args: dict[str, Any]) -> HandlerResult:
        return execution_flow.on_continue(self, _args)

    def _on_pause(self, _args: dict[str, Any]) -> HandlerResult:
        return execution_flow.on_pause(self, _args)

    def _on_setBreakpoints(self, args: dict[str, Any]) -> HandlerResult:
        return breakpoint_requests.on_set_breakpoints(self, args)

    def _requested_breakpoints(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return breakpoint_requests.requested_breakpoints(self, args)

    def _on_evaluate(self, args: dict[str, Any]) -> HandlerResult:
        return stack_variables_evaluate.on_evaluate(self, args)

    def _evaluate_repl_command_locked(self, expression: str) -> str:
        return stack_variables_evaluate.evaluate_repl_command_locked(self, expression)

    def _evaluate_watch_expression_locked(self, expression: str) -> Any:
        return stack_variables_evaluate.evaluate_watch_expression_locked(self, expression)

    def _effective_evaluate_state_locked(self) -> SystemState:
        return stack_variables_evaluate.effective_evaluate_state_locked(self)

    def _expression_references(self, expr: Any) -> set[str]:
        return stack_variables_evaluate.expression_references(self, expr)

    def _state_has_reference(self, state: SystemState, name: str) -> bool:
        return stack_variables_evaluate.state_has_reference(self, state, name)

    def _state_value_for_reference(self, state: SystemState, name: str) -> Any:
        return stack_variables_evaluate.state_value_for_reference(self, state, name)

    def _on_dataBreakpointInfo(self, args: dict[str, Any]) -> HandlerResult:
        return monitor_data_breakpoints.on_data_breakpoint_info(self, args)

    def _on_setDataBreakpoints(self, args: dict[str, Any]) -> HandlerResult:
        return monitor_data_breakpoints.on_set_data_breakpoints(self, args)

    def _on_pyrungAddMonitor(self, args: dict[str, Any]) -> HandlerResult:
        return monitor_data_breakpoints.on_pyrung_add_monitor(self, args)

    def _on_pyrungRemoveMonitor(self, args: dict[str, Any]) -> HandlerResult:
        return monitor_data_breakpoints.on_pyrung_remove_monitor(self, args)

    def _on_pyrungListMonitors(self, _args: dict[str, Any]) -> HandlerResult:
        return monitor_data_breakpoints.on_pyrung_list_monitors(self, _args)

    def _on_pyrungFindLabel(self, args: dict[str, Any]) -> HandlerResult:
        return monitor_data_breakpoints.on_pyrung_find_label(self, args)

    def _continue_worker(self) -> None:
        execution_flow.continue_worker(self)

    def _advance_one_step_locked(self) -> bool:
        return execution_flow.advance_one_step_locked(self)

    def _advance_with_step_logpoints_locked(self) -> bool:
        return execution_flow.advance_with_step_logpoints_locked(self)

    def _step_until(self, should_stop: Callable[[ScanStep | None], bool]) -> None:
        execution_flow.step_until(self, should_stop)

    def _assert_can_step_locked(self) -> None:
        execution_flow.assert_can_step_locked(self)

    def _thread_running_locked(self) -> bool:
        return execution_flow.thread_running_locked(self)

    def _current_rung_hits_breakpoint_locked(self) -> bool:
        return execution_flow.current_rung_hits_breakpoint_locked(self)

    def _process_logpoints_for_current_rung_locked(self) -> None:
        execution_flow.process_logpoints_for_current_rung_locked(self)

    def _rebuild_breakpoint_index_locked(self) -> None:
        runner = self._require_runner_locked()
        self._breakpoints.rebuild_index(runner)

    def _canonical_path(self, path: str | None) -> str | None:
        return self._breakpoints.canonical_path(path)

    def _compile_condition(self, source: str) -> Callable[[SystemState], bool]:
        return breakpoint_requests.compile_condition_for_breakpoint(self, source)

    def _parse_hit_condition(self, raw_value: Any) -> int | None:
        return breakpoint_requests.parse_hit_condition(self, raw_value)

    def _monitor_variables(self) -> list[dict[str, Any]]:
        return monitor_data_breakpoints.monitor_variables(self)

    def _build_monitor_callback(
        self,
        *,
        tag_name: str,
        monitor_id_ref: dict[str, int],
    ) -> Callable[[Any, Any], None]:
        return monitor_data_breakpoints.build_monitor_callback(
            self,
            tag_name=tag_name,
            monitor_id_ref=monitor_id_ref,
        )

    def _build_data_breakpoint_callback(self, *, data_id: str) -> Callable[[Any, Any], None]:
        return monitor_data_breakpoints.build_data_breakpoint_callback(self, data_id=data_id)

    def _data_id_for_tag(self, tag_name: str) -> str:
        return monitor_data_breakpoints.data_id_for_tag(self, tag_name)

    def _tag_from_data_id(self, data_id: str) -> str | None:
        return monitor_data_breakpoints.tag_from_data_id(self, data_id)

    def _handle_logpoint_hit_locked(
        self,
        breakpoint: SourceBreakpoint,
        state: SystemState,
        *,
        active_scan_id: int | None,
    ) -> None:
        if breakpoint.snapshot_label:
            if active_scan_id is None:
                self._record_snapshot_locked(label=breakpoint.snapshot_label, state=state)
            else:
                pending = self._pending_snapshot_labels_by_scan.setdefault(active_scan_id, set())
                pending.add(breakpoint.snapshot_label)
            return

        message = breakpoint.log_message or ""
        self._enqueue_internal_event(
            "output",
            {"category": "console", "output": f"{message}\n"},
        )

    def _record_snapshot_locked(self, *, label: str, state: SystemState) -> None:
        runner = self._runner
        if runner is None:
            return
        metadata = runner._snapshot_metadata_for_state(state)
        runner.history._label_scan(label, state.scan_id, metadata=metadata)
        self._enqueue_internal_event(
            "pyrungSnapshot",
            {
                "label": label,
                "scanId": state.scan_id,
                "timestamp": state.timestamp,
                "rtcIso": metadata["rtc_iso"],
                "rtcOffsetSeconds": metadata["rtc_offset_seconds"],
            },
        )
        self._enqueue_internal_event(
            "output",
            {
                "category": "console",
                "output": f"Snapshot taken: {label} (scan {state.scan_id})\n",
            },
        )

    def _flush_pending_snapshots_locked(self) -> None:
        runner = self._runner
        if runner is None:
            return
        committed_scan_id = runner.current_state.scan_id
        labels = self._pending_snapshot_labels_by_scan.pop(committed_scan_id, None)
        if not labels:
            return
        state = runner.current_state
        for label in sorted(labels):
            self._record_snapshot_locked(label=label, state=state)

    def _clear_debug_registrations_locked(self) -> None:
        monitor_data_breakpoints.clear_debug_registrations_locked(self)

    def _require_runner_locked(self) -> PLCRunner:
        if self._runner is None:
            raise DAPAdapterError("No program launched")
        return self._runner

    def _shutdown(self) -> HandlerResult:
        return lifecycle_launch.shutdown(self)

    def _top_level_rungs(self, runner: PLCRunner) -> list[Rung]:
        """Return top-level rungs through the runner's public debug API."""
        return list(runner.iter_top_level_rungs())

    def _current_trace_body_locked(self) -> dict[str, Any] | None:
        runner = self._runner
        if runner is None:
            return None

        return self._formatter.current_trace_body(
            event_result=runner.inspect_event(),
            current_scan_id=runner.current_state.scan_id,
            trace_version=self.TRACE_VERSION,
            canonical_path=self._canonical_path,
            format_value=self._format_value,
        )

    def _stopped_body(self, reason: str) -> dict[str, Any]:
        return {"reason": reason, "threadId": self.THREAD_ID, "allThreadsStopped": True}

    def _as_dap_variables(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        variables: list[dict[str, Any]] = []
        for name in sorted(values):
            value = values[name]
            variables.append(
                {
                    "name": name,
                    "value": self._format_value(value),
                    "type": type(value).__name__,
                    "variablesReference": 0,
                }
            )
        return variables

    def _format_value(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return repr(value)

    def _parse_literal(self, raw_value: str) -> bool | int | float | str:
        text = raw_value.strip()
        lowered = text.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1]
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return text

    def _unique_instances(self, candidates: Any, cls: type[Any]) -> list[Any]:
        seen: set[int] = set()
        result: list[Any] = []
        for value in candidates:
            if isinstance(value, cls):
                obj_id = id(value)
                if obj_id in seen:
                    continue
                seen.add(obj_id)
                result.append(value)
        return result

    def _drain_internal_events(self) -> int:
        """Drain queued internal events (used by tests without run loop)."""
        processed = 0
        pending: list[dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item.get("kind") == "internal_event":
                self._send_event(item["event"], item.get("body"))
                if item.get("event") == "stopped":
                    self._emit_trace_event()
                processed += 1
                continue
            pending.append(item)
        for item in pending:
            self._queue.put(item)
        return processed
