"""A moving process boundary is renegotiated from ordinary PILOT evidence."""

from __future__ import annotations

from typing import Any

from pyrung import PLC, Int, Program, Timer, calc, copy, on_delay, rung
from pyrung.core.analysis.pilot.pilot import pilot_events


def _oven_ramp_program() -> tuple[Program, dict[str, Any]]:
    """Advance the setpoint one stair only after the process catches up."""
    pv = Int("RampPvTemp", external=True, min=0, max=300, default=20)
    sv = Int("RampSvTemp", default=20)
    step = Int("RampStep")
    tick = Timer.clone("RampTick")
    tracking = Timer.clone("RampTrackingWD")
    state = Int("RampState", default=0)

    with Program() as program:
        with rung(state == 0, step < 5):
            on_delay(tick, 20, "ms")
        with rung(state == 0, tick.Done, pv >= sv, step < 5):
            calc(sv + 10, sv)
            calc(step + 1, step)
            copy(0, tick.Acc)

        # Fault is the only externally visible symptom of falling behind.  The
        # target route does not expose the live tracking predicate, and the
        # test gives PILOT no ``PV := SV`` repair or relational hold.
        with rung(state == 0, pv < sv):
            on_delay(tracking, 40, "ms")
        with rung(state == 0, tracking.Done):
            copy(2, state)
        with rung(state == 0, step >= 5, pv >= sv):
            copy(1, state)

    return program, {
        "pv": pv,
        "sv": sv,
        "step": step,
        "state": state,
    }


def test_pilot_renegotiates_a_moving_process_boundary() -> None:
    """A stale concrete PV correction becomes a normal new investigation.

    Every accepted PV value permits exactly one ramp stair.  The next stair
    makes that value stale, so PILOT must keep using ordinary progress and
    departure evidence rather than installing a dynamic predicate operation.
    """
    program, tags = _oven_ramp_program()
    plc = PLC(program, dt=0.010)

    events = list(pilot_events(plc, tags["state"] == 1, max_scans=3000))
    finished = next(event for event in events if event.kind == "finished")

    assert finished.data["reached"], finished.data.get("reason")
    final = finished.data["work"].state.tags
    assert final[tags["step"].name] == 5
    assert final[tags["pv"].name] >= final[tags["sv"].name]
    assert final[tags["state"].name] == 1

    pv_corrections = [
        rung
        for event in events
        if event.kind == "trend_regression"
        for rung in event.data["pilot_rungs"]
        if rung.dest == tags["pv"].name
    ]
    assert len({rung.value for rung in pv_corrections}) >= 2, pv_corrections
    assert all(rung.operation is None for rung in pv_corrections)
    # Each concrete PV fact expires with its incident scope. It need not be
    # globally revoked merely because the moving boundary later asks for a new
    # concrete value.
