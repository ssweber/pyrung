"""Fast unit tests for the PILOT decision-divergence developer tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from devtools.pilot_divergence import (
    compare_event_stream,
    format_comparison,
    parse_target,
)
from tests.tumbler.skeleton import extract_skeleton

pytestmark = pytest.mark.tumbler


@dataclass(frozen=True)
class _Event:
    kind: str
    data: dict[str, Any]
    scan: int


def test_parse_target_supports_bool_and_explicit_values() -> None:
    assert parse_target("Motor") == parse_target("Motor")
    assert parse_target("Motor").tag_name == "Motor"
    assert parse_target("Step=4").value == 4
    assert parse_target("Ready=false").value is False


def test_compare_event_stream_stops_at_first_difference() -> None:
    expected_events = [
        _Event("iteration", {"distance": 3}, 1),
        _Event("iteration", {"distance": 2}, 2),
        _Event("finished", {"reached": True}, 3),
    ]
    actual_events = [
        expected_events[0],
        _Event("iteration", {"distance": 4}, 2),
        expected_events[2],
    ]
    consumed = 0

    def stream():
        nonlocal consumed
        for event in actual_events:
            consumed += 1
            yield event

    comparison = compare_event_stream(stream(), extract_skeleton(expected_events))

    assert consumed == 2
    assert comparison.divergence is not None
    assert comparison.divergence[0] == 1
    assert comparison.last_scan == 2


def test_finished_prefix_reports_that_the_actual_stream_ended_early() -> None:
    first = _Event("iteration", {"distance": 3}, 1)
    finished = _Event("finished", {"reached": True}, 2)
    golden = extract_skeleton([first, _Event("iteration", {"distance": 2}, 2), finished])

    comparison = compare_event_stream([first, finished], golden)

    assert comparison.divergence is not None
    assert comparison.divergence[0] == 1


def test_format_comparison_includes_context_and_raw_scan() -> None:
    golden = extract_skeleton(
        [
            _Event("iteration", {"distance": 3}, 1),
            _Event("finished", {"reached": True}, 2),
        ]
    )
    comparison = compare_event_stream(
        [
            _Event("iteration", {"distance": 3}, 1),
            _Event("finished", {"reached": False}, 2),
        ],
        golden,
    )

    message = format_comparison(comparison, context=1)

    assert "diverged at event 1" in message
    assert "raw scan 2" in message
    assert "previous matching events:" in message
    assert '"reached": true' in message
    assert '"reached": false' in message
