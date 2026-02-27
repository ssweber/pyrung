"""Source-breakpoint ownership for DAP command handling.

Owns setBreakpoints parsing/validation and condition parsing helpers.
Must preserve verification behavior, ordering, and message text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyrung.core.state import SystemState
from pyrung.dap.args import parse_args
from pyrung.dap.breakpoints import SourceBreakpoint
from pyrung.dap.expressions import ExpressionParseError
from pyrung.dap.expressions import compile as compile_condition
from pyrung.dap.expressions import parse as parse_condition

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _SourceArgs:
    path: Any = None


@dataclass(frozen=True)
class _SetBreakpointsRequestArgs:
    source: Any = None
    breakpoints: Any = None
    lines: Any = None


@dataclass(frozen=True)
class _SetBreakpointEntryArgs:
    line: Any = None
    condition: Any = None
    hitCondition: Any = None
    logMessage: Any = None
    enabled: Any = True


def on_set_breakpoints(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_SetBreakpointsRequestArgs, args)
    source = parsed.source
    if not isinstance(source, dict):
        raise adapter.DAPAdapterError("setBreakpoints.source is required")
    source_args = parse_args(_SourceArgs, source, error=adapter.DAPAdapterError)
    source_path = source_args.path
    if not isinstance(source_path, str):
        raise adapter.DAPAdapterError("setBreakpoints.source.path is required")

    canonical = adapter._canonical_path(source_path)
    if canonical is None:
        return {"breakpoints": []}, []

    requested_breakpoints = adapter._requested_breakpoints(args)
    with adapter._state_lock:
        valid_lines = adapter._breakpoints.valid_lines(canonical)
        existing = adapter._breakpoints.source_breakpoints(canonical)
        new_map: dict[int, SourceBreakpoint] = {}
        response_bps: list[dict[str, Any]] = []

        for requested in requested_breakpoints:
            line = requested["line"]
            if line not in valid_lines:
                response_bps.append({"verified": False, "line": line})
                continue

            condition_source = requested.get("condition")
            condition: Callable[[SystemState], bool] | None = None
            if isinstance(condition_source, str) and condition_source.strip():
                try:
                    condition = adapter._compile_condition(condition_source)
                except adapter.DAPAdapterError as exc:
                    response_bps.append({"verified": False, "line": line, "message": str(exc)})
                    continue

            try:
                hit_condition = adapter._parse_hit_condition(requested.get("hitCondition"))
            except adapter.DAPAdapterError as exc:
                response_bps.append({"verified": False, "line": line, "message": str(exc)})
                continue

            enabled = bool(requested.get("enabled", True))
            log_message = requested.get("logMessage")
            if log_message is not None and not isinstance(log_message, str):
                response_bps.append(
                    {"verified": False, "line": line, "message": "logMessage must be a string"}
                )
                continue
            snapshot_label: str | None = None
            if isinstance(log_message, str) and log_message.startswith("Snapshot:"):
                snapshot_label = log_message.split(":", 1)[1].strip()
                if not snapshot_label:
                    response_bps.append(
                        {
                            "verified": False,
                            "line": line,
                            "message": "Snapshot logpoint requires a non-empty label",
                        }
                    )
                    continue

            previous = existing.get(line)
            hit_count = previous.hit_count if previous is not None else 0
            last_scan_id = previous.last_scan_id if previous is not None else None
            new_map[line] = SourceBreakpoint(
                line=line,
                enabled=enabled,
                condition_source=condition_source.strip()
                if isinstance(condition_source, str)
                else None,
                condition=condition,
                hit_condition=hit_condition,
                hit_count=hit_count,
                log_message=log_message,
                snapshot_label=snapshot_label,
                last_scan_id=last_scan_id,
            )
            response_bps.append({"verified": True, "line": line})

        adapter._breakpoints.set_source_breakpoints(canonical, new_map)

    return {"breakpoints": response_bps}, []


def requested_breakpoints(adapter: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    parsed = adapter._parse_request_args(_SetBreakpointsRequestArgs, args)
    requested: list[dict[str, Any]] = []
    raw_bps = parsed.breakpoints
    if isinstance(raw_bps, list):
        for bp in raw_bps:
            if not isinstance(bp, dict):
                continue
            bp_args = parse_args(_SetBreakpointEntryArgs, bp, error=adapter.DAPAdapterError)
            line = bp_args.line
            if not isinstance(line, int):
                continue
            requested.append(
                {
                    "line": line,
                    "condition": bp_args.condition,
                    "hitCondition": bp_args.hitCondition,
                    "logMessage": bp_args.logMessage,
                    "enabled": bp_args.enabled,
                }
            )
        return requested

    raw_lines = parsed.lines
    if isinstance(raw_lines, list):
        for line in raw_lines:
            if isinstance(line, int):
                requested.append({"line": line, "enabled": True})
    return requested


def compile_condition_for_breakpoint(adapter: Any, source: str) -> Callable[[SystemState], bool]:
    try:
        expr = parse_condition(source)
    except ExpressionParseError as exc:
        raise adapter.DAPAdapterError(str(exc)) from exc
    return compile_condition(expr)


def parse_hit_condition(adapter: Any, raw_value: Any) -> int | None:
    if raw_value is None:
        return None
    if isinstance(raw_value, int):
        if raw_value <= 0:
            raise adapter.DAPAdapterError("hitCondition must be >= 1")
        return raw_value
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        if text.isdigit():
            parsed = int(text)
            if parsed <= 0:
                raise adapter.DAPAdapterError("hitCondition must be >= 1")
            return parsed
    raise adapter.DAPAdapterError("hitCondition must be a positive integer")
