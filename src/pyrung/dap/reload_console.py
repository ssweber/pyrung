"""Hot-reload console commands for the DAP adapter.

Provides ``reload``, ``watch``, and ``unwatch`` commands that re-execute the
program file while preserving PLC state (tags, memory, forces, time mode).
"""

from __future__ import annotations

import os
import runpy
import threading
from pathlib import Path
from typing import Any

from pyrsistent import pmap

from pyrung.core import PLC
from pyrung.core.state import SystemState
from pyrung.dap.console import ConsoleResult, register

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


def _reload_locked(
    adapter: Any,
) -> tuple[str, list[tuple[str, dict[str, Any] | None]]]:
    """Re-execute the program file and swap the runner, preserving state.

    Must be called with ``adapter._state_lock`` held.  Returns
    ``(summary_text, events)`` for the caller to wrap in a
    ``ConsoleResult`` or emit via ``_enqueue_internal_event``.
    """
    if adapter._thread_running_locked():
        raise adapter.DAPAdapterError("Cannot reload while continue is running")
    if adapter._capture.recording:
        raise adapter.DAPAdapterError("Cannot reload while recording — stop recording first")

    old_runner: PLC = adapter._require_runner_locked()
    old_state: SystemState = old_runner.current_state
    old_forces = dict(old_runner.forces)
    old_time_mode = old_runner._time_mode
    old_dt = old_runner._dt
    old_known_tags = dict(old_runner._known_tags_by_name)
    old_rtc = old_runner._system_runtime._rtc_now(old_state)

    program_path = adapter._program_path
    if not program_path:
        raise adapter.DAPAdapterError("No program loaded — cannot reload")
    path = Path(program_path)
    if not path.is_file():
        raise adapter.DAPAdapterError(f"Program file not found: {path}")

    previous_dap_flag = os.environ.get("PYRUNG_DAP_ACTIVE")
    os.environ["PYRUNG_DAP_ACTIVE"] = "1"
    try:
        namespace = runpy.run_path(str(path), run_name="__main__")
    except Exception as exc:
        raise adapter.DAPAdapterError(f"Reload failed: {exc}") from exc
    finally:
        if previous_dap_flag is None:
            os.environ.pop("PYRUNG_DAP_ACTIVE", None)
        else:
            os.environ["PYRUNG_DAP_ACTIVE"] = previous_dap_flag

    try:
        new_runner = adapter._discover_runner(namespace)
    except adapter.DAPAdapterError as exc:
        raise adapter.DAPAdapterError(
            "Reload failed: could not discover runner in new program. Old runner preserved."
        ) from exc

    new_known_tags = dict(new_runner._known_tags_by_name)
    warnings: list[str] = []
    tags_to_drop: set[str] = set()
    for name, old_tag in old_known_tags.items():
        new_tag = new_known_tags.get(name)
        if new_tag is None:
            continue
        if old_tag.type != new_tag.type:
            warnings.append(
                f"  {name}: type changed {old_tag.type.name} -> {new_tag.type.name}, "
                f"using new default"
            )
            tags_to_drop.add(name)

    patched_tags = {k: v for k, v in old_state.tags.items() if k not in tags_to_drop}
    patched_state = SystemState(
        scan_id=old_state.scan_id,
        timestamp=old_state.timestamp,
        tags=pmap(patched_tags),
        memory=old_state.memory,
    )

    new_logic = new_runner._program if new_runner._program is not None else list(new_runner._logic)
    reloaded = PLC(
        logic=new_logic,
        initial_state=patched_state,
        history=new_runner._history_retention_scans,
        cache=new_runner._cache_retention_scans,
        history_budget=new_runner._recent_state_cache_budget,
        checkpoint_interval=new_runner._checkpoint_interval,
        record_all_tags=new_runner._record_all_tags,
    )
    reloaded._set_time_mode(old_time_mode, dt=old_dt)
    reloaded._set_rtc_internal(old_rtc, reloaded.current_state.timestamp)

    for tag_name, value in old_forces.items():
        if tag_name not in tags_to_drop:
            try:
                reloaded.force(tag_name, value)
            except Exception:
                warnings.append(f"  Could not re-apply force {tag_name}={value!r}")

    adapter._clear_debug_registrations_locked()
    adapter._runner = reloaded
    adapter._scan_gen = None
    adapter._current_scan_id = None
    adapter._current_step = None
    adapter._current_rung_index = None
    adapter._current_rung = None
    adapter._current_ctx = None
    adapter._breakpoints.clear()
    adapter._pending_predicate_pause = False
    adapter._rebuild_breakpoint_index_locked()

    adapter._bounds_accumulator.clear()
    adapter._notes.clear()
    adapter._action_log.clear()
    adapter._miner_candidates.clear()
    adapter._miner_accepted.clear()
    adapter._miner_suppressed.clear()

    adapter._harness = None
    from pyrung.dap.harness_console import try_auto_install

    banner = try_auto_install(adapter)

    scan_id = reloaded.current_state.scan_id
    n_tags = len(reloaded._known_tags_by_name)
    parts = [f"Reloaded at scan {scan_id} ({n_tags} tag(s))"]
    if warnings:
        parts.append("Warnings:")
        parts.extend(warnings)
    if banner:
        parts.append(banner)

    events: list[tuple[str, dict[str, Any] | None]] = [("stopped", adapter._stopped_body("entry"))]
    return "\n".join(parts), events


@register("reload", usage="reload", group="execution")
def _cmd_reload(adapter: Any, _expression: str) -> ConsoleResult:
    summary, events = _reload_locked(adapter)
    return ConsoleResult(summary, events=events)


def _watch_loop(adapter: Any, program_path: str, stop_event: threading.Event) -> None:
    last_mtime = os.stat(program_path).st_mtime
    while not stop_event.wait(timeout=1.0):
        try:
            current_mtime = os.stat(program_path).st_mtime
        except OSError:
            continue
        if current_mtime != last_mtime:
            stop_event.wait(timeout=0.3)
            try:
                current_mtime = os.stat(program_path).st_mtime
            except OSError:
                continue
            last_mtime = current_mtime

            with adapter._state_lock:
                if adapter._thread_running_locked():
                    adapter._enqueue_internal_event(
                        "output",
                        {
                            "category": "console",
                            "output": "[watch] Skipped reload — continue is running\n",
                        },
                    )
                    continue
                try:
                    summary, events = _reload_locked(adapter)
                except Exception as exc:
                    adapter._enqueue_internal_event(
                        "output",
                        {
                            "category": "console",
                            "output": f"[watch] Reload failed: {exc}\n",
                        },
                    )
                    continue

            adapter._enqueue_internal_event(
                "output",
                {"category": "console", "output": f"[watch] {summary}\n"},
            )
            for event_name, event_body in events:
                adapter._enqueue_internal_event(event_name, event_body)


@register("watch", usage="watch", group="execution")
def _cmd_watch(adapter: Any, _expression: str) -> ConsoleResult:
    if getattr(adapter, "_watch_thread", None) is not None:
        return ConsoleResult("Already watching")
    program_path = adapter._program_path
    if not program_path:
        raise adapter.DAPAdapterError("No program loaded")

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_watch_loop,
        args=(adapter, program_path, stop_event),
        daemon=True,
        name="pyrung-dap-watch",
    )
    adapter._watch_stop_event = stop_event
    adapter._watch_thread = thread
    thread.start()
    return ConsoleResult(f"Watching {Path(program_path).name} for changes")


@register("unwatch", usage="unwatch", group="execution")
def _cmd_unwatch(adapter: Any, _expression: str) -> ConsoleResult:
    stop_event: threading.Event | None = getattr(adapter, "_watch_stop_event", None)
    thread: threading.Thread | None = getattr(adapter, "_watch_thread", None)
    if thread is None or stop_event is None:
        return ConsoleResult("Not watching")
    stop_event.set()
    thread.join(timeout=2.0)
    adapter._watch_thread = None
    adapter._watch_stop_event = None
    return ConsoleResult("Stopped watching")
