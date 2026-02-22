"""Debug Adapter Protocol server for pyrung."""

from __future__ import annotations

import os
import queue
import runpy
import sys
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from pyrung.core import PLCRunner, Program
from pyrung.core.context import ScanContext
from pyrung.core.debug_trace import TraceEvent
from pyrung.core.instruction import CallInstruction, Instruction
from pyrung.core.rung import Rung
from pyrung.core.runner import ScanStep
from pyrung.core.state import SystemState
from pyrung.dap.expressions import And
from pyrung.dap.expressions import Compare
from pyrung.dap.expressions import Expr
from pyrung.dap.expressions import ExpressionParseError
from pyrung.dap.expressions import Not
from pyrung.dap.expressions import Or
from pyrung.dap.expressions import compile as compile_condition
from pyrung.dap.expressions import parse as parse_condition
from pyrung.dap.protocol import (
    MessageSequencer,
    make_event,
    make_response,
    read_message,
    write_message,
)


class DAPAdapterError(Exception):
    """Adapter-level protocol/usage error."""


HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass
class _SourceBreakpoint:
    line: int
    enabled: bool = True
    condition_source: str | None = None
    condition: Callable[[SystemState], bool] | None = None
    hit_condition: int | None = None
    hit_count: int = 0
    log_message: str | None = None
    snapshot_label: str | None = None
    last_scan_id: int | None = None


class DAPAdapter:
    """Minimal single-thread DAP adapter with async continue support."""

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

        self._runner: PLCRunner | None = None
        self._scan_gen: Generator[ScanStep, None, None] | None = None
        self._current_scan_id: int | None = None
        self._current_step: ScanStep | None = None
        self._current_rung_index: int | None = None
        self._current_rung: Rung | None = None
        self._current_ctx: ScanContext | None = None
        self._program_path: str | None = None

        self._source_breakpoints_by_file: dict[str, dict[int, _SourceBreakpoint]] = {}
        self._breakpoint_rung_map: dict[str, set[int]] = {}
        self._subroutine_source_map: dict[str, tuple[str, int, int | None]] = {}
        self._monitor_handles: dict[int, Any] = {}
        self._monitor_meta: dict[int, dict[str, Any]] = {}
        self._monitor_scope_ref: int = self.MONITORS_SCOPE_REF
        self._monitor_values: dict[int, str] = {}
        self._data_bp_handles: dict[str, Any] = {}
        self._data_bp_meta: dict[str, dict[str, Any]] = {}
        self._pending_snapshot_labels_by_scan: dict[int, set[str]] = {}
        self._pending_predicate_pause = False

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

    def _emit_trace_event(self) -> None:
        with self._state_lock:
            body = self._current_trace_body_locked()
        if body is None:
            return
        self._send_event("pyrungTrace", body)

    def _on_initialize(self, _args: dict[str, Any]) -> HandlerResult:
        capabilities = {
            "supportsConfigurationDoneRequest": True,
            "supportsEvaluateForHovers": False,
            "supportsStepBack": False,
            "supportsStepOut": True,
            "supportsTerminateRequest": True,
            "supportsConditionalBreakpoints": True,
            "supportsHitConditionalBreakpoints": True,
            "supportsLogPoints": True,
            "supportsDataBreakpoints": True,
        }
        return capabilities, [("initialized", {})]

    def _on_configurationDone(self, _args: dict[str, Any]) -> HandlerResult:
        return {}, []

    def _on_disconnect(self, _args: dict[str, Any]) -> HandlerResult:
        return self._shutdown()

    def _on_terminate(self, _args: dict[str, Any]) -> HandlerResult:
        return self._shutdown()

    def _on_threads(self, _args: dict[str, Any]) -> HandlerResult:
        return {"threads": [{"id": self.THREAD_ID, "name": "PLC Scan"}]}, []

    def _on_launch(self, args: dict[str, Any]) -> HandlerResult:
        program_arg = args.get("program")
        if not isinstance(program_arg, str) or not program_arg.strip():
            raise DAPAdapterError("launch.program must be a Python file path")

        program_path = Path(program_arg).expanduser().resolve()
        if not program_path.is_file():
            raise DAPAdapterError(f"launch.program file not found: {program_path}")

        previous_dap_flag = os.environ.get("PYRUNG_DAP_ACTIVE")
        os.environ["PYRUNG_DAP_ACTIVE"] = "1"
        try:
            namespace = runpy.run_path(str(program_path), run_name="__main__")
        finally:
            if previous_dap_flag is None:
                os.environ.pop("PYRUNG_DAP_ACTIVE", None)
            else:
                os.environ["PYRUNG_DAP_ACTIVE"] = previous_dap_flag
        runner = self._discover_runner(namespace)

        with self._state_lock:
            if self._thread_running_locked():
                raise DAPAdapterError("Cannot launch while continue is running")
            self._clear_debug_registrations_locked()
            self._runner = runner
            self._scan_gen = None
            self._current_scan_id = None
            self._current_step = None
            self._current_rung_index = None
            self._current_rung = None
            self._current_ctx = None
            self._program_path = str(program_path)
            self._source_breakpoints_by_file = {}
            self._breakpoint_rung_map = {}
            self._subroutine_source_map = {}
            self._pending_predicate_pause = False
            self._rebuild_breakpoint_index_locked()

        return {}, [("stopped", self._stopped_body("entry"))]

    def _discover_runner(self, namespace: dict[str, Any]) -> PLCRunner:
        named_runner = namespace.get("runner")
        if isinstance(named_runner, PLCRunner):
            return named_runner

        runners = self._unique_instances(namespace.values(), PLCRunner)
        if len(runners) == 1:
            return runners[0]

        programs = self._unique_instances(namespace.values(), Program)
        if len(programs) == 1:
            return PLCRunner(programs[0])

        raise DAPAdapterError(
            "Launch script must provide 'runner' as PLCRunner, or define exactly one PLCRunner "
            f"or exactly one Program. Found {len(runners)} PLCRunner(s), {len(programs)} Program(s)."
        )

    def _on_stackTrace(self, args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            runner = self._require_runner_locked()
            rungs = self._top_level_rungs(runner)
            current_step = self._current_step
            current_index = self._current_rung_index
            program_path = self._program_path

        start_frame = int(args.get("startFrame", 0))
        levels = int(args.get("levels", 0))

        if current_step is not None:
            frames = self._build_current_stack_frames(current_step=current_step, rungs=rungs)
        elif rungs:
            order = list(range(len(rungs)))
            if current_index is not None and 0 <= current_index < len(rungs):
                order = [current_index, *[i for i in order if i != current_index]]

            frames = [
                self._stack_frame_from_rung(
                    frame_id=idx,
                    name=f"Rung {idx}",
                    rung=rungs[idx],
                )
                for idx in order
            ]
        else:
            frame = {"id": 0, "name": "Scan", "line": 1, "column": 1}
            if program_path is not None:
                frame["source"] = {"name": Path(program_path).name, "path": program_path}
            frames = [frame]

        if levels > 0:
            visible = frames[start_frame : start_frame + levels]
        else:
            visible = frames[start_frame:]

        return {"stackFrames": visible, "totalFrames": len(frames)}, []

    def _on_scopes(self, _args: dict[str, Any]) -> HandlerResult:
        scopes = [
            {"name": "Tags", "variablesReference": self.TAGS_SCOPE_REF, "expensive": False},
            {"name": "Forces", "variablesReference": self.FORCES_SCOPE_REF, "expensive": False},
            {"name": "Memory", "variablesReference": self.MEMORY_SCOPE_REF, "expensive": False},
        ]
        with self._state_lock:
            has_monitors = bool(self._monitor_handles)
            monitor_count = len(self._monitor_handles)
        if has_monitors:
            scopes.append(
                {
                    "name": "PLC Monitors",
                    "variablesReference": self._monitor_scope_ref,
                    "expensive": False,
                    "namedVariables": monitor_count,
                }
            )
        return {"scopes": scopes}, []

    def _on_variables(self, args: dict[str, Any]) -> HandlerResult:
        ref = int(args.get("variablesReference", 0))
        with self._state_lock:
            runner = self._require_runner_locked()
            current_ctx = self._current_ctx
            state = runner.current_state
            forces = dict(runner.forces)

        if ref == self.TAGS_SCOPE_REF:
            values = dict(state.tags)
            if current_ctx is not None:
                values.update(getattr(current_ctx, "_tags_pending", {}))
            return {"variables": self._as_dap_variables(values)}, []

        if ref == self.FORCES_SCOPE_REF:
            return {"variables": self._as_dap_variables(forces)}, []

        if ref == self.MEMORY_SCOPE_REF:
            values = dict(state.memory)
            if current_ctx is not None:
                values.update(getattr(current_ctx, "_memory_pending", {}))
            return {"variables": self._as_dap_variables(values)}, []

        if ref == self._monitor_scope_ref:
            variables = self._monitor_variables()
            return {"variables": variables}, []

        raise DAPAdapterError(f"Unknown variablesReference: {ref}")

    def _on_next(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            self._assert_can_step_locked()
            self._advance_with_step_logpoints_locked()
            while self._current_step is not None and self._current_step.kind != "rung":
                if not self._advance_with_step_logpoints_locked():
                    break
        return {}, [("stopped", self._stopped_body("step"))]

    def _on_stepIn(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            self._assert_can_step_locked()
            self._advance_with_step_logpoints_locked()
            while self._current_step is not None and self._current_step.kind in {
                "branch",
                "rung",
                "subroutine",
            }:
                if not self._advance_with_step_logpoints_locked():
                    break
        return {}, [("stopped", self._stopped_body("step"))]

    def _on_stepOut(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            self._assert_can_step_locked()
            origin_step = self._current_step
            origin_ctx = self._current_ctx
            if origin_step is None:
                self._advance_with_step_logpoints_locked()
            else:
                origin_depth = origin_step.depth
                origin_stack_len = len(origin_step.call_stack)
                while True:
                    if not self._advance_with_step_logpoints_locked():
                        break
                    current_step = self._current_step
                    if current_step is None:
                        break
                    if len(current_step.call_stack) < origin_stack_len:
                        break
                    if current_step.depth < origin_depth:
                        break
                    if self._current_ctx is not origin_ctx:
                        break
        return {}, [("stopped", self._stopped_body("step"))]

    def _on_continue(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            self._require_runner_locked()
            if not self._thread_running_locked():
                self._pause_event.clear()
                thread = threading.Thread(
                    target=self._continue_worker,
                    daemon=True,
                    name="pyrung-dap-continue",
                )
                self._continue_thread = thread
                thread.start()
        return {"allThreadsContinued": True}, []

    def _on_pause(self, _args: dict[str, Any]) -> HandlerResult:
        self._pause_event.set()
        return {}, []

    def _on_setBreakpoints(self, args: dict[str, Any]) -> HandlerResult:
        source = args.get("source")
        if not isinstance(source, dict):
            raise DAPAdapterError("setBreakpoints.source is required")
        source_path = source.get("path")
        if not isinstance(source_path, str):
            raise DAPAdapterError("setBreakpoints.source.path is required")

        canonical = self._canonical_path(source_path)
        if canonical is None:
            return {"breakpoints": []}, []

        requested_breakpoints = self._requested_breakpoints(args)
        with self._state_lock:
            valid_lines = self._breakpoint_rung_map.get(canonical, set())
            existing = self._source_breakpoints_by_file.get(canonical, {})
            new_map: dict[int, _SourceBreakpoint] = {}
            response_bps: list[dict[str, Any]] = []

            for requested in requested_breakpoints:
                line = requested["line"]
                if line not in valid_lines:
                    response_bps.append({"verified": False, "line": line})
                    continue

                condition_source = requested.get("condition")
                condition: Callable[[SystemState], bool] | None = None
                if isinstance(condition_source, str) and condition_source.strip():
                    try:
                        condition = self._compile_condition(condition_source)
                    except DAPAdapterError as exc:
                        response_bps.append(
                            {"verified": False, "line": line, "message": str(exc)}
                        )
                        continue

                try:
                    hit_condition = self._parse_hit_condition(requested.get("hitCondition"))
                except DAPAdapterError as exc:
                    response_bps.append({"verified": False, "line": line, "message": str(exc)})
                    continue

                enabled = bool(requested.get("enabled", True))
                log_message = requested.get("logMessage")
                if log_message is not None and not isinstance(log_message, str):
                    response_bps.append(
                        {"verified": False, "line": line, "message": "logMessage must be a string"}
                    )
                    continue
                snapshot_label: str | None = None
                if isinstance(log_message, str) and log_message.startswith("Snapshot:"):
                    snapshot_label = log_message.split(":", 1)[1].strip()
                    if not snapshot_label:
                        response_bps.append(
                            {
                                "verified": False,
                                "line": line,
                                "message": "Snapshot logpoint requires a non-empty label",
                            }
                        )
                        continue

                previous = existing.get(line)
                hit_count = previous.hit_count if previous is not None else 0
                last_scan_id = previous.last_scan_id if previous is not None else None
                new_map[line] = _SourceBreakpoint(
                    line=line,
                    enabled=enabled,
                    condition_source=condition_source.strip() if isinstance(condition_source, str) else None,
                    condition=condition,
                    hit_condition=hit_condition,
                    hit_count=hit_count,
                    log_message=log_message,
                    snapshot_label=snapshot_label,
                    last_scan_id=last_scan_id,
                )
                response_bps.append({"verified": True, "line": line})

            self._source_breakpoints_by_file[canonical] = new_map

        return {"breakpoints": response_bps}, []

    def _requested_breakpoints(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        requested: list[dict[str, Any]] = []
        raw_bps = args.get("breakpoints")
        if isinstance(raw_bps, list):
            for bp in raw_bps:
                if not isinstance(bp, dict):
                    continue
                line = bp.get("line")
                if not isinstance(line, int):
                    continue
                requested.append(
                    {
                        "line": line,
                        "condition": bp.get("condition"),
                        "hitCondition": bp.get("hitCondition"),
                        "logMessage": bp.get("logMessage"),
                        "enabled": bp.get("enabled", True),
                    }
                )
            return requested

        raw_lines = args.get("lines")
        if isinstance(raw_lines, list):
            for line in raw_lines:
                if isinstance(line, int):
                    requested.append({"line": line, "enabled": True})
        return requested

    def _on_evaluate(self, args: dict[str, Any]) -> HandlerResult:
        expression = args.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise DAPAdapterError("evaluate.expression is required")
        context = args.get("context")
        evaluate_context = context if isinstance(context, str) else "repl"

        with self._state_lock:
            self._require_runner_locked()
            if evaluate_context == "watch":
                result = self._evaluate_watch_expression_locked(expression)
            else:
                result = self._evaluate_repl_command_locked(expression)

        return {"result": self._format_value(result), "variablesReference": 0}, []

    def _evaluate_repl_command_locked(self, expression: str) -> str:
        parts = expression.strip().split()
        command = parts[0]
        runner = self._require_runner_locked()
        if command == "force":
            if len(parts) < 3:
                raise DAPAdapterError("Usage: force <tag> <value>")
            tag = parts[1]
            raw_value = expression.strip().split(None, 2)[2]
            value = self._parse_literal(raw_value)
            runner.add_force(tag, value)
            return f"Forced {tag}={value!r}"
        if command in {"remove_force", "unforce"}:
            if len(parts) != 2:
                raise DAPAdapterError("Usage: remove_force <tag>")
            tag = parts[1]
            runner.remove_force(tag)
            return f"Removed force {tag}"
        if command == "clear_forces":
            runner.clear_forces()
            return "Cleared all forces"
        raise DAPAdapterError(
            "Unsupported Debug Console command. Use force/remove_force/unforce/clear_forces. "
            "Use Watch for predicate expressions."
        )

    def _evaluate_watch_expression_locked(self, expression: str) -> Any:
        state = self._effective_evaluate_state_locked()
        try:
            parsed = parse_condition(expression)
        except ExpressionParseError as exc:
            raise DAPAdapterError(str(exc)) from exc

        for name in sorted(self._expression_references(parsed)):
            if not self._state_has_reference(state, name):
                raise DAPAdapterError(f"Unknown tag or memory reference: {name}")

        if isinstance(parsed, Compare) and parsed.op is None and parsed.right is None:
            return self._state_value_for_reference(state, parsed.tag.name)

        return compile_condition(parsed)(state)

    def _effective_evaluate_state_locked(self) -> SystemState:
        runner = self._require_runner_locked()
        state = runner.current_state
        current_ctx = self._current_ctx
        if current_ctx is None:
            return state

        pending_tags = getattr(current_ctx, "_tags_pending", {})
        if isinstance(pending_tags, dict) and pending_tags:
            state = state.with_tags(dict(pending_tags))

        pending_memory = getattr(current_ctx, "_memory_pending", {})
        if isinstance(pending_memory, dict) and pending_memory:
            state = state.with_memory(dict(pending_memory))

        return state

    def _expression_references(self, expr: Expr) -> set[str]:
        if isinstance(expr, Compare):
            return {expr.tag.name}
        if isinstance(expr, Not):
            return {expr.child.name}
        if isinstance(expr, And):
            refs: set[str] = set()
            for child in expr.children:
                refs.update(self._expression_references(child))
            return refs
        if isinstance(expr, Or):
            refs: set[str] = set()
            for child in expr.children:
                refs.update(self._expression_references(child))
            return refs
        return set()

    def _state_has_reference(self, state: SystemState, name: str) -> bool:
        return name in state.tags or name in state.memory

    def _state_value_for_reference(self, state: SystemState, name: str) -> Any:
        if name in state.tags:
            return state.tags.get(name)
        if name in state.memory:
            return state.memory.get(name)
        return None

    def _on_dataBreakpointInfo(self, args: dict[str, Any]) -> HandlerResult:
        variables_reference = int(args.get("variablesReference", 0))
        name = args.get("name")
        if variables_reference != self._monitor_scope_ref or not isinstance(name, str):
            return {"dataId": None, "description": "Data breakpoints are supported for PLC monitors"}, []

        with self._state_lock:
            monitor_tags = {meta["tag"] for meta in self._monitor_meta.values()}
        if name not in monitor_tags:
            return {"dataId": None, "description": f"No monitor registered for {name}"}, []

        data_id = self._data_id_for_tag(name)
        return {
            "dataId": data_id,
            "description": f"Break when {name} changes",
            "canPersist": True,
            "accessTypes": ["write"],
        }, []

    def _on_setDataBreakpoints(self, args: dict[str, Any]) -> HandlerResult:
        raw_breakpoints = args.get("breakpoints")
        if not isinstance(raw_breakpoints, list):
            raw_breakpoints = []

        parsed_requests: list[tuple[int, dict[str, Any]]] = []
        responses: list[dict[str, Any] | None] = []
        for raw_bp in raw_breakpoints:
            if not isinstance(raw_bp, dict):
                responses.append({"verified": False, "message": "Breakpoint entry must be an object"})
                continue
            data_id = raw_bp.get("dataId")
            if not isinstance(data_id, str) or not data_id.strip():
                responses.append({"verified": False, "message": "dataId is required"})
                continue
            data_id = data_id.strip()
            condition_source = raw_bp.get("condition")
            if condition_source is not None and not isinstance(condition_source, str):
                responses.append({"verified": False, "message": "condition must be a string"})
                continue
            try:
                hit_condition = self._parse_hit_condition(raw_bp.get("hitCondition"))
            except DAPAdapterError as exc:
                responses.append({"verified": False, "message": str(exc)})
                continue
            parsed_requests.append(
                (
                    len(responses),
                    {
                        "dataId": data_id,
                        "condition": (
                            condition_source.strip()
                            if isinstance(condition_source, str)
                            else None
                        ),
                        "hitCondition": hit_condition,
                    },
                )
            )
            responses.append(None)

        with self._state_lock:
            runner = self._require_runner_locked()
            requested_data_ids = {requested_bp["dataId"] for _, requested_bp in parsed_requests}
            stale = [
                data_id for data_id in self._data_bp_handles if data_id not in requested_data_ids
            ]
            for data_id in stale:
                handle = self._data_bp_handles.pop(data_id, None)
                if handle is not None:
                    handle.remove()
                self._data_bp_meta.pop(data_id, None)

            for response_index, requested_bp in parsed_requests:
                data_id = requested_bp["dataId"]
                condition_source = requested_bp["condition"]
                existing_meta = self._data_bp_meta.get(data_id)
                unchanged = (
                    existing_meta is not None
                    and existing_meta.get("conditionSource") == condition_source
                    and existing_meta.get("hitCondition") == requested_bp["hitCondition"]
                )
                if unchanged:
                    responses[response_index] = {"verified": True, "message": f"Watching {data_id}"}
                    continue

                if data_id in self._data_bp_handles:
                    handle = self._data_bp_handles.pop(data_id, None)
                    if handle is not None:
                        handle.remove()
                    self._data_bp_meta.pop(data_id, None)

                condition: Callable[[SystemState], bool] | None = None
                if condition_source:
                    try:
                        condition = self._compile_condition(condition_source)
                    except DAPAdapterError as exc:
                        responses[response_index] = {"verified": False, "message": str(exc)}
                        continue

                tag_name = self._tag_from_data_id(data_id)
                if tag_name is None:
                    responses[response_index] = {
                        "verified": False,
                        "message": f"Unsupported dataId: {data_id}",
                    }
                    continue

                handle = runner.monitor(
                    tag_name,
                    self._build_data_breakpoint_callback(data_id=data_id),
                )
                self._data_bp_handles[data_id] = handle
                self._data_bp_meta[data_id] = {
                    "dataId": data_id,
                    "tag": tag_name,
                    "conditionSource": condition_source,
                    "condition": condition,
                    "hitCondition": requested_bp["hitCondition"],
                    "hitCount": 0,
                }
                responses[response_index] = {"verified": True, "message": f"Watching {tag_name}"}

        return {
            "breakpoints": [
                response if response is not None else {"verified": False, "message": "Invalid data breakpoint"}
                for response in responses
            ]
        }, []

    def _on_pyrungAddMonitor(self, args: dict[str, Any]) -> HandlerResult:
        tag = args.get("tag")
        if not isinstance(tag, str) or not tag.strip():
            raise DAPAdapterError("pyrungAddMonitor.tag is required")
        tag_name = tag.strip()

        with self._state_lock:
            runner = self._require_runner_locked()
            monitor_id_ref: dict[str, int] = {"id": 0}
            handle = runner.monitor(
                tag_name,
                self._build_monitor_callback(tag_name=tag_name, monitor_id_ref=monitor_id_ref),
            )
            monitor_id_ref["id"] = handle.id
            self._monitor_handles[handle.id] = handle
            self._monitor_meta[handle.id] = {"id": handle.id, "tag": tag_name, "enabled": True}
            current = runner.current_state.tags.get(tag_name)
            self._monitor_values[handle.id] = self._format_value(current)
        return {"id": handle.id, "tag": tag_name, "enabled": True}, []

    def _on_pyrungRemoveMonitor(self, args: dict[str, Any]) -> HandlerResult:
        raw_id = args.get("id")
        if not isinstance(raw_id, int):
            raise DAPAdapterError("pyrungRemoveMonitor.id must be an integer")
        with self._state_lock:
            handle = self._monitor_handles.pop(raw_id, None)
            if handle is not None:
                handle.remove()
            removed = raw_id in self._monitor_meta
            self._monitor_meta.pop(raw_id, None)
            self._monitor_values.pop(raw_id, None)
        return {"id": raw_id, "removed": removed}, []

    def _on_pyrungListMonitors(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            monitors = []
            for monitor_id in sorted(self._monitor_meta):
                meta = self._monitor_meta[monitor_id]
                monitors.append(
                    {
                        "id": monitor_id,
                        "tag": meta["tag"],
                        "enabled": bool(meta.get("enabled", True)),
                        "value": self._monitor_values.get(monitor_id, "None"),
                    }
                )
        return {"monitors": monitors}, []

    def _on_pyrungFindLabel(self, args: dict[str, Any]) -> HandlerResult:
        label = args.get("label")
        if not isinstance(label, str) or not label.strip():
            raise DAPAdapterError("pyrungFindLabel.label is required")
        find_all = bool(args.get("all", False))

        with self._state_lock:
            runner = self._require_runner_locked()
            if find_all:
                states = runner.history.find_all(label)
            else:
                latest = runner.history.find(label)
                states = [] if latest is None else [latest]

        matches = [{"scanId": state.scan_id, "timestamp": state.timestamp} for state in states]
        return {"matches": matches}, []

    def _continue_worker(self) -> None:
        try:
            self._pending_predicate_pause = False
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    self._queue.put(
                        {
                            "kind": "internal_event",
                            "event": "stopped",
                            "body": self._stopped_body("pause"),
                        }
                    )
                    return

                with self._state_lock:
                    if self._runner is None:
                        return
                    advanced = self._advance_one_step_locked()
                    self._flush_pending_snapshots_locked()
                    hit_breakpoint = self._current_rung_hits_breakpoint_locked()
                    self._flush_pending_snapshots_locked()
                    hit_data_breakpoint = self._pending_predicate_pause
                    if hit_data_breakpoint:
                        self._pending_predicate_pause = False

                if hit_breakpoint:
                    self._queue.put(
                        {
                            "kind": "internal_event",
                            "event": "stopped",
                            "body": self._stopped_body("breakpoint"),
                        }
                    )
                    return

                if hit_data_breakpoint:
                    self._queue.put(
                        {
                            "kind": "internal_event",
                            "event": "stopped",
                            "body": self._stopped_body("data breakpoint"),
                        }
                    )
                    return

                if not advanced:
                    time.sleep(0.005)
        finally:
            with self._state_lock:
                self._continue_thread = None
                self._pending_predicate_pause = False
            self._pause_event.clear()

    def _advance_one_step_locked(self) -> bool:
        runner = self._require_runner_locked()
        if not self._top_level_rungs(runner):
            runner.step()
            self._scan_gen = None
            self._current_scan_id = None
            self._current_step = None
            self._current_rung_index = None
            self._current_rung = None
            self._current_ctx = None
            return False

        if self._scan_gen is None:
            self._scan_gen = runner.scan_steps_debug()
            self._current_scan_id = runner.current_state.scan_id + 1

        try:
            step = next(self._scan_gen)
        except StopIteration:
            self._scan_gen = runner.scan_steps_debug()
            self._current_scan_id = runner.current_state.scan_id + 1
            step = next(self._scan_gen)

        self._current_step = step
        self._current_rung_index = step.rung_index
        self._current_rung = step.rung
        self._current_ctx = step.ctx
        return True

    def _advance_with_step_logpoints_locked(self) -> bool:
        advanced = self._advance_one_step_locked()
        self._flush_pending_snapshots_locked()
        self._process_logpoints_for_current_rung_locked()
        self._flush_pending_snapshots_locked()
        return advanced

    def _advance_one_rung_locked(self) -> bool:
        """Backward-compatible alias for tests/helpers."""
        return self._advance_one_step_locked()

    def _assert_can_step_locked(self) -> None:
        self._require_runner_locked()
        if self._thread_running_locked():
            raise DAPAdapterError("Cannot step while continue is running")

    def _thread_running_locked(self) -> bool:
        return self._continue_thread is not None and self._continue_thread.is_alive()

    def _current_rung_hits_breakpoint_locked(self) -> bool:
        if self._current_rung is None:
            return False
        source = self._canonical_path(self._current_rung.source_file)
        if source is None:
            return False
        file_breakpoints = self._source_breakpoints_by_file.get(source)
        if not file_breakpoints:
            return False
        if self._current_rung.source_line is None:
            return False
        runner = self._runner
        if runner is None:
            return False

        start_line = int(self._current_rung.source_line)
        end_line = int(self._current_rung.end_line or self._current_rung.source_line)
        if end_line < start_line:
            start_line, end_line = end_line, start_line

        scan_id = self._current_scan_id
        for line, breakpoint in file_breakpoints.items():
            if not breakpoint.enabled:
                continue
            if not (start_line <= line <= end_line):
                continue
            if scan_id is not None and breakpoint.last_scan_id == scan_id:
                continue
            breakpoint.last_scan_id = scan_id

            if breakpoint.condition is not None and not breakpoint.condition(runner.current_state):
                continue

            if not self._source_breakpoint_hit_matches_locked(breakpoint):
                continue

            if breakpoint.log_message is not None:
                self._handle_logpoint_hit_locked(
                    breakpoint,
                    runner.current_state,
                    active_scan_id=scan_id,
                )
                continue

            return True

        return False

    def _process_logpoints_for_current_rung_locked(self) -> None:
        if self._current_rung is None:
            return
        source = self._canonical_path(self._current_rung.source_file)
        if source is None:
            return
        file_breakpoints = self._source_breakpoints_by_file.get(source)
        if not file_breakpoints:
            return
        if self._current_rung.source_line is None:
            return
        runner = self._runner
        if runner is None:
            return

        start_line = int(self._current_rung.source_line)
        end_line = int(self._current_rung.end_line or self._current_rung.source_line)
        if end_line < start_line:
            start_line, end_line = end_line, start_line

        scan_id = self._current_scan_id
        for line, breakpoint in file_breakpoints.items():
            if breakpoint.log_message is None:
                continue
            if not breakpoint.enabled:
                continue
            if not (start_line <= line <= end_line):
                continue
            if scan_id is not None and breakpoint.last_scan_id == scan_id:
                continue
            breakpoint.last_scan_id = scan_id

            if breakpoint.condition is not None and not breakpoint.condition(runner.current_state):
                continue

            if not self._source_breakpoint_hit_matches_locked(breakpoint):
                continue

            self._handle_logpoint_hit_locked(
                breakpoint,
                runner.current_state,
                active_scan_id=scan_id,
            )

    def _source_breakpoint_hit_matches_locked(self, breakpoint: _SourceBreakpoint) -> bool:
        hit_condition = breakpoint.hit_condition
        if hit_condition is None:
            return True
        breakpoint.hit_count += 1
        if breakpoint.hit_count != hit_condition:
            return False
        breakpoint.hit_count = 0
        return True

    def _rebuild_breakpoint_index_locked(self) -> None:
        self._breakpoint_rung_map = {}
        self._subroutine_source_map = {}
        runner = self._require_runner_locked()
        visited_rungs: set[int] = set()
        visited_programs: set[int] = set()
        for rung in self._top_level_rungs(runner):
            self._index_rung_lines(
                rung=rung, visited_rungs=visited_rungs, visited_programs=visited_programs
            )

    def _index_rung_lines(
        self,
        *,
        rung: Rung,
        visited_rungs: set[int],
        visited_programs: set[int],
    ) -> None:
        rung_id = id(rung)
        if rung_id in visited_rungs:
            return
        visited_rungs.add(rung_id)

        self._index_rung_range(
            source_file=rung.source_file,
            source_line=rung.source_line,
            end_line=rung.end_line,
        )
        for instruction in rung._instructions:
            self._index_instruction_lines(
                instruction=instruction,
                fallback_source_file=rung.source_file,
                visited_rungs=visited_rungs,
                visited_programs=visited_programs,
            )
        for branch in rung._branches:
            self._index_rung_lines(
                rung=branch,
                visited_rungs=visited_rungs,
                visited_programs=visited_programs,
            )

    def _index_instruction_lines(
        self,
        *,
        instruction: Instruction,
        fallback_source_file: str | None,
        visited_rungs: set[int],
        visited_programs: set[int],
    ) -> None:
        source_file = getattr(instruction, "source_file", None) or fallback_source_file
        source_line = getattr(instruction, "source_line", None)
        self._index_line(source_file, source_line)
        debug_substeps = getattr(instruction, "debug_substeps", None)
        if debug_substeps:
            for substep in debug_substeps:
                self._index_line(
                    getattr(substep, "source_file", None) or source_file,
                    getattr(substep, "source_line", None),
                )

        if isinstance(instruction, CallInstruction):
            self._index_subroutine_lines_for_call(
                instruction=instruction,
                visited_rungs=visited_rungs,
                visited_programs=visited_programs,
            )

        nested = getattr(instruction, "instructions", None)
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, Instruction):
                    self._index_instruction_lines(
                        instruction=child,
                        fallback_source_file=source_file,
                        visited_rungs=visited_rungs,
                        visited_programs=visited_programs,
                    )

    def _index_subroutine_lines_for_call(
        self,
        *,
        instruction: CallInstruction,
        visited_rungs: set[int],
        visited_programs: set[int],
    ) -> None:
        program = getattr(instruction, "_program", None)
        if program is None:
            return
        program_id = id(program)
        if program_id in visited_programs:
            return
        visited_programs.add(program_id)
        for subroutine_name, subroutine_rungs in program.subroutines.items():
            self._index_subroutine_source(subroutine_name=subroutine_name, rungs=subroutine_rungs)
            for rung in subroutine_rungs:
                self._index_rung_lines(
                    rung=rung,
                    visited_rungs=visited_rungs,
                    visited_programs=visited_programs,
                )

    def _index_subroutine_source(self, *, subroutine_name: str, rungs: list[Rung]) -> None:
        if subroutine_name in self._subroutine_source_map:
            return
        for rung in rungs:
            source_path = self._canonical_path(rung.source_file)
            if source_path is None or rung.source_line is None:
                continue
            end_line = int(rung.end_line) if rung.end_line is not None else None
            self._subroutine_source_map[subroutine_name] = (
                source_path,
                int(rung.source_line),
                end_line,
            )
            return

    def _index_rung_range(
        self,
        *,
        source_file: str | None,
        source_line: int | None,
        end_line: int | None,
    ) -> None:
        if source_line is None:
            return
        start_line = int(source_line)
        final_line = int(end_line) if end_line is not None else start_line
        if final_line < start_line:
            start_line, final_line = final_line, start_line
        for line in range(start_line, final_line + 1):
            self._index_line(source_file, line)

    def _index_line(self, source_file: str | None, source_line: int | None) -> None:
        canonical = self._canonical_path(source_file)
        if canonical is None or source_line is None:
            return
        lines = self._breakpoint_rung_map.setdefault(canonical, set())
        lines.add(int(source_line))

    def _canonical_path(self, path: str | None) -> str | None:
        if path is None or path.startswith("<"):
            return None
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def _build_current_stack_frames(
        self,
        *,
        current_step: ScanStep,
        rungs: list[Rung],
    ) -> list[dict[str, Any]]:
        step_name = f"Rung {current_step.rung_index}"
        if current_step.kind == "branch":
            step_name = f"Branch (rung {current_step.rung_index})"
        elif current_step.kind == "subroutine":
            sub_name = current_step.subroutine_name or "subroutine"
            step_name = f"{sub_name} (rung {current_step.rung_index})"
        elif current_step.kind == "instruction":
            kind_name = current_step.instruction_kind or "Instruction"
            if current_step.subroutine_name:
                step_name = (
                    f"{kind_name} ({current_step.subroutine_name}, rung {current_step.rung_index})"
                )
            else:
                step_name = f"{kind_name} (rung {current_step.rung_index})"

        frames: list[dict[str, Any]] = [
            self._stack_frame_from_step(
                frame_id=0,
                name=step_name,
                step=current_step,
            )
        ]

        next_frame_id = 1
        for depth, subroutine_name in enumerate(reversed(current_step.call_stack)):
            innermost = depth == 0 and current_step.subroutine_name == subroutine_name
            frames.append(
                self._stack_frame_from_subroutine(
                    frame_id=next_frame_id,
                    subroutine_name=subroutine_name,
                    current_step=current_step,
                    innermost=innermost,
                )
            )
            next_frame_id += 1

        if current_step.kind != "rung" and 0 <= current_step.rung_index < len(rungs):
            frames.append(
                self._stack_frame_from_rung(
                    frame_id=next_frame_id,
                    name=f"Rung {current_step.rung_index}",
                    rung=rungs[current_step.rung_index],
                )
            )

        return frames

    def _stack_frame_from_step(
        self,
        *,
        frame_id: int,
        name: str,
        step: ScanStep,
    ) -> dict[str, Any]:
        source_line = int(step.source_line or step.rung.source_line or 1)
        frame: dict[str, Any] = {
            "id": frame_id,
            "name": name,
            "line": source_line,
            "column": 1,
        }
        end_line = step.end_line or step.source_line
        if end_line is not None:
            frame["endLine"] = int(end_line)
        source_file = step.source_file or step.rung.source_file
        if source_file:
            source_path = str(Path(source_file))
            frame["source"] = {"name": Path(source_path).name, "path": source_path}
        return frame

    def _stack_frame_from_rung(
        self,
        *,
        frame_id: int,
        name: str,
        rung: Rung,
    ) -> dict[str, Any]:
        frame: dict[str, Any] = {
            "id": frame_id,
            "name": name,
            "line": int(rung.source_line or 1),
            "column": 1,
        }
        if rung.end_line is not None:
            frame["endLine"] = int(rung.end_line)
        if rung.source_file:
            source_path = str(Path(rung.source_file))
            frame["source"] = {"name": Path(source_path).name, "path": source_path}
        return frame

    def _stack_frame_from_subroutine(
        self,
        *,
        frame_id: int,
        subroutine_name: str,
        current_step: ScanStep,
        innermost: bool,
    ) -> dict[str, Any]:
        source_location: tuple[str, int, int | None] | None = None
        if innermost:
            source_location = self._subroutine_source_from_step_rung(current_step)
        if source_location is None:
            source_location = self._subroutine_source_map.get(subroutine_name)

        frame: dict[str, Any] = {
            "id": frame_id,
            "name": f"Subroutine {subroutine_name}",
            "line": 1,
            "column": 1,
        }
        if source_location is None:
            return frame

        source_path, source_line, end_line = source_location
        frame["line"] = int(source_line)
        if end_line is not None:
            frame["endLine"] = int(end_line)
        frame["source"] = {"name": Path(source_path).name, "path": source_path}
        return frame

    def _subroutine_source_from_step_rung(
        self,
        step: ScanStep,
    ) -> tuple[str, int, int | None] | None:
        source_path = self._canonical_path(step.rung.source_file)
        if source_path is None or step.rung.source_line is None:
            return None
        end_line = int(step.rung.end_line) if step.rung.end_line is not None else None
        return source_path, int(step.rung.source_line), end_line

    def _compile_condition(self, source: str) -> Callable[[SystemState], bool]:
        try:
            expr = parse_condition(source)
        except ExpressionParseError as exc:
            raise DAPAdapterError(str(exc)) from exc
        return compile_condition(expr)

    def _parse_hit_condition(self, raw_value: Any) -> int | None:
        if raw_value is None:
            return None
        if isinstance(raw_value, int):
            if raw_value <= 0:
                raise DAPAdapterError("hitCondition must be >= 1")
            return raw_value
        if isinstance(raw_value, str):
            text = raw_value.strip()
            if not text:
                return None
            if text.isdigit():
                parsed = int(text)
                if parsed <= 0:
                    raise DAPAdapterError("hitCondition must be >= 1")
                return parsed
        raise DAPAdapterError("hitCondition must be a positive integer")

    def _monitor_variables(self) -> list[dict[str, Any]]:
        variables: list[dict[str, Any]] = []
        for monitor_id in sorted(self._monitor_meta):
            meta = self._monitor_meta[monitor_id]
            tag_name = str(meta.get("tag"))
            variables.append(
                {
                    "name": tag_name,
                    "value": self._monitor_values.get(monitor_id, "None"),
                    "type": "monitor",
                    "evaluateName": tag_name,
                    "variablesReference": 0,
                }
            )
        return variables

    def _build_monitor_callback(
        self,
        *,
        tag_name: str,
        monitor_id_ref: dict[str, int],
    ) -> Callable[[Any, Any], None]:
        def _callback(current: Any, previous: Any) -> None:
            try:
                monitor_id = monitor_id_ref.get("id")
                if not isinstance(monitor_id, int) or monitor_id <= 0:
                    return
                if monitor_id not in self._monitor_handles:
                    return
                self._monitor_values[monitor_id] = self._format_value(current)
                runner = self._runner
                if runner is None:
                    return
                state = runner.current_state
                self._queue.put(
                    {
                        "kind": "internal_event",
                        "event": "pyrungMonitor",
                        "body": {
                            "id": monitor_id,
                            "tag": tag_name,
                            "current": self._format_value(current),
                            "previous": self._format_value(previous),
                            "scanId": state.scan_id,
                            "timestamp": state.timestamp,
                        },
                    }
                )
            except Exception:
                return

        return _callback

    def _build_data_breakpoint_callback(self, *, data_id: str) -> Callable[[Any, Any], None]:
        def _callback(_current: Any, _previous: Any) -> None:
            try:
                meta = self._data_bp_meta.get(data_id)
                runner = self._runner
                if meta is None or runner is None:
                    return

                condition = meta.get("condition")
                if callable(condition) and not condition(runner.current_state):
                    return

                hit_condition = meta.get("hitCondition")
                hit_count = int(meta.get("hitCount", 0)) + 1
                if hit_condition is None:
                    meta["hitCount"] = hit_count
                else:
                    if hit_count != int(hit_condition):
                        meta["hitCount"] = hit_count
                        return
                    meta["hitCount"] = 0

                self._pending_predicate_pause = True
            except Exception:
                return

        return _callback

    def _data_id_for_tag(self, tag_name: str) -> str:
        return f"tag:{tag_name}"

    def _tag_from_data_id(self, data_id: str) -> str | None:
        prefix = "tag:"
        if not data_id.startswith(prefix):
            return None
        tag_name = data_id[len(prefix) :]
        return tag_name or None

    def _handle_logpoint_hit_locked(
        self,
        breakpoint: _SourceBreakpoint,
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
        self._queue.put(
            {
                "kind": "internal_event",
                "event": "output",
                "body": {"category": "console", "output": f"{message}\n"},
            }
        )

    def _record_snapshot_locked(self, *, label: str, state: SystemState) -> None:
        runner = self._runner
        if runner is None:
            return
        runner.history._label_scan(label, state.scan_id)
        self._queue.put(
            {
                "kind": "internal_event",
                "event": "pyrungSnapshot",
                "body": {
                    "label": label,
                    "scanId": state.scan_id,
                    "timestamp": state.timestamp,
                },
            }
        )
        self._queue.put(
            {
                "kind": "internal_event",
                "event": "output",
                "body": {
                    "category": "console",
                    "output": f"Snapshot taken: {label} (scan {state.scan_id})\n",
                },
            }
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
        for handle in self._monitor_handles.values():
            try:
                handle.remove()
            except Exception:
                continue
        for handle in self._data_bp_handles.values():
            try:
                handle.remove()
            except Exception:
                continue
        self._monitor_handles.clear()
        self._monitor_meta.clear()
        self._monitor_values.clear()
        self._data_bp_handles.clear()
        self._data_bp_meta.clear()
        self._source_breakpoints_by_file.clear()
        self._pending_snapshot_labels_by_scan.clear()
        self._pending_predicate_pause = False

    def _require_runner_locked(self) -> PLCRunner:
        if self._runner is None:
            raise DAPAdapterError("No program launched")
        return self._runner

    def _shutdown(self) -> HandlerResult:
        with self._state_lock:
            self._clear_debug_registrations_locked()
            self._runner = None
            self._scan_gen = None
            self._current_scan_id = None
            self._current_step = None
            self._current_rung_index = None
            self._current_rung = None
            self._current_ctx = None
        self._stop_event.set()
        self._pause_event.set()
        return {}, [("terminated", {})]

    def _top_level_rungs(self, runner: PLCRunner) -> list[Rung]:
        """Return top-level rungs through the runner's public debug API."""
        return list(runner.iter_top_level_rungs())

    def _current_trace_body_locked(self) -> dict[str, Any] | None:
        runner = self._runner
        if runner is None:
            return None

        event_result = runner.inspect_event()
        if event_result is None:
            return None

        scan_id, rung_index, event = event_result
        trace_source = "live" if scan_id > runner.current_state.scan_id else "inspect"
        trace = event.trace if isinstance(event.trace, TraceEvent) else None
        step_kind: str | None = event.kind
        instruction_kind: str | None = event.instruction_kind
        enabled_state: str | None = event.enabled_state
        subroutine_name: str | None = event.subroutine_name
        call_stack: list[str] = list(event.call_stack)
        source_line: int | None = event.source_line
        end_line: int | None = event.end_line if event.end_line is not None else event.source_line
        step_source_file = event.source_file

        regions = self._regions_from_trace_event(trace)
        step_source = None
        step_source_path = self._canonical_path(step_source_file)
        if step_source_path:
            step_source = {"name": Path(step_source_path).name, "path": step_source_path}

        display_status = self._step_display_status_from_fields(enabled_state=enabled_state)
        display_text = self._step_display_text_from_fields(
            kind=step_kind,
            instruction_kind=instruction_kind,
            display_status=display_status,
        )

        return {
            "traceVersion": self.TRACE_VERSION,
            "traceSource": trace_source,
            "scanId": scan_id,
            "rungId": rung_index,
            "step": {
                "kind": step_kind,
                "instructionKind": instruction_kind,
                "enabledState": enabled_state,
                "displayStatus": display_status,
                "displayText": display_text,
                "source": step_source,
                "line": source_line,
                "endLine": end_line if end_line is not None else source_line,
                "subroutineName": subroutine_name,
                "callStack": call_stack,
                "rungIndex": rung_index,
            },
            "regions": regions,
        }

    def _regions_from_trace_event(self, trace: TraceEvent | None) -> list[dict[str, Any]]:
        regions: list[dict[str, Any]] = []
        if not isinstance(trace, TraceEvent):
            return regions

        for region in trace.regions:
            source_body = None
            source_path = (
                self._canonical_path(region.source.source_file)
                if isinstance(region.source.source_file, str)
                else None
            )
            if source_path:
                source_body = {"name": Path(source_path).name, "path": source_path}

            conditions: list[dict[str, Any]] = []
            for cond in region.conditions:
                cond_source = None
                cond_path = (
                    self._canonical_path(cond.source_file)
                    if isinstance(cond.source_file, str)
                    else None
                )
                if cond_path:
                    cond_source = {"name": Path(cond_path).name, "path": cond_path}
                details = [
                    {
                        "name": str(detail.get("name", "")),
                        "value": self._format_value(detail.get("value")),
                    }
                    for detail in cond.details
                    if isinstance(detail, dict)
                ]
                conditions.append(
                    {
                        "source": cond_source,
                        "line": cond.source_line,
                        "expression": cond.expression,
                        "status": cond.status,
                        "value": cond.value,
                        "details": details,
                        "summary": cond.summary,
                        "annotation": cond.annotation,
                    }
                )

            regions.append(
                {
                    "kind": region.kind,
                    "enabledState": region.enabled_state,
                    "source": source_body,
                    "line": region.source.source_line,
                    "endLine": region.source.end_line,
                    "conditions": conditions,
                }
            )

        return regions

    def _step_display_status(self, step: ScanStep) -> str:
        return self._step_display_status_from_fields(enabled_state=step.enabled_state)

    def _step_display_text(self, step: ScanStep) -> str:
        return self._step_display_text_from_fields(
            kind=step.kind,
            instruction_kind=step.instruction_kind,
            display_status=self._step_display_status(step),
        )

    def _step_display_status_from_fields(self, *, enabled_state: str | None) -> str:
        if enabled_state == "enabled":
            return "enabled"
        if enabled_state == "disabled_parent":
            return "skipped"
        return "disabled"

    def _step_display_text_from_fields(
        self,
        *,
        kind: str | None,
        instruction_kind: str | None,
        display_status: str,
    ) -> str:
        if display_status == "enabled":
            prefix = "[RUN]" if kind == "instruction" else "[ON]"
        elif display_status == "skipped":
            prefix = "[SKIP]"
        else:
            prefix = "[OFF]"

        if kind == "instruction":
            label = instruction_kind or "Instruction"
        elif kind == "branch":
            label = "Branch"
        elif kind == "subroutine":
            label = "Subroutine"
        else:
            label = "Rung"
        return f"{prefix} {label}"

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
