"""History time-travel seek for the DAP adapter.

Owns pyrungHistoryInfo and pyrungSeek custom request handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _SeekRequestArgs:
    scanId: Any = None


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
    raw_scan_id = parsed.scanId
    if not isinstance(raw_scan_id, int):
        raise adapter.DAPAdapterError("pyrungSeek.scanId must be an integer")

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
