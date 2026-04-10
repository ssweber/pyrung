"""Lesson 10: Testing — docs/learn/testing.md

Requires pytest: uv run pytest examples/learn/testing.py
"""

import pytest

from pyrung import (
    PLC,
    And,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    branch,
    comment,
    copy,
    latch,
    on_delay,
    out,
    reset,
    rise,
)

# -- Tags (from lessons 7-9) --

IDLE = Int("IDLE", default=0)
DETECTING = Int("DETECTING", default=1)
SORTING = Int("SORTING", default=2)
RESETTING = Int("RESETTING", default=3)

State = Int("State")

EntrySensor = Bool("EntrySensor")
SizeReading = Int("SizeReading")
SizeThreshold = Int("SizeThreshold")

IsLarge = Bool("IsLarge")
DetTimer = Timer.clone("DetTimer")
HoldTimer = Timer.clone("HoldTimer")

Auto = Bool("Auto")
Manual = Bool("Manual")
StopBtn = Bool("StopBtn")
StartBtn = Bool("StartBtn")
EstopOK = Bool("EstopOK")
Running = Bool("Running")
DiverterBtn = Bool("DiverterBtn")
DiverterCmd = Bool("DiverterCmd")
ConveyorMotor = Bool("ConveyorMotor")
StatusLight = Bool("StatusLight")

# -- Program (combines lessons 7-8) --

with Program() as logic:
    # Start/stop (lesson 8)
    comment("Start/stop — NC stop resets when pressed or wire broken")
    with Rung(StartBtn, Or(Auto, Manual)):
        latch(Running)
    with Rung(~StopBtn):
        reset(Running)
    with Rung(~EstopOK):
        reset(Running)

    # State machine (lesson 7)
    comment("IDLE to DETECTING: box arrives")
    with Rung(State == IDLE, rise(EntrySensor)):
        copy(DETECTING, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == DETECTING):
        on_delay(DetTimer, preset=500, unit="Tms")
    with Rung(State == DETECTING, SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetTimer.Done):
        copy(SORTING, State)

    comment("SORTING: hold diverter for 2 seconds")
    with Rung(State == SORTING):
        on_delay(HoldTimer, preset=2000, unit="Tms")
    with Rung(HoldTimer.Done):
        copy(RESETTING, State)

    comment("RESETTING: clean up and return to idle")
    with Rung(State == RESETTING):
        reset(IsLarge)
        copy(IDLE, State)

    # Outputs (lesson 8)
    comment("Motor output — EstopOK gates all outputs")
    with Rung(EstopOK):
        with branch(Running):
            out(ConveyorMotor)
        with branch(Running):
            out(StatusLight)

    comment("Diverter output — auto sort OR manual button, gated by EstopOK")
    with Rung(
        EstopOK,
        Or(
            And(State == SORTING, IsLarge, Auto),
            And(Manual, DiverterBtn),
        ),
    ):
        out(DiverterCmd)


# -- Fixture --


@pytest.fixture
def plc():
    r = PLC(logic, dt=0.010)
    r.force(StopBtn, True)  # NC inputs: healthy wiring
    r.force(EstopOK, True)
    r.force(Auto, True)  # Default to auto mode
    return r


# -- Tests --


def test_start_stop(plc):
    with plc:
        StartBtn.value = True
        plc.step()
        StartBtn.value = False
        plc.step()
        assert Running.value is True
        assert ConveyorMotor.value is True


def test_estop_overrides_start(plc):
    """Safety: E-stop kills everything, even if Start is held."""
    plc.unforce(EstopOK)
    with plc:
        EstopOK.value = False
        StartBtn.value = True
        plc.step()
        assert Running.value is False
        assert ConveyorMotor.value is False


def test_small_vs_large_box(plc):
    """Same setup, two outcomes."""
    with plc:
        SizeThreshold.value = 100
        StartBtn.value = True
        plc.step()
        EntrySensor.value = True
        plc.step()

    # Fork: large box — run past detection, check mid-sorting
    large = plc.fork()
    large.force(SizeReading, 150)
    with large:
        large.run(cycles=50)
        assert State.value == 2  # SORTING
        assert DiverterCmd.value is True

    # Fork: small box
    small = plc.fork()
    small.force(SizeReading, 50)
    with small:
        small.run(cycles=50)
        assert State.value == 2
        assert DiverterCmd.value is False


@pytest.mark.parametrize(
    "box_size,expected_diverter",
    [
        (50, False),  # small
        (150, True),  # large
        (99, False),  # boundary, just under
        (100, False),  # boundary, exactly at threshold
        (101, True),  # boundary, just over
    ],
)
def test_box_classification(plc, box_size, expected_diverter):
    with plc:
        SizeThreshold.value = 100
        StartBtn.value = True
        plc.step()

        plc.force(EntrySensor, True)
        plc.force(SizeReading, box_size)
        plc.run(cycles=55)  # Past detection, mid-sorting
        assert DiverterCmd.value is expected_diverter


def test_sorting_sequence(plc):
    """Full auto sort: box arrives, gets classified, exits to correct bin."""
    with plc:
        SizeThreshold.value = 100
        StartBtn.value = True
        plc.step()

        plc.force(EntrySensor, True)
        plc.force(SizeReading, 150)  # Large box

        # Run past detection period into sorting
        plc.run(cycles=55)
        assert DiverterCmd.value is True  # Extended for large box

        plc.unforce(EntrySensor)
        plc.run(cycles=250)  # Past hold period
        assert DiverterCmd.value is False  # Retracted after sort
        assert State.value == 0  # Back to idle
