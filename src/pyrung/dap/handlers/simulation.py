"""Simulator control for DAP: RUN/STOP mode, pacing speed, state frames, Modbus.

Owns pyrungSetMode, pyrungSetSpeed, and pyrungModbusInfo custom requests,
Click address resolution for force/patch targets, and the
``pyrungScanFrame`` bodies emitted after state changes outside continue.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pyrung.dap import execution_flow
from pyrung.dap.handlers.force_patch import json_safe_forces

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]

_MODES = ("run", "stop")

#: How long a mode change waits for a running continue worker to pause.
_PAUSE_JOIN_TIMEOUT = 2.0


@dataclass(frozen=True)
class _SetModeRequestArgs:
    mode: Any = None
    patches: Any = None
    forces: Any = None


@dataclass(frozen=True)
class _SetSpeedRequestArgs:
    speed: Any = None


#: Requests answered without the state lock.  They must not wait behind a
#: long command holding it, so no state frame check runs around them.
LOCK_FREE_COMMANDS = frozenset(
    {
        "initialize",
        "configurationDone",
        "threads",
        "pause",
        "disconnect",
        "terminate",
        "pyrungModbusInfo",
    }
)


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


def enter_stop_locked(adapter: Any) -> None:
    """Abandon any partial scan and put the runner in STOP mode."""
    adapter._pending_stop = False
    runner = adapter._require_runner_locked()
    execution_flow.invalidate_mid_scan(adapter)
    adapter._current_scan_id = None
    runner.stop()


def request_stop_locked(adapter: Any) -> None:
    """Enter STOP now, or ask a running continue worker to do it as it pauses.

    Entering STOP under a running worker would be undone by its next scan
    (every scan starts with a STOP -> RUN transition), so the worker performs
    the stop itself once it has stopped scanning.
    """
    if adapter._thread_running_locked():
        adapter._pending_stop = True
        adapter._pause_event.set()
        return
    enter_stop_locked(adapter)


def set_mode_locked(
    adapter: Any,
    mode: str,
    *,
    forces: Mapping[str, bool | int | float | str] | None = None,
    patches: Mapping[str, bool | int | float | str] | None = None,
) -> None:
    """Change PLC mode, then apply *forces* and *patches* (targets already resolved).

    ``run`` from STOP performs the STOP -> RUN transition (non-retentive
    reset, forces and patches cleared) without executing a scan, so the
    first scan sees the overrides given here.
    """
    runner = adapter._require_runner_locked()
    if mode == "stop":
        request_stop_locked(adapter)
    else:
        # A RUN request cancels a STOP still waiting on the worker.
        adapter._pending_stop = False
        if runner.mode == "stop":
            if adapter._thread_running_locked():
                raise adapter.DAPAdapterError("Cannot enter RUN while continue is running")
            execution_flow.invalidate_mid_scan(adapter)
            adapter._current_scan_id = None
            runner.start()
    for name, value in (forces or {}).items():
        _apply(adapter, "force", lambda n=name, v=value: runner.force(n, v), name)
    if patches:
        _apply(adapter, "patch", lambda: runner.patch(dict(patches)), ", ".join(patches))
    if forces or patches:
        execution_flow.invalidate_mid_scan(adapter)


def _apply(adapter: Any, verb: str, action: Any, target: str) -> None:
    try:
        action()
    except KeyError as exc:
        raise adapter.DAPAdapterError(f"unknown tag: {target}") from exc
    except (ValueError, TypeError) as exc:
        raise adapter.DAPAdapterError(f"cannot {verb} {target}: {exc}") from exc


#: Console verbs that run scans themselves; they pause a running worker first.
PAUSE_FIRST_VERBS = frozenset({"step", "run"})


def pause_before_console_command(adapter: Any, expression: str) -> None:
    """Pause a running continue worker if *expression* will run scans (no lock held).

    Anyone can drive and the last command wins: a ``step`` or ``run`` sent
    while continue runs pauses it and then executes.
    """
    parts = expression.strip().split(None, 1)
    if parts and parts[0].lower() in PAUSE_FIRST_VERBS:
        pause_executor(adapter)


def pause_executor(adapter: Any) -> None:
    """Pause a running continue worker and wait for it to finish (no lock held)."""
    with adapter._state_lock:
        thread = adapter._continue_thread
        if thread is None or not thread.is_alive():
            return
        adapter._pause_event.set()
    thread.join(timeout=_PAUSE_JOIN_TIMEOUT)
    if thread.is_alive():
        raise adapter.DAPAdapterError("Timed out waiting for execution to pause")


def _target_values(
    adapter: Any, raw: Any, *, field: str
) -> dict[str, bool | int | float | str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise adapter.DAPAdapterError(f"pyrungSetMode.{field} must be an object")
    values: dict[str, bool | int | float | str] = {}
    for target, value in raw.items():
        if not isinstance(target, str) or not target.strip():
            raise adapter.DAPAdapterError(f"pyrungSetMode.{field} keys must be non-empty strings")
        if not isinstance(value, (bool, int, float, str)):
            raise adapter.DAPAdapterError(
                f"pyrungSetMode.{field}[{target!r}] must be a bool, number, or string"
            )
        values[target.strip()] = value
    return values


def resolve_targets_locked(
    adapter: Any, values: Mapping[str, bool | int | float | str] | None
) -> dict[str, bool | int | float | str] | None:
    if values is None:
        return None
    return {adapter._resolve_target_locked(target): value for target, value in values.items()}


def on_pyrung_set_mode(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_SetModeRequestArgs, args)
    mode = parsed.mode
    if mode not in _MODES:
        raise adapter.DAPAdapterError('pyrungSetMode.mode must be "run" or "stop"')
    forces = _target_values(adapter, parsed.forces, field="forces")
    patches = _target_values(adapter, parsed.patches, field="patches")

    with adapter._state_lock:
        adapter._require_runner_locked()
    pause_executor(adapter)

    with adapter._state_lock:
        resolved_forces = resolve_targets_locked(adapter, forces)
        resolved_patches = resolve_targets_locked(adapter, patches)
        set_mode_locked(adapter, mode, forces=resolved_forces, patches=resolved_patches)
        runner = adapter._require_runner_locked()
        body = {
            "mode": runner.mode,
            "forces": json_safe_forces(runner.forces),
            "pendingPatches": json_safe_forces(runner.pending_patches),
        }
    return body, []


# ---------------------------------------------------------------------------
# Speed
# ---------------------------------------------------------------------------


def parse_speed(adapter: Any, raw: Any, *, prefix: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise adapter.DAPAdapterError(f"{prefix} must be a number")
    speed = float(raw)
    if not math.isfinite(speed) or speed < 0:
        raise adapter.DAPAdapterError(f"{prefix} must be >= 0 (0 = unpaced)")
    return speed


def on_pyrung_set_speed(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_SetSpeedRequestArgs, args)
    speed = parse_speed(adapter, parsed.speed, prefix="pyrungSetSpeed.speed")
    with adapter._state_lock:
        adapter._speed = speed
    return {"speed": speed}, []


# ---------------------------------------------------------------------------
# Modbus
# ---------------------------------------------------------------------------


def on_pyrung_modbus_info(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    from pyrung.dap.modbus import modbus_info

    return modbus_info(adapter._modbus), []


# ---------------------------------------------------------------------------
# Address targets
# ---------------------------------------------------------------------------


def resolve_target_locked(adapter: Any, target: str) -> str:
    """Return the tag name for *target*, a tag name or a Click address.

    Tag names win, so tags named like addresses (codegen names un-nicknamed
    operands ``X003``) keep working.  A Click address ("X001", "ds10")
    resolves through the session TagMap and the project's Click bank blocks.
    Anything else is an unknown target: forcing or patching it would only
    create a tag the program never reads.
    """
    runner = adapter._require_runner_locked()
    if (
        target in runner._known_tags_by_name
        or target in runner.current_state.tags
        or target in runner.forces
        or runner._system_runtime.resolve(target, runner.current_state)[0]
    ):
        return target

    from pyclickplc.addresses import format_address_display, parse_address

    try:
        bank, index = parse_address(target)
    except ValueError:
        raise adapter.DAPAdapterError(f"unknown tag: {target}") from None
    display = format_address_display(bank, index)

    provider = adapter._click_provider
    name = provider.tag_name(display) if provider is not None else None
    if name is not None:
        return name
    if bank in ("XD", "YD"):
        raise adapter.DAPAdapterError(
            f"{display} is a word image of {bank[0]} bits; use the bit addresses instead"
        )
    suffix = "" if adapter._has_tag_map else "; the project has no TagMap"
    raise adapter.DAPAdapterError(
        f"unknown tag: {target} (address {display} is not mapped to a tag{suffix})"
    )


# ---------------------------------------------------------------------------
# State frames
# ---------------------------------------------------------------------------


def state_fingerprint_locked(adapter: Any) -> tuple[Any, ...] | None:
    """Return a cheap value that changes whenever a state frame is due.

    While continue runs, committed scans are reported by the worker's own
    frames, so the committed state is left out of the fingerprint.
    """
    runner = adapter._runner
    if runner is None:
        return None
    executing = adapter._executor_running_locked()
    return (
        id(runner),
        None if executing else id(runner.current_state),
        None if executing else id(adapter._current_step),
        tuple(runner.forces.items()),
        tuple(runner.pending_patches.items()),
        runner.mode,
        adapter._speed,
        executing,
        adapter._links_revision,
    )


def frame_body_locked(
    adapter: Any,
    *,
    running: bool,
    trace_body: dict[str, Any] | None = None,
    changes: list[dict[str, Any]] | None = None,
    monitors: list[dict[str, Any]] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    outputs: list[str] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``pyrungScanFrame`` body for the current runner.

    *source* says who caused the frame: ``{"origin": "dap" | "live" |
    "worker" | "modbus" | "autoreload", "command": str}``.
    """
    runner = adapter._require_runner_locked()
    if trace_body is None:
        trace_body = adapter._live_trace_body_locked(allow_empty=True)
    assert trace_body is not None
    return {
        "scanId": trace_body.get("scanId"),
        "trace": trace_body,
        "monitors": monitors or [],
        "changes": changes or [],
        "snapshots": snapshots or [],
        "outputs": outputs or [],
        "mode": runner.mode,
        "running": running,
        "speed": adapter._speed,
        "actualSpeed": adapter._actual_speed if running else None,
        "pendingPatches": json_safe_forces(runner.pending_patches),
        "linksRevision": adapter._links_revision,
        "source": source,
    }


def state_frame_body_locked(adapter: Any, source: dict[str, Any] | None) -> dict[str, Any] | None:
    """Frame for a state change outside the continue worker's own cadence."""
    if adapter._runner is None or not adapter._configuration_done:
        return None
    # After a step, carry the same trace that step's ``pyrungTrace`` did, so
    # editor decorations keep showing it rather than a merged scan.
    trace_body = None
    if adapter._last_trace_was_step and not adapter._thread_running_locked():
        trace_body = adapter._current_trace_body_locked()
    return frame_body_locked(
        adapter,
        running=adapter._executor_running_locked(),
        trace_body=trace_body,
        source=source,
    )


def request_source(command: str, args: Any) -> dict[str, Any]:
    """``source`` of a frame caused by a DAP request."""
    if command == "evaluate" and isinstance(args, dict):
        expression = args.get("expression")
        if isinstance(expression, str) and expression.strip():
            return {"origin": "dap", "command": expression.strip()}
    return {"origin": "dap", "command": command}
