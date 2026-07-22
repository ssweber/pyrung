"""PILOT liveness when complementary contacts require owned dwell."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pyrung import PLC, Bool, Int, Or, Program, Timer, copy, on_delay, out, rung
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot._ops import _coast_holding_state, _set_rungs
from pyrung.core.analysis.pilot.corrections import correct_enablers
from pyrung.core.analysis.pilot.investigate import build_deviation_incident
from pyrung.core.analysis.pilot.pilot import pilot_events
from pyrung.core.analysis.steerable import compute_steerable


def _delayed_rotate_sensor_program() -> tuple[Program, dict[str, object]]:
    """A physical sensor must dwell at each polarity to reset two watchdogs."""
    sensor = Bool("DwellRotateSensor", external=True)
    stable_on = Timer.clone("DwellRotateSensorStableOn")
    stable_off = Timer.clone("DwellRotateSensorStableOff")
    sensor_on_wd = Timer.clone("DwellRotateSensorOnWD")
    sensor_off_wd = Timer.clone("DwellRotateSensorOffWD")
    run = Timer.clone("DwellRotateRun")
    error = Int("DwellRotateError")
    complete = Bool("DwellRotateComplete")

    with Program() as program:
        # These are the input-conditioning owners absent from the Burner
        # fixture today. A one-scan pulse satisfies neither contact.
        with rung(sensor):
            on_delay(stable_on, 30, "ms")
        with rung(~sensor):
            on_delay(stable_off, 30, "ms")

        # Each watchdog needs the opposite stable contact before its boundary.
        # Both failures converge on the same error, just as Rotate does.
        with rung():
            on_delay(sensor_on_wd, 70, "ms").reset(stable_off.Done)
        with rung():
            on_delay(sensor_off_wd, 70, "ms").reset(stable_on.Done)
        with rung(Or(sensor_on_wd.Done, sensor_off_wd.Done)):
            copy(1, error)

        with rung(error == 0):
            on_delay(run, 250, "ms")
        with rung(run.Done):
            out(complete)

    return program, {
        "sensor": sensor,
        "error": error,
        "complete": complete,
    }


def _delayed_rotate_sensor_state_program() -> tuple[Program, dict[str, object]]:
    """State-shaped twin that exercises PILOT's full incident lifecycle."""
    sensor = Bool("DwellPilotRotateSensor", external=True)
    stable_on = Timer.clone("DwellPilotRotateSensorStableOn")
    stable_off = Timer.clone("DwellPilotRotateSensorStableOff")
    sensor_on_wd = Timer.clone("DwellPilotRotateSensorOnWD")
    sensor_off_wd = Timer.clone("DwellPilotRotateSensorOffWD")
    run = Timer.clone("DwellPilotRotateRun")
    state = Int("DwellPilotRotateState", default=6)

    with Program() as program:
        with rung(state == 6, sensor):
            on_delay(stable_on, 30, "ms")
        with rung(state == 6, ~sensor):
            on_delay(stable_off, 30, "ms")
        with rung(state == 6):
            on_delay(sensor_on_wd, 70, "ms").reset(stable_off.Done)
        with rung(state == 6):
            on_delay(sensor_off_wd, 70, "ms").reset(stable_on.Done)
        with rung(state == 6, Or(sensor_on_wd.Done, sensor_off_wd.Done)):
            copy(8, state)
        with rung(state == 6):
            on_delay(run, 250, "ms")
        with rung(state == 6, run.Done):
            copy(7, state)

    return program, {"sensor": sensor, "state": state}


def _context(program: Program, plc: PLC) -> SimpleNamespace:
    pdg = build_program_graph(program)
    return SimpleNamespace(
        pdg=pdg,
        program=program,
        steerable=frozenset(compute_steerable(pdg, plc._known_tags_by_name, program)),
        opaque_loop=frozenset(),
        pipeline_internal_tags=frozenset(),
        route=None,
        compass=SimpleNamespace(action_tags=frozenset()),
    )


def _held_sensor_incident(
    program: Program,
    tags: dict[str, Any],
    polarity: bool,
) -> tuple[PLC, Any]:
    plc = PLC(program, dt=0.010)
    plc.force(tags["sensor"], polarity)
    plc.step()
    anchor = plc.state.scan_id
    before = dict(plc.state.tags)
    for _ in range(10):
        plc.force(tags["sensor"], polarity)
        plc.step()
    assert plc.state.tags[tags["error"].name] == 1
    return plc, build_deviation_incident(
        anchor_scan=anchor,
        end_scan=plc.state.scan_id,
        action=((tags["sensor"].name, polarity),),
        bearing=((tags["complete"].name, True),),
        before_snap=before,
        after_snap=dict(plc.state.tags),
    )


def test_delayed_rotate_sensor_rejects_scan_oscillation_but_accepts_dwell() -> None:
    """The fixture distinguishes lucky pulses from a real stable contact."""
    program, tags = _delayed_rotate_sensor_program()

    lucky = PLC(program, dt=0.010)
    for scan in range(40):
        lucky.force(tags["sensor"], scan % 2 == 0)
        lucky.step()
    assert lucky.state.tags[tags["error"].name] == 1
    assert lucky.state.tags[tags["complete"].name] is False

    ground_truth = PLC(program, dt=0.010)
    for scan in range(40):
        ground_truth.force(tags["sensor"], (scan // 4) % 2 == 0)
        ground_truth.step()
    assert ground_truth.state.tags[tags["error"].name] == 0
    assert ground_truth.state.tags[tags["complete"].name] is True


def test_pilot_watchdog_corrections_compose_into_alternating_owned_dwell() -> None:
    """Independent contact corrections retain enough ownership to work together."""
    program, tags = _delayed_rotate_sensor_program()

    corrections = []
    for polarity in (False, True):
        plc, incident = _held_sensor_incident(program, tags, polarity)
        hypotheses = [
            hypothesis
            for hypothesis in correct_enablers(plc, incident, _context(program, plc))
            if hypothesis.kind == "liveness"
        ]
        assert len(hypotheses) == 1
        corrections.extend(hypotheses[0].holds)

    assert {(rung.dest, rung.value) for rung in corrections} == {
        (tags["sensor"].name, False),
        (tags["sensor"].name, True),
    }

    replay = PLC(program, dt=0.010)
    replay.step()
    _set_rungs(replay, corrections)
    receipt = _coast_holding_state(
        replay,
        tags["complete"].name,
        True,
        (),
        budget=100,
    )
    assert receipt.reached is True
    assert replay.state.tags[tags["error"].name] == 0


def test_full_pilot_learns_complementary_dwell_as_separate_local_incidents() -> None:
    """One bounded correction per watchdog composes on the live retry path."""
    program, tags = _delayed_rotate_sensor_state_program()
    plc = PLC(program, dt=0.010)
    plc.step()

    events = tuple(
        pilot_events(
            plc,
            tags["state"] == 7,
            max_scans=2_000,
        )
    )
    finished = [event for event in events if event.kind == "finished"]
    assert len(finished) == 1
    assert finished[0].data["reached"] is True
    assert finished[0].data["work"].state.tags[tags["state"].name] == 7

    confirmed = [
        detail
        for event in events
        if event.kind == "trend_regression"
        for detail in event.data["investigation"]["confirmed_detail"]
    ]
    assert [detail["kind"] for detail in confirmed] == ["liveness", "liveness"]
    assert {(hold.dest, hold.value) for detail in confirmed for hold in detail["holds"]} == {
        (tags["sensor"].name, False),
        (tags["sensor"].name, True),
    }
    assert not any(
        rejection["slug"] == "sibling-regression"
        for event in events
        if event.kind == "trend_regression"
        for rejection in event.data["investigation"]["rejected_detail"]
    )
