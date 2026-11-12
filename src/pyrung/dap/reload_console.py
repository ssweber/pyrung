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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pyrsistent import pmap

from pyrung.core import PLC
from pyrung.core._source import clear_source_index
from pyrung.core.harness import (
    CoilWiring,
    Coupling,
    coil_feedback,
    link_problem,
    rename_coil_state,
    rename_coil_wiring,
)
from pyrung.core.site_memory import (
    PLANT_FEEDBACK_SITE,
    instruction_memory,
    remap_site_memory_report,
    seed_new_edge_memory,
)
from pyrung.core.state import SystemState
from pyrung.core.system_points import SYSTEM_TAGS_BY_NAME
from pyrung.dap.console import ConsoleResult, register
from pyrung.dap.links import BenchDrop, BenchLink, active_overrides

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


def _addresses(adapter: Any) -> dict[str, str] | None:
    """Each mapped tag's CLICK address, or ``None`` when the program has no tag map."""
    tag_map = adapter._tag_map
    if tag_map is None:
        return None
    return {slot.logical_name: slot.hardware_address for slot in tag_map.mapped_slots()}


@dataclass(frozen=True)
class ReloadPlan:
    """Where a reload puts each force and bench change, keyed by the old tag name.

    ``forces`` maps a forced tag to the tag it moves to, or to ``None`` to release
    it.  ``links`` maps a bench change's feedback to ``{"en": enable, "fb": feedback}``,
    to ``{"cut": True, "fb": feedback}`` for a cut, or to ``None`` to remove it.  A
    force or bench change the plan leaves out follows its tag name.
    """

    forces: Mapping[str, Any] = field(default_factory=dict)
    links: Mapping[str, Any] = field(default_factory=dict)


@dataclass(eq=False)
class _Carry:
    """One force or bench change in a reload, with where it goes and what became of it."""

    #: The old name of the forced tag, or of the bench change's feedback.
    tag: str
    kind: str
    planned: bool
    #: The name it goes to in the new program.  ``None`` when released or dropped.
    to: str | None = None
    value: Any = None
    entry: BenchLink | None = None
    new_entry: BenchLink | None = None
    released: bool = False
    #: Why it was dropped, ``None`` while it is kept.
    reason: str | None = None
    #: The tag the reason is about.
    about: str | None = None
    detail: str | None = None

    @property
    def dropped(self) -> bool:
        return self.reason is not None

    def drop(self, reason: str, about: str | None = None, detail: str | None = None) -> None:
        self.reason, self.about, self.detail = reason, about, detail


def _place(carries: list[_Carry], targets: dict[_Carry, tuple[str, Any]], what: str) -> None:
    """Give each carry in *targets* its new name, a planned carry before one that follows its own.

    Two carries on one name would fight over it, so the later one is dropped.
    """
    claimed: dict[str, str] = {}
    for carry in sorted(carries, key=lambda c: not c.planned):
        if carry not in targets:
            continue
        name, new_entry = targets[carry]
        other = claimed.get(name)
        if other is not None:
            carry.drop("taken", name, f"the {what} on {other} moves to {name}")
            continue
        claimed[name] = carry.tag
        carry.to, carry.new_entry = name, new_entry


def _plan_forces(old_forces: Mapping[str, Any], plan: Mapping[str, Any]) -> list[_Carry]:
    carries: list[_Carry] = []
    targets: dict[_Carry, tuple[str, Any]] = {}
    for tag, value in old_forces.items():
        carry = _Carry(tag, "force", planned=tag in plan, value=value)
        carries.append(carry)
        raw = plan.get(tag, tag)
        if raw is None:
            carry.released = True
        elif isinstance(raw, str) and raw.strip():
            targets[carry] = (raw.strip(), None)
        else:
            carry.drop("bad plan", tag, "its plan is not a tag name or null")
    _place(carries, targets, "force")
    for tag in plan:
        if tag not in old_forces:
            carry = _Carry(tag, "force", planned=True)
            carry.drop("not forced", tag, f"{tag} is not forced")
            carries.append(carry)
    return carries


_PLAN_LINK_KEYS = frozenset({"en", "fb", "cut"})


def _planned_bench(fb: str, entry: BenchLink, raw: Any) -> tuple[str, BenchLink] | str:
    """The new feedback and bench entry *raw* plans for *fb*, or why the plan is wrong."""
    if not isinstance(raw, Mapping):
        return "its plan is not an object or null"
    unknown = sorted(str(key) for key in raw if key not in _PLAN_LINK_KEYS)
    if unknown:
        return f"its plan has unknown key(s) {', '.join(unknown)}"
    target, en, cut = raw.get("fb"), raw.get("en"), raw.get("cut", False)
    if not isinstance(target, str) or not target.strip():
        return "its plan names no feedback (fb)"
    if not isinstance(cut, bool):
        return "its plan's cut is not true or false"
    target = target.strip()
    if cut:
        if en is not None:
            return "its plan sets both en and cut"
        if entry.coupling is not None:
            return f"its plan is a cut, but {fb} is a bench link"
        return target, BenchLink(None, enabled=entry.enabled)
    if not isinstance(en, str) or not en.strip():
        return "its plan names no enable (en) and is not a cut"
    coupling = entry.coupling
    if coupling is None:
        return f'its plan names an enable, but {fb} is a bench cut (plan it as "cut": true)'
    physical = coupling.physical
    # A link made without physical= is named after its feedback, so it keeps that.
    if physical.name == fb:
        physical = replace(physical, name=target)
    moved = Coupling(en.strip(), target, physical, coupling.trigger_value)
    return target, BenchLink(moved, enabled=entry.enabled)


def _plan_links(old_bench: Mapping[str, BenchLink], plan: Mapping[str, Any]) -> list[_Carry]:
    carries: list[_Carry] = []
    targets: dict[_Carry, tuple[str, Any]] = {}
    for fb, entry in old_bench.items():
        kind = "cut" if entry.coupling is None else "link"
        carry = _Carry(fb, kind, planned=fb in plan, entry=entry)
        carries.append(carry)
        if fb not in plan:
            targets[carry] = (fb, entry)
            continue
        if plan[fb] is None:
            carry.released = True
            continue
        planned = _planned_bench(fb, entry, plan[fb])
        if isinstance(planned, str):
            carry.drop("bad plan", fb, planned)
        else:
            targets[carry] = planned
    _place(carries, targets, "bench change")
    for fb in plan:
        if fb not in old_bench:
            carry = _Carry(fb, "link", planned=True)
            carry.drop("no bench change", fb, f"{fb} has no bench change")
            carries.append(carry)
    return carries


def _why(carry: _Carry) -> str:
    """Why *carry* was dropped, as the rest of "Dropped the force on A because ..."."""
    about = carry.about or carry.tag
    if carry.reason == "gone":
        return f"{about} is not in the new program"
    if carry.reason == "type changed":
        return f"{about} changed type"
    if carry.reason == "not forceable":
        return f"the new program cannot force {about}"
    if carry.reason == "unlinked":
        return f"the new program does not link {about}"
    if carry.reason == "in program":
        return f"the new program links {about} the same way"
    return carry.detail or str(carry.reason)


def _addr(table: dict[str, str] | None, name: str | None) -> str | None:
    return None if table is None or name is None else table.get(name)


def _carry_report(
    *,
    forces: list[_Carry],
    bench: list[_Carry],
    kept_bench: Mapping[str, BenchLink],
    old_addr: dict[str, str] | None,
    new_addr: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """List each force and bench change a plain reload moved to another address or dropped.

    Forces and bench changes follow the tag name.  Returns one entry per change
    for clients, and a sentence for each force drop and each move.  A bench
    change that was dropped already has its own note from the harness fit.
    """
    entries: list[dict[str, Any]] = []
    lines: list[str] = []

    def bench_entry(fb: str, entry: BenchLink, tag: str, **fields: Any) -> dict[str, Any]:
        kind = "cut" if entry.coupling is None else "link"
        return {
            "tag": tag,
            "kind": kind,
            "feedback": fb,
            "old": _addr(old_addr, tag),
            "new": _addr(new_addr, tag),
            "dropped": False,
            "enabled": entry.enabled,
            **fields,
        }

    for carry in forces:
        tag = carry.tag
        old, new = _addr(old_addr, tag), _addr(new_addr, tag)
        if not carry.dropped and old == new:
            continue
        entry: dict[str, Any] = {
            "tag": tag,
            "kind": "force",
            "old": old,
            "new": None if carry.dropped else new,
            "dropped": carry.dropped,
            "value": carry.value,
        }
        if carry.dropped:
            entry["reason"] = carry.reason
            lines.append(f"Dropped the force on {tag} because {_why(carry)}.")
        elif old is None:
            lines.append(f"The force on {tag} is now at {new}.")
        elif new is None:
            lines.append(f"The force on {tag} moved from {old} to no address.")
        else:
            lines.append(f"The force on {tag} moved from {old} to {new}.")
        entries.append(entry)

    for carry in bench:
        if carry.dropped and carry.entry is not None:
            # The tag is the one the drop is about, the enable when the enable left.
            tag = carry.about or carry.tag
            entries.append(
                bench_entry(
                    carry.tag, carry.entry, tag, new=None, dropped=True, reason=carry.reason
                )
            )

    for fb, kept in kept_bench.items():
        label = "bench cut" if kept.coupling is None else "bench link"
        tags = [fb] if kept.coupling is None else [fb, kept.coupling.en_name]
        for tag in tags:
            item = bench_entry(fb, kept, tag)
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


def _plan_report(
    *,
    forces: list[_Carry],
    bench: list[_Carry],
    old_addr: dict[str, str] | None,
    new_addr: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """List every force and bench change of a planned reload, and every plan entry.

    Unlike a plain reload's report, this lists an entry that kept its name and
    address too.  The caller then sees each change its plan left out.
    """
    entries: list[dict[str, Any]] = []
    lines: list[str] = []
    for carry in [*forces, *bench]:
        to = None if carry.dropped or carry.released else carry.to
        entry: dict[str, Any] = {
            "tag": carry.tag,
            "kind": carry.kind,
            "to": to,
            "old": _addr(old_addr, carry.tag),
            "new": _addr(new_addr, to),
            "planned": carry.planned,
            "released": carry.released,
            "dropped": carry.dropped,
        }
        if carry.kind == "force":
            entry["value"] = carry.value
            what = "force"
        else:
            kept = carry.new_entry if to is not None else None
            entry["feedback"] = carry.tag
            entry["enable"] = (
                kept.coupling.en_name if kept is not None and kept.coupling is not None else None
            )
            entry["enabled"] = carry.entry.enabled if carry.entry is not None else None
            what = "bench cut" if carry.kind == "cut" else "bench link"
        if carry.dropped:
            entry["reason"] = carry.reason
            entry["detail"] = _why(carry)
        entries.append(entry)

        tag = carry.tag
        if carry.reason == "not forced":
            lines.append(f"The plan names a force on {tag}, but {tag} is not forced.")
        elif carry.reason == "no bench change":
            lines.append(f"The plan names a bench change on {tag}, but {tag} has none.")
        elif carry.dropped:
            lines.append(f"Dropped the {what} on {tag} because {_why(carry)}.")
        elif carry.released:
            verb = "Released" if carry.kind == "force" else "Removed"
            lines.append(f"{verb} the {what} on {tag}.")
        elif not carry.planned:
            lines.append(f"The {what} on {tag} is not in the plan, so it stays on {tag}.")
        else:
            if to != tag:
                lines.append(f"Moved the {what} on {tag} to {to}.")
            old_link = carry.entry.coupling if carry.entry is not None else None
            new_link = carry.new_entry.coupling if carry.new_entry is not None else None
            if old_link is not None and new_link is not None:
                if old_link.en_name != new_link.en_name:
                    lines.append(
                        f"The bench link on {to} reads {new_link.en_name} "
                        f"(it read {old_link.en_name})."
                    )
    return entries, lines


def _reload_locked(
    adapter: Any, plan: ReloadPlan | None = None
) -> tuple[str, list[tuple[str, dict[str, Any] | None]], list[dict[str, Any]]]:
    """Re-execute the program file and swap the runner, preserving state.

    Must be called with ``adapter._state_lock`` held.  Returns
    ``(summary_text, events, carried)``.  The caller wraps the first two in a
    ``ConsoleResult`` or emits them via ``_enqueue_internal_event``, and
    ``carried`` lists the forces and bench changes that moved or were dropped.
    With a *plan*, ``carried`` lists every force and bench change and every plan
    entry.  The plan is applied at the swap, so a reload that fails changes nothing.
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
    force_carries = _plan_forces(old_forces, plan.forces if plan is not None else {})
    bench_carries = _plan_links(adapter._bench_links, plan.links if plan is not None else {})
    old_active = active_overrides(adapter)

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

    # A planned link that the old harness ran and the new program accepts runs the
    # same coil under its new names, so the coil keeps its memory and its count.
    moved_coils: dict[str, Coupling] = {}
    for carry in bench_carries:
        new_entry = carry.new_entry
        if not carry.planned or new_entry is None or new_entry.coupling is None:
            continue
        if old_active.get(carry.tag) is None:
            continue
        if link_problem(new_runner, new_entry.coupling) is None:
            moved_coils[carry.tag] = new_entry.coupling
    renames = {fb: c.fb_name for fb, c in moved_coils.items() if c.fb_name != fb}

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
    if renames:
        patched_tags, patched_memory = rename_coil_state(
            patched_tags, patched_memory, old_runner._plant_tag_defaults(), renames
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

    # A force goes to the tag its plan names, or follows its tag name.  One whose tag
    # is not in the new program is dropped, since PLC.force would accept it and keep it.
    for carry in force_carries:
        target = carry.to
        if target is None:
            continue
        old_tag, new_tag = old_known_tags.get(carry.tag), new_known_tags.get(target)
        if target in tags_to_drop or (
            old_tag is not None and new_tag is not None and old_tag.type != new_tag.type
        ):
            carry.drop("type changed", target)
        elif (
            new_tag is None
            and reloaded._tag_known_or_reached(target) is None
            and target not in SYSTEM_TAGS_BY_NAME
        ):
            carry.drop("gone", target)
        else:
            try:
                reloaded.force(target, carry.value)
            except Exception:
                carry.drop("not forceable", target)

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
    # new to the program, or one of a harness removed before the reload, settles.  A
    # coil the plan moved carries on under its new feedback and enable only, so a coil
    # the new program runs on the old feedback settles.
    old_harness = adapter._harness
    carry_coils: set[CoilWiring] = set()
    for wiring in old_harness._coil_wiring() if old_harness is not None else ():
        moved = moved_coils.get(coil_feedback(wiring))
        if moved is None:
            carry_coils.add(wiring)
        else:
            carry_coils.add(rename_coil_wiring(wiring, moved.fb_name, moved.en_name))
    adapter._harness = None
    adapter._bench_links = {
        carry.to: carry.new_entry
        for carry in bench_carries
        if carry.to is not None and carry.new_entry is not None
    }
    from pyrung.dap.harness_console import try_auto_install

    bench_drops: list[BenchDrop] = []
    banner = try_auto_install(adapter, carry=carry_coils, drops=bench_drops)
    placed = {carry.to: carry for carry in bench_carries if carry.to is not None}
    for fb, entry, reason, tag in bench_drops:
        problem = link_problem(reloaded, entry.coupling) if entry.coupling is not None else None
        placed[fb].drop(reason, tag, problem)
    stale = _drop_coil_memory_no_coupling_owns(reloaded)
    if stale:
        warnings.append(
            f"  {len(stale)} harness coil memory value(s) had no matching coupling, "
            "reset: " + ", ".join(stale)
        )

    new_addr = _addresses(adapter)
    if plan is None:
        carried, carry_lines = _carry_report(
            forces=force_carries,
            bench=bench_carries,
            kept_bench=dict(adapter._bench_links),
            old_addr=old_addr,
            new_addr=new_addr,
        )
    else:
        carried, carry_lines = _plan_report(
            forces=force_carries, bench=bench_carries, old_addr=old_addr, new_addr=new_addr
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
