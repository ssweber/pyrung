"""WorkingTheory recording and production-control integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Program, copy, latch, rung, system
from pyrung.core.analysis.pilot import attempt_transition as attempt_transition_module
from pyrung.core.analysis.pilot import pilot_events
from pyrung.core.analysis.pilot import theory_recording as theory_recording_module
from pyrung.core.analysis.pilot.attempt_interpretation import AttemptInterpretationKind
from pyrung.core.analysis.pilot.compass import Compass
from pyrung.core.analysis.pilot.conductivity import (
    ConductivityProgress,
    ConductivityReach,
)
from pyrung.core.analysis.pilot.effects import EffectObservationSnapshot
from pyrung.core.analysis.pilot.theory_reducer import (
    AdvanceTheory,
    ComposeTheoryCorrection,
    OpenTheory,
    ProveTheory,
    RecordConductivityResearch,
    RecordTheoryAttempt,
    RefineTheory,
)
from pyrung.core.analysis.pilot.working_theory import (
    active_theory_configurations,
    theory_view,
)
from pyrung.core.analysis.pilot.world_key import _semantic_key
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
    """Retain every stable diagnostic field in one comparable value."""

    if field_name in {"execution_ref", "checkpoint_ref"}:
        return field_name
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
        _stable_public_value(_semantic_key(value)),
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


def test_noncontrolling_interpretation_stays_out_of_working_theory(
    monkeypatch: Any,
) -> None:
    logic, consumer = _direct_producer_program()
    recorded_facts: list[Any] = []
    recorded_events: list[Any] = []
    original = theory_recording_module._record_optional_theory_fact

    def record(state: Any, fact: Any) -> None:
        recorded_facts.append(fact)
        original(state, fact)

    monkeypatch.setattr(theory_recording_module, "_record_optional_theory_fact", record)
    with_recording = PLC(logic).how(consumer, max_scans=30, on_event=recorded_events.append)

    no_recording_events: list[Any] = []
    monkeypatch.setattr(
        theory_recording_module,
        "_run_optional_theory_hook",
        lambda *_args, **_kwargs: None,
    )
    without_recording = PLC(logic).how(consumer, max_scans=30, on_event=no_recording_events.append)

    assert recorded_facts == []
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
    original = theory_recording_module._record_controlling_theory_fact

    def record(state: Any, fact: Any) -> None:
        facts.append(fact)
        original(state, fact)

    monkeypatch.setattr(theory_recording_module, "_record_controlling_theory_fact", record)
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
    assert snapshot.execution_ref
    assert snapshot.checkpoint_ref


def test_optional_reducer_failure_cannot_change_production_result(monkeypatch: Any) -> None:
    logic, consumer = _direct_producer_program()
    baseline = PLC(logic).how(consumer, max_scans=30)

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("optional recorder failure")

    monkeypatch.setattr(theory_recording_module, "reduce_theory", fail)
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
        attempt_transition_module,
        "_theory_transition_from_attempt",
        lambda *_args, **_kwargs: None,
    )
    without_recording = PLC(logic).how(consumer, max_scans=30)

    assert with_recording.reachable and without_recording.reachable
    assert with_recording_count > 0
    assert len(projection_calls) == with_recording_count


def _capture_theory_transitions(monkeypatch: Any) -> list[Any]:
    captured: list[Any] = []
    original = theory_recording_module._record_working_theory_transition

    def record(state: Any, observation: Any, **kwargs: Any) -> None:
        if observation is not None:
            captured.append(observation)
        original(state, observation, **kwargs)

    monkeypatch.setattr(theory_recording_module, "_record_working_theory_transition", record)
    return captured


def _capture_conductivity_fronts(monkeypatch: Any) -> list[Any]:
    captured: list[Any] = []
    original = theory_recording_module._record_working_theory_transition

    def record(state: Any, transition: Any, **kwargs: Any) -> None:
        original(state, transition, **kwargs)
        view = theory_view(state.theory_state)
        front = Compass().conductivity_front(view)
        if front is not None:
            captured.append(front)

    monkeypatch.setattr(theory_recording_module, "_record_working_theory_transition", record)
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


def test_retained_reset_done_overwrites_are_sequential_exact_attempts(
    monkeypatch: Any,
) -> None:
    fixture = alarmed_at_start
    captured = _capture_theory_transitions(monkeypatch)

    result = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.COMPLETE,
        max_scans=100,
    )

    assert result.reachable
    observations = tuple(
        observation
        for observation in captured
        if any(
            requirement.deadline_occurrence[1] == fixture.WatchdogPresetMs.name
            for requirement in observation.requirements
        )
    )
    assert len(observations) == 2
    initial, later = observations
    assert tuple(item.source.scan_id for item in observations) == (1, 2)
    assert later.act_identity != initial.act_identity
    assert tuple(item.interpretation.kind for item in observations) == (
        AttemptInterpretationKind.RETRY_TOGETHER,
        AttemptInterpretationKind.RETRY_TOGETHER,
    )
    assert all(item.conductivity_observations for item in observations)
    assert all(
        isinstance(item, EffectObservationSnapshot)
        for observation in observations
        for item in observation.conductivity_observations
    )
    requirements = tuple(observation.requirements[0] for observation in observations)
    assert tuple(item.deadline_occurrence[2] for item in requirements) == (2, 3)
    assert all(
        item.deadline_occurrence[1] == fixture.WatchdogPresetMs.name
        and item.demanding_occurrence[1] == fixture.Watchdog.Done.name
        and item.deadline_occurrence[3][-1] < item.demanding_occurrence[3][-1]
        for item in requirements
    )


def test_sequential_retry_retains_prior_consumer_displacement_fronts(
    monkeypatch: Any,
) -> None:
    fixture = alarmed_at_start
    captured = _capture_conductivity_fronts(monkeypatch)

    result = PLC(fixture.logic, dt=0.010).how(
        fixture.ProcessStep == fixture.COMPLETE,
        max_scans=100,
    )

    assert result.reachable
    assert len(captured) == 2
    assert tuple(len(front.flows) for front in captured) == (1, 2)
    flows = tuple(
        flow
        for flow in captured[-1].flows
        if any(
            obligation.tag == fixture.ProcessStep.name and obligation.value == fixture.RUNNING
            for obligation in flow.obligations
        )
    )
    assert len(flows) == 1
    assert all(flow.reach is ConductivityReach.CONSUMER for flow in flows)
    assert flows[0] == captured[0].flows[0]
    assert tuple(
        (
            flow.appeared.scan_id if flow.appeared is not None else None,
            flow.appeared.ordinal if flow.appeared is not None else None,
            flow.front_occurrence.ordinal if flow.front_occurrence is not None else None,
            flow.displacement.scan_id if flow.displacement is not None else None,
            flow.displacement.ordinal if flow.displacement is not None else None,
        )
        for flow in flows
    ) == ((2, 5, 6, 2, 21),)
    terminal = captured[-1].flows[-1]
    assert tuple((item.tag, item.value) for item in terminal.obligations) == (
        (fixture.ProcessStep.name, fixture.COMPLETE),
    )
    assert terminal.reach is ConductivityReach.PRODUCER
    assert terminal.appeared is not None
    # Scan-entry configuration is not an executable PilotRung, so it adds no
    # synthetic overlay occurrence ahead of the program's exact write.
    assert (terminal.appeared.scan_id, terminal.appeared.ordinal) == (3, 9)
    assert terminal.displacement is not None
    assert (terminal.displacement.scan_id, terminal.displacement.ordinal) == (3, 22)


def test_advanced_reconnect_front_yields_to_watchdog_research_and_replacement(
    monkeypatch: Any,
) -> None:
    captured: list[Any] = []
    compositions: list[Any] = []
    original = theory_recording_module._record_controlling_theory_fact

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
        if isinstance(fact, ComposeTheoryCorrection):
            compositions.append((fact, active_theory_configurations(state.theory_state)))

    monkeypatch.setattr(theory_recording_module, "_record_controlling_theory_fact", record)

    events: list[Any] = []
    research_seen = False
    stream = pilot_events(
        PLC(sequence_route.logic, dt=0.010),
        sequence_route.SequenceStep == 81,
        max_scans=40,
    )
    try:
        for emitted in stream:
            events.append(emitted)
            if emitted.kind == "conductivity_research_requested" and emitted.data[
                "displacement"
            ].rung == (None, 16):
                research_seen = True
            elif emitted.kind == "theory_correction_composed" and research_seen:
                break
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    research_events = tuple(
        event
        for event in events
        if event.kind == "conductivity_research_requested"
        and event.data["displacement"].rung == (None, 16)
    )
    watchdog_captures = tuple(item for item in captured if item[0].displacement.rung == (None, 16))
    assert len(research_events) == 1
    assert len(watchdog_captures) == 1
    (event,) = research_events
    finding, progress_before, progress_after, view, repeated_request = watchdog_captures[0]
    front = Compass().conductivity_front(view)
    assert front is not None
    assert tuple(comparison.progress for comparison in front.comparisons) == (
        ConductivityProgress.SAME_STOP,
        ConductivityProgress.STOP_CHANGED,
        ConductivityProgress.SAME_STOP,
    )
    assert tuple(
        flow.displacement.rung
        for attempt in front.attempts
        for flow in attempt.flows
        if flow.displacement is not None
    ) == ((None, 19), (None, 19), (None, 16), (None, 16))
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
    watchdog_research_index = events.index(event)
    post_research = events[watchdog_research_index + 1 :]
    assert not any(item.kind == "candidate_try" for item in post_research)
    assert events[-1].kind == "theory_correction_composed"
    assert events[-1].scan == event.scan
    assert events[-1].data["configuration"] == ((sequence_route.FirstWatchdogPresetMs.name, 21),)
    fact, installed = compositions[-1]
    assert fact.research_finding_identity == finding.identity
    assert len(fact.superseded_configuration_identities) == 1
    preset_configurations = tuple(
        configuration
        for configuration in installed
        if configuration.assignments[0][0] == sequence_route.FirstWatchdogPresetMs.name
    )
    assert tuple(configuration.assignments[0][1] for configuration in preset_configurations) == (
        21,
    )


def test_neutral_route_steers_again_before_researching_third_intrascan_correction(
    monkeypatch: Any,
) -> None:
    compositions: list[tuple[Any, tuple[Any, ...]]] = []
    original = theory_recording_module._record_controlling_theory_fact

    def record(state: Any, fact: Any) -> None:
        original(state, fact)
        if isinstance(fact, ComposeTheoryCorrection):
            compositions.append((fact, active_theory_configurations(state.theory_state)))

    monkeypatch.setattr(theory_recording_module, "_record_controlling_theory_fact", record)

    events: list[Any] = []
    stream = pilot_events(
        PLC(sequence_route.logic, dt=0.010),
        sequence_route.SequenceStep == 81,
        max_scans=40,
    )
    try:
        for emitted in stream:
            events.append(emitted)
            if len(compositions) == 3 and emitted.kind == "bearing_coast":
                break
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()

    composed_values = tuple(fact.configuration.assignments[0][1] for fact, _ in compositions)
    assert composed_values == (
        11,
        21,
        31,
    ), tuple(
        (event.kind, event.scan, event.data.get("applied"), event.data.get("reason"))
        for event in events[-20:]
    )
    third_requirements = tuple(
        event.data["requirement"]
        for event in events
        if event.kind == "requirement_activated"
        and getattr(event.data["requirement"].condition, "tag", None)
        == sequence_route.FirstWatchdogPresetMs.name
        and getattr(event.data["requirement"].condition, "bound", None) == 30
    )
    assert third_requirements
    assert third_requirements[-1].operand_authority.value == "adjustable"
    second_composition_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "theory_correction_composed" and event.data["configuration"][0][1] == 21
    )
    third_composition_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "theory_correction_composed" and event.data["configuration"][0][1] == 31
    )
    intervening = events[second_composition_index + 1 : third_composition_index]
    assert any(
        event.kind == "candidate_try"
        and event.data["applied"] == ((sequence_route.CheckpointSensor.name, True),)
        for event in intervening
    )
    assert sum(event.kind == "conductivity_research_requested" for event in intervening) == 1

    third_fact, installed = compositions[-1]
    assert third_fact.research_finding_identity is not None
    assert len(third_fact.superseded_configuration_identities) == 1
    preset_configurations = tuple(
        configuration
        for configuration in installed
        if configuration.assignments[0][0] == sequence_route.FirstWatchdogPresetMs.name
    )
    assert tuple(configuration.assignments[0][1] for configuration in preset_configurations) == (
        31,
    )

    checkpoint_try = next(
        event
        for event in intervening
        if event.kind == "candidate_try"
        and event.data["applied"] == ((sequence_route.CheckpointSensor.name, True),)
    )
    obligation = checkpoint_try.data["candidate"]["effect_expectation"][0]
    assert obligation.tag == sequence_route.SequenceStep.name
    assert obligation.value == 41
    assert obligation.producer == (None, 4, (0,))
    assert obligation.consumer == (None, 5, ())
    assert obligation.required_shape == ((sequence_route.SequenceStep.name, 41),)
    assert events[-1].kind == "bearing_coast"
    post_composition_decision = next(
        event
        for event in events[third_composition_index + 1 :]
        if event.kind in {"candidate_try", "bearing_coast"}
    )
    assert post_composition_decision.kind == "candidate_try"
    assert post_composition_decision.data["applied"] == (
        (sequence_route.CheckpointSensor.name, True),
    )


def test_prestepped_watchdog_retains_composed_world_for_followup_research(
    monkeypatch: Any,
) -> None:
    """A same-scan overlay change remains part of the active theory's World."""

    class ResearchObserved(RuntimeError):
        pass

    class ResearchMissing(RuntimeError):
        pass

    transition_count = 0
    original = theory_recording_module._record_working_theory_transition

    def record(state: Any, transition: Any, **kwargs: Any) -> None:
        nonlocal transition_count
        transition_count += 1
        original(state, transition, **kwargs)
        request = Compass().conductivity_research(theory_view(state.theory_state))
        if request is not None:
            raise ResearchObserved
        if transition_count >= 7:
            raise ResearchMissing("bounded run never retained the comparable attempt")

    monkeypatch.setattr(theory_recording_module, "_record_working_theory_transition", record)
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
            requirement.deadline_occurrence[1] == sequence_route.FirstWatchdogPresetMs.name
            for requirement in observation.requirements
        )
    )
    # The first two attempts refine the reconnect edge.  Once its exact write
    # conducts through the charted outer consumer, the checkpoint steer exposes
    # a third, later displacement from a new source World.
    assert len(matching) == 3
    initial, refined, later = matching
    assert (initial.source.scan_id, refined.source.scan_id, later.source.scan_id) == (
        3,
        3,
        5,
    )
    # Composition changes WorkingTheory's desired entry configuration, not the
    # physical source World.  Each retry's receipt names the exact configuration
    # that distinguishes otherwise identical source boundaries.
    assert initial.source.world_key == refined.source.world_key
    assert initial.interpretation.kind is AttemptInterpretationKind.RETRY_TOGETHER
    assert refined.interpretation.kind is AttemptInterpretationKind.RETRY_THROUGH_DEADLINE
    assert later.interpretation.kind is AttemptInterpretationKind.RETRY_TOGETHER
    assert tuple(
        (obligation.tag, obligation.value) for obligation in initial.claim.obligations
    ) == ((sequence_route.SequenceStep.name, 40),)
    assert tuple(
        (obligation.tag, obligation.value) for obligation in refined.claim.obligations
    ) == ((sequence_route.SequenceStep.name, 40),)
    assert tuple((obligation.tag, obligation.value) for obligation in later.claim.obligations) == (
        (sequence_route.SequenceStep.name, 41),
    )
    assert initial.requirements[0].deadline_occurrence[2] == 4
    assert refined.requirements[0].deadline_occurrence[2] == 5
    assert later.requirements[0].deadline_occurrence[2] == 6
    assert initial.act_identity == refined.act_identity
    assert later.act_identity != refined.act_identity
    assert initial.configurations == ()
    assert tuple(item.assignments for item in refined.configurations) == (
        ((sequence_route.FirstWatchdogPresetMs.name, 11),),
    )
    assert tuple(item.assignments for item in later.configurations) == (
        ((sequence_route.FirstWatchdogPresetMs.name, 21),),
    )
