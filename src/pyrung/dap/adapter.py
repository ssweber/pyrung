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
from pyrung.core.debug_trace import RungTraceEvent, TraceEvent
from pyrung.core.instruction import CallInstruction, Instruction
from pyrung.core.rung import Rung
from pyrung.core.runner import ScanStep
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

    TRACE_VERSION = 1
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
        self._scan_gen: Generator[ScanStep, None, None] | None = None
        self._current_scan_id: int | None = None
        self._current_step: ScanStep | None = None
        self._current_rung_index: int | None = None
        self._current_rung: Rung | None = None
        self._current_ctx: ScanContext | None = None
        self._last_committed_scan_id: int | None = None
        self._last_committed_rung_index: int | None = None
        self._program_path: str | None = None

        self._breakpoints_by_file: dict[str, set[int]] = {}
        self._breakpoint_rung_map: dict[str, set[int]] = {}
        self._subroutine_source_map: dict[str, tuple[str, int, int | None]] = {}

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
            self._runner = runner
            self._scan_gen = None
            self._current_scan_id = None
            self._current_step = None
            self._current_rung_index = None
            self._current_rung = None
            self._current_ctx = None
            self._last_committed_scan_id = None
            self._last_committed_rung_index = None
            self._program_path = str(program_path)
            self._breakpoints_by_file = {}
            self._breakpoint_rung_map = {}
            self._subroutine_source_map = {}
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
            self._advance_one_step_locked()
            while self._current_step is not None and self._current_step.kind != "rung":
                if not self._advance_one_step_locked():
                    break
        return {}, [("stopped", self._stopped_body("step"))]

    def _on_stepIn(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            self._assert_can_step_locked()
            self._advance_one_step_locked()
            while self._current_step is not None and self._current_step.kind in {
                "branch",
                "rung",
                "subroutine",
            }:
                if not self._advance_one_step_locked():
                    break
        return {}, [("stopped", self._stopped_body("step"))]

    def _on_stepOut(self, _args: dict[str, Any]) -> HandlerResult:
        with self._state_lock:
            self._assert_can_step_locked()
            origin_step = self._current_step
            origin_ctx = self._current_ctx
            if origin_step is None:
                self._advance_one_step_locked()
            else:
                origin_depth = origin_step.depth
                origin_stack_len = len(origin_step.call_stack)
                while True:
                    if not self._advance_one_step_locked():
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

        requested_lines = self._requested_breakpoint_lines(args)
        with self._state_lock:
            valid_lines = self._breakpoint_rung_map.get(canonical, set())
            verified_lines = {line for line in requested_lines if line in valid_lines}
            self._breakpoints_by_file[canonical] = verified_lines

        response_bps = [
            {"verified": line in verified_lines, "line": line} for line in requested_lines
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
            self._last_committed_scan_id = runner.current_state.scan_id
            self._last_committed_rung_index = None
            return False

        if self._scan_gen is None:
            self._scan_gen = runner.scan_steps_debug()
            self._current_scan_id = runner.current_state.scan_id + 1

        try:
            step = next(self._scan_gen)
        except StopIteration:
            # Exhausting the scan generator commits the in-flight scan.
            previous_step = self._current_step
            committed_scan_id = runner.current_state.scan_id
            if previous_step is not None:
                self._last_committed_scan_id = committed_scan_id
                self._last_committed_rung_index = previous_step.rung_index

            self._scan_gen = runner.scan_steps_debug()
            self._current_scan_id = runner.current_state.scan_id + 1
            step = next(self._scan_gen)

        self._current_step = step
        self._current_rung_index = step.rung_index
        self._current_rung = step.rung
        self._current_ctx = step.ctx
        return True

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
        active_lines = self._breakpoints_by_file.get(source)
        if not active_lines:
            return False
        if self._current_rung.source_line is None:
            return False
        start_line = int(self._current_rung.source_line)
        end_line = int(self._current_rung.end_line or self._current_rung.source_line)
        if end_line < start_line:
            start_line, end_line = end_line, start_line
        return any(start_line <= line <= end_line for line in active_lines)

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

    def _require_runner_locked(self) -> PLCRunner:
        if self._runner is None:
            raise DAPAdapterError("No program launched")
        return self._runner

    def _shutdown(self) -> HandlerResult:
        self._stop_event.set()
        self._pause_event.set()
        return {}, [("terminated", {})]

    def _top_level_rungs(self, runner: PLCRunner) -> list[Rung]:
        """Return top-level rungs through the runner's public debug API."""
        return list(runner.iter_top_level_rungs())

    def _current_trace_body_locked(self) -> dict[str, Any] | None:
        runner = self._runner
        step = self._current_step

        trace_source = "live"
        trace: TraceEvent | None = None
        if step is not None and isinstance(step.trace, TraceEvent):
            trace = step.trace

        step_kind: str | None = step.kind if step is not None else None
        instruction_kind: str | None = step.instruction_kind if step is not None else None
        enabled_state: str | None = step.enabled_state if step is not None else None
        subroutine_name: str | None = step.subroutine_name if step is not None else None
        call_stack: list[str] = list(step.call_stack) if step is not None else []
        rung_index: int | None = step.rung_index if step is not None else None
        source_line: int | None = (
            step.source_line or step.rung.source_line if step is not None else None
        )
        end_line: int | None = (
            step.end_line or step.source_line or step.rung.end_line if step is not None else None
        )
        step_source_file = (step.source_file or step.rung.source_file) if step is not None else None
        scan_id = self._current_scan_id

        if trace is None:
            preferred_scan_id: int | None = None
            preferred_rung_id: int | None = None
            if (
                runner is not None
                and step is not None
                and self._current_scan_id is not None
                and self._current_scan_id <= runner.current_state.scan_id
            ):
                preferred_scan_id = self._current_scan_id
                preferred_rung_id = step.rung_index

            inspect_event = self._inspect_trace_event_locked(
                preferred_scan_id=preferred_scan_id,
                preferred_rung_id=preferred_rung_id,
            )
            if inspect_event is not None:
                inspect_scan_id, inspect_rung_id, event = inspect_event
                if isinstance(event.trace, TraceEvent):
                    trace_source = "inspect"
                    trace = event.trace
                    scan_id = inspect_scan_id
                    rung_index = inspect_rung_id
                    if step is None:
                        step_kind = event.kind
                        instruction_kind = event.instruction_kind
                        enabled_state = event.enabled_state
                        subroutine_name = event.subroutine_name
                        call_stack = list(event.call_stack)
                        source_line = event.source_line
                        end_line = event.end_line
                        step_source_file = event.source_file

        if step is None and step_kind is None:
            return None

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

    def _inspect_trace_event_locked(
        self,
        *,
        preferred_scan_id: int | None,
        preferred_rung_id: int | None,
    ) -> tuple[int, int, RungTraceEvent] | None:
        runner = self._runner
        if runner is None:
            return None

        candidates: list[tuple[int, int]] = []
        if preferred_scan_id is not None and preferred_rung_id is not None:
            candidates.append((preferred_scan_id, preferred_rung_id))
        if self._last_committed_scan_id is not None and self._last_committed_rung_index is not None:
            fallback = (self._last_committed_scan_id, self._last_committed_rung_index)
            if fallback not in candidates:
                candidates.append(fallback)

        for scan_id, rung_id in candidates:
            try:
                trace = runner.inspect(rung_id=rung_id, scan_id=scan_id)
            except KeyError:
                continue

            for event in reversed(trace.events):
                if isinstance(event.trace, TraceEvent):
                    return trace.scan_id, trace.rung_id, event

        return None

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
