from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrsistent import pmap

from pyrung.core.analysis.causal.history import _find_last_transition_scan
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
