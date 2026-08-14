"""Acceptance contract for the field-faithful sequential correction route."""

from typing import Any

from pyrung import PLC
from tests.fixtures import pilot_scan_zero_sequence_route as fixture


def _assert_clicknick_success(
    events: tuple[Any, ...],
    plan: Any,
    *,
    anchor_scan: int,
) -> None:
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is True
    assert events[-1].data["reason"] == "target reached"

    tries = tuple(event for event in events if event.kind == "candidate_try")
    attempted = tuple(event.data["applied"] for event in tries)
    reconnect = ((fixture.NetworkAvailable.name, True),)

    # The first two discoveries already follow the desired lifecycle: persist
    # exactly one correction, yield, then issue a fresh reconnect steer.
    ready = ((fixture.ReadyCommand.name, True),)
    ready_index = attempted.index(ready)
    assert attempted[ready_index + 1] == reconnect

    simulation = ((fixture.SimulationMode.name, True),)
    simulation_index = attempted.index(simulation)
    assert attempted[simulation_index - 1] == reconnect

    # ProgramStep reads the communication transaction as context for a fresh
    # reconnect.  WorkingTheory corrections remain separate World changes.
    contextual_reconnect = (
        (fixture.NetworkAvailable.name, True),
        (fixture.SimulationMode.name, False),
        (fixture.NetworkPeerReady.name, True),
    )
    assert attempted[simulation_index + 1] == contextual_reconnect
    contextual_try = tries[simulation_index + 1]
    assert contextual_try.data["candidate"]["program_context_actions"] == (
        (fixture.SimulationMode.name, False),
        (fixture.NetworkPeerReady.name, True),
    )

    contextual_rejection = next(
        event
        for event in events
        if event.kind == "candidate_rejected" and event.data.get("applied") == contextual_reconnect
    )
    watchdog_overwrite = next(
        observation
        for observation in contextual_rejection.data["effect_observations"]
        if observation.appeared is not None
        and observation.displacement is not None
        and observation.appeared.tag == fixture.SequenceStep.name
        and observation.appeared.values == (98, 40)
    )
    assert watchdog_overwrite.displacement.tag == fixture.SequenceStep.name
    assert watchdog_overwrite.displacement.values == (40, 91)

    compositions = tuple(
        (event.data["pilot_rung"].dest, event.data["pilot_rung"].value)
        for event in events
        if event.kind == "theory_correction_composed"
    )
    assert compositions == (
        (fixture.FirstWatchdogPresetMs.name, 11),
        (fixture.FirstWatchdogPresetMs.name, 21),
        (fixture.FirstWatchdogPresetMs.name, 31),
        (fixture.SecondWatchdogPresetMs.name, 11),
    )
    assert not any(
        tag in {fixture.FirstWatchdogPresetMs.name, fixture.SecondWatchdogPresetMs.name}
        for event in tries
        for tag, _value in event.data["applied"]
    )

    decision_kinds = {"candidate_try", "bearing_coast"}
    next_decisions = []
    for index, event in enumerate(events):
        if event.kind != "theory_correction_composed":
            continue
        next_decisions.append(
            next(item for item in events[index + 1 :] if item.kind in decision_kinds)
        )
    assert tuple(event.kind for event in next_decisions) == (
        "candidate_try",
        "candidate_try",
        "candidate_try",
        "bearing_coast",
    )
    assert tuple(next_decisions[index].data["applied"] for index in range(2)) == (
        contextual_reconnect,
        contextual_reconnect,
    )
    assert next_decisions[2].data["applied"] == ((fixture.CheckpointSensor.name, True),)

    checkpoint_acceptance = next(
        event
        for event in events
        if event.kind == "candidate_accepted"
        and event.data.get("applied") == ((fixture.CheckpointSensor.name, True),)
        and any(
            observation.obligation.tag == fixture.SequenceStep.name
            and observation.obligation.value == 41
            and observation.consumer_read is not None
            for observation in event.data.get("effect_observations", ())
        )
    )
    assert checkpoint_acceptance.scan == 7

    adjacent = next(
        event
        for event in events
        if event.kind == "pending_departure_started"
        and event.data["from_value"] == 41
        and event.data["settled_value"] == 50
    )
    assert adjacent.data["classification"] == "clean_continuation"
    assert adjacent.data["settle_scans"] == 2

    assert plan.reachable, plan.reason
    assert plan.anchor_scan == anchor_scan
    assert plan.total_scans == 10 - anchor_scan
    assert plan.state.scan_id == 10
    assert plan.state.tags[fixture.SequenceStep.name] == 81
    assert plan.state.tags[fixture.FirstWatchdogPresetMs.name] == 31
    assert plan.state.tags[fixture.SecondWatchdogPresetMs.name] == 11
    replay = plan.replay()
    assert replay.state.scan_id == 10
    assert replay.state.tags[fixture.SequenceStep.name] == 81
    assert replay.state.tags[fixture.FirstWatchdogPresetMs.name] == 31
    assert replay.state.tags[fixture.SecondWatchdogPresetMs.name] == 11


def test_neutral_route_resolves_clicknick_sequential_corrections() -> None:
    events: list[Any] = []
    plan = PLC(fixture.logic, dt=0.010).how(
        fixture.SequenceStep == 81,
        max_scans=120,
        on_event=events.append,
    )
    recorded = tuple(events)

    reconnect_overwrite = next(
        observation
        for event in recorded
        if event.kind == "candidate_accepted"
        for observation in event.data.get("effect_observations", ())
        if observation.appeared is not None
        and observation.displacement is not None
        and observation.appeared.tag == fixture.SequenceStep.name
        and observation.appeared.values == (98, 10)
    )
    assert reconnect_overwrite.displacement.tag == fixture.SequenceStep.name
    assert reconnect_overwrite.displacement.values == (10, 94)
    assert reconnect_overwrite.displacement.scan_id == reconnect_overwrite.appeared.scan_id + 1

    first_coast = next(event for event in recorded if event.kind == "bearing_coast")
    assert first_coast.scan == 0
    assert first_coast.data["reason"] == "observe exactly one entry scan"
    _assert_clicknick_success(recorded, plan, anchor_scan=0)


def test_neutral_route_resolves_corrections_after_runner_has_already_stepped() -> None:
    runner = PLC(fixture.logic, dt=0.010)
    runner.step()

    assert runner.state.scan_id == 1
    assert runner.state.tags[fixture.SequenceStep.name] == 99

    events: list[Any] = []
    plan = runner.how(
        fixture.SequenceStep == 81,
        max_scans=120,
        on_event=events.append,
    )
    _assert_clicknick_success(tuple(events), plan, anchor_scan=1)
