"""History time-travel request handling for the DAP adapter.

Owns pyrungHistoryInfo, pyrungSeek, pyrungTagChanges, and pyrungForkAt
custom requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.dap.handlers import monitor_data_breakpoints

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _SeekRequestArgs:
    scanId: Any = None


@dataclass(frozen=True)
class _TagChangesRequestArgs:
    tags: Any = None
    count: Any = 50
    beforeScan: Any = None


@dataclass(frozen=True)
class _ForkAtRequestArgs:
    scanId: Any = None


def _require_scan_id(raw_scan_id: Any, *, prefix: str, error: type[Exception]) -> int:
    if not isinstance(raw_scan_id, int):
        raise error(f"{prefix}.scanId must be an integer")
    return raw_scan_id


def _reset_runner_state_locked(adapter: Any, *, runner: Any) -> None:
    monitor_data_breakpoints.clear_debug_registrations_locked(
        adapter,
        clear_source_breakpoints=False,
    )
    adapter._runner = runner
    adapter._scan_gen = None
    adapter._current_scan_id = None
    adapter._current_step = None
    adapter._current_rung_index = None
    adapter._current_rung = None
    adapter._current_ctx = None
    adapter._pending_predicate_pause = False
    adapter._rebuild_breakpoint_index_locked()

    # Preserve source breakpoints across forks, but clear any branch-local hit state.
    for file_breakpoints in adapter._breakpoints.source_breakpoints_by_file.values():
        for breakpoint in file_breakpoints.values():
            breakpoint.hit_count = 0
            breakpoint.last_scan_id = None


def on_pyrung_history_info(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        history = runner.history
        return {
            "minScanId": history.oldest_scan_id,
            "maxScanId": history.newest_scan_id,
            "playhead": runner.playhead,
            "count": len(history._order),
        }, []


def on_pyrung_seek(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_SeekRequestArgs, args)
    raw_scan_id = _require_scan_id(
        parsed.scanId,
        prefix="pyrungSeek",
        error=adapter.DAPAdapterError,
    )

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        try:
            state = runner.seek(raw_scan_id)
        except KeyError as exc:
            raise adapter.DAPAdapterError(f"Scan {raw_scan_id} is not retained in history") from exc

        tags = {name: adapter._format_value(value) for name, value in state.tags.items()}

    return {
        "scanId": state.scan_id,
        "timestamp": state.timestamp,
        "tags": tags,
    }, []


def on_pyrung_tag_changes(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_TagChangesRequestArgs, args)

    raw_tags = parsed.tags
    if not isinstance(raw_tags, list):
        raise adapter.DAPAdapterError("pyrungTagChanges.tags must be an array")

    tags: list[str] = []
    seen: set[str] = set()
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str) or not raw_tag.strip():
            raise adapter.DAPAdapterError("pyrungTagChanges.tags must contain non-empty strings")
        tag_name = raw_tag.strip()
        if tag_name in seen:
            continue
        seen.add(tag_name)
        tags.append(tag_name)

    raw_count = parsed.count
    if not isinstance(raw_count, int):
        raise adapter.DAPAdapterError("pyrungTagChanges.count must be an integer")
    if raw_count < 1:
        raise adapter.DAPAdapterError("pyrungTagChanges.count must be >= 1")

    raw_before_scan = parsed.beforeScan
    if raw_before_scan is not None and not isinstance(raw_before_scan, int):
        raise adapter.DAPAdapterError("pyrungTagChanges.beforeScan must be an integer")

    if not tags:
        return {"entries": []}, []

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        retained_scan_ids = list(runner.history._order)
        entries: list[dict[str, Any]] = []

        for index in range(len(retained_scan_ids) - 1, 0, -1):
            scan_id = retained_scan_ids[index]
            if raw_before_scan is not None and scan_id >= raw_before_scan:
                continue

            prev_scan_id = retained_scan_ids[index - 1]
            previous_state = runner.history.at(prev_scan_id)
            current_state = runner.history.at(scan_id)

            changes: dict[str, list[str]] = {}
            for tag_name in tags:
                old_value = previous_state.tags.get(tag_name)
                new_value = current_state.tags.get(tag_name)
                if old_value == new_value:
                    continue
                changes[tag_name] = [
                    adapter._format_value(old_value),
                    adapter._format_value(new_value),
                ]

            if not changes:
                continue

            entries.append(
                {
                    "scanId": scan_id,
                    "prevScanId": prev_scan_id,
                    "timestamp": current_state.timestamp,
                    "changes": changes,
                }
            )
            if len(entries) >= raw_count:
                break

    return {"entries": entries}, []


def on_pyrung_fork_at(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_ForkAtRequestArgs, args)
    raw_scan_id = _require_scan_id(
        parsed.scanId,
        prefix="pyrungForkAt",
        error=adapter.DAPAdapterError,
    )

    with adapter._state_lock:
        if adapter._thread_running_locked():
            raise adapter.DAPAdapterError("Cannot fork while continue is running")

        runner = adapter._require_runner_locked()
        try:
            forked_runner = runner.fork(raw_scan_id)
        except KeyError as exc:
            raise adapter.DAPAdapterError(f"Scan {raw_scan_id} is not retained in history") from exc

        _reset_runner_state_locked(adapter, runner=forked_runner)
        state = forked_runner.current_state

    return {
        "scanId": state.scan_id,
        "timestamp": state.timestamp,
    }, [("stopped", adapter._stopped_body("pause"))]
