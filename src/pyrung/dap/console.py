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

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class ConsoleResult:
    """Return value from a console command."""

    text: str
    events: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)


_REGISTRY: dict[str, tuple[Callable[..., ConsoleResult], str]] = {}


def register(verb: str, *, usage: str = "") -> Callable[..., Any]:
    def decorator(fn: Callable[..., ConsoleResult]) -> Callable[..., ConsoleResult]:
        _REGISTRY[verb] = (fn, usage)
        return fn

    return decorator


def dispatch(adapter: Any, expression: str) -> ConsoleResult:
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
    handler, _usage = entry
    return handler(adapter, expression)


# ---------------------------------------------------------------------------
# Existing verbs (migrated from stack_variables_evaluate)
# ---------------------------------------------------------------------------


@register("force", usage="force <tag> <value>")
def _cmd_force(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise adapter.DAPAdapterError("Usage: force <tag> <value>")
    tag = parts[1]
    raw_value = parts[2]
    value = adapter._parse_literal(raw_value)
    runner = adapter._require_runner_locked()
    runner.force(tag, value)
    return ConsoleResult(f"Forced {tag}={value!r}")


@register("unforce", usage="unforce <tag>")
def _cmd_unforce(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) != 2:
        raise adapter.DAPAdapterError("Usage: unforce <tag>")
    tag = parts[1]
    runner = adapter._require_runner_locked()
    runner.unforce(tag)
    return ConsoleResult(f"Removed force {tag}")


@register("clear_forces", usage="clear_forces")
def _cmd_clear_forces(adapter: Any, _expression: str) -> ConsoleResult:
    runner = adapter._require_runner_locked()
    runner.clear_forces()
    return ConsoleResult("Cleared all forces")


# ---------------------------------------------------------------------------
# New state-mutation verbs
# ---------------------------------------------------------------------------


@register("patch", usage="patch <tag> <value>")
def _cmd_patch(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split(None, 2)
    if len(parts) < 3:
        raise adapter.DAPAdapterError("Usage: patch <tag> <value>")
    tag = parts[1]
    raw_value = parts[2]
    value = adapter._parse_literal(raw_value)
    runner = adapter._require_runner_locked()
    runner.patch({tag: value})
    return ConsoleResult(f"Patched {tag}={value!r}")


@register("step", usage="step [N]")
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


@register("run", usage="run <cycles|duration>")
def _cmd_run(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError(
            "Usage: run <cycles> or run <duration> (e.g. run 10, run 500ms)"
        )
    spec = parts[1]
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


# ---------------------------------------------------------------------------
# Query verbs
# ---------------------------------------------------------------------------


@register("cause", usage="cause <tag>[@scan|:value]")
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


@register("effect", usage="effect <tag>[@scan|:value]")
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


@register("recovers", usage="recovers <tag>")
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


@register("dataview", usage="dataview <query>")
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


@register("upstream", usage="upstream <tag>")
def _cmd_upstream(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: upstream <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview().upstream(tag_name)
    return _format_dataview(view)


@register("downstream", usage="downstream <tag>")
def _cmd_downstream(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 2:
        raise adapter.DAPAdapterError("Usage: downstream <tag>")
    tag_name = parts[1]
    runner = adapter._require_runner_locked()
    view = runner.program.dataview().downstream(tag_name)
    return _format_dataview(view)


def _format_dataview(view: Any) -> ConsoleResult:
    roles = view.roles()
    if not roles:
        return ConsoleResult("No matching tags")
    lines = []
    for tag in sorted(roles):
        lines.append(f"  {tag}  ({roles[tag].value})")
    return ConsoleResult(f"{len(roles)} tag(s):\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Monitor verbs
# ---------------------------------------------------------------------------


@register("monitor", usage="monitor <tag>")
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


@register("unmonitor", usage="unmonitor <tag>")
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
# Help
# ---------------------------------------------------------------------------


@register("help", usage="help")
def _cmd_help(adapter: Any, _expression: str) -> ConsoleResult:
    lines = ["Available commands:"]
    for verb in sorted(_REGISTRY):
        _fn, usage = _REGISTRY[verb]
        lines.append(f"  {usage}" if usage else f"  {verb}")
    return ConsoleResult("\n".join(lines))
