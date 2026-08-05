"""Exact parent-consumer receipt and delayed local repair contracts."""

from __future__ import annotations

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_parent_consumer_watchdog as fixture


def _events():
    return tuple(
        pilot_events(
            PLC(fixture.logic, dt=0.010),
            fixture.SequenceState == fixture.ADVANCED,
            max_scans=100,
        )
    )


def _start_attempt(events):
    return next(
        event
        for event in events
        if event.kind == "candidate_try"
        and (fixture.StartCommand.name, True) in tuple(event.data["applied"])
    )


def test_selected_handoff_names_the_parent_read_as_its_consumer() -> None:
    events = _events()
    attempt = _start_attempt(events)
    obligation = attempt.data["candidate"]["effect_expectation"][0]

    assert obligation.tag == fixture.SequenceState.name
    assert obligation.value == fixture.INTERMEDIATE
    assert obligation.producer == (None, 0, ())
    assert obligation.consumer == (None, 1, ())

    observed = next(
        observation
        for event in events
        if event.kind in {"candidate_accepted", "candidate_rejected"}
        and (fixture.StartCommand.name, True) in tuple(event.data["applied"])
        for observation in event.data["effect_observations"]
        if observation.obligation == obligation
    )
    assert observed.disposition == "SURVIVED"
    assert observed.consumer_read is not None
    assert observed.consumer_read.rung == (None, 1)
    assert observed.consumer_read.execution_kind == "rung"


def test_surviving_parent_handoff_commits_an_exact_expectation_receipt() -> None:
    events = _events()
    receipts = tuple(
        event.data["receipt"]
        for event in events
        if event.kind == "expectation_committed"
        and event.data["receipt"].producer_occurrences
        and event.data["receipt"].producer_occurrences[0].tag == fixture.SequenceState.name
        and event.data["receipt"].producer_occurrences[0].values[-1] == fixture.INTERMEDIATE
    )

    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.source_scan == 1
    assert receipt.obligations[0].consumer == (None, 1, ())
    assert receipt.producer_occurrences[0].rung == (None, 0)
    assert receipt.consumer_occurrences[0].rung == (None, 1)
    assert receipt.consumer_occurrences[0].execution_kind == "rung"


def test_delayed_watchdog_departure_repairs_only_the_source_transaction() -> None:
    events = _events()
    requirements = tuple(
        (index, event.data["requirement"])
        for index, event in enumerate(events)
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None)
        == fixture.WatchdogPresetMs.name
    )
    assert len(requirements) == 1
    requirement_index, requirement = requirements[0]
    receipt_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "expectation_committed" and event.data["receipt"].source_scan == 1
    )
    assert receipt_index < requirement_index
    assert (
        requirement.condition.op,
        requirement.condition.bound,
        requirement.operand_authority.value,
        requirement.source_scan,
        requirement.deadline.scan_id,
    ) == (">", 10, "adjustable", 1, 2)

    repairs = tuple(event for event in events if event.kind == "requirement_locally_repaired")
    assert len(repairs) == 1
    assert repairs[0].data["assignments"] == ((fixture.WatchdogPresetMs.name, 11),)
    assert repairs[0].data["detail"] == "local transaction repaired"
    assert not {
        "retained_replay_try",
        "retained_replay_rejected",
        "retained_replay_accepted",
    }.intersection(event.kind for event in events)
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True

    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.SequenceState == fixture.ADVANCED,
        max_scans=100,
    )
    assert plan.reachable, plan.reason
    assert plan.anchor_scan == 0
    assert plan.total_scans == 2
    assert plan.state.scan_id == 2
    assert plan.state.tags[fixture.SequenceState.name] == fixture.ADVANCED
    assert plan.state.tags[fixture.WatchdogPresetMs.name] == 11
    assert all(step.scan <= 1 for step in plan.journal)
