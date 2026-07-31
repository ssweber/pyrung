from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
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


def _boundary_transition_at_scan(
    history: History,
    tag_name: str,
    scan_id: int,
    ids: Sequence[int],
) -> Transition | None:
    """Resolve one committed scan-boundary transition.

    Firing timelines identify scans worth checking, but their per-rung write
    payloads are intentionally lossy: multiple occurrences may write the same
    tag during one scan.  Adjacent committed states are therefore the sole
    authority for the transition endpoints.
    """
    idx = _scan_index(ids, scan_id)
    if idx is None:
        return None
    to_value = history.at(scan_id).tags.get(tag_name)
    from_value = history.at(ids[idx - 1]).tags.get(tag_name) if idx > 0 else None
    if from_value == to_value:
        return None
    return Transition(tag_name, scan_id, from_value, to_value)


def _scan_log_transition_before(
    history: History,
    scan_log: ScanLog | None,
    tag_name: str,
    before_scan_id: int,
    initial_tags: Mapping[str, Any] | None,
    ids: Sequence[int],
) -> Transition | None:
    """Find the latest committed transition proposed by ``ScanLog``.

    The event index identifies candidate scans only. A program write later in
    the same scan may overwrite an input event, so adjacent committed states
    validate the boundary before it becomes causal history.
    """
    if scan_log is None or initial_tags is None:
        return None
    initial_value = initial_tags.get(tag_name)
    cursor = before_scan_id
    while True:
        scan_id = scan_log.last_effective_input_change_before(
            tag_name,
            cursor,
            initial_value=initial_value,
        )
        if scan_id is None:
            return None
        transition = _boundary_transition_at_scan(history, tag_name, scan_id, ids)
        if transition is not None:
            return transition
        cursor = scan_id


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

    When *timelines* and *pdg* are provided, the compressed firing timeline
    proposes candidate scans.  Adjacent committed states validate each
    candidate and supply its endpoints.
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
    writers = _writer_indices(pdg, tag_name) if pdg is not None else None
    n = len(ids)

    # Statically writerless: an external input recorded in the ScanLog
    # and/or a runtime-resolved (indirect) writer the PDG can't see.  The
    # observed-writer index recovers the latter so they take the timeline
    # branch instead of the state walk.
    if writers is not None and not writers:
        scan_log_t = _scan_log_transition_before(
            history,
            scan_log,
            tag_name,
            ids[-1] + 1 if ids else 0,
            initial_tags,
            ids,
        )
        observed = timelines.observed_writers_of(tag_name) if timelines is not None else frozenset()
        if observed and timelines is not None:
            timeline_t = _find_transition_via_timeline(history, timelines, observed, tag_name, ids)
            # Contract: most recent transition wins when a tag carries both
            # a recorded input change and an observed rung write.
            later = _later_transition(scan_log_t, timeline_t)
            if later is not None:
                return later
            # Neither source found one — fall through to the guarded state
            # walk (uncovered mutation sources).
        elif scan_log_t is not None:
            return scan_log_t

    if timelines is not None and writers is not None and writers:
        transition = _find_transition_via_timeline(history, timelines, writers, tag_name, ids)
        if transition is not None:
            return transition
        # A PDG-known writer can be absent from the consumed-tag firing
        # payload. Preserve the guarded state walk for that filtered case.

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
    """Check the committed boundary at exactly *scan_id* for a transition.

    Timeline and scan-log parameters are accepted for the shared lookup API,
    but exact endpoint validation always comes from adjacent history states.
    """
    ids = history.scan_ids()
    return _boundary_transition_at_scan(history, tag_name, scan_id, ids)


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

    # Static-known writers: the compressed candidate branch is
    # authoritative — return its answer (a scan or None) without ever
    # falling through to the state walk.
    if writers:
        if timelines is not None:
            return _last_transition_scan_via_timeline(
                history, timelines, writers, tag_name, before_scan_id, ids
            )
        # timelines unavailable — drop to the state walk below.

    # Statically writerless: the tag may be an external input (recorded
    # in the ScanLog as effective-value changes) and/or written only
    # through a runtime-resolved / indirect copy that static analysis
    # can't see (``timeline_writers_of`` returns ``frozenset()``).  The
    # observed-writer index recovers those runtime writers so they take
    # the same fast branch instead of a full-history state walk.
    elif writers is not None:
        scan_log_transition = _scan_log_transition_before(
            history,
            scan_log,
            tag_name,
            before_scan_id,
            initial_tags,
            ids,
        )
        scan_log_scan = (
            scan_log_transition.scan_id if scan_log_transition is not None else None
        )
        observed = timelines.observed_writers_of(tag_name) if timelines is not None else frozenset()
        if observed and timelines is not None:
            timeline_scan = _last_transition_scan_via_timeline(
                history, timelines, observed, tag_name, before_scan_id, ids
            )
            # Route observed writers exactly like static-known writers:
            # the timeline branch is authoritative, no state walk.  A tag
            # can carry BOTH a recorded input change and an observed rung
            # write; the contract is "most recent transition before X", so
            # the later scan wins.
            candidates = [s for s in (scan_log_scan, timeline_scan) if s is not None]
            return max(candidates) if candidates else None
        if scan_log_scan is not None:
            return scan_log_scan
        # No static writer, no observed writer, and no recorded input
        # change.  Fall through to the state walk as a guarded correctness
        # fallback: a mutation source outside both indexes — an I/O
        # submit/drain tag write, or a rung write dropped by the
        # consumed-tags firing filter — could still have changed the tag.
        # This is the rare path; the expensive indirect-copy tags that
        # dominated the profile all carry observed writers and never
        # reach it.

    # State-based fallback (no PDG, no timelines, or a genuinely
    # uncovered writerless tag).
    for i in range(n - 1, 0, -1):
        if ids[i] >= before_scan_id:
            continue
        cur_val = history.at(ids[i]).tags.get(tag_name)
        prev_val = history.at(ids[i - 1]).tags.get(tag_name)
        if cur_val != prev_val:
            return ids[i]
    return None


def _last_transition_scan_via_timeline(
    history: History,
    timelines: RungFiringTimelines,
    writers: frozenset[int],
    tag_name: str,
    before_scan_id: int,
    ids: Sequence[int],
) -> int | None:
    """Most recent scan ``< before_scan_id`` where a *writer* changed *tag_name*.

    Walks the compressed transition candidates (one entry per stable /
    arithmetic / alternating range) rather than every committed scan.  The
    timeline is only a candidate index; adjacent committed states determine
    whether the boundary changed.
    """
    for candidate_scan in _timeline_candidate_scans_before(
        timelines, writers, tag_name, before_scan_id
    ):
        if candidate_scan >= before_scan_id:
            continue
        idx = _scan_index(ids, candidate_scan)
        if idx is None or idx <= 0:
            continue
        if _boundary_transition_at_scan(history, tag_name, candidate_scan, ids) is not None:
            return candidate_scan
    return None


def _timeline_candidate_scans_before(
    timelines: RungFiringTimelines,
    writers: frozenset[int],
    tag_name: str,
    before_scan_id: int,
) -> Iterator[int]:
    """Yield compressed candidates newest-first, advancing after rejections.

    Arithmetic and alternating ranges name only their latest candidate before
    a cursor.  Asking again below that newest candidate exposes the
    preceding candidate without expanding the range into per-scan storage.
    """
    cursor = before_scan_id
    while True:
        candidates = timelines.tag_transition_candidate_scans_before(
            writers,
            tag_name,
            cursor,
        )
        eligible = tuple(scan_id for scan_id in candidates if scan_id < cursor)
        if not eligible:
            return
        candidate = max(eligible)
        yield candidate
        cursor = candidate


def _find_transition_via_timeline(
    history: History,
    timelines: RungFiringTimelines,
    writers: frozenset[int],
    tag_name: str,
    ids: Sequence[int],
) -> Transition | None:
    """Most recent transition of *tag_name* attributable to *writers*.

    The Transition-returning companion to
    :func:`_last_transition_scan_via_timeline`.  It replaces
    :func:`_find_transition`'s per-scan backward loop with the compressed
    candidate enumeration, jumping straight to the range boundaries where
    the value can change instead of probing every committed scan.  Adjacent
    committed states validate each candidate and supply the endpoints, so
    per-rung projections never stand in for the end-of-scan value.
    """
    if not ids:
        return None
    before_scan_id = ids[-1] + 1
    for candidate_scan in _timeline_candidate_scans_before(
        timelines, writers, tag_name, before_scan_id
    ):
        idx = _scan_index(ids, candidate_scan)
        if idx is None or idx <= 0:
            continue
        transition = _boundary_transition_at_scan(history, tag_name, candidate_scan, ids)
        if transition is not None:
            return transition
    return None


def _later_transition(a: Transition | None, b: Transition | None) -> Transition | None:
    """Return whichever transition happened later, or the non-None one."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a.scan_id >= b.scan_id else b


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


def _writer_indices(pdg: ProgramGraph, tag_name: str) -> frozenset[int]:
    """Return the set of main-rung indices whose capture scope writes *tag_name*.

    Uses ``timeline_writers_of`` so subroutine writers resolve to their
    call-site main-rung indices — matching the keys in ``RungFiringTimelines``.
    """
    return pdg.timeline_writers_of(tag_name)


# ---------------------------------------------------------------------------
# Recorded backward walk
# ---------------------------------------------------------------------------
