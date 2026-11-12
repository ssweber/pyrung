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
from pyrung.core._source import clear_source_index
from pyrung.core.site_memory import (
    PLANT_FEEDBACK_SITE,
    instruction_memory,
    remap_site_memory_report,
    seed_new_edge_memory,
)
from pyrung.core.state import SystemState
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.dap.console import ConsoleResult, register

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


def _addresses(adapter: Any) -> dict[str, str] | None:
    """Each mapped tag's CLICK address, or ``None`` when the program has no tag map."""
    tag_map = adapter._tag_map
    if tag_map is None:
        return None
    return {slot.logical_name: slot.hardware_address for slot in tag_map.mapped_slots()}


def _carry_report(
    *,
    old_forces: dict[str, Any],
    force_drops: dict[str, str],
    bench_drops: list[tuple[str, Any, str]],
    kept_bench: dict[str, Any],
    old_addr: dict[str, str] | None,
    new_addr: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """List each force and bench change a reload moved to another address or dropped.

    Forces and bench changes follow the tag name.  Returns one entry per change
    for clients, and a sentence for each force drop and each move.  A bench
    change that was dropped already has its own note from the harness fit.
    """
    entries: list[dict[str, Any]] = []
    lines: list[str] = []

    def addr(table: dict[str, str] | None, name: str) -> str | None:
        return None if table is None else table.get(name)

    def bench_entry(fb: str, entry: Any, tag: str, **fields: Any) -> dict[str, Any]:
        kind = "cut" if entry.coupling is None else "link"
        return {
            "tag": tag,
            "kind": kind,
            "feedback": fb,
            "old": addr(old_addr, tag),
            "new": addr(new_addr, tag),
            "dropped": False,
            "enabled": entry.enabled,
            **fields,
        }

    for tag, value in old_forces.items():
        old, new = addr(old_addr, tag), addr(new_addr, tag)
        why = force_drops.get(tag)
        if why is None and old == new:
            continue
        entry: dict[str, Any] = {
            "tag": tag,
            "kind": "force",
            "old": old,
            "new": None if why else new,
            "dropped": why is not None,
            "value": value,
        }
        if why is not None:
            entry["reason"] = why
            lines.append(f"Dropped the force on {tag} because {_DROP_WHY[why].format(tag=tag)}.")
        elif old is None:
            lines.append(f"The force on {tag} is now at {new}.")
        elif new is None:
            lines.append(f"The force on {tag} moved from {old} to no address.")
        else:
            lines.append(f"The force on {tag} moved from {old} to {new}.")
        entries.append(entry)

    for fb, entry, why in bench_drops:
        entries.append(bench_entry(fb, entry, fb, new=None, dropped=True, reason=why))

    for fb, entry in kept_bench.items():
        label = "bench cut" if entry.coupling is None else "bench link"
        tags = [fb] if entry.coupling is None else [fb, entry.coupling.en_name]
        for tag in tags:
            item = bench_entry(fb, entry, tag)
            if item["old"] == item["new"]:
                continue
            entries.append(item)
            old, new = item["old"], item["new"]
            if old is None:
                moved = f"is now at {new}"
            elif new is None:
                moved = f"moved from {old} to no address"
            else:
                moved = f"moved from {old} to {new}"
            if tag == fb:
                lines.append(f"The {label} on {fb} {moved}.")
            else:
                lines.append(f"The {label} on {fb} reads {tag}, which {moved}.")
    return entries, lines


#: Why a force was dropped, as the rest of "Dropped the force on A because ...".
_DROP_WHY = {
    "gone": "{tag} is not in the new program",
    "type changed": "{tag} changed type",
    "not forceable": "the new program cannot force {tag}",
}


def _reload_locked(
    adapter: Any,
) -> tuple[str, list[tuple[str, dict[str, Any] | None]], list[dict[str, Any]]]:
    """Re-execute the program file and swap the runner, preserving state.

    Must be called with ``adapter._state_lock`` held.  Returns
    ``(summary_text, events, carried)``.  The caller wraps the first two in a
    ``ConsoleResult`` or emits them via ``_enqueue_internal_event``, and
    ``carried`` lists the forces and bench changes that moved or were dropped.
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
    old_addr = _addresses(adapter)

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
    clear_source_index()

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
    # Site memory (one-shots, drum and shift edges, edge contacts) is keyed by
    # site, and site keys are ordinal: carry each site's memory to the site of
    # the new program with the same fingerprint, drop what no longer matches.
    remap = remap_site_memory_report(
        old_state.memory, old_runner._site_table, new_runner._site_table
    )
    patched_memory = remap.memory
    if remap.dropped:
        warnings.append(
            f"  {len(remap.dropped)} site memory value(s) had no matching site "
            "in the new program, reset: " + ", ".join(sorted(remap.dropped))
        )
    # An edge contact with no match in the old program (new, or edited) starts
    # from its tag's current value, so a held bit does not rise on the first
    # scan after reload.  A matched contact keeps its memory, written or not.
    patched_memory, seeded_edges = seed_new_edge_memory(
        patched_memory, old_runner._site_table, new_runner._site_table, patched_tags
    )
    if seeded_edges:
        warnings.append(
            f"  {len(seeded_edges)} edge contact(s) new or edited start from "
            "their tag's current value: "
            + ", ".join(
                f"{site.kind}({site.tag})"
                + (f" line {site.source_line}" if site.source_line is not None else "")
                + f" = {'on' if patched_memory[site.memory[0][0]] else 'off'}"
                for site in seeded_edges
            )
        )
    patched_state = SystemState(
        scan_id=old_state.scan_id,
        timestamp=old_state.timestamp,
        tags=pmap(patched_tags),
        memory=pmap(patched_memory),
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
        keep_forces_on_run=(
            new_runner.keep_forces_on_run
            if adapter._keep_forces_on_run is None
            else adapter._keep_forces_on_run
        ),
    )
    if old_runner.mode == "stop":
        reloaded.stop()
    reloaded._set_time_mode(old_time_mode, dt=old_dt)
    reloaded._set_rtc_internal(old_rtc, reloaded.current_state.timestamp)

    # A force follows its tag name.  One whose name left the program is dropped, since
    # PLC.force would accept it and keep it active.
    force_drops: dict[str, str] = {}
    for tag_name, value in old_forces.items():
        if tag_name in tags_to_drop:
            force_drops[tag_name] = "type changed"
        elif (
            tag_name not in new_known_tags
            and reloaded._tag_known_or_reached(tag_name) is None
            and tag_name not in SYSTEM_TAGS_BY_NAME
        ):
            force_drops[tag_name] = "gone"
        else:
            try:
                reloaded.force(tag_name, value)
            except Exception:
                force_drops[tag_name] = "not forceable"

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
    adapter._pending_stop = False

    from pyrung.dap.handlers.lifecycle_launch import bind_click_project_locked

    bind_click_project_locked(adapter, namespace, path.parent)

    adapter._bounds_accumulator.clear()
    adapter._notes.clear()
    adapter._action_log.clear()
    adapter._miner_candidates.clear()
    adapter._miner_accepted.clear()
    adapter._miner_suppressed.clear()

    # Each coil the old harness ran carries on from the memory the reload kept.  A coil
    # new to the program, or one of a harness removed before the reload, settles.
    old_harness = adapter._harness
    carry = old_harness._coil_wiring() if old_harness is not None else frozenset()
    adapter._harness = None
    from pyrung.dap.harness_console import try_auto_install

    bench_drops: list[tuple[str, Any, str]] = []
    banner = try_auto_install(adapter, carry=carry, drops=bench_drops)
    stale = _drop_coil_memory_no_coupling_owns(reloaded)
    if stale:
        warnings.append(
            f"  {len(stale)} harness coil memory value(s) had no matching coupling, "
            "reset: " + ", ".join(stale)
        )

    carried, carry_lines = _carry_report(
        old_forces=old_forces,
        force_drops=force_drops,
        bench_drops=bench_drops,
        kept_bench=dict(adapter._bench_links),
        old_addr=old_addr,
        new_addr=_addresses(adapter),
    )
    warnings.extend(f"  {line}" for line in carry_lines)

    scan_id = reloaded.current_state.scan_id
    n_tags = len(reloaded._known_tags_by_name)
    parts = [f"Reloaded at scan {scan_id} ({n_tags} tag(s))"]
    if warnings:
        parts.append("Warnings:")
        parts.extend(warnings)
    if banner:
        parts.append(banner)

    events: list[tuple[str, dict[str, Any] | None]] = [("stopped", adapter._stopped_body("entry"))]
    return "\n".join(parts), events, carried


def _drop_coil_memory_no_coupling_owns(runner: PLC) -> list[str]:
    """Drop each harness coil memory that no coil of the new harness keeps.

    A reload keeps a coil's memory for the coil of the same kind on the same
    feedback.  Kept for a renamed feedback or another kind, the memory would wait in
    the state.  A later reload back to it would then read a stale value and skip the
    settle at install.
    """
    owned: set[str] = set()
    synthesis = runner._synthesis
    for rung in synthesis.plant if synthesis is not None else ():
        for instr in rung._instructions:
            key = instr._state_key
            if key is not None and key.startswith(PLANT_FEEDBACK_SITE):
                owned.update(f"{prefix}:{key}" for prefix, _default in instruction_memory(instr))
    stale = sorted(
        name
        for name in runner.current_state.memory
        if f":{PLANT_FEEDBACK_SITE}" in name and name not in owned
    )
    if stale:
        runner._edit_tip_tags({}, drop_memory=stale)
    return stale


@register("reload", usage="reload", group="execution")
def _cmd_reload(adapter: Any, _expression: str) -> ConsoleResult:
    summary, events, carried = _reload_locked(adapter)
    return ConsoleResult(summary, events=events, body={"carried": carried})


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
                summary, events, carried = _reload_locked(adapter)
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
            {
                "category": "console",
                "output": f"[autoreload] {summary}\n",
                "data": {"carried": carried},
            },
        )
        for event_name, event_body in events:
            adapter._enqueue_internal_event(event_name, event_body)
        adapter._request_state_frame({"origin": "autoreload", "command": "reload"})


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
