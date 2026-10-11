"""Lifecycle/launch ownership for DAP command handling.

Owns initialization, launch discovery, and shutdown state reset paths.
Must not change protocol payload shapes or launch/shutdown side effects.
"""

from __future__ import annotations

import os
import runpy
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyrung.core import PLC, Program

if TYPE_CHECKING:
    from types import ModuleType

    from pyrung.click.tag_map import TagMap

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
    adapter._configuration_done = True
    return {}, []


def on_disconnect(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    return adapter._shutdown()


def on_terminate(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    return adapter._shutdown()


def on_threads(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    return {"threads": [{"id": adapter.THREAD_ID, "name": "PLC Scan"}]}, []


def _find_project_tags_module(project_dir: Path) -> ModuleType | None:
    """Return the launched project's module exporting a ``mapping`` TagMap, if any.

    Generated projects keep it in ``plc.tags`` while hand-written ones often use
    a top-level ``tags`` module, so search by location rather than module name.
    """
    import sys

    from pyrung.click.tag_map import TagMap

    root = os.path.normcase(os.path.abspath(str(project_dir))) + os.sep
    fallback: ModuleType | None = None
    for name, mod in list(sys.modules.items()):
        if not isinstance(getattr(mod, "mapping", None), TagMap):
            continue
        mod_file = getattr(mod, "__file__", None)
        if not isinstance(mod_file, str):
            continue
        if not os.path.normcase(os.path.abspath(mod_file)).startswith(root):
            continue
        # Other project modules re-import ``mapping``; prefer where it is defined.
        if name.rpartition(".")[2] == "tags":
            return mod
        if fallback is None:
            fallback = mod
    if fallback is not None:
        return fallback

    # A ``tags`` module importable from outside the program directory.
    tags_mod = sys.modules.get("tags")
    return tags_mod if isinstance(getattr(tags_mod, "mapping", None), TagMap) else None


def _apply_snapshot(runner: PLC, snapshot_path: str, project_dir: Path) -> PLC:
    """Reconstruct *runner* with initial state loaded from a Click CSV snapshot."""
    tags_mod = _find_project_tags_module(project_dir)
    if tags_mod is None:
        raise ValueError("Cannot load snapshot: no TagMap found in project")
    mapping: TagMap = tags_mod.mapping
    # Generated projects name plain addresses on the Click banks themselves
    # rather than in the TagMap, so seed those slots too.
    blocks = getattr(tags_mod, "blocks", None)
    if not isinstance(blocks, Iterable):
        blocks = None

    resolved = Path(snapshot_path).expanduser()
    if not resolved.is_absolute():
        resolved = project_dir / resolved

    # A real dump lists every address, so an empty parse means some other CSV
    # (a nickname export, say) that would otherwise seed nothing without a word.
    from pyclickplc import read_plc_data

    if not read_plc_data(resolved):
        raise ValueError(
            f"Cannot load snapshot: no PLC data found in {resolved.name}. "
            "Expected a CSV saved from PLC > Read Data from PLC in CLICK."
        )

    state = mapping.load_snapshot(resolved, blocks=blocks)
    logic = runner._program if runner._program is not None else list(runner._logic)
    return PLC(logic, initial_state=state)


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

    if parsed.snapshotPath:
        runner = _apply_snapshot(runner, parsed.snapshotPath, program_path.parent)

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
        adapter._configuration_done = False
        adapter._program_path = str(program_path)
        adapter._breakpoints.clear()
        adapter._pending_predicate_pause = False
        adapter._rebuild_breakpoint_index_locked()

    session_name = parsed.session or program_path.stem
    adapter._session.session_name = session_name
    _start_live_server(adapter, session_name)
    _try_auto_install_harness(adapter)

    if parsed.autoReload:
        from pyrung.dap.reload_console import start_autoreload

        banner = start_autoreload(adapter)
        if banner:
            adapter._enqueue_internal_event(
                "output", {"category": "console", "output": f"{banner}\n"}
            )

    return {}, [("stopped", adapter._stopped_body("entry"))]


def discover_runner(adapter: Any, namespace: dict[str, Any]) -> PLC:
    named_runner = namespace.get("runner")
    if isinstance(named_runner, PLC):
        return named_runner

    runners = adapter._unique_instances(namespace.values(), PLC)
    if len(runners) == 1:
        return runners[0]

    programs = adapter._unique_instances(namespace.values(), Program)
    if len(programs) == 1:
        return PLC(programs[0])

    raise adapter.DAPAdapterError(
        "Launch script must provide 'runner' as PLC, or define exactly one PLC "
        f"or exactly one Program. Found {len(runners)} PLC(s), {len(programs)} Program(s)."
    )


def shutdown(adapter: Any) -> HandlerResult:
    from pyrung.dap.reload_console import stop_autoreload

    stop_autoreload(adapter)

    _stop_live_server(adapter)
    _uninstall_harness(adapter)
    with adapter._state_lock:
        adapter._clear_debug_registrations_locked()
        adapter._runner = None
        adapter._scan_gen = None
        adapter._current_scan_id = None
        adapter._current_step = None
        adapter._current_rung_index = None
        adapter._current_rung = None
        adapter._current_ctx = None
        adapter._configuration_done = False
    adapter._stop_event.set()
    adapter._pause_event.set()
    return {}, [("terminated", {})]


def _start_live_server(adapter: Any, session_name: str) -> None:
    if adapter._live_server is not None:
        adapter._live_server.stop()
    from pyrung.dap.live import LiveServer

    server = LiveServer(adapter, session_name)
    try:
        server.start()
    except OSError:
        adapter._live_server = None
        return
    adapter._live_server = server


def _stop_live_server(adapter: Any) -> None:
    if adapter._live_server is not None:
        adapter._live_server.stop()
        adapter._live_server = None


def _try_auto_install_harness(adapter: Any) -> None:
    from pyrung.dap.harness_console import try_auto_install

    banner = try_auto_install(adapter)
    if banner is not None:
        adapter._enqueue_internal_event("output", {"category": "console", "output": f"{banner}\n"})


def _uninstall_harness(adapter: Any) -> None:
    from pyrung.dap.harness_console import uninstall_harness

    uninstall_harness(adapter)


@dataclass(frozen=True)
class _LaunchRequestArgs:
    program: Any = None
    session: Any = None
    snapshotPath: Any = None
    autoReload: Any = False
