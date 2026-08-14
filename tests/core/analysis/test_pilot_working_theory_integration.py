"""WorkingTheory recording and production-control integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, copy, latch, rung, system
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot.attempt_interpretation import AttemptInterpretationKind
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.conductivity import (
    ConductivityProgress,
    ConductivityReach,
)
from pyrung.core.analysis.pilot.effects import EffectObservationSnapshot
from pyrung.core.analysis.pilot.working_theory import (
    AdvanceTheory,
    OpenTheory,
    ProveTheory,
    RecordConductivityResearch,
    RecordTheoryAttempt,
    RecordUnattributedEvidence,
    RefineTheory,
    theory_view,
)
from tests.fixtures import pilot_scan_zero_sequence_route as sequence_route
from tests.fixtures.pilot_alarm_presets import (
    aborted_on_first_scan,
    alarmed_at_start,
)


def _direct_producer_program() -> tuple[Program, Bool]:
    producer = Bool("producer", external=True)
    consumer = Bool("consumer")
    with Program() as logic:
        with rung(producer):
            latch(consumer)
    return logic, consumer


def _stable_public_value(value: Any, *, field_name: str = "") -> Any:
    """Retain every stable diagnostic field while naming opaque owner roles."""

    if field_name == "causal_identity":
        assert len(value) == 3
        return ("execution-epoch", "execution-owner", "checkpoint-owner")
    if value is None or isinstance(value, bool | int | float | str | bytes):
        return value
    if isinstance(value, Enum):
        return (type(value).__module__, type(value).__qualname__, value.name)
    if isinstance(value, PLC):
        return (
            "PLC",
            value.state.scan_id,
            _stable_public_value(dict(value.state.tags)),
        )
    if isinstance(value, Mapping):
        return tuple(
            sorted(
                (
                    (_stable_public_value(key), _stable_public_value(member))
                    for key, member in value.items()
                ),
                key=repr,
            )
        )
    if isinstance(value, tuple | list):
        return tuple(_stable_public_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_stable_public_value(item) for item in value), key=repr))
    if is_dataclass(value):
        return (
            type(value).__module__,
            type(value).__qualname__,
            tuple(
                (
                    item.name,
                    _stable_public_value(getattr(value, item.name), field_name=item.name),
                )
                for item in fields(value)
            ),
        )
    snapshot = getattr(value, "diagnostic_snapshot", None)
    if callable(snapshot):
        return (
            type(value).__module__,
            type(value).__qualname__,
            _stable_public_value(snapshot()),
        )
    return (
        type(value).__module__,
        type(value).__qualname__,
        _stable_public_value(pilot_module._semantic_key(value)),
    )


def _stable_public_event(event: Any) -> tuple[Any, ...]:
    return (event.kind, event.scan, _stable_public_value(event.data))


def _stable_plan_result(plan: Any) -> tuple[Any, ...]:
    return (
        plan.status,
        plan.reachable,
        plan.target_tag,
        plan.target_value,
        plan.reason,
        _stable_public_value(plan.route),
        _stable_public_value(plan.journal),
        plan.anchor_scan,
        _stable_public_value(plan.journey),
        _stable_public_value(plan.hold_log),
        _stable_public_value(plan.lever_notes),
        plan.avoid_names,
        plan.total_scans,
        _stable_public_value(plan.changes),
        _stable_public_value(plan.ordered_steps),
        _stable_public_value(dict(plan.tags)),
    )


def test_optional_theory_recording_does_not_change_existing_decisions(
    monkeypatch: Any,
) -> None:
    logic, consumer = _direct_producer_program()
    recorded_facts: list[Any] = []
    recorded_events: list[Any] = []
    original = pilot_module._record_optional_theory_fact

    def record(state: Any, fact: Any) -> None:
        recorded_facts.append(fact)
        original(state, fact)

    monkeypatch.setattr(pilot_module, "_record_optional_theory_fact", record)
    with_recording = PLC(logic).how(consumer, max_scans=30, on_event=recorded_events.append)

    no_recording_events: list[Any] = []
    monkeypatch.setattr(
        pilot_module,
        "_run_optional_theory_hook",
        lambda *_args, **_kwargs: None,
    )
    without_recording = PLC(logic).how(consumer, max_scans=30, on_event=no_recording_events.append)

    interpretations = tuple(
        fact
        for fact in recorded_facts
        if isinstance(fact, RecordUnattributedEvidence)
        and fact.observation.evidence
        and fact.observation.evidence[0] == AttemptInterpretationKind.KEEP_AND_REREAD.value
    )
    assert len(interpretations) == 1
    assert not any(isinstance(fact, OpenTheory) for fact in recorded_facts)
    assert not any(isinstance(fact, RecordTheoryAttempt) for fact in recorded_facts)
    assert not any(isinstance(fact, AdvanceTheory) for fact in recorded_facts)
    assert not any(isinstance(fact, ProveTheory) for fact in recorded_facts)
    assert not any("theory" in event.kind for event in recorded_events)

    assert _stable_plan_result(with_recording) == _stable_plan_result(without_recording)
    assert tuple(map(_stable_public_event, recorded_events)) == tuple(
        map(_stable_public_event, no_recording_events)
    )


def test_bootstrap_overwrite_retry_records_controlling_theory_evidence(
    monkeypatch: Any,
) -> None:
    stepper = Int("theory_stepper", default=0)
    consumer_guard = Bool("theory_consumer_guard", external=True)
    with Program() as logic:
        with rung(system.sys.first_scan):
            copy(1, stepper)
        with rung(~consumer_guard):
            copy(9, stepper, oneshot=True)

    facts: list[Any] = []
    original = pilot_module._record_controlling_theory_fact

    def record(state: Any, fact: Any) -> None:
        facts.append(fact)
        original(state, fact)

    monkeypatch.setattr(pilot_module, "_record_controlling_theory_fact", record)
    events: list[Any] = []
    result = PLC(logic).how(stepper == 1, max_scans=20, on_event=events.append)

    assert result.reachable
    assert not any(event.kind == "requirement_locally_repaired" for event in events)
    assert any(
        event.kind == "candidate_try" and event.data["applied"] == ((consumer_guard.name, True),)
        for event in events
    )
    lifecycle = tuple(
        fact
        for fact in facts
        if isinstance(
            fact,
            OpenTheory | RecordTheoryAttempt | RefineTheory | AdvanceTheory | ProveTheory,
        )
    )
    assert lifecycle, "the bootstrap retry must record its controlling theory lifecycle"


def test_requirement_event_preserves_exact_scan_call_and_deadline() -> None:
    stepper = Int("identity_stepper", default=0)
    consumer_guard = Bool("identity_consumer_guard", external=True)
    with Program() as logic:
        with rung(system.sys.first_scan):
            copy(1, stepper)
        with rung(~consumer_guard):
            copy(9, stepper, oneshot=True)

    events: list[Any] = []
    result = PLC(logic).how(stepper == 1, max_scans=20, on_event=events.append)

    assert result.reachable
    captured = tuple(
        event.data["requirement"] for event in events if event.kind == "requirement_activated"
    )
    assert len(captured) == 1
    snapshot = captured[0]
    assert snapshot.source_scan == 0
    assert snapshot.demanding_occurrence.kind == "read"
    assert snapshot.demanding_occurrence.tag == consumer_guard.name
    assert snapshot.demanding_occurrence.scan_id == 1
    assert snapshot.deadline == snapshot.demanding_occurrence
    assert snapshot.deadline.dynamic_address[5] == snapshot.deadline.call_invocation
    assert snapshot.causal_identity


def test_optional_reducer_failure_cannot_change_production_result(monkeypatch: Any) -> None:
    logic, consumer = _direct_producer_program()
    baseline = PLC(logic).how(consumer, max_scans=30)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("optional recorder failure")

    monkeypatch.setattr(pilot_module, "reduce_theory", fail)
    observed = PLC(logic).how(consumer, max_scans=30)

    assert observed.status is baseline.status
    assert observed.reason == baseline.reason
    assert observed.changes == baseline.changes
    assert observed.ordered_steps == baseline.ordered_steps
    assert observed.total_scans == baseline.total_scans
    assert dict(observed.tags) == dict(baseline.tags)


def test_theory_attempt_adapter_adds_no_projection_replay(monkeypatch: Any) -> None:
    """Theory interpretation reuses the projection paid for by the steer."""

    logic, consumer = _direct_producer_program()
    original_projection = PLC._replay_pilot_rung_write_projection_at
    projection_calls: list[tuple[PLC, int]] = []

    def counted_projection(plc: PLC, scan_id: int):
        projection_calls.append((plc, scan_id))
        return original_projection(plc, scan_id)

    monkeypatch.setattr(
        PLC,
        "_replay_pilot_rung_write_projection_at",
        counted_projection,
    )
    with_recording = PLC(logic).how(consumer, max_scans=30)
    with_recording_count = len(projection_calls)

    projection_calls.clear()
    monkeypatch.setattr(
        pilot_module,
        "_theory_transition_from_attempt",
        lambda *_args, **_kwargs: None,
    )
    without_recording = PLC(logic).how(consumer, max_scans=30)

    assert with_recording.reachable and without_recording.reachable
    assert with_recording_count > 0
    assert len(projection_calls) == with_recording_count


def _capture_theory_transitions(monkeypatch: Any) -> list[Any]:
    captured: list[Any] = []
    original = pilot_module._record_working_theory_transition

    def record(state: Any, observation: Any, **kwargs: Any) -> None:
        if observation is not None:
            captured.append(observation)
        original(state, observation, **kwargs)

    monkeypatch.setattr(pilot_module, "_record_working_theory_transition", record)
    return captured


def _capture_conductivity_fronts(monkeypatch: Any) -> list[Any]:
    captured: list[Any] = []
    original = pilot_module._record_working_theory_transition

    def record(state: Any, transition: Any, **kwargs: Any) -> None:
        original(state, transition, **kwargs)
        view = theory_view(state.theory_state)
        front = Compass().conductivity_front(view)
        if front is not None:
            captured.append(front)

    monkeypatch.setattr(pilot_module, "_record_working_theory_transition", record)
    return captured


def _preset_transition(captured: list[Any], preset_name: str) -> Any:
    matching = tuple(
        observation
        for observation in captured
        if any(
            requirement.deadline_occurrence[1] == preset_name
            for requirement in observation.requirements
        )
    )
    assert len(matching) == 1
    return matching[0]


def test_scan_zero_done_overwrite_is_setup_first_from_exact_preset_deadline(
    monkeypatch: Any,
) -> None:
    fixture = aborted_on_first_scan
    captured = _capture_theory_transitions(monkeypatch)

    result = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.AT_TARGET,
        max_scans=100,
    )

    assert result.reachable
    observation = _preset_transition(captured, fixture.WatchdogPresetMs.name)
    assert observation.source.scan_id == 0
    assert observation.act_identity[0] == "executed-program-scan"
    assert observation.interpretation.kind is AttemptInterpretationKind.SETUP_FIRST
    requirement = observation.requirements[0]
    assert requirement.deadline_occurrence[1] == fixture.WatchdogPresetMs.name
    assert requirement.demanding_occurrence[1] == fixture.Watchdog.Done.name
    assert requirement.deadline_occurrence[3][-1] < requirement.demanding_occurrence[3][-1]


def test_retained_reset_done_overwrite_is_retry_together_from_same_receipts(
    monkeypatch: Any,
) -> None:
    fixture = alarmed_at_start
    captured = _capture_theory_transitions(monkeypatch)

    result = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.COMPLETE,
        max_scans=100,
    )

    assert result.reachable
    observation = _preset_transition(captured, fixture.WatchdogPresetMs.name)
    assert observation.source.scan_id == 1
    assert observation.act_identity[0] != "executed-program-scan"
    assert observation.interpretation.kind is AttemptInterpretationKind.RETRY_TOGETHER
    assert observation.conductivity_observations
    assert all(
        isinstance(item, EffectObservationSnapshot)
        for item in observation.conductivity_observations
    )
    requirement = observation.requirements[0]
    assert requirement.deadline_occurrence[1] == fixture.WatchdogPresetMs.name
    assert requirement.demanding_occurrence[1] == fixture.Watchdog.Done.name
    assert requirement.deadline_occurrence[3][-1] < requirement.demanding_occurrence[3][-1]


def test_neutral_retry_exposes_consumer_then_displacement_front(monkeypatch: Any) -> None:
    fixture = alarmed_at_start
    captured = _capture_conductivity_fronts(monkeypatch)

    result = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.COMPLETE,
        max_scans=100,
    )

    assert result.reachable
    assert len(captured) == 1
    flows = tuple(
        flow
        for flow in captured[0].flows
        if any(
            obligation.tag == fixture.ProcessStep.name
            and obligation.value == fixture.RUNNING
            for obligation in flow.obligations
        )
    )
    assert len(flows) == 1
    flow = flows[0]
    assert flow.reach is ConductivityReach.CONSUMER
    assert len(flow.obligations) == 2
    assert flow.appeared is not None
    assert flow.front_occurrence is not None
    assert flow.displacement is not None
    assert (
        flow.appeared.scan_id,
        flow.appeared.ordinal,
        flow.front_occurrence.ordinal,
        flow.displacement.ordinal,
    ) == (2, 5, 6, 21)


def test_extended_watchdog_requests_research_after_same_stop_drifts(
    monkeypatch: Any,
) -> None:
    captured: list[Any] = []
    original = pilot_module._record_controlling_theory_fact

    def record(state: Any, fact: Any) -> None:
        before = (
            state.theory_state.ledger.theories[fact.finding.theory_id].current_progress_id
            if isinstance(fact, RecordConductivityResearch)
            else None
        )
        original(state, fact)
        if isinstance(fact, RecordConductivityResearch):
            theory = state.theory_state.ledger.theories[fact.finding.theory_id]
            view = theory_view(state.theory_state)
            captured.append(
                (
                    fact.finding,
                    before,
                    theory.current_progress_id,
                    view,
                    Compass().conductivity_research(view),
                )
            )

    monkeypatch.setattr(pilot_module, "_record_controlling_theory_fact", record)

    events = tuple(
        pilot_events(
            PLC(sequence_route.logic, dt=0.010),
            sequence_route.SequenceStep == 81,
            max_scans=40,
        )
    )

    research_events = tuple(
        event for event in events if event.kind == "conductivity_research_requested"
    )
    assert len(research_events) == 1
    assert len(captured) == 1
    finding, progress_before, progress_after, view, repeated_request = captured[0]
    event = research_events[0]
    assert event.data["finding_identity"] == finding.identity
    assert finding.source.scan_id == event.scan
    assert finding.comparison_identity[3] is ConductivityProgress.SAME_STOP
    assert len(finding.requirement_drift_identities) == 1
    assert finding.displacement.rung == (None, 16)
    assert tuple(read.tag for read in finding.enabling_reads) == (
        sequence_route.FirstWatchdog.Done.name,
        "_oneshot:i26",
    )
    assert progress_after == progress_before
    assert view is not None
    assert view.research_findings[-1] == finding
    assert repeated_request is None
    research_index = events.index(event)
    assert not any(item.kind == "candidate_try" for item in events[research_index + 1 :])
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is False


def test_prestepped_watchdog_retains_composed_world_for_followup_research(
    monkeypatch: Any,
) -> None:
    """A same-scan overlay change remains part of the active theory's World."""

    class ResearchObserved(RuntimeError):
        pass

    class ResearchMissing(RuntimeError):
        pass

    transition_count = 0
    original = pilot_module._record_working_theory_transition

    def record(state: Any, transition: Any, **kwargs: Any) -> None:
        nonlocal transition_count
        transition_count += 1
        original(state, transition, **kwargs)
        request = Compass().conductivity_research(theory_view(state.theory_state))
        if request is not None:
            raise ResearchObserved
        if transition_count >= 7:
            raise ResearchMissing("bounded run never retained the comparable attempt")

    monkeypatch.setattr(pilot_module, "_record_working_theory_transition", record)
    plc = PLC(sequence_route.logic, dt=0.010)
    plc.step()

    with pytest.raises(ResearchObserved):
        plc.how(
            sequence_route.SequenceStep == 81,
            max_scans=40,
        )


def test_monitor_records_initial_and_refined_watchdog_attempts_from_exact_receipts(
    monkeypatch: Any,
) -> None:
    captured = _capture_theory_transitions(monkeypatch)

    PLC(sequence_route.logic, dt=0.010).how(
        sequence_route.SequenceStep == 81,
        max_scans=40,
    )

    matching = tuple(
        observation
        for observation in captured
        if any(
            requirement.deadline_occurrence[1]
            == sequence_route.FirstWatchdogPresetMs.name
            for requirement in observation.requirements
        )
    )
    # These are two physical attempts at the same chart edge, not duplicate
    # recording. The first reconnect exposes the watchdog; composition changes
    # the same-scan World, and a fresh Compass read selects that same reconnect
    # steer again. Its later displacement proves that 11 is still insufficient.
    assert len(matching) == 2
    initial, refined = matching
    assert initial.source.scan_id == 3
    assert refined.source.scan_id == 3
    assert initial.source.world_key != refined.source.world_key
    assert initial.interpretation.kind is AttemptInterpretationKind.RETRY_TOGETHER
    assert refined.interpretation.kind is AttemptInterpretationKind.RETRY_THROUGH_DEADLINE
    assert tuple(
        (obligation.tag, obligation.value) for obligation in initial.claim.obligations
    ) == ((sequence_route.SequenceStep.name, 40),)
    assert tuple(
        (obligation.tag, obligation.value) for obligation in refined.claim.obligations
    ) == ((sequence_route.SequenceStep.name, 40),)
    assert initial.requirements[0].deadline_occurrence[2] == 4
    assert refined.requirements[0].deadline_occurrence[2] == 5
    assert initial.act_identity == refined.act_identity
    assert initial.pilot_rung_identities != refined.pilot_rung_identities
