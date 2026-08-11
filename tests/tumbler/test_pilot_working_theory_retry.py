"""Bounded real-program checks for WorkingTheory same-scan retries."""

from __future__ import annotations

import time
from typing import Any

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.working_theory import (
    ProveTheory,
    RecordTheoryAttempt,
    RefineTheory,
    TheoryAttemptDisposition,
    TheoryTemporalIntent,
)
from pyrung.core.runner import _compile_avoid

pytestmark = pytest.mark.tumbler


def test_mode_request_rebuilds_atomic_retry_before_separate_clear(
    tumbler_logic: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first exact causal branch controls once, then Compass reads afresh."""

    facts: list[Any] = []
    active_after_fact: list[Any] = []
    transitions = 0
    original_record = pilot_module._record_controlling_theory_fact
    original_transition = pilot_module._transition_once

    def record_fact(state: Any, fact: Any) -> None:
        facts.append(fact)
        original_record(state, fact)
        active_after_fact.append(state.theory_state.active_theory_id)

    def transition_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal transitions
        transitions += 1
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(pilot_module, "_record_controlling_theory_fact", record_fact)
    monkeypatch.setattr(pilot_module, "_transition_once", transition_once)

    plc = PLC(tumbler_logic, dt=0.010)
    plc.step()
    tags = plc._known_tags_by_name
    avoid_pred = _compile_avoid(tags["Cmd_State_Complete"])
    tried: list[tuple[tuple[str, Any], ...]] = []
    clear_snapshot: dict[str, Any] | None = None
    latest_snapshot: dict[str, Any] = {}
    deadline = time.monotonic() + 30.0

    for event in pilot_events(
        plc,
        tags["Sts_State_Completed"],
        max_scans=20,
        avoid_pred=avoid_pred,
    ):
        assert time.monotonic() <= deadline, (
            f"Completed prefix exceeded 30s at {event.kind} scan {event.scan}"
        )
        if event.kind == "iteration":
            latest_snapshot = dict(event.data["snapshot"])
        elif event.kind == "candidate_try":
            tried.append(tuple(event.data["applied"]))
        elif event.kind == "candidates_built":
            candidates = tuple(candidate["pair"] for candidate in event.data.get("candidates", ()))
            route_candidates = tuple(event.data.get("route_candidates", ()))
            if ("Cmd_State_Clear", True) in (*candidates, *route_candidates):
                clear_snapshot = latest_snapshot
                break
    else:
        pytest.fail("PILOT did not surface the separate Clear decision")

    request = (("Cmd_UnitModeChgRequest", True),)
    retry = (
        ("Cmd_UnitModeChgRequest", True),
        ("Cmd_Mode_Production", True),
    )
    assert tried == [request, retry]
    assert transitions == 2
    assert clear_snapshot is not None
    assert clear_snapshot["Sts_UnitModeCurrent"] == 1
    assert clear_snapshot["Sts_StateCurrent"] == 9

    refinements = tuple(fact for fact in facts if isinstance(fact, RefineTheory))
    attempts = tuple(fact for fact in facts if isinstance(fact, RecordTheoryAttempt))
    proofs = tuple(fact for fact in facts if isinstance(fact, ProveTheory))
    assert len(refinements) == 2
    assert refinements[0].temporal_intent is TheoryTemporalIntent.RETRY_TOGETHER
    assert not hasattr(refinements[0], "retry_artifact")
    assert refinements[1].requirements == ()
    assert refinements[1].temporal_intent is None
    assert [attempt.disposition for attempt in attempts] == [
        TheoryAttemptDisposition.REJECTED_EXACT,
        TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
    ]
    assert proofs == ()
    assert active_after_fact[-1] is not None
