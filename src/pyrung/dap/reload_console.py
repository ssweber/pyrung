"""Hot-reload console commands for the DAP adapter.

Provides ``reload`` and ``autoreload [off]`` commands that re-execute the
program while preserving PLC state (tags, memory, forces, time mode).  The
watcher covers every Python file under the program directory.
"""

from __future__ import annotations

import importlib
import os
import runpy
import sys
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
        raise adapter.DAPAdapterError("Cannot reload while recording; stop recording first")

    old_runner: PLC = adapter._require_runner_locked()
    old_state: SystemState = old_runner.current_state
    old_forces = dict(old_runner.forces)
    old_time_mode = old_runner._time_mode
    old_dt = old_runner._dt
    old_known_tags = dict(old_runner._known_tags_by_name)
    old_rtc = old_runner._system_runtime._rtc_now(old_state)

    program_path = adapter._program_path
    if not program_path:
        raise adapter.DAPAdapterError("No program loaded; cannot reload")
    path = Path(program_path)
    if not path.is_file():
        raise adapter.DAPAdapterError(f"Program file not found: {path}")

    program_dir = os.path.normcase(os.path.abspath(str(path.parent))) + os.sep
    stale = [
        name
        for name, mod in sys.modules.items()
        if isinstance(mod_file := getattr(mod, "__file__", None), str)
        and os.path.normcase(os.path.abspath(mod_file)).startswith(program_dir)
    ]
    for name in stale:
        del sys.modules[name]
    importlib.invalidate_caches()

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


def _collect_py_mtimes(directory: Path) -> dict[str, float]:
    mtimes: dict[str, float] = {}
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(root, f)
                try:
                    mtimes[full] = os.stat(full).st_mtime
                except OSError:
                    pass
    return mtimes


def _autoreload_loop(adapter: Any, program_dir: Path, stop_event: threading.Event) -> None:
    last_mtimes = _collect_py_mtimes(program_dir)
    while not stop_event.wait(timeout=1.0):
        current_mtimes = _collect_py_mtimes(program_dir)
        if current_mtimes == last_mtimes:
            continue
        stop_event.wait(timeout=0.3)
        current_mtimes = _collect_py_mtimes(program_dir)
        last_mtimes = current_mtimes

        with adapter._state_lock:
            if adapter._thread_running_locked():
                adapter._enqueue_internal_event(
                    "output",
                    {
                        "category": "console",
                        "output": "[autoreload] Skipped: continue is running\n",
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
                        "output": f"[autoreload] Reload failed: {exc}\n",
                    },
                )
                continue

        adapter._enqueue_internal_event(
            "output",
            {"category": "console", "output": f"[autoreload] {summary}\n"},
        )
        for event_name, event_body in events:
            adapter._enqueue_internal_event(event_name, event_body)


def start_autoreload(adapter: Any) -> str | None:
    if getattr(adapter, "_autoreload_thread", None) is not None:
        return None
    program_path = adapter._program_path
    if not program_path:
        return None

    program_dir = Path(program_path).parent
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_autoreload_loop,
        args=(adapter, program_dir, stop_event),
        daemon=True,
        name="pyrung-dap-autoreload",
    )
    adapter._autoreload_stop_event = stop_event
    adapter._autoreload_thread = thread
    thread.start()
    return f"Auto-reload enabled for {program_dir.name}/"


def stop_autoreload(adapter: Any) -> None:
    stop_event: threading.Event | None = getattr(adapter, "_autoreload_stop_event", None)
    thread: threading.Thread | None = getattr(adapter, "_autoreload_thread", None)
    if thread is None or stop_event is None:
        return
    stop_event.set()
    thread.join(timeout=2.0)
    adapter._autoreload_thread = None
    adapter._autoreload_stop_event = None


@register("autoreload", usage="autoreload [off]", group="execution")
def _cmd_autoreload(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) >= 2 and parts[1].lower() == "off":
        stop_autoreload(adapter)
        return ConsoleResult("Auto-reload disabled")
    msg = start_autoreload(adapter)
    if msg is None:
        return ConsoleResult("Already auto-reloading")
    return ConsoleResult(msg)
