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
from pyrung.core.rung_firings import RungFiringTimelines


@dataclass(frozen=True)
class _State:
    tags: dict[str, Any]


class _HistoryNoStateReads:
    def scan_ids(self) -> range:
        return range(0, 101)

    def at(self, scan_id: int) -> _State:
        raise AssertionError(f"history.at({scan_id}) should not be needed")


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
    """Minimal ScanLog exposing only the effective-input change lookup."""

    def __init__(self, change_scan: int | None) -> None:
        self._change_scan = change_scan

    def last_effective_input_change_before(
        self, tag_name: str, before_scan_id: int, *, initial_value: Any
    ) -> int | None:
        if self._change_scan is not None and self._change_scan < before_scan_id:
            return self._change_scan
        return None


def test_find_last_transition_scan_uses_timeline_range_boundaries() -> None:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Ready": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Ready": True}))

    assert (
        _find_last_transition_scan(
            _HistoryNoStateReads(),  # type: ignore[arg-type]
            "Ready",
            101,
            timelines=timelines,
            pdg=_PDG(),  # type: ignore[arg-type]
        )
        == 50
    )


def test_find_last_transition_scan_uses_observed_writers_for_indirect_tag() -> None:
    """A statically-writerless tag written only at runtime avoids the state walk.

    Rung 0 writes ``Indirect`` (an indirect/pointer-copy target the PDG
    reports no static writer for): False for scans 1-49, True for 50-100.
    Without the observed-writer index this tag pays a full reverse state
    walk; the index routes it onto the compressed timeline branch, which
    must locate the 49->50 transition without a single ``history.at`` read
    (``_HistoryNoStateReads.at`` raises if touched).
    """
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Indirect": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Indirect": True}))

    assert timelines.observed_writers_of("Indirect") == frozenset({0})

    assert (
        _find_last_transition_scan(
            _HistoryNoStateReads(),  # type: ignore[arg-type]
            "Indirect",
            101,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
        )
        == 50
    )


def test_find_last_transition_scan_later_source_wins_for_indirect_tag() -> None:
    """When a tag has both a recorded input change and an observed rung write,
    the most recent transition before the cutoff wins — no state walk either way.
    """
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Indirect": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Indirect": True}))

    # Observed rung-write transition is at 50.  A later recorded input
    # change at 70 wins.
    assert (
        _find_last_transition_scan(
            _HistoryNoStateReads(),  # type: ignore[arg-type]
            "Indirect",
            101,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
            scan_log=_FakeScanLog(70),  # type: ignore[arg-type]
            initial_tags={"Indirect": False},
        )
        == 70
    )

    # An earlier recorded input change (30) loses to the rung write (50).
    assert (
        _find_last_transition_scan(
            _HistoryNoStateReads(),  # type: ignore[arg-type]
            "Indirect",
            101,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
            scan_log=_FakeScanLog(30),  # type: ignore[arg-type]
            initial_tags={"Indirect": False},
        )
        == 50
    )


def _indirect_timelines() -> RungFiringTimelines[int]:
    timelines: RungFiringTimelines[int] = RungFiringTimelines()
    for scan_id in range(1, 50):
        timelines.append(0, scan_id, pmap({"Indirect": False}))
    for scan_id in range(50, 101):
        timelines.append(0, scan_id, pmap({"Indirect": True}))
    return timelines


def test_find_transition_uses_observed_writers_for_indirect_tag() -> None:
    """_find_transition (most-recent) routes a writerless indirect tag onto the
    observed-writer timeline branch — no state reads for adjacent writes."""
    timelines = _indirect_timelines()
    assert _find_transition(
        _HistoryNoStateReads(),  # type: ignore[arg-type]
        "Indirect",
        timelines=timelines,
        pdg=_PDGWriterless(),  # type: ignore[arg-type]
    ) == Transition("Indirect", 50, False, True)


def test_find_transition_at_scan_uses_observed_writers_for_indirect_tag() -> None:
    """_find_transition_at_scan routes a writerless indirect tag onto the
    observed-writer timeline branch, resolving from/to without state reads."""
    timelines = _indirect_timelines()
    assert _find_transition_at_scan(
        _HistoryNoStateReads(),  # type: ignore[arg-type]
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
            _HistoryNoStateReads(),  # type: ignore[arg-type]
            "Indirect",
            75,
            timelines=timelines,
            pdg=_PDGWriterless(),  # type: ignore[arg-type]
        )
        is None
    )
