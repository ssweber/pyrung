"""Bounded real-program checks for exact same-scan operation reads."""

from __future__ import annotations

import time
from typing import Any

import pytest

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.runner import _compile_avoid

pytestmark = pytest.mark.tumbler


def test_mode_request_reads_exact_atomic_operation_before_separate_clear(
    tumbler_logic: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first exact causal branch controls once, then Compass reads afresh."""

    facts: list[Any] = []
    transitions = 0
    original_record = pilot_module._record_controlling_theory_fact
    original_transition = pilot_module._transition_once

    def record_fact(state: Any, fact: Any) -> None:
        facts.append(fact)
        original_record(state, fact)

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
    crossing_tries: list[dict[str, Any]] = []
    crossing_accepts: list[dict[str, Any]] = []
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
        elif event.kind == "crossing_try":
            crossing_tries.append(event.data)
        elif event.kind == "crossing_accepted":
            crossing_accepts.append(event.data)
        elif event.kind == "candidates_built":
            candidates = tuple(candidate["pair"] for candidate in event.data.get("candidates", ()))
            route_candidates = tuple(event.data.get("route_candidates", ()))
            if ("Cmd_State_Clear", True) in (*candidates, *route_candidates):
                clear_snapshot = latest_snapshot
                break
    else:
        pytest.fail("PILOT did not surface the separate Clear decision")

    operation = (
        ("Cmd_UnitModeChgRequest", True),
        ("Cmd_Mode_Production", True),
    )
    assert tried == []
    assert [tuple(event["actions"]) for event in crossing_tries] == [operation]
    assert [tuple(event["applied"]) for event in crossing_accepts] == [operation]
    assert crossing_tries[0]["crossing"] == {
        "constraints": (),
        "reason": "exact local operation for Sts_UnitModeCurrent=1",
        "verify_required": True,
        "exact": True,
        "proposed": False,
    }
    assert transitions == 1
    assert clear_snapshot is not None
    assert clear_snapshot["Sts_UnitModeCurrent"] == 1
    assert clear_snapshot["Sts_StateCurrent"] == 9

    assert facts == []
