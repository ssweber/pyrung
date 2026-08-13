"""Acceptance contract for the field-faithful sequential-correction gap."""

from typing import Any

from pyrung import PLC
from pyrung.core.analysis.pilot import pilot_events
from tests.fixtures import pilot_scan_zero_sequence_route as fixture


def _assert_clicknick_failure_prefix(events: tuple[Any, ...]) -> None:
    assert events[-1].kind == "finished"
    assert events[-1].data["reached"] is False
    assert events[-1].data["reason"].startswith("No productive next action was found")

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

    # Once both corrections are retained, ProgramStep reads the communication
    # transaction as context for the reconnect. This faithfully reproduces the
    # ClickNick composite which contradicts the retained SimulationMode=True.
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
        if event.kind == "candidate_rejected"
        and event.data.get("applied") == contextual_reconnect
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

    # The next WorkingTheory act atomically carries that stale communication
    # context beside the newly discovered watchdog correction. This is the
    # behavior the next production change must replace with another singleton
    # corrective steer followed by a fresh reconnect.
    watchdog_retry = (
        (fixture.FirstWatchdogPresetMs.name, 11),
        *contextual_reconnect,
    )
    assert attempted[simulation_index + 2] == watchdog_retry
    assert tries[simulation_index + 2].data["candidate"]["provenance"] == (
        "working-theory temporal retry",
    )


def test_neutral_route_reproduces_clicknick_sequential_correction_gap() -> None:
    events = tuple(
        pilot_events(
            PLC(fixture.logic, dt=0.010),
            fixture.SequenceStep == 81,
            max_scans=120,
        )
    )

    reconnect_overwrite = next(
        observation
        for event in events
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

    first_coast = next(event for event in events if event.kind == "bearing_coast")
    assert first_coast.scan == 0
    assert first_coast.data["reason"] == "observe exactly one entry scan"
    _assert_clicknick_failure_prefix(events)


def test_neutral_route_reproduces_gap_after_runner_has_already_stepped() -> None:
    runner = PLC(fixture.logic, dt=0.010)
    runner.step()

    assert runner.state.scan_id == 1
    assert runner.state.tags[fixture.SequenceStep.name] == 99

    events = tuple(
        pilot_events(
            runner,
            fixture.SequenceStep == 81,
            max_scans=120,
        )
    )
    _assert_clicknick_failure_prefix(events)
