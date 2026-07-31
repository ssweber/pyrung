from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrsistent import pmap

from pyrung.core.analysis.causal.history import (
    _find_last_transition_scan,
    _find_transition,
    _find_transition_at_scan,
)
from pyrung.core.analysis.causal.models import Transition
from pyrung.core.rung_firings import (
    _FIRED_ONLY_THRESHOLD,
    ArithmeticRun,
    FiredOnly,
    RungFiringTimelines,
)


@dataclass(frozen=True)
class _State:
    tags: dict[str, Any]


class _HistoryCandidateReads:
    """Committed history that permits reads only at named candidate boundaries."""

    def __init__(
        self,
        *,
        transition_scan: int = 50,
        allowed_reads: frozenset[int] = frozenset({49, 50}),
        indirect_reset_scan: int | None = None,
    ) -> None:
        self.transition_scan = transition_scan
        self.allowed_reads = allowed_reads
        self.indirect_reset_scan = indirect_reset_scan
        self.read_scan_ids: list[int] = []

    def scan_ids(self) -> range:
        return range(0, 101)

    def at(self, scan_id: int) -> _State:
        assert scan_id in self.allowed_reads, (
            f"history.at({scan_id}) is outside the compressed candidate boundary"
        )
        self.read_scan_ids.append(scan_id)
        indirect = scan_id >= 50 and (
            self.indirect_reset_scan is None or scan_id < self.indirect_reset_scan
        )
        return _State({"Ready": scan_id >= self.transition_scan, "Indirect": indirect})


class _CandidateOrderingHistory:
    """Committed values for rejecting 99 before accepting 98."""

    def __init__(self) -> None:
        self.read_scan_ids: list[int] = []

    def scan_ids(self) -> range:
        return range(0, 101)

    def at(self, scan_id: int) -> _State:
        # Reading scan 50 would prove that lookup skipped the next candidate
        # inside the newer arithmetic range and prematurely visited the older
        # PatternRef range.
        assert scan_id in {97, 98, 99}, f"unexpected older candidate read at scan {scan_id}"
        self.read_scan_ids.append(scan_id)
        return _State({"Ready": scan_id >= 98})


class _OpaqueInteriorHistory:
    """Committed transition hidden inside a value-opaque firing range."""

    def __init__(self, transition_scan: int) -> None:
        self.transition_scan = transition_scan
        self.read_scan_ids: list[int] = []

    def scan_ids(self) -> range:
        return range(0, _FIRED_ONLY_THRESHOLD + 6)

    def at(self, scan_id: int) -> _State:
        self.read_scan_ids.append(scan_id)
        return _State({"Ready": scan_id >= self.transition_scan})


class _PDG:
    def timeline_writers_of(self, tag_name: str) -> frozenset[int]:
        assert tag_name == "Ready"
        return frozenset({0})


class _PDGWriterless:
    """Static analysis sees no writers — mimics a pointer/indirect copy target.

    ``timeline_writers_of`` returns the empty set for the queried tag, so
    ``_find_last_transition_scan`` must recover its runtime writers from the
    timelines' observed-writer index rather than the static PDG.
    """

    def timeline_writers_of(self, tag_name: str) -> frozenset[int]:
        return frozenset()


class _FakeScanLog:
    """Minimal ScanLog effective-input transition index."""

    def __init__(self, change: tuple[int, Any, Any] | None) -> None:
        self._change = change

    def last_effective_input_change_before(
        self, tag_name: str, before_scan_id: int, *, initial_value: Any
    ) -> int | None:
        if self._change is not None and self._change[0] < before_scan_id:
            return self._change[0]
        return None

    def latest_effective_input_transition(
        self, tag_name: str, *, initial_value: Any
    ) -> tuple[int, Any, Any] | None:
        return self._change

    def effective_input_transition_at(
        self, tag_name: str, scan_id: int, *, initial_value: Any
    ) -> tuple[int, Any, Any] | None:
        if self._change is not None and self._change[0] == scan_id:
            return self._change
        return None


class _FakeScanLogHistory:
    """ScanLog index with a newest event that commit immediately overwrote."""

    def __init__(self, changes: tuple[tuple[int, Any, Any], ...]) -> None:
        self._changes = changes

    def last_effective_input_change_before(
        self, tag_name: str, before_scan_id: int, *, initial_value: Any
    ) -> int | None:
        del tag_name, initial_value
        candidates = [scan for scan, _before, _after in self._changes if scan < before_scan_id]
        return max(candidates, default=None)

    def latest_effective_input_transition(
        self, tag_name: str, *, initial_value: Any
    ) -> tuple[int, Any, Any] | None:
        del tag_name, initial_value
        return self._changes[-1] if self._changes else None

    def effective_input_transition_at(
        self, tag_name: str, scan_id: int, *, initial_value: Any
    ) -> tuple[int, Any, Any] | None:
        del tag_name, initial_value
        return next((change for change in self._changes if change[0] == scan_id), None)


def test_find_last_transition_scan_uses_timeline_range_boundaries() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Ready": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Ready": True}))

    history = _HistoryCandidateReads()
    assert (
        _find_last_transition_scan(
            history,  # type: ignore[arg-type]
            "Ready",
            101,
            timelines=timelines,
            pdg=_PDG(),  # type: ignore[arg-type]
        )
        == 50
    )
    assert history.read_scan_ids == [50, 49]


def test_rejected_arithmetic_tail_checks_next_scan_before_older_range() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    timelines.append(0, 50, pmap({"Ready": 1000}))
    for scan_id in range(51, 100):
        timelines.append(0, scan_id, pmap({"Ready": scan_id - 50}))

    ranges = timelines._timelines[0]
    assert ranges[0].start_scan_id == ranges[0].end_scan_id == 50
    assert isinstance(ranges[1].payload, ArithmeticRun)
    # Candidate enumeration stays compressed: one tail for the arithmetic
    # range and one boundary for the older range, not every scan in between.
    assert timelines.tag_transition_candidate_scans_before(frozenset({0}), "Ready", 100) == (99, 50)

    scan_history = _CandidateOrderingHistory()
    assert (
        _find_last_transition_scan(
            scan_history,  # type: ignore[arg-type]
            "Ready",
            100,
            timelines=timelines,
            pdg=_PDG(),  # type: ignore[arg-type]
        )
        == 98
    )
    assert 50 not in scan_history.read_scan_ids

    transition_history = _CandidateOrderingHistory()
    assert _find_transition(
        transition_history,  # type: ignore[arg-type]
        "Ready",
        timelines=timelines,
        pdg=_PDG(),  # type: ignore[arg-type]
    ) == Transition("Ready", 98, False, True)
    assert 50 not in transition_history.read_scan_ids


def test_fired_only_interior_is_searched_conservatively() -> None:
    """An opaque range cannot turn a real committed transition into a miss."""
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, _FIRED_ONLY_THRESHOLD + 1):
        # Quadratic values defeat stable, alternating, and arithmetic encoding,
        # naturally promoting this real timeline to fired-only mode.
        timelines.append(0, scan_id, pmap({"Ready": scan_id**2}))
    for scan_id in range(_FIRED_ONLY_THRESHOLD + 1, _FIRED_ONLY_THRESHOLD + 6):
        timelines.append(0, scan_id, pmap({"Ready": "discarded"}))

    opaque = timelines._timelines[0][-1]
    assert isinstance(opaque.payload, FiredOnly)
    transition_scan = _FIRED_ONLY_THRESHOLD + 3
    history = _OpaqueInteriorHistory(transition_scan)

    assert (
        _find_last_transition_scan(
            history,  # type: ignore[arg-type]
            "Ready",
            _FIRED_ONLY_THRESHOLD + 6,
            timelines=timelines,
            pdg=_PDG(),  # type: ignore[arg-type]
        )
        == transition_scan
    )
    assert _find_transition(
        history,  # type: ignore[arg-type]
        "Ready",
        timelines=timelines,
        pdg=_PDG(),  # type: ignore[arg-type]
    ) == Transition("Ready", transition_scan, False, True)
    # The opaque range is searched backward on demand; no occurrence values
    # were expanded into the compressed timeline.
    assert opaque.start_scan_id < transition_scan < opaque.end_scan_id


def test_scan_log_candidate_is_validated_against_committed_boundary() -> None:
    """A same-scan overwrite rejects the newest input event, then searches older."""
    history = _HistoryCandidateReads(
        transition_scan=1,
        allowed_reads=frozenset({0, 1, 2}),
    )
    scan_log = _FakeScanLogHistory(
        (
            (1, False, True),
            # The input changed, but program execution restored True before
            # the scan-2 boundary. This is not an observed transition.
            (2, True, False),
        )
    )

    assert (
        _find_last_transition_scan(
            history,  # type: ignore[arg-type]
            "Ready",
            3,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
            scan_log=scan_log,  # type: ignore[arg-type]
            initial_tags={"Ready": False},
        )
        == 1
    )
    assert _find_transition(
        history,  # type: ignore[arg-type]
        "Ready",
        pdg=_PDGWriterless(),  # type: ignore[arg-type]
        scan_log=scan_log,  # type: ignore[arg-type]
        initial_tags={"Ready": False},
    ) == Transition("Ready", 1, False, True)


def test_find_last_transition_scan_uses_observed_writers_for_indirect_tag() -> None:
    """A statically-writerless tag checks only its compressed candidate boundary.

    Rung 0 writes ``Indirect`` (an indirect/pointer-copy target the PDG
    reports no static writer for): False for scans 1-49, True for 50-100.
    Without the observed-writer index this tag pays a full reverse state
    walk; the index routes it onto the compressed timeline branch, which
    must locate the 49->50 transition with two authoritative boundary reads,
    not a reverse per-scan walk.
    """
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Indirect": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Indirect": True}))

    assert timelines.observed_writers_of("Indirect") == frozenset({0})

    history = _HistoryCandidateReads()
    assert (
        _find_last_transition_scan(
            history,  # type: ignore[arg-type]
            "Indirect",
            101,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
        )
        == 50
    )
    assert history.read_scan_ids == [50, 49]


def test_find_last_transition_scan_later_source_wins_for_indirect_tag() -> None:
    """When a tag has both a recorded input change and an observed rung write,
    the most recent transition before the cutoff wins without a per-scan walk.
    """
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Indirect": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Indirect": True}))

    # Observed rung-write transition is at 50.  A later recorded input
    # change at 70 wins.
    later_history = _HistoryCandidateReads(
        allowed_reads=frozenset({49, 50, 69, 70}),
        indirect_reset_scan=70,
    )
    assert (
        _find_last_transition_scan(
            later_history,  # type: ignore[arg-type]
            "Indirect",
            101,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
            scan_log=_FakeScanLog((70, True, False)),  # type: ignore[arg-type]
            initial_tags={"Indirect": False},
        )
        == 70
    )
    assert later_history.read_scan_ids == [70, 69, 50, 49]

    # An earlier recorded input event (30) was overwritten before commit, so
    # it is rejected and the real rung-written boundary (50) wins.
    earlier_history = _HistoryCandidateReads(
        allowed_reads=frozenset({29, 30, 49, 50}),
    )
    assert (
        _find_last_transition_scan(
            earlier_history,  # type: ignore[arg-type]
            "Indirect",
            101,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
            scan_log=_FakeScanLog((30, False, True)),  # type: ignore[arg-type]
            initial_tags={"Indirect": False},
        )
        == 50
    )
    assert earlier_history.read_scan_ids == [30, 29, 50, 49]

    later_transition_history = _HistoryCandidateReads(
        allowed_reads=frozenset({49, 50, 69, 70}),
        indirect_reset_scan=70,
    )
    assert _find_transition(
        later_transition_history,  # type: ignore[arg-type]
        "Indirect",
        timelines=timelines,
        pdg=_PDGWriterless(),  # type: ignore[arg-type]
        scan_log=_FakeScanLog((70, True, False)),  # type: ignore[arg-type]
        initial_tags={"Indirect": False},
    ) == Transition("Indirect", 70, True, False)
    assert later_transition_history.read_scan_ids == [70, 69, 50, 49]

    earlier_transition_history = _HistoryCandidateReads(
        allowed_reads=frozenset({29, 30, 49, 50}),
    )
    assert _find_transition(
        earlier_transition_history,  # type: ignore[arg-type]
        "Indirect",
        timelines=timelines,
        pdg=_PDGWriterless(),  # type: ignore[arg-type]
        scan_log=_FakeScanLog((30, False, True)),  # type: ignore[arg-type]
        initial_tags={"Indirect": False},
    ) == Transition("Indirect", 50, False, True)
    assert earlier_transition_history.read_scan_ids == [30, 29, 50, 49]


def _indirect_timelines() -> RungFiringTimelines[int]:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Indirect": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Indirect": True}))
    return timelines


def test_find_transition_uses_observed_writers_for_indirect_tag() -> None:
    """_find_transition (most-recent) routes a writerless indirect tag onto the
    observed-writer timeline branch and validates only candidate boundaries."""
    timelines = _indirect_timelines()
    history = _HistoryCandidateReads()
    assert _find_transition(
        history,  # type: ignore[arg-type]
        "Indirect",
        timelines=timelines,
        pdg=_PDGWriterless(),  # type: ignore[arg-type]
    ) == Transition("Indirect", 50, False, True)
    assert history.read_scan_ids == [50, 49]


def test_find_transition_at_scan_uses_observed_writers_for_indirect_tag() -> None:
    """Exact scan lookup reads the requested committed boundary only."""
    timelines = _indirect_timelines()
    history = _HistoryCandidateReads(allowed_reads=frozenset({49, 50, 74, 75}))
    assert _find_transition_at_scan(
        history,  # type: ignore[arg-type]
        "Indirect",
        50,
        timelines=timelines,
        pdg=_PDGWriterless(),  # type: ignore[arg-type]
    ) == Transition("Indirect", 50, False, True)
    # Scan 75 is written (True) but unchanged from scan 74 (also True), so
    # the observed branch reports no transition — resolving the prior value
    # from the timeline, not from state.
    assert (
        _find_transition_at_scan(
            history,  # type: ignore[arg-type]
            "Indirect",
            75,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
        )
        is None
    )
    assert history.read_scan_ids == [50, 49, 75, 74]
