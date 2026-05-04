from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import Transition

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.history import History
    from pyrung.core.rung_firings import RungFiringTimelines


def _scan_ids_descending(history: History) -> list[int]:
    """Return addressable scan ids newest-first."""
    return list(reversed(list(history.scan_ids())))


def _find_transition(
    history: History,
    tag_name: str,
    scan_id: int | None = None,
    *,
    timelines: RungFiringTimelines | None = None,
    pdg: ProgramGraph | None = None,
) -> Transition | None:
    """Find a transition of *tag_name* in addressable history.

    If *scan_id* is given, check whether the tag changed at that exact scan.
    Otherwise find the most recent transition.

    When *timelines* and *pdg* are provided, uses the firing timeline
    instead of per-scan state reads — O(W × log S) where W is the
    number of writer rungs for the tag.
    """
    ids = list(history.scan_ids())

    if scan_id is not None:
        return _find_transition_at_scan(
            history,
            tag_name,
            scan_id,
            timelines=timelines,
            pdg=pdg,
        )

    # Walk backward to find most recent transition.
    # Timeline path: check each scan for a writer that changed the value.
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    if timelines is not None and writers is not None and writers:
        # Walk backward through scans using the timeline for value checks.
        for i in range(len(ids) - 1, 0, -1):
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
    for i in range(len(ids) - 1, 0, -1):
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
) -> Transition | None:
    """Check if *tag_name* transitioned at exactly *scan_id*.

    Timeline path avoids state reads by checking writer firings.
    """
    ids = list(history.scan_ids())
    idx = None
    for i, sid in enumerate(ids):
        if sid == scan_id:
            idx = i
            break
    if idx is None:
        return None

    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
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
) -> int | None:
    """Find the most recent scan where *tag_name* changed, before *before_scan_id*.

    Returns the scan_id, or None if no transition found in addressable history.

    Timeline path uses reverse iteration over writer rung timelines —
    O(W × log S) where W is the writer count.
    """
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    if timelines is not None and writers is not None and writers:
        # Walk backward via the timeline's range lists.
        ids = list(history.scan_ids())
        for i in range(len(ids) - 1, 0, -1):
            if ids[i] >= before_scan_id:
                continue
            cur_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i])
            if cur_val is _NO_WRITE:
                continue
            prev_val = _tag_value_at_scan(timelines, writers, tag_name, ids[i - 1])
            if prev_val is _NO_WRITE:
                prev_val = history.at(ids[i - 1]).tags.get(tag_name)
            if cur_val != prev_val:
                return ids[i]
        return None

    # State-based fallback (also used for external-input tags with no writers)
    ids = list(history.scan_ids())
    for i in range(len(ids) - 1, 0, -1):
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
    )
    if t is not None:
        return t

    # Check immediately preceding scan
    ids = list(history.scan_ids())
    idx = None
    for i, sid in enumerate(ids):
        if sid == scan_id:
            idx = i
            break
    if idx is not None and idx > 0:
        prev_scan = ids[idx - 1]
        t = _find_transition_at_scan(
            history,
            tag_name,
            prev_scan,
            timelines=timelines,
            pdg=pdg,
        )
        if t is not None:
            return t

    return None


# Sentinel for "no rung wrote this tag at this scan".
_NO_WRITE: Any = object()


def _writer_indices(pdg: ProgramGraph, tag_name: str) -> frozenset[int]:
    """Return the set of rung indices that can write *tag_name*."""
    return pdg.writers_of.get(tag_name, frozenset())


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


# ---------------------------------------------------------------------------
# Recorded backward walk
# ---------------------------------------------------------------------------
