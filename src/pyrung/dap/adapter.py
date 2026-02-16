"""Debug Adapter Protocol server for pyrung."""

from __future__ import annotations

import os
import queue
import runpy
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any, BinaryIO

from pyrung.core import PLCRunner, Program
from pyrung.core.context import ScanContext
from pyrung.core.instruction import Instruction
from pyrung.core.rung import Rung
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


class DAPAdapter:
    """Minimal single-thread DAP adapter with async continue support."""

    THREAD_ID = 1
    TAGS_SCOPE_REF = 1
    FORCES_SCOPE_REF = 2
    MEMORY_SCOPE_REF = 3

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
        self._scan_gen: Generator[tuple[int, Rung, ScanContext], None, None] | None = None
        self._current_rung_index: int | None = None
        self._current_rung: Rung | None = None
        self._current_ctx: ScanContext | None = None
        self._program_path: str | None = None

        self._breakpoints_by_file: dict[str, set[int]] = {}
        self._breakpoint_rung_map: dict[str, dict[int, set[int]]] = {}

    def run(self) -> None:
        """Run the adapter loop until EOF or disconnect."""
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="pyrung-dap-reader")
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

    def _on_initialize(self, _args: dict[str, Any]) -> HandlerResult:
        capabilities = {
            "supportsConfigurationDoneRequest": True,
            "supportsEvaluateForHovers": False,
            "supportsStepBack": False,
        }
        return capabilities, [("initialized", {})]

    def _on_configurationDone(self, _args: dict[str, Any]) -> HandlerResult:
        return {}, []

    def _on_disconnect(self, _args: dict[str, Any]) -> HandlerResult:
        self._stop_event.set()
        self._pause_event.set()
        return {}, [("terminated", {})]

    def _on_threads(self, _args: dict[str, Any]) -> HandlerResult:
        return {"threads": [{"id": self.THREAD_ID, "name": "PLC Scan"}]}, []

    def _on_launch(self, args: dict[str, Any]) -> HandlerResult:
        program_arg = args.get("program")
        if not isinstance(program_arg, str) or not program_arg.strip():
            raise DAPAdapterError("launch.program must be a Python file path")

        program_path = Path(program_arg).expanduser().resolve()
        if not program_path.is_file():
            raise DAPAdapterError(f"launch.program file not found: {program_path}")

        namespace = runpy.run_path(str(program_path), run_name="__main__")
        runner = self._discover_runner(namespace)

        with self._state_lock:
            if self._thread_running_locked():
                raise DAPAdapterError("Cannot launch while continue is running")
            self._runner = runner
            self._scan_gen = None
            self._current_rung_index = None
            self._current_rung = None
            self._current_ctx = None
            self._program_path = str(program_path)
            self._breakpoints_by_file = {}
            self._breakpoint_rung_map = {}
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
            rungs = list(runner._logic)
            current_index = self._current_rung_index
            program_path = self._program_path

        start_frame = int(args.get("startFrame", 0))
        levels = int(args.get("levels", 0))

        frames: list[dict[str, Any]] = []
        if rungs:
            order = list(range(len(rungs)))
            if current_index is not None and 0 <= current_index < len(rungs):
                order = [current_index, *[i for i in order if i != current_index]]

            for idx in order:
                rung = rungs[idx]
                frame: dict[str, Any] = {
                    "id": idx,
                    "name": f"Rung {idx}",
                    "line": int(rung.source_line or 1),
                    "column": 1,
                }
                if rung.end_line is not None:
                    frame["endLine"] = int(rung.end_line)
                if rung.source_file:
                    source_path = str(Path(rung.source_file))
                    frame["source"] = {"name": Path(source_path).name, "path": source_path}
                frames.append(frame)
        else:
            frame = {"id": 0, "name": "Scan", "line": 1, "column": 1}
            if program_path is not None:
                frame["source"] = {"name": Path(program_path).name, "path": program_path}
            frames.append(frame)

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

        raise DAPAdapterError(f"Unknown variablesReference: {ref}")

    def _on_next(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            self._assert_can_step_locked()
            self._advance_one_rung_locked()
        return {}, [("stopped", self._stopped_body("step"))]

    def _on_stepIn(self, _args: dict[str, Any]) -> HandlerResult:
        return self._on_next({})

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

        requested_lines = self._requested_breakpoint_lines(args)
        with self._state_lock:
            line_map = self._breakpoint_rung_map.get(canonical, {})
            verified_lines = {line for line in requested_lines if line in line_map}
            self._breakpoints_by_file[canonical] = verified_lines

        response_bps = [
            {"verified": line in verified_lines, "line": line}
            for line in requested_lines
        ]
        return {"breakpoints": response_bps}, []

    def _requested_breakpoint_lines(self, args: dict[str, Any]) -> list[int]:
        lines: list[int] = []
        raw_lines = args.get("lines")
        if isinstance(raw_lines, list):
            for line in raw_lines:
                if isinstance(line, int):
                    lines.append(line)
        else:
            raw_bps = args.get("breakpoints")
            if isinstance(raw_bps, list):
                for bp in raw_bps:
                    if isinstance(bp, dict) and isinstance(bp.get("line"), int):
                        lines.append(bp["line"])
        return lines

    def _on_evaluate(self, args: dict[str, Any]) -> HandlerResult:
        expression = args.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise DAPAdapterError("evaluate.expression is required")
        parts = expression.strip().split()
        command = parts[0]

        with self._state_lock:
            runner = self._require_runner_locked()
            if command == "force":
                if len(parts) < 3:
                    raise DAPAdapterError("Usage: force <tag> <value>")
                tag = parts[1]
                raw_value = expression.strip().split(None, 2)[2]
                value = self._parse_literal(raw_value)
                runner.add_force(tag, value)
                result = f"Forced {tag}={value!r}"
            elif command == "remove_force":
                if len(parts) != 2:
                    raise DAPAdapterError("Usage: remove_force <tag>")
                tag = parts[1]
                runner.remove_force(tag)
                result = f"Removed force {tag}"
            elif command == "clear_forces":
                runner.clear_forces()
                result = "Cleared all forces"
            else:
                raise DAPAdapterError(
                    "Unsupported evaluate command. Use force/remove_force/clear_forces."
                )

        return {"result": result, "variablesReference": 0}, []

    def _continue_worker(self) -> None:
        try:
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    self._queue.put(
                        {"kind": "internal_event", "event": "stopped", "body": self._stopped_body("pause")}
                    )
                    return

                with self._state_lock:
                    if self._runner is None:
                        return
                    advanced = self._advance_one_rung_locked()
                    hit_breakpoint = self._current_rung_hits_breakpoint_locked()

                if hit_breakpoint:
                    self._queue.put(
                        {
                            "kind": "internal_event",
                            "event": "stopped",
                            "body": self._stopped_body("breakpoint"),
                        }
                    )
                    return

                if not advanced:
                    time.sleep(0.005)
        finally:
            with self._state_lock:
                self._continue_thread = None
            self._pause_event.clear()

    def _advance_one_rung_locked(self) -> bool:
        runner = self._require_runner_locked()
        if not runner._logic:
            runner.step()
            self._scan_gen = None
            self._current_rung_index = None
            self._current_rung = None
            self._current_ctx = None
            return False

        if self._scan_gen is None:
            self._scan_gen = runner.scan_steps()

        try:
            index, rung, ctx = next(self._scan_gen)
        except StopIteration:
            self._scan_gen = runner.scan_steps()
            index, rung, ctx = next(self._scan_gen)

        self._current_rung_index = index
        self._current_rung = rung
        self._current_ctx = ctx
        return True

    def _assert_can_step_locked(self) -> None:
        self._require_runner_locked()
        if self._thread_running_locked():
            raise DAPAdapterError("Cannot step while continue is running")

    def _thread_running_locked(self) -> bool:
        return self._continue_thread is not None and self._continue_thread.is_alive()

    def _current_rung_hits_breakpoint_locked(self) -> bool:
        if self._current_rung_index is None or self._current_rung is None:
            return False
        source = self._canonical_path(self._current_rung.source_file)
        if source is None:
            return False
        active_lines = self._breakpoints_by_file.get(source)
        if not active_lines:
            return False
        line_map = self._breakpoint_rung_map.get(source, {})
        return any(self._current_rung_index in line_map.get(line, set()) for line in active_lines)

    def _rebuild_breakpoint_index_locked(self) -> None:
        self._breakpoint_rung_map = {}
        runner = self._require_runner_locked()
        for index, rung in enumerate(runner._logic):
            self._index_rung_lines(top_level_index=index, rung=rung)

    def _index_rung_lines(self, *, top_level_index: int, rung: Rung) -> None:
        self._index_line(rung.source_file, rung.source_line, top_level_index)
        for instruction in rung._instructions:
            self._index_instruction_lines(
                top_level_index=top_level_index,
                instruction=instruction,
                fallback_source_file=rung.source_file,
            )
        for branch in rung._branches:
            self._index_rung_lines(top_level_index=top_level_index, rung=branch)

    def _index_instruction_lines(
        self,
        *,
        top_level_index: int,
        instruction: Instruction,
        fallback_source_file: str | None,
    ) -> None:
        source_file = getattr(instruction, "source_file", None) or fallback_source_file
        source_line = getattr(instruction, "source_line", None)
        self._index_line(source_file, source_line, top_level_index)

        nested = getattr(instruction, "instructions", None)
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, Instruction):
                    self._index_instruction_lines(
                        top_level_index=top_level_index,
                        instruction=child,
                        fallback_source_file=source_file,
                    )

    def _index_line(self, source_file: str | None, source_line: int | None, rung_index: int) -> None:
        canonical = self._canonical_path(source_file)
        if canonical is None or source_line is None:
            return
        line_map = self._breakpoint_rung_map.setdefault(canonical, {})
        line_map.setdefault(int(source_line), set()).add(rung_index)

    def _canonical_path(self, path: str | None) -> str | None:
        if path is None or path.startswith("<"):
            return None
        return os.path.normcase(os.path.normpath(os.path.abspath(path)))

    def _require_runner_locked(self) -> PLCRunner:
        if self._runner is None:
            raise DAPAdapterError("No program launched")
        return self._runner

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
                processed += 1
                continue
            pending.append(item)
        for item in pending:
            self._queue.put(item)
        return processed
