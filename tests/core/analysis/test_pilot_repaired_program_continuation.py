"""Exact-writer contracts for folded repaired program continuation."""

from __future__ import annotations

import importlib
from itertools import islice

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_progress_repeated_subroutine_landing as repeated
from tests.fixtures import pilot_progress_same_landing_interference as fixture


def test_off_path_same_value_writer_is_handled_without_borrowing_program_step(
    monkeypatch,
) -> None:
    pilot_module = importlib.import_module("pyrung.core.analysis.pilot.pilot")
    original = pilot_module._repaired_program_continuation
    continuation_results = []

    def capture_continuation(*args, **kwargs):
        result = original(*args, **kwargs)
        continuation_results.append(result)
        return result

    monkeypatch.setattr(
        pilot_module,
        "_repaired_program_continuation",
        capture_continuation,
    )
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
    assert any(
        event.kind == "candidate_try"
        and event.data["applied"] == ((fixture.InterferenceArmed.name, False),)
        for event in events
    )
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    assert all(result is None for result in continuation_results)

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.SequenceState == fixture.COMPLETE,
        max_scans=20,
    )
    assert plan.reachable, plan.reason
    assert plan.replay().state.tags[fixture.SequenceState.name] == fixture.COMPLETE


def test_repeated_subroutine_writer_is_followed_without_borrowing_another_call_site(
    monkeypatch,
) -> None:
    pilot_module = importlib.import_module("pyrung.core.analysis.pilot.pilot")
    original = pilot_module._repaired_program_continuation
    continuation_results = []

    def capture_continuation(*args, **kwargs):
        result = original(*args, **kwargs)
        continuation_results.append(result)
        return result

    monkeypatch.setattr(
        pilot_module,
        "_repaired_program_continuation",
        capture_continuation,
    )
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
    assert any(event.kind == "bearing_coast_accepted" for event in events)
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    assert all(result is None for result in continuation_results)

    plan = PLC(repeated.logic, dt=0.010).how(
        repeated.SequenceState == repeated.COMPLETE,
        max_scans=20,
    )
    assert plan.reachable, plan.reason
    assert plan.replay().state.tags[repeated.SequenceState.name] == repeated.COMPLETE
