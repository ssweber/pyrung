"""Console command dispatcher for the DAP REPL.

Each verb is registered in a module-level registry. The dispatcher
looks up the verb and delegates. All handlers run under the adapter's
``_state_lock`` — they must NOT call handler entry points that also
acquire it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pyrung.dap import execution_flow

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class ConsoleResult:
    """Return value from a console command."""

    text: str
    events: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)


_REGISTRY: dict[str, tuple[Callable[..., ConsoleResult], str, str]] = {}


def register(verb: str, *, usage: str = "", group: str = "") -> Callable[..., Any]:
    def decorator(fn: Callable[..., ConsoleResult]) -> Callable[..., ConsoleResult]:
        _REGISTRY[verb] = (fn, usage, group)
        return fn

    return decorator


_GROUP_ORDER = ["execution", "data", "analysis", "capture", "review", ""]

_GROUP_LAYOUT: dict[str, list[str | None]] = {
    "analysis": [
        "log",
        None,
        "dataview",
        "downstream",
        "upstream",
        "structures",
        None,
        "cause",
        "effect",
        "recovers",
        None,
        "simplified",
    ],
}


def _format_grouped_help() -> str:
    groups: dict[str, list[str]] = {g: [] for g in _GROUP_ORDER}
    for verb in sorted(_REGISTRY):
        _fn, usage, group = _REGISTRY[verb]
        groups.setdefault(group, []).append(usage or verb)
    usage_by_verb = {v: (u or v) for v, (_f, u, _g) in _REGISTRY.items()}
    lines: list[str] = []
    for group in _GROUP_ORDER:
        entries = groups.get(group)
        if not entries:
            continue
        if lines:
            lines.append("")
        lines.append(f"{group}:" if group else "other:")
        layout = _GROUP_LAYOUT.get(group)
        if layout:
            for item in layout:
                if item is None:
                    lines.append("")
                else:
                    lines.append(f"  {usage_by_verb[item]}")
        else:
            for entry in entries:
                lines.append(f"  {entry}")
    return "\n".join(lines)


def dispatch(adapter: Any, expression: str, *, provenance: str = "console") -> ConsoleResult:
    parts = expression.strip().split()
    if not parts:
        raise adapter.DAPAdapterError("Empty command")
    verb = parts[0].lower()
    entry = _REGISTRY.get(verb)
    if entry is None:
        known = ", ".join(sorted(_REGISTRY))
        raise adapter.DAPAdapterError(
            f"Unknown command '{verb}'. Available: {known}. Use Watch for predicate expressions."
        )
    handler, _usage, _group = entry
    result = handler(adapter, expression)

    from pyrung.dap.capture import capture_hook

    capture_hook(adapter, verb, expression, provenance=provenance)

    action_log: list[tuple[int | None, str, str]] | None = getattr(adapter, "_action_log", None)
    if action_log is not None:
        runner = getattr(adapter, "_runner", None)
        scan_id = runner.current_state.scan_id if runner else None
        action_log.append((scan_id, expression.strip(), provenance))

    return result


# ---------------------------------------------------------------------------
# Existing verbs (migrated from stack_variables_evaluate)
# ---------------------------------------------------------------------------


@register("force", usage="force <tag> <value>", group="data")
def _cmd_force(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise adapter.DAPAdapterError("Usage: force <tag> <value>")
    tag = parts[1]
    raw_value = parts[2]
    value = adapter._parse_literal(raw_value)
    runner = adapter._require_runner_locked()
    runner.force(tag, value)
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult(f"Forced {tag}={value!r}")


@register("unforce", usage="unforce <tag>", group="data")
def _cmd_unforce(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) != 2:
        raise adapter.DAPAdapterError("Usage: unforce <tag>")
    tag = parts[1]
    runner = adapter._require_runner_locked()
    runner.unforce(tag)
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult(f"Removed force {tag}")


@register("clear_forces", usage="clear_forces", group="data")
def _cmd_clear_forces(adapter: Any, _expression: str) -> ConsoleResult:
    runner = adapter._require_runner_locked()
    runner.clear_forces()
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult("Cleared all forces")


# ---------------------------------------------------------------------------
# New state-mutation verbs
# ---------------------------------------------------------------------------


@register("patch", usage="patch <tag> <value>", group="data")
def _cmd_patch(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise adapter.DAPAdapterError("Usage: patch <tag> <value>")
    tag = parts[1]
    raw_value = parts[2]
    value = adapter._parse_literal(raw_value)
    runner = adapter._require_runner_locked()
    runner.patch({tag: value})
    execution_flow.invalidate_mid_scan(adapter)
    return ConsoleResult(f"Patched {tag}={value!r}")


@register("continue", usage="continue", group="execution")
def _cmd_continue(adapter: Any, _expression: str) -> ConsoleResult:
    import threading

    adapter._require_runner_locked()
    if adapter._thread_running_locked():
        raise adapter.DAPAdapterError("Already running")
    adapter._pause_event.clear()
    thread = threading.Thread(
        target=adapter._continue_worker,
        daemon=True,
        name="pyrung-dap-continue",
    )
    adapter._continue_thread = thread
    thread.start()
    return ConsoleResult("Continuing")


@register("pause", usage="pause", group="execution")
def _cmd_pause(adapter: Any, _expression: str) -> ConsoleResult:
    if not adapter._thread_running_locked():
        raise adapter.DAPAdapterError("Not running")
    adapter._pause_event.set()
    return ConsoleResult("Pausing after current scan")


@register("step", usage="step [N]", group="execution")
def _cmd_step(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    n = 1
    if len(parts) > 1:
        try:
            n = int(parts[1])
        except ValueError as exc:
            raise adapter.DAPAdapterError(
                f"step count must be an integer, got '{parts[1]}'"
            ) from exc
    if n < 1:
        raise adapter.DAPAdapterError("step count must be >= 1")

    adapter._assert_can_step_locked()
    scans_completed = 0
    hit_bp = False
    for _ in range(n):
        _advance_one_full_scan(adapter)
        scans_completed += 1
        if adapter._current_rung_hits_breakpoint_locked():
            hit_bp = True
            break

    scan_id = adapter._current_scan_id
    suffix = " (breakpoint)" if hit_bp else ""
    return ConsoleResult(
        f"Stepped {scans_completed} scan(s), now at scan {scan_id}{suffix}",
        events=[("stopped", adapter._stopped_body("step"))],
    )


@register("run", usage="run <N | duration>  (e.g. 10, 500ms, 2 s)", group="execution")
def _cmd_run(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError(
            "Usage: run <cycles> or run <duration> (e.g. run 10, run 500ms)"
        )
    spec = _run_spec(parts)
    adapter._assert_can_step_locked()
    runner = adapter._require_runner_locked()

    try:
        cycles = int(spec)
        return _run_cycles(adapter, runner, cycles)
    except ValueError:
        pass

    from pyrung.core.physical import parse_duration

    try:
        ms = parse_duration(spec)
    except ValueError as exc:
        raise adapter.DAPAdapterError(f"Cannot parse '{spec}' as cycle count or duration") from exc

    return _run_duration(adapter, runner, ms / 1000.0)


def _run_cycles(adapter: Any, runner: Any, cycles: int) -> ConsoleResult:
    if cycles < 1:
        raise adapter.DAPAdapterError("cycle count must be >= 1")
    scans = 0
    hit_bp = False
    for _ in range(cycles):
        _advance_one_full_scan(adapter)
        scans += 1
        if adapter._current_rung_hits_breakpoint_locked():
            hit_bp = True
            break
    scan_id = adapter._current_scan_id
    suffix = " (breakpoint)" if hit_bp else ""
    return ConsoleResult(
        f"Ran {scans} cycle(s), now at scan {scan_id}{suffix}",
        events=[("stopped", adapter._stopped_body("step"))],
    )


def _run_spec(parts: list[str]) -> str:
    if len(parts) >= 3 and _looks_like_split_duration(parts[1], parts[2]):
        return f"{parts[1]}{parts[2]}"
    return parts[1]


def _looks_like_split_duration(value: str, unit: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return unit.lower() in {"ms", "s", "m", "min", "h", "d"}


def _run_duration(adapter: Any, runner: Any, seconds: float) -> ConsoleResult:
    if seconds <= 0:
        raise adapter.DAPAdapterError("duration must be positive")
    start_time = runner.current_state.timestamp
    target_time = start_time + seconds
    scans = 0
    hit_bp = False
    while runner.current_state.timestamp < target_time:
        _advance_one_full_scan(adapter)
        scans += 1
        if adapter._current_rung_hits_breakpoint_locked():
            hit_bp = True
            break
    elapsed = runner.current_state.timestamp - start_time
    suffix = " (breakpoint)" if hit_bp else ""
    return ConsoleResult(
        f"Ran {scans} scan(s) ({elapsed:.3f}s elapsed){suffix}",
        events=[("stopped", adapter._stopped_body("step"))],
    )


def _advance_one_full_scan(adapter: Any) -> None:
    """Advance through one complete scan using the debug stepping machinery."""
    origin_ctx = adapter._current_ctx
    if origin_ctx is None:
        if not adapter._advance_with_step_logpoints_locked():
            return
        origin_ctx = adapter._current_ctx
    while adapter._current_ctx is origin_ctx:
        if not adapter._advance_with_step_logpoints_locked():
            return
    from pyrung.dap.bounds_console import emit_bounds_violations

    emit_bounds_violations(adapter)


# ---------------------------------------------------------------------------
# Query verbs
# ---------------------------------------------------------------------------


@register("cause", usage="cause <tag>[@scan|:value]", group="analysis")
def _cmd_cause(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: cause <tag>[@scan|:value]")
    tag, scan, has_value, value = _parse_tag_spec(parts[1])
    runner = adapter._require_runner_locked()
    if has_value:
        chain = runner.cause(tag, to=value)
    else:
        chain = runner.cause(tag, scan=scan)
    if chain is None:
        return ConsoleResult(f"No causal chain found for {tag}")
    return ConsoleResult(str(chain))


@register("effect", usage="effect <tag>[@scan|:value]", group="analysis")
def _cmd_effect(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: effect <tag>[@scan|:value]")
    tag, scan, has_value, value = _parse_tag_spec(parts[1])
    runner = adapter._require_runner_locked()
    if has_value:
        chain = runner.effect(tag, from_=value)
    else:
        chain = runner.effect(tag, scan=scan)
    if chain is None:
        return ConsoleResult(f"No effect chain found for {tag}")
    return ConsoleResult(str(chain))


@register("recovers", usage="recovers <tag>", group="analysis")
def _cmd_recovers(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: recovers <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    ok = runner.recovers(tag_name)
    resting = runner._resolve_resting_value(tag_name)
    witness = runner.cause(tag_name, to=resting)
    text = f"recovers: {ok}"
    if witness is not None:
        text += f"\n{witness}"
    return ConsoleResult(text)


def _parse_tag_spec(spec: str) -> tuple[str, int | None, bool, Any]:
    """Parse ``Tag``, ``Tag@5``, or ``Tag:value`` into (tag, scan, has_value, value)."""
    if "@" in spec:
        tag, _, scan_s = spec.partition("@")
        try:
            scan = int(scan_s.strip())
        except ValueError as exc:
            raise ValueError(f"scan after '@' must be an integer, got '{scan_s}'") from exc
        return (tag.strip(), scan, False, None)
    if ":" in spec:
        tag, _, value_s = spec.partition(":")
        return (tag.strip(), None, True, _parse_value(value_s))
    return (spec.strip(), None, False, None)


def _parse_value(raw: str) -> Any:
    s = raw.strip()
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("none", "null"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


# ---------------------------------------------------------------------------
# DataView verbs
# ---------------------------------------------------------------------------


@register("dataview", usage="dataview <text | i: | p: | t: | upstream:tag>", group="analysis")
def _cmd_dataview(adapter: Any, expression: str) -> ConsoleResult:
    rest = expression.strip().split(None, 1)
    if len(rest) < 2:
        raise adapter.DAPAdapterError(
            "Usage: dataview <query> (e.g. dataview Motor, dataview i:, dataview upstream:Running)"
        )
    query = rest[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview()

    from pyrung.dap.handlers.graph_slice import _parse_query

    result = _parse_query(query, view)
    return _format_dataview(result)


@register("upstream", usage="upstream <tag>", group="analysis")
def _cmd_upstream(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: upstream <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview().upstream(tag_name)
    return _format_dataview(view)


@register("downstream", usage="downstream <tag>", group="analysis")
def _cmd_downstream(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: downstream <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview().downstream(tag_name)
    return _format_dataview(view)


def _tag_annotations(detail: Any) -> str:
    parts: list[str] = []
    if detail.type != "bool":
        parts.append(detail.type.capitalize())
    flags = []
    if detail.retentive:
        flags.append("retentive")
    if detail.readonly:
        flags.append("readonly")
    if detail.external:
        flags.append("external")
    if detail.final:
        flags.append("final")
    if detail.public:
        flags.append("public")
    if flags:
        parts.append(", ".join(flags))
    range_parts = []
    if detail.min is not None:
        range_parts.append(f"min:{detail.min}")
    if detail.max is not None:
        range_parts.append(f"max:{detail.max}")
    if detail.uom:
        range_parts.append(f"uom:{detail.uom}")
    if range_parts:
        parts.append(" ".join(range_parts))
    if detail.physical:
        parts.append(f"physical:{detail.physical}")
    if detail.link:
        parts.append(f"link:{detail.link}")
    if detail.choices:
        parts.append(f"choices:{detail.choices}")
    if detail.structure_kind and detail.structure_name:
        struct = f"{detail.structure_kind}:{detail.structure_name}"
        if detail.structure_field:
            struct += f".{detail.structure_field}"
        parts.append(struct)
    return "  ".join(parts)


def _format_dataview(view: Any) -> ConsoleResult:
    details = view.details()
    if not details:
        return ConsoleResult("No matching tags")
    sorted_names = sorted(details)
    max_name = max(len(n) for n in sorted_names)
    lines = []
    for name in sorted_names:
        d = details[name]
        pad = " " * (max_name - len(name))
        ann = _tag_annotations(d)
        suffix = f"  {ann}" if ann else ""
        lines.append(f"  {name}{pad}  ({d.role}){suffix}")
    return ConsoleResult(f"{len(details)} tag(s):\n" + "\n".join(lines))


def _field_annotations(f: Any) -> str:
    parts: list[str] = []
    flags = []
    if f.readonly:
        flags.append("readonly")
    if f.external:
        flags.append("external")
    if f.final:
        flags.append("final")
    if f.public:
        flags.append("public")
    if flags:
        parts.append(", ".join(flags))
    range_parts = []
    if f.min is not None:
        range_parts.append(f"min:{f.min}")
    if f.max is not None:
        range_parts.append(f"max:{f.max}")
    if f.uom:
        range_parts.append(f"uom:{f.uom}")
    if range_parts:
        parts.append(" ".join(range_parts))
    if f.physical:
        parts.append(f"physical:{f.physical}")
    if f.link:
        parts.append(f"link:{f.link}")
    if f.choices:
        parts.append(f"choices:{f.choices}")
    return "  ".join(parts)


@register("structures", usage="structures", group="analysis")
def _cmd_structures(adapter: Any, expression: str) -> ConsoleResult:
    runner = adapter._require_runner_locked()
    view = runner.program.dataview()
    structs = view.structures()
    if not structs:
        return ConsoleResult("No structures found")

    sections: list[str] = []
    udts = [s for s in structs if s.kind == "udt"]
    named_arrays = [s for s in structs if s.kind == "named_array"]

    if udts:
        lines = ["UDTs:"]
        for s in udts:
            lines.append(f"  {s.name} (count={s.count})")
            if s.fields:
                max_fname = max(len(f.name) for f in s.fields)
                for f in s.fields:
                    pad = " " * (max_fname - len(f.name))
                    ann = _field_annotations(f)
                    suffix = f"  {ann}" if ann else ""
                    lines.append(f"    {f.name}{pad}  {f.type.capitalize()}{suffix}")
        sections.append("\n".join(lines))

    if named_arrays:
        lines = ["Named Arrays:"]
        for s in named_arrays:
            header = f"  {s.name} (count={s.count}"
            if s.stride is not None:
                header += f", stride={s.stride}"
            if s.base_type is not None:
                header += f", type={s.base_type.capitalize()}"
            header += ")"
            lines.append(header)
            if s.fields:
                max_fname = max(len(f.name) for f in s.fields)
                for f in s.fields:
                    pad = " " * (max_fname - len(f.name))
                    ann = _field_annotations(f)
                    suffix = f"  {ann}" if ann else ""
                    lines.append(f"    {f.name}{pad}  {f.type.capitalize()}{suffix}")
        sections.append("\n".join(lines))

    return ConsoleResult("\n\n".join(sections))


# ---------------------------------------------------------------------------
# Simplified form
# ---------------------------------------------------------------------------


@register("simplified", usage="simplified [tag]", group="analysis")
def _cmd_simplified(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    runner = adapter._require_runner_locked()
    forms = runner.program.simplified()

    if len(parts) >= 2:
        tag_name = parts[1]
        form = forms.get(tag_name)
        if form is None:
            if tag_name not in {n for n in runner.program.dataview().tags}:
                raise adapter.DAPAdapterError(f"Unknown tag '{tag_name}'")
            raise adapter.DAPAdapterError(
                f"'{tag_name}' is not a terminal tag. Only terminals have simplified forms."
            )
        stats = f"  ({form.writer_count} writer(s), {form.pivot_count} pivot(s) resolved, depth {form.depth})"
        return ConsoleResult(f"{form}\n{stats}")

    if not forms:
        return ConsoleResult("No terminal tags found")
    lines = [str(f) for f in forms.values()]
    return ConsoleResult(f"{len(forms)} terminal(s):\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Monitor verbs
# ---------------------------------------------------------------------------


@register("monitor", usage="monitor <tag>", group="data")
def _cmd_monitor(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: monitor <tag>")
    tag_name = parts[1].strip()
    runner = adapter._require_runner_locked()
    monitor_id_ref: dict[str, int] = {"id": 0}
    handle = runner.monitor(
        tag_name,
        adapter._build_monitor_callback(tag_name=tag_name, monitor_id_ref=monitor_id_ref),
    )
    monitor_id_ref["id"] = handle.id
    adapter._monitor_handles[handle.id] = handle
    from pyrung.dap.handlers.monitor_data_breakpoints import MonitorMeta

    adapter._monitor_meta[handle.id] = MonitorMeta(id=handle.id, tag=tag_name, enabled=True)
    current = runner.current_state.tags.get(tag_name)
    adapter._monitor_values[handle.id] = adapter._format_value(current)
    return ConsoleResult(f"Monitor added: {tag_name} (id={handle.id})")


@register("unmonitor", usage="unmonitor <tag>", group="data")
def _cmd_unmonitor(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: unmonitor <tag>")
    tag_name = parts[1].strip()
    target_id = None
    for mid, meta in adapter._monitor_meta.items():
        if meta.tag == tag_name:
            target_id = mid
            break
    if target_id is None:
        raise adapter.DAPAdapterError(f"No monitor found for tag '{tag_name}'")
    handle = adapter._monitor_handles.pop(target_id, None)
    if handle is not None:
        handle.remove()
    adapter._monitor_meta.pop(target_id, None)
    adapter._monitor_values.pop(target_id, None)
    return ConsoleResult(f"Monitor removed: {tag_name}")


# ---------------------------------------------------------------------------
# Note / Log
# ---------------------------------------------------------------------------


@register("note", usage="note <text>", group="data")
def _cmd_note(adapter: Any, expression: str) -> ConsoleResult:
    rest = expression.strip()
    idx = rest.find(" ")
    if idx < 0 or not rest[idx:].strip():
        raise adapter.DAPAdapterError("Usage: note <text>")
    text = rest[idx:].strip()
    runner = adapter._require_runner_locked()
    scan_id = runner.current_state.scan_id
    adapter._notes.setdefault(scan_id, []).append(text)
    return ConsoleResult(f"Note at scan {scan_id}: {text}")


@register("log", usage="log [N]", group="analysis")
def _cmd_log(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    n = 20
    i = 1
    while i < len(parts):
        if parts[i] == "-n" and i + 1 < len(parts):
            try:
                n = int(parts[i + 1])
            except ValueError as exc:
                raise adapter.DAPAdapterError(
                    f"log count must be integer, got '{parts[i + 1]}'"
                ) from exc
            i += 2
        else:
            try:
                n = int(parts[i])
            except ValueError as exc:
                raise adapter.DAPAdapterError(
                    f"log count must be integer, got '{parts[i]}'"
                ) from exc
            i += 1
    if n < 1:
        raise adapter.DAPAdapterError("log count must be >= 1")

    runner = adapter._require_runner_locked()
    log = runner._scan_log
    tip = runner._state.scan_id
    forces = dict(runner.forces)

    start = max(log.base_scan, tip - n + 1)
    lines: list[str] = []
    lines.append(f"scan {tip}  forces: {_format_forces(forces)}")
    lines.append("")

    notes: dict[int, list[str]] = getattr(adapter, "_notes", {})
    action_log: list[tuple[int | None, str, str]] = getattr(adapter, "_action_log", [])
    scan_sources: dict[int, set[str]] = {}
    for a_scan, _cmd, prov in action_log:
        if a_scan is not None and prov != "console":
            scan_sources.setdefault(a_scan, set()).add(prov)

    prev_state = None
    for scan_id in range(start, tip + 1):
        entries: list[str] = []
        for note_text in notes.get(scan_id, []):
            entries.append(f"  # {note_text}")
        patches = log._patches_by_scan.get(scan_id)
        if patches:
            for tag, val in sorted(patches.items()):
                entries.append(f"  patch {tag} {val!r}")
        force_snap = log._force_changes_by_scan.get(scan_id)
        if force_snap is not None:
            prev_scan = scan_id - 1
            prev_forces = (
                log._force_changes_by_scan.get(prev_scan, {}) if prev_scan >= log.base_scan else {}
            )
            for tag in sorted(set(force_snap) | set(prev_forces)):
                old = prev_forces.get(tag)
                new = force_snap.get(tag)
                if old != new:
                    if new is not None and old is None:
                        entries.append(f"  force {tag} {new!r}")
                    elif new is None and old is not None:
                        entries.append(f"  unforce {tag}")
                    else:
                        entries.append(f"  force {tag} {old!r} → {new!r}")

        cur_state = runner.history.at(scan_id)
        if prev_state is not None:
            for key in sorted(set(cur_state.tags.keys()) | set(prev_state.tags.keys())):
                old_v = prev_state.tags.get(key)
                new_v = cur_state.tags.get(key)
                if old_v != new_v:
                    entries.append(f"  {key}: {old_v!r} → {new_v!r}")
        prev_state = cur_state

        if entries:
            sources = scan_sources.get(scan_id)
            tag = f"  ({', '.join(sorted(sources))})" if sources else ""
            lines.append(f"scan {scan_id}:{tag}")
            lines.extend(entries)

    if len(lines) == 2:
        lines.append("(no changes)")

    return ConsoleResult("\n".join(lines))


def _format_forces(forces: dict[str, Any]) -> str:
    if not forces:
        return "none"
    return ", ".join(f"{k}={v!r}" for k, v in sorted(forces.items()))


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


@register("help", usage="help")
def _cmd_help(adapter: Any, _expression: str) -> ConsoleResult:
    return ConsoleResult(_format_grouped_help())
