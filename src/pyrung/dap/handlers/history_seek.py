"""History time-travel request handling for the DAP adapter.

Owns pyrungHistoryInfo, pyrungSeek, pyrungTagChanges, and pyrungForkAt
custom requests.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from pyrung.dap.handlers import monitor_data_breakpoints
from pyrung.dap.internal_tags import visible_tag_names

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]

# What one panel request may spend stepping unrecorded scans again.  The scans it reads stay
# read, so the next request goes on from there.
_SETTLE_CLOCK_S = 3.0
# Scans to step at a time, so the clock is looked at often: the first stretch, then what a
# quarter of a second buys at the speed measured.
_SETTLE_FIRST_STRETCH = 500
_SETTLE_MIN_STRETCH = 200
_SETTLE_MAX_STRETCH = 16_000
_SETTLE_STRETCH_S = 0.25


@dataclass(frozen=True)
class _SeekRequestArgs:
    scanId: Any = None


@dataclass(frozen=True)
class _TagChangesRequestArgs:
    tags: Any = None
    count: Any = 50
    beforeScan: Any = None
    afterScan: Any = None


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
            "count": history.newest_scan_id - history.oldest_scan_id + 1,
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

        tags = adapter._tag_values_body(state.tags)

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

    raw_after_scan = parsed.afterScan
    if raw_after_scan is not None and not isinstance(raw_after_scan, int):
        raise adapter.DAPAdapterError("pyrungTagChanges.afterScan must be an integer")

    if not tags:
        return {"entries": []}, []

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        entries = _read_changes(
            adapter,
            runner,
            visible_tag_names(tags),
            raw_count,
            before=raw_before_scan,
            after=raw_after_scan,
        )
    return {"entries": entries}, []


def _read_changes(
    adapter: Any,
    runner: Any,
    names: list[str],
    count: int,
    *,
    before: int | None,
    after: int | None,
) -> list[dict[str, Any]]:
    """The newest *count* scans that changed *names*, read from what each scan committed.

    Scans that ran with no records are stepped again first, a stretch at a time from the newest,
    and keep the records that gives them.  When a request may step no more, what it has not read
    comes back as one window entry, which says how the tags stand at its two ends and claims
    no scan for any change inside.

    A refresh that ran out of allowance leaves the next refreshes to read only what is recorded,
    so each stop of a stepping session does not pay for it again: a page asked for by its
    *before* goes on reading.
    """
    history = runner.history
    newest = history.newest_scan_id
    lower = max(history.oldest_scan_id, after if after is not None else history.oldest_scan_id)
    cursor = newest + 1 if before is None else min(before, newest + 1)
    entries: list[dict[str, Any]] = []
    may_settle = not adapter._thread_running_locked() and (
        before is not None or adapter._tag_changes_ran_out_on is not runner
    )
    deadline = time.monotonic() + _SETTLE_CLOCK_S
    stretch = _SETTLE_FIRST_STRETCH
    while cursor - 1 > lower and len(entries) < count:
        unread = runner._unrecorded_in(lower + 1, cursor - 1)
        top = max((end for _owner, (_start, end) in unread), default=None)
        if top is None:
            first = lower + 1
        elif top < cursor - 1:
            first = top + 1
        else:
            if not may_settle or time.monotonic() >= deadline:
                if before is None and may_settle:
                    adapter._tag_changes_ran_out_on = runner
                break
            # From a checkpoint, so stepping starts there and a window can end there at no cost.
            interval = runner._checkpoint_interval
            first = max(lower + 1, (cursor - stretch) // interval * interval + 1)
            began = time.monotonic()
            runner._settle_compiled_ranges(first, cursor - 1)
            took = time.monotonic() - began
            stretch = int((cursor - first) / max(took, 1e-3) * _SETTLE_STRETCH_S)
            stretch = max(_SETTLE_MIN_STRETCH, min(_SETTLE_MAX_STRETCH, stretch))
        found = _changes_within(runner, names, first, cursor, count - len(entries))
        for scan_id in sorted(found, reverse=True)[: count - len(entries)]:
            entries.append(
                {
                    "scanId": scan_id,
                    "prevScanId": scan_id - 1,
                    "timestamp": runner._timestamp_at(scan_id),
                    "changes": {
                        name: [
                            adapter._format_value(transition.from_value),
                            adapter._format_value(transition.to_value),
                        ]
                        for name, transition in found[scan_id].items()
                    },
                }
            )
        cursor = first
    if len(entries) < count and cursor - 1 > lower:
        entries.append(_window_entry(adapter, history, names, lower, cursor - 1))
    return entries


def _changes_within(
    runner: Any, names: list[str], first: int, before: int, most: int
) -> dict[int, dict[str, Any]]:
    """Scans in *first*..*before*-1 and what changed on them, each tag's newest *most* changes.

    A tag cut short has *most* newer scans of its own, so the newest *most* scans here are whole.
    """
    found: dict[int, dict[str, Any]] = {}
    for name in names:
        cursor = before
        for _ in range(most):
            transition = runner._causal_last_committed_transition_before(name, cursor)
            if transition is None or transition.scan_id < first:
                break
            found.setdefault(transition.scan_id, {})[name] = transition
            cursor = transition.scan_id
    return found


def _window_entry(
    adapter: Any, history: Any, names: list[str], lower: int, upper: int
) -> dict[str, Any]:
    """Scans *lower*+1..*upper*, not read: how the tags differ across them, and no more."""
    start = history.at(lower)
    end = history.at(upper)
    changes: dict[str, list[str]] = {}
    for name in names:
        old_value = start.tags.get(name)
        new_value = end.tags.get(name)
        if old_value != new_value:
            changes[name] = [adapter._format_value(old_value), adapter._format_value(new_value)]
    return {
        "window": True,
        "scanId": upper,
        "prevScanId": lower,
        "timestamp": end.timestamp,
        "changes": changes,
    }


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
