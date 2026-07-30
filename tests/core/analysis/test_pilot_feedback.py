"""PILOT coverage for physical feedback patterns.

Bool on_delay, bool off_delay, and profile-driven analog ramp through
the Harness.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Real, Rung, copy
from pyrung.core.analysis.pilot import pilot_how
from pyrung.core.physical import Physical, Ramp
from pyrung.core.runner import PLC

MOTOR_FB = Physical("MotorFb", on_delay="200ms", off_delay="100ms")
VALVE_FB = Physical("ValveFb", on_delay="50ms", off_delay="50ms")
SENSOR = Physical("TempSensor", profile=Ramp(up=1.0, down=-0.5))


# ---------------------------------------------------------------------------
# Bool on_delay: enable -> feedback delayed -> interlock clears
# ---------------------------------------------------------------------------


def _on_delay_program():
    Enable = Bool("Enable", external=True)
    Feedback = Bool("Feedback", physical=MOTOR_FB, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Feedback):
            copy(1, Stage)
    return prog, Stage, Enable, Feedback


def test_on_delay_premise() -> None:
    from pyrung.core.harness import Harness

    prog, Stage, _Enable, _Feedback = _on_delay_program()
    plc = PLC(prog, dt=0.010)
    harness = Harness(plc)
    harness.install()

    plc.patch({"Enable": True})
    for _ in range(25):
        plc.step()
    assert plc.state.tags["Stage"] == 1


def test_on_delay_solves() -> None:
    prog, Stage, _Enable, _Feedback = _on_delay_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Stage == 1, max_scans=3000)
    assert path.reachable


# ---------------------------------------------------------------------------
# Bool off_delay: de-energize -> feedback drops delayed -> gate clears
# ---------------------------------------------------------------------------


def _off_delay_program():
    Enable = Bool("Enable", external=True, default=True)
    Feedback = Bool("Feedback", physical=MOTOR_FB, link="Enable", default=True)
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(~Enable, ~Feedback):
            copy(1, Stage)
    return prog, Stage, Enable, Feedback


def test_off_delay_premise() -> None:
    from pyrung.core.harness import Harness

    prog, Stage, _Enable, _Feedback = _off_delay_program()
    plc = PLC(prog, dt=0.010)
    harness = Harness(plc)
    harness.install()

    plc.patch({"Enable": False})
    for _ in range(15):
        plc.step()
    assert plc.state.tags["Stage"] == 1


def test_off_delay_solves() -> None:
    prog, Stage, _Enable, _Feedback = _off_delay_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Stage == 1, max_scans=3000)
    assert path.reachable


# ---------------------------------------------------------------------------
# Profile: analog ramp to comparison threshold
# ---------------------------------------------------------------------------


def _profile_program():
    Enable = Bool("Enable", external=True)
    Temp = Real("Temp", physical=SENSOR, link="Enable")
    Stage = Int("Stage")
    with Program() as prog:
        with Rung(Enable, Temp >= 5.0):
            copy(1, Stage)
    return prog, Stage, Enable, Temp


def test_profile_premise() -> None:
    from pyrung.core.harness import Harness

    prog, Stage, _Enable, _Temp = _profile_program()
    plc = PLC(prog, dt=0.010)
    harness = Harness(plc)
    harness.install()

    plc.patch({"Enable": True})
    for _ in range(600):
        plc.step()
    assert plc.state.tags["Stage"] == 1


def test_profile_solves() -> None:
    prog, Stage, _Enable, _Temp = _profile_program()
    plc = PLC(prog, dt=0.010)
    path = pilot_how(plc, Stage == 1, max_scans=3000)
    assert path.reachable
