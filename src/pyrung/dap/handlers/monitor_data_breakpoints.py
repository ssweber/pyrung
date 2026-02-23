"""Monitor/data-breakpoint ownership for DAP command handling.

Owns monitor CRUD/list events and dataBreakpoint* request handling.
Must preserve stop-reason semantics and event payload ordering.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyrung.core.state import SystemState
from pyrung.dap.args import parse_args

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass
class MonitorMeta:
    id: int
    tag: str
    enabled: bool = True


@dataclass
class DataBreakpointMeta:
    data_id: str
    tag: str
    condition_source: str | None
    condition: Callable[[SystemState], bool] | None
    hit_condition: int | None
    hit_count: int = 0


@dataclass(frozen=True)
class _DataBreakpointInfoRequestArgs:
    variablesReference: Any = 0
    name: Any = None


@dataclass(frozen=True)
class _SetDataBreakpointsRequestArgs:
    breakpoints: Any = None


@dataclass(frozen=True)
class _SetDataBreakpointEntryArgs:
    dataId: Any = None
    condition: Any = None
    hitCondition: Any = None


@dataclass(frozen=True)
class _AddMonitorRequestArgs:
    tag: Any = None


@dataclass(frozen=True)
class _RemoveMonitorRequestArgs:
    id: Any = None


@dataclass(frozen=True)
class _FindLabelRequestArgs:
    label: Any = None
    all: Any = False


def on_data_breakpoint_info(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_DataBreakpointInfoRequestArgs, args)
    variables_reference = int(parsed.variablesReference)
    name = parsed.name
    if variables_reference != adapter._monitor_scope_ref or not isinstance(name, str):
        return {
            "dataId": None,
            "description": "Data breakpoints are supported for PLC monitors",
        }, []

    with adapter._state_lock:
        monitor_tags = {meta.tag for meta in adapter._monitor_meta.values()}
    if name not in monitor_tags:
        return {"dataId": None, "description": f"No monitor registered for {name}"}, []

    data_id = adapter._data_id_for_tag(name)
    return {
        "dataId": data_id,
        "description": f"Break when {name} changes",
        "canPersist": True,
        "accessTypes": ["write"],
    }, []


def on_set_data_breakpoints(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_SetDataBreakpointsRequestArgs, args)
    raw_breakpoints = parsed.breakpoints
    if not isinstance(raw_breakpoints, list):
        raw_breakpoints = []

    parsed_requests: list[tuple[int, dict[str, Any]]] = []
    responses: list[dict[str, Any] | None] = []
    for raw_bp in raw_breakpoints:
        if not isinstance(raw_bp, dict):
            responses.append({"verified": False, "message": "Breakpoint entry must be an object"})
            continue
        bp_args = parse_args(_SetDataBreakpointEntryArgs, raw_bp, error=adapter.DAPAdapterError)
        data_id = bp_args.dataId
        if not isinstance(data_id, str) or not data_id.strip():
            responses.append({"verified": False, "message": "dataId is required"})
            continue
        data_id = data_id.strip()
        condition_source = bp_args.condition
        if condition_source is not None and not isinstance(condition_source, str):
            responses.append({"verified": False, "message": "condition must be a string"})
            continue
        try:
            hit_condition = adapter._parse_hit_condition(bp_args.hitCondition)
        except adapter.DAPAdapterError as exc:
            responses.append({"verified": False, "message": str(exc)})
            continue
        parsed_requests.append(
            (
                len(responses),
                {
                    "dataId": data_id,
                    "condition": (
                        condition_source.strip() if isinstance(condition_source, str) else None
                    ),
                    "hitCondition": hit_condition,
                },
            )
        )
        responses.append(None)

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        requested_data_ids = {requested_bp["dataId"] for _, requested_bp in parsed_requests}
        stale = [
            data_id for data_id in adapter._data_bp_handles if data_id not in requested_data_ids
        ]
        for data_id in stale:
            handle = adapter._data_bp_handles.pop(data_id, None)
            if handle is not None:
                handle.remove()
            adapter._data_bp_meta.pop(data_id, None)

        for response_index, requested_bp in parsed_requests:
            data_id = requested_bp["dataId"]
            condition_source = requested_bp["condition"]
            existing_meta = adapter._data_bp_meta.get(data_id)
            unchanged = (
                existing_meta is not None
                and existing_meta.condition_source == condition_source
                and existing_meta.hit_condition == requested_bp["hitCondition"]
            )
            if unchanged:
                responses[response_index] = {"verified": True, "message": f"Watching {data_id}"}
                continue

            if data_id in adapter._data_bp_handles:
                handle = adapter._data_bp_handles.pop(data_id, None)
                if handle is not None:
                    handle.remove()
                adapter._data_bp_meta.pop(data_id, None)

            condition = None
            if condition_source:
                try:
                    condition = adapter._compile_condition(condition_source)
                except adapter.DAPAdapterError as exc:
                    responses[response_index] = {"verified": False, "message": str(exc)}
                    continue

            tag_name = adapter._tag_from_data_id(data_id)
            if tag_name is None:
                responses[response_index] = {
                    "verified": False,
                    "message": f"Unsupported dataId: {data_id}",
                }
                continue

            handle = runner.monitor(
                tag_name,
                adapter._build_data_breakpoint_callback(data_id=data_id),
            )
            adapter._data_bp_handles[data_id] = handle
            adapter._data_bp_meta[data_id] = DataBreakpointMeta(
                data_id=data_id,
                tag=tag_name,
                condition_source=condition_source,
                condition=condition,
                hit_condition=requested_bp["hitCondition"],
                hit_count=0,
            )
            responses[response_index] = {"verified": True, "message": f"Watching {tag_name}"}

    return {
        "breakpoints": [
            response
            if response is not None
            else {"verified": False, "message": "Invalid data breakpoint"}
            for response in responses
        ]
    }, []


def on_pyrung_add_monitor(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_AddMonitorRequestArgs, args)
    tag = parsed.tag
    if not isinstance(tag, str) or not tag.strip():
        raise adapter.DAPAdapterError("pyrungAddMonitor.tag is required")
    tag_name = tag.strip()

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        monitor_id_ref: dict[str, int] = {"id": 0}
        handle = runner.monitor(
            tag_name,
            adapter._build_monitor_callback(tag_name=tag_name, monitor_id_ref=monitor_id_ref),
        )
        monitor_id_ref["id"] = handle.id
        adapter._monitor_handles[handle.id] = handle
        adapter._monitor_meta[handle.id] = MonitorMeta(id=handle.id, tag=tag_name, enabled=True)
        current = runner.current_state.tags.get(tag_name)
        adapter._monitor_values[handle.id] = adapter._format_value(current)
    return {"id": handle.id, "tag": tag_name, "enabled": True}, []


def on_pyrung_remove_monitor(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_RemoveMonitorRequestArgs, args)
    raw_id = parsed.id
    if not isinstance(raw_id, int):
        raise adapter.DAPAdapterError("pyrungRemoveMonitor.id must be an integer")
    with adapter._state_lock:
        handle = adapter._monitor_handles.pop(raw_id, None)
        if handle is not None:
            handle.remove()
        removed = raw_id in adapter._monitor_meta
        adapter._monitor_meta.pop(raw_id, None)
        adapter._monitor_values.pop(raw_id, None)
    return {"id": raw_id, "removed": removed}, []


def on_pyrung_list_monitors(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        monitors = []
        for monitor_id in sorted(adapter._monitor_meta):
            meta = adapter._monitor_meta[monitor_id]
            monitors.append(
                {
                    "id": monitor_id,
                    "tag": meta.tag,
                    "enabled": bool(meta.enabled),
                    "value": adapter._monitor_values.get(monitor_id, "None"),
                }
            )
    return {"monitors": monitors}, []


def on_pyrung_find_label(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_FindLabelRequestArgs, args)
    label = parsed.label
    if not isinstance(label, str) or not label.strip():
        raise adapter.DAPAdapterError("pyrungFindLabel.label is required")
    find_all = bool(parsed.all)

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        if find_all:
            states = runner.history.find_all(label)
        else:
            latest = runner.history.find(label)
            states = [] if latest is None else [latest]

    matches = [{"scanId": state.scan_id, "timestamp": state.timestamp} for state in states]
    return {"matches": matches}, []


def monitor_variables(adapter: Any) -> list[dict[str, Any]]:
    variables: list[dict[str, Any]] = []
    for monitor_id in sorted(adapter._monitor_meta):
        meta = adapter._monitor_meta[monitor_id]
        tag_name = meta.tag
        variables.append(
            {
                "name": tag_name,
                "value": adapter._monitor_values.get(monitor_id, "None"),
                "type": "monitor",
                "evaluateName": tag_name,
                "variablesReference": 0,
            }
        )
    return variables


def build_monitor_callback(
    adapter: Any,
    *,
    tag_name: str,
    monitor_id_ref: dict[str, int],
) -> Callable[[Any, Any], None]:
    def _callback(current: Any, previous: Any) -> None:
        try:
            monitor_id = monitor_id_ref.get("id")
            if not isinstance(monitor_id, int) or monitor_id <= 0:
                return
            if monitor_id not in adapter._monitor_handles:
                return
            adapter._monitor_values[monitor_id] = adapter._format_value(current)
            runner = adapter._runner
            if runner is None:
                return
            state = runner.current_state
            adapter._enqueue_internal_event(
                "pyrungMonitor",
                {
                    "id": monitor_id,
                    "tag": tag_name,
                    "current": adapter._format_value(current),
                    "previous": adapter._format_value(previous),
                    "scanId": state.scan_id,
                    "timestamp": state.timestamp,
                },
            )
        except Exception:
            return

    return _callback


def build_data_breakpoint_callback(
    adapter: Any,
    *,
    data_id: str,
) -> Callable[[Any, Any], None]:
    def _callback(_current: Any, _previous: Any) -> None:
        try:
            meta = adapter._data_bp_meta.get(data_id)
            runner = adapter._runner
            if meta is None or runner is None:
                return

            condition = meta.condition
            if callable(condition) and not condition(runner.current_state):
                return

            hit_condition = meta.hit_condition
            hit_count = meta.hit_count + 1
            if hit_condition is None:
                meta.hit_count = hit_count
            else:
                if hit_count != int(hit_condition):
                    meta.hit_count = hit_count
                    return
                meta.hit_count = 0

            adapter._pending_predicate_pause = True
        except Exception:
            return

    return _callback


def data_id_for_tag(adapter: Any, tag_name: str) -> str:
    return f"tag:{tag_name}"


def tag_from_data_id(adapter: Any, data_id: str) -> str | None:
    prefix = "tag:"
    if not data_id.startswith(prefix):
        return None
    tag_name = data_id[len(prefix) :]
    return tag_name or None


def clear_debug_registrations_locked(adapter: Any) -> None:
    for handle in adapter._monitor_handles.values():
        try:
            handle.remove()
        except Exception:
            continue
    for handle in adapter._data_bp_handles.values():
        try:
            handle.remove()
        except Exception:
            continue
    adapter._monitor_handles.clear()
    adapter._monitor_meta.clear()
    adapter._monitor_values.clear()
    adapter._data_bp_handles.clear()
    adapter._data_bp_meta.clear()
    adapter._breakpoints.clear_source_breakpoints()
    adapter._pending_snapshot_labels_by_scan.clear()
    adapter._pending_predicate_pause = False
