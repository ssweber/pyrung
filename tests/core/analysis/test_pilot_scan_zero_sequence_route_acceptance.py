"""Acceptance contract for the field-faithful sequential correction route.

The lifecycle contracts run ``adjacent_logic``, which keeps the adjacent-scan
98 -> 10 -> 94 mechanism; the field-order tests at the end run ``logic``,
where the status branch sees the repair on the same scan.
"""

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
    assert attempted[simulation_index + 1] == reconnect
    contextual_try = tries[simulation_index + 1]
    assert contextual_try.data["candidate"]["program_context_actions"] == (
        (fixture.SimulationMode.name, False),
        (fixture.NetworkPeerReady.name, True),
    )

    contextual_rejection = next(
        event
        for event in events
        if event.kind == "candidate_rejected"
        and event.data.get("applied") == reconnect
        and any(
            observation.appeared is not None
            and observation.appeared.tag == fixture.SequenceStep.name
            and observation.appeared.values == (98, 40)
            for observation in event.data.get("effect_observations", ())
        )
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
        assignment
        for event in events
        if event.kind == "theory_correction_composed"
        for assignment in event.data["configuration"]
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
        reconnect,
        reconnect,
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
    # The preset is now applied at the entry boundary of this real steer; the
    # legacy dedicated preset Pulse no longer consumes its own scan.
    assert checkpoint_acceptance.scan == 6

    adjacent = next(
        event
        for event in events
        if event.kind == "bearing_coast_accepted"
        and event.data["bearing_coast_before_value"] == 41
        and event.data["bearing_coast_target_value"] == 50
    )
    assert adjacent.data["bearing_coast_actual_value"] == 50
    assert adjacent.data["bearing_stop_reason"] == "reached"
    assert adjacent.data["agency"] == "program"
    assert adjacent.data["coast_logical_scans"] == 1

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
    plan = PLC(fixture.adjacent_logic, dt=0.010).how(
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
    runner = PLC(fixture.adjacent_logic, dt=0.010)
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


def test_neutral_reconnect_boundary_uses_one_exact_intrascan_pulse() -> None:
    """A retained 98 is consumed alone before Compass considers another steer."""

    boundary = PLC(fixture.adjacent_logic, dt=0.010)
    boundary.step()
    runner = PLC(
        fixture.adjacent_logic,
        dt=0.010,
        initial_state=boundary.state.with_tags(
            {
                fixture.SequenceStep.name: 98,
                fixture.SafetyPermit.name: True,
            }
        ),
    )
    assert runner.state.scan_id == 1
    assert runner.state.tags[fixture.SequenceStep.name] == 98

    events: list[Any] = []
    plan = runner.how(
        fixture.SequenceStep == 10,
        max_scans=30,
        on_event=events.append,
    )

    assert plan.reachable, plan.reason
    assert plan.state.scan_id == 2
    assert plan.state.tags[fixture.SequenceStep.name] == 10
    researched = tuple(event for event in events if event.kind == "intrascan_traceback_researched")
    assert len(researched) == 1
    realization = researched[0].data["boundary_realization"]
    assert realization.direct
    assert not realization.staged
    assert realization.consumer_assignments == ((fixture.NetworkAvailable.name, True),)

    pulse = next(event for event in events if event.kind == "intrascan_pulse")
    accepted = next(event for event in events if event.kind == "intrascan_pulse_accepted")
    assert pulse.data["applied"] == ((fixture.NetworkAvailable.name, True),)
    assert accepted.data["applied"] == pulse.data["applied"]
    assert pulse.data["expected_write"].tag == fixture.SequenceStep.name
    assert (
        pulse.data["expected_write"].before,
        pulse.data["expected_write"].after,
    ) == (98, 10)


def _field_order_reconnect_logic() -> Any:
    """The reconnect rung in the field program's order (the repair branch above
    the status branch), cut to the rungs around the reconnect.

    The status branch reads its conditions when the scan reaches it, so it
    sees the repair's 10 on the same scan.  The pilot used to raise
    "conflicting lifecycle fact" here: a later act that reached the same
    rejected source re-opened its spent theory with a smaller budget.
    """
    from pyrung import Or, Program, branch, copy, count_up, rung

    S = fixture.SequenceStep
    with Program() as logic:
        with rung(S == 80, fixture.SettleDelay.Done):
            copy(81, S, oneshot=True)
        with rung(
            Or(
                fixture.PositionReading < fixture.PositionThreshold,
                fixture.BypassPositionCheck,
                fixture.SequenceInterrupted == 1,
            ),
            S == 81,
        ):
            copy(10, S, oneshot=True)
        with rung(~fixture.InterruptionInput):
            with branch(fixture.BaseSensor):
                copy(10, S, oneshot=True)
            with branch(S < 50, ~fixture.BaseSensor):
                copy(80, S, oneshot=True)
        with rung(~fixture.NetworkAvailable, S <= 20):
            copy(98, S, oneshot=True)
        with rung(fixture.NetworkAvailable):
            with branch(S == 98):
                copy(10, S, oneshot=True)
            with branch(S <= 20):
                with branch(fixture.RecoveryCounter.Done):
                    copy(97, S, oneshot=True)
                with branch(~fixture.RecoveryCounter.Done, fixture.ServiceStatus == 0):
                    copy(94, S, oneshot=True)
        with rung(fixture.CommunicationError):
            count_up(fixture.RecoveryCounter, 60).reset(~fixture.CommunicationError)
        with rung():
            copy(fixture.ServiceStatusInput, fixture.ServiceStatus)
    return logic


def _retained_98(logic: Any) -> PLC:
    boundary = PLC(logic, dt=0.010)
    boundary.step()
    return PLC(
        logic,
        dt=0.010,
        initial_state=boundary.state.with_tags(
            {fixture.SequenceStep.name: 98, fixture.SafetyPermit.name: True}
        ),
    )


def _reconnect_overwrites(events: list[Any]) -> set[tuple[Any, Any, bool]]:
    """What each rejected reconnect saw happen to the step: the value it
    wrote, the value that displaced it, and whether that was the same scan."""
    reconnect = ((fixture.NetworkAvailable.name, True),)
    return {
        (
            observation.appeared.values,
            observation.displacement.values,
            observation.displacement.scan_id == observation.appeared.scan_id,
        )
        for event in events
        if event.kind == "candidate_rejected" and event.data.get("applied") == reconnect
        for observation in event.data.get("effect_observations", ())
        if observation.appeared is not None and observation.displacement is not None
    }


def _assert_reconnect_overwritten_on_its_scan(plan: Any, events: list[Any]) -> None:
    # The pilot tries the reconnect and rejects it each time: the status
    # branch below the repair turns its 10 into 94 on the same scan.  It does
    # not yet try a nonzero ServiceStatusInput, which keeps that branch off,
    # so the search runs out of budget.
    assert _reconnect_overwrites(events) == {((98, 10), (10, 94), True)}
    assert not plan.reachable
    assert plan.reason.startswith("budget exhausted")
    assert "RouteSequenceStep=10 (have 98)" in plan.reason


def test_field_order_reconnect_boundary_does_not_crash() -> None:
    events: list[Any] = []
    plan = _retained_98(_field_order_reconnect_logic()).how(
        fixture.SequenceStep == 10, max_scans=30, on_event=events.append
    )

    _assert_reconnect_overwritten_on_its_scan(plan, events)


def test_field_order_reconnect_overwrite_is_intrascan() -> None:
    """In the field order the status branch, below the repair, sees its 10."""
    runner = _retained_98(fixture.logic)
    runner.patch({fixture.NetworkAvailable.name: True})
    runner.step()

    assert runner.state.tags[fixture.SequenceStep.name] == 94
    events: list[Any] = []
    plan = _retained_98(fixture.logic).how(
        fixture.SequenceStep == 10, max_scans=30, on_event=events.append
    )
    _assert_reconnect_overwritten_on_its_scan(plan, events)


def test_field_order_route_reaches_the_terminal_step() -> None:
    for prestep in (False, True):
        events: list[Any] = []
        runner = PLC(fixture.logic, dt=0.010)
        if prestep:
            runner.step()
        plan = runner.how(fixture.SequenceStep == 81, max_scans=120, on_event=events.append)

        assert plan.reachable, plan.reason
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
        assert reconnect_overwrite.displacement.values == (10, 94)
        assert reconnect_overwrite.displacement.scan_id == reconnect_overwrite.appeared.scan_id
        replay = plan.replay()
        assert replay.state.scan_id == plan.state.scan_id
        assert replay.state.tags[fixture.SequenceStep.name] == 81
