"""Exact-writer contracts for folded repaired program continuation."""

from __future__ import annotations

from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_progress_repeated_subroutine_landing as repeated
from tests.fixtures import pilot_progress_same_landing_interference as fixture


def test_off_path_same_value_writer_cannot_borrow_target_program_step() -> None:
    events = tuple(
        islice(
            pilot_events(
                PLC(fixture.logic, dt=0.010),
                fixture.SequenceState == fixture.COMPLETE,
                max_scans=20,
            ),
            80,
        )
    )

    assert any(
        event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None) == fixture.PresetMs.name
        for event in events
    )
    assert not any(
        event.kind == "requirement_locally_repaired"
        and event.data["assignments"] == ((fixture.PresetMs.name, 11),)
        for event in events
    )
    assert not any(event.kind == "finished" and event.data["reached"] is True for event in events)


def test_repeated_subroutine_writer_cannot_borrow_another_call_site() -> None:
    events = tuple(
        islice(
            pilot_events(
                PLC(repeated.logic, dt=0.010),
                repeated.SequenceState == repeated.COMPLETE,
                max_scans=20,
            ),
            80,
        )
    )

    assert any(
        event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None) == repeated.PresetMs.name
        for event in events
    )
    assert not any(
        event.kind == "requirement_locally_repaired"
        and event.data["assignments"] == ((repeated.PresetMs.name, 11),)
        for event in events
    )
    assert not any(event.kind == "finished" and event.data["reached"] is True for event in events)
