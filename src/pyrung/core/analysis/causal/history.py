from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .models import Transition

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.history import History
    from pyrung.core.rung_firings import RungFiringTimelines
    from pyrung.core.scan_log import ScanLog


def _scan_ids_descending(history: History) -> list[int]:
    """Return addressable scan ids newest-first."""
    return list(reversed(list(history.scan_ids())))


def _scan_index(ids: Sequence[int], scan_id: int) -> int | None:
    """O(1) index lookup when *ids* is a contiguous range, linear fallback."""
    if isinstance(ids, range):
        idx = scan_id - ids.start
        return idx if 0 <= idx < len(ids) else None
    try:
        return ids.index(scan_id)
    except ValueError:
        return None


def _scan_log_transition(
    scan_log: ScanLog | None,
    tag_name: str,
    scan_id: int | None,
    initial_tags: Mapping[str, Any] | None,
) -> Transition | None:
    """Resolve a writerless input transition from ``ScanLog``'s event index."""
    if scan_log is None or initial_tags is None:
        return None
    initial_value = initial_tags.get(tag_name)
    if scan_id is None:
        change = scan_log.latest_effective_input_transition(
            tag_name,
            initial_value=initial_value,
        )
    else:
        change = scan_log.effective_input_transition_at(
            tag_name,
            scan_id,
            initial_value=initial_value,
        )
    if change is None:
        return None
    change_scan, from_value, to_value = change
    return Transition(tag_name, change_scan, from_value, to_value)


def _find_transition(
    history: History,
    tag_name: str,
    scan_id: int | None = None,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
    scan_log: ScanLog | None = None,
    initial_tags: Mapping[str, Any] | None = None,
) -> Transition | None:
    """Find a transition of *tag_name* in addressable history.

    If *scan_id* is given, check whether the tag changed at that exact scan.
    Otherwise find the most recent transition.

    When *timelines* and *pdg* are provided, uses the firing timeline
    instead of per-scan state reads — O(W × log S) where W is the
    number of writer rungs for the tag.
    """
    ids = history.scan_ids()

    if scan_id is not None:
        return _find_transition_at_scan(
            history,
            tag_name,
            scan_id,
            timelines=timelines,
            pdg=pdg,
            scan_log=scan_log,
            initial_tags=initial_tags,
        )

    # Walk backward to find most recent transition.
    # Timeline path: check each scan for a writer that changed the value.
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    n = len(ids)

    # ScanLog path: writerless inputs are recorded as nondeterministic
    # events and exposed here as a derived effective-value index.
    if writers is not None and not writers:
        t = _scan_log_transition(scan_log, tag_name, None, initial_tags)
        if t is not None:
            return t

    if timelines is not None and writers is not None and writers:
        # Walk backward through scans using the timeline for value checks.
        for i in range(n - 1, 0, -1):
            cur_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i])
            prev_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i - 1])
            if cur_val is not _NO_WRITE and prev_val is not _NO_WRITE and cur_val != prev_val:
                return Transition(tag_name, ids[i], prev_val, cur_val)
            if cur_val is not _NO_WRITE and prev_val is _NO_WRITE:
                # No rung wrote the tag at the previous scan — fall
                # back to state to get the prior value (could be a
                # default or an external input).
                prev_state_val = history.at(ids[i - 1]).tags.get(tag_name)
                if cur_val != prev_state_val:
                    return Transition(tag_name, ids[i], prev_state_val, cur_val)
        # Timeline didn't find a write — may be PDG-filtered.
        # Fall through to state reads.

    # State-based fallback: external inputs (no writers), PDG-filtered
    # writes, or no timeline available.
    for i in range(n - 1, 0, -1):
        cur_state = history.at(ids[i])
        prev_state = history.at(ids[i - 1])
        cur_val = cur_state.tags.get(tag_name)
        prev_val = prev_state.tags.get(tag_name)
        if cur_val != prev_val:
            return Transition(tag_name, ids[i], prev_val, cur_val)
    return None


def _find_transition_at_scan(
    history: History,
    tag_name: str,
    scan_id: int,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
    scan_log: ScanLog | None = None,
    initial_tags: Mapping[str, Any] | None = None,
) -> Transition | None:
    """Check if *tag_name* transitioned at exactly *scan_id*.

    Timeline path avoids state reads by checking writer firings.
    """
    ids = history.scan_ids()
    idx = _scan_index(ids, scan_id)
    if idx is None:
        return None

    writers = _writer_indices(pdg, tag_name) if pdg is not None else None

    # ScanLog path for writerless input tags.
    if writers is not None and not writers:
        t = _scan_log_transition(scan_log, tag_name, scan_id, initial_tags)
        if t is not None:
            return t

    if timelines is not None and writers is not None and writers:
        to_value = _tag_value_at_scan(timelines, writers, tag_name, scan_id)
        if to_value is not _NO_WRITE:
            if idx > 0:
                prev_result = timelines.last_tag_write_before(writers, tag_name, scan_id)
                if prev_result is not None:
                    from_value = prev_result[1]
                else:
                    from_value = history.at(ids[idx - 1]).tags.get(tag_name)
            else:
                from_value = None
            if from_value != to_value:
                return Transition(tag_name, scan_id, from_value, to_value)
            return None
        # _NO_WRITE — fall through to state reads (PDG-filtered or
        # external input).

    # State-based fallback
    state = history.at(scan_id)
    to_value = state.tags.get(tag_name)
    if idx > 0:
        prev_state = history.at(ids[idx - 1])
        from_value = prev_state.tags.get(tag_name)
    else:
        from_value = None
    if from_value != to_value:
        return Transition(tag_name, scan_id, from_value, to_value)
    return None


def _find_last_transition_scan(
    history: History,
    tag_name: str,
    before_scan_id: int,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
    scan_log: ScanLog | None = None,
    initial_tags: Mapping[str, Any] | None = None,
) -> int | None:
    """Find the most recent scan where *tag_name* changed, before *before_scan_id*.

    Returns the scan_id, or None if no transition found in addressable history.

    Timeline path uses reverse iteration over writer rung timelines —
    O(W × log S) where W is the writer count.
    """
    ids = history.scan_ids()
    n = len(ids)
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None

    # ScanLog path for writerless input tags.
    if writers is not None and not writers and scan_log is not None and initial_tags is not None:
        scan = scan_log.last_effective_input_change_before(
            tag_name,
            before_scan_id,
            initial_value=initial_tags.get(tag_name),
        )
        if scan is not None:
            return scan

    if timelines is not None and writers is not None and writers:
        for candidate_scan in timelines.tag_transition_candidate_scans_before(
            writers,
            tag_name,
            before_scan_id,
        ):
            if candidate_scan >= before_scan_id:
                continue
            idx = _scan_index(ids, candidate_scan)
            if idx is None or idx <= 0:
                continue
            cur_val = _tag_value_at_scan(timelines, writers, tag_name, candidate_scan)
            if cur_val is _NO_WRITE:
                continue
            prev_val = _tag_value_at_scan(timelines, writers, tag_name, ids[idx - 1])
            if prev_val is _NO_WRITE:
                prev_val = history.at(ids[idx - 1]).tags.get(tag_name)
            if cur_val != prev_val:
                return candidate_scan
        return None

    # State-based fallback (also used for external-input tags with no writers)
    for i in range(n - 1, 0, -1):
        if ids[i] >= before_scan_id:
            continue
        cur_val = history.at(ids[i]).tags.get(tag_name)
        prev_val = history.at(ids[i - 1]).tags.get(tag_name)
        if cur_val != prev_val:
            return ids[i]
    return None


def _find_recent_transition(
    history: History,
    tag_name: str,
    scan_id: int,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
    scan_log: ScanLog | None = None,
    initial_tags: Mapping[str, Any] | None = None,
) -> Transition | None:
    """Find a transition of *tag_name* at *scan_id* or the immediately preceding scan.

    PLC effects propagate one scan at a time: a contact that transitioned at
    scan N may not affect a downstream rung until scan N+1 (if the reading
    rung comes before the writing rung in program order).  Checking both the
    current and previous scan captures this one-scan propagation delay.
    """
    # Check exact scan first
    t = _find_transition_at_scan(
        history,
        tag_name,
        scan_id,
        timelines=timelines,
        pdg=pdg,
        scan_log=scan_log,
        initial_tags=initial_tags,
    )
    if t is not None:
        return t

    # Check immediately preceding scan
    ids = history.scan_ids()
    idx = _scan_index(ids, scan_id)
    if idx is not None and idx > 0:
        prev_scan = ids[idx - 1]
        t = _find_transition_at_scan(
            history,
            tag_name,
            prev_scan,
            timelines=timelines,
            pdg=pdg,
            scan_log=scan_log,
            initial_tags=initial_tags,
        )
        if t is not None:
            return t

    return None


# Sentinel for "no rung wrote this tag at this scan".
_NO_WRITE: Any = object()


def _writer_indices(pdg: ProgramGraph, tag_name: str) -> frozenset[int]:
    """Return the set of main-rung indices whose capture scope writes *tag_name*.

    Uses ``timeline_writers_of`` so subroutine writers resolve to their
    call-site main-rung indices — matching the keys in ``RungFiringTimelines``.
    """
    return pdg.timeline_writers_of(tag_name)


def _tag_value_at_scan(
    timelines: RungFiringTimelines,
    writers: frozenset[int],
    tag_name: str,
    scan_id: int,
) -> Any:
    """Return the value written to *tag_name* at *scan_id*, or ``_NO_WRITE``.

    Checks each writer rung's timeline for a firing at ``scan_id``
    that includes ``tag_name`` in its writes.
    """
    for rung_index in writers:
        writes = timelines.rung_writes_at(rung_index, scan_id)
        if writes is not None and tag_name in writes:
            return writes[tag_name]
    return _NO_WRITE


def _end_of_scan_value(
    timelines: RungFiringTimelines,
    writers: frozenset[int],
    tag_name: str,
    scan_id: int,
) -> Any:
    """Return the end-of-scan value of *tag_name* at *scan_id*, or ``_NO_WRITE``.

    When multiple writer rungs fire at the same scan, the highest rung
    index (last in program execution order) determines the end-of-scan
    value.  Used by :func:`resolve_tag_at_scan` for condition evaluation.
    """
    best_value: Any = _NO_WRITE
    best_rung: int = -1
    for rung_index in writers:
        writes = timelines.rung_writes_at(rung_index, scan_id)
        if writes is not None and tag_name in writes:
            if rung_index > best_rung:
                best_rung = rung_index
                best_value = writes[tag_name]
    return best_value


def resolve_tag_at_scan(
    tag_name: str,
    scan_id: int,
    *,
    timelines: RungFiringTimelines,
    pdg: ProgramGraph,
    scan_log: ScanLog | None,
    initial_tags: Mapping[str, Any],
) -> Any:
    """Resolve a tag's value at *scan_id* without state replay.

    Uses rung firing timelines for writer tags and ``ScanLog``'s derived
    effective-input index for writerless tags. Falls back to the initial
    state when neither source has a record.
    """
    from pyrung.core.rung_firings import _FIRED_ONLY_SENTINEL

    writers = pdg.timeline_writers_of(tag_name)

    if writers:
        val = _tag_value_at_scan(timelines, writers, tag_name, scan_id)
        if val is not _NO_WRITE and val is not _FIRED_ONLY_SENTINEL:
            return val
        result = timelines.last_tag_write_before(writers, tag_name, scan_id + 1)
        if result is not None:
            best_scan, v = result
            if v is not _FIRED_ONLY_SENTINEL:
                exact = _tag_value_at_scan(timelines, writers, tag_name, best_scan)
                if exact is not _NO_WRITE and exact is not _FIRED_ONLY_SENTINEL:
                    return exact
                return v

    if not writers and scan_log is not None:
        return scan_log.effective_input_value_at(
            tag_name,
            scan_id,
            initial_value=initial_tags.get(tag_name),
        )

    return initial_tags.get(tag_name)


# ---------------------------------------------------------------------------
# Recorded backward walk
# ---------------------------------------------------------------------------
