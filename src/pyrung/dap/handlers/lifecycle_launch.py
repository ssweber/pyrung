"""Lifecycle/launch ownership for DAP command handling.

Owns initialization, launch discovery, and shutdown state reset paths.
Must not change protocol payload shapes or launch/shutdown side effects.
"""

from __future__ import annotations

import os
import runpy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyrung.core import PLCRunner, Program

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


def on_initialize(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
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


def on_configuration_done(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    return {}, []


def on_disconnect(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    return adapter._shutdown()


def on_terminate(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    return adapter._shutdown()


def on_threads(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    return {"threads": [{"id": adapter.THREAD_ID, "name": "PLC Scan"}]}, []


def on_launch(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_LaunchRequestArgs, args)
    program_arg = parsed.program
    if not isinstance(program_arg, str) or not program_arg.strip():
        raise adapter.DAPAdapterError("launch.program must be a Python file path")

    program_path = Path(program_arg).expanduser().resolve()
    if not program_path.is_file():
        raise adapter.DAPAdapterError(f"launch.program file not found: {program_path}")

    previous_dap_flag = os.environ.get("PYRUNG_DAP_ACTIVE")
    os.environ["PYRUNG_DAP_ACTIVE"] = "1"
    try:
        namespace = runpy.run_path(str(program_path), run_name="__main__")
    finally:
        if previous_dap_flag is None:
            os.environ.pop("PYRUNG_DAP_ACTIVE", None)
        else:
            os.environ["PYRUNG_DAP_ACTIVE"] = previous_dap_flag
    runner = adapter._discover_runner(namespace)

    with adapter._state_lock:
        if adapter._thread_running_locked():
            raise adapter.DAPAdapterError("Cannot launch while continue is running")
        adapter._clear_debug_registrations_locked()
        adapter._runner = runner
        adapter._scan_gen = None
        adapter._current_scan_id = None
        adapter._current_step = None
        adapter._current_rung_index = None
        adapter._current_rung = None
        adapter._current_ctx = None
        adapter._program_path = str(program_path)
        adapter._breakpoints.clear()
        adapter._pending_predicate_pause = False
        adapter._rebuild_breakpoint_index_locked()

    return {}, [("stopped", adapter._stopped_body("entry"))]


def discover_runner(adapter: Any, namespace: dict[str, Any]) -> PLCRunner:
    named_runner = namespace.get("runner")
    if isinstance(named_runner, PLCRunner):
        return named_runner

    runners = adapter._unique_instances(namespace.values(), PLCRunner)
    if len(runners) == 1:
        return runners[0]

    programs = adapter._unique_instances(namespace.values(), Program)
    if len(programs) == 1:
        return PLCRunner(programs[0])

    raise adapter.DAPAdapterError(
        "Launch script must provide 'runner' as PLCRunner, or define exactly one PLCRunner "
        f"or exactly one Program. Found {len(runners)} PLCRunner(s), {len(programs)} Program(s)."
    )


def shutdown(adapter: Any) -> HandlerResult:
    with adapter._state_lock:
        adapter._clear_debug_registrations_locked()
        adapter._runner = None
        adapter._scan_gen = None
        adapter._current_scan_id = None
        adapter._current_step = None
        adapter._current_rung_index = None
        adapter._current_rung = None
        adapter._current_ctx = None
    adapter._stop_event.set()
    adapter._pause_event.set()
    return {}, [("terminated", {})]


@dataclass(frozen=True)
class _LaunchRequestArgs:
    program: Any = None
