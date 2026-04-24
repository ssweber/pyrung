"""Conveyor sorting station with Physical annotations and autoharness.

Same conveyor logic as click_conveyor.py, but tags carry physical metadata:
  - Physical feedback declarations (sensor timing, analog profiles)
  - min/max/uom operating ranges
  - Tag flags (readonly, external, final, public)
  - The autoharness synthesizes feedback patches automatically

Run under DAP to see autoharness feedback, tag flag badges, and
runtime bounds checking in action.
"""

from typing import Any, cast

from pyrung import (
    PLC,
    And,
    Bool,
    Counter,
    Field,
    Int,
    Or,
    Physical,
    Rung,
    Timer,
    branch,
    comment,
    copy,
    count_up,
    latch,
    named_array,
    on_delay,
    out,
    program,
    reset,
    rise,
    udt,
)
from pyrung.click import TagMap, c, ct, ctd, ds, t, td, x, y

# ---------------------------------------------------------------------------
# Structured types — UDTs with physical annotations
# ---------------------------------------------------------------------------


@udt()
class ConveyorIO:
    Motor: Bool = Field(public=True)
    MotorFb: Bool = Field(
        external=True,
        physical=Physical("MotorFb", on_delay="500ms", off_delay="200ms"),
        link="Motor",
    )
    StatusLight: Bool = Field(public=True)
    Diverter: Bool = Field(public=True)
    DiverterFb: Bool = Field(
        external=True,
        physical=Physical("DiverterFb", on_delay="100ms", off_delay="100ms"),
        link="Diverter",
    )


ConveyorIO = cast(Any, ConveyorIO)
conv = ConveyorIO.clone("Conv")


@udt()
class SizeAnalog:
    Reading: Int = Field(min=0, max=4095, uom="counts")
    Threshold: Int = Field(default=100, public=True, min=0, max=4095, uom="counts")


SizeAnalog = cast(Any, SizeAnalog)
Size = SizeAnalog.clone("Size")


# ---------------------------------------------------------------------------
# Tags — inputs
# ---------------------------------------------------------------------------
StartBtn = Bool("StartBtn", public=True)
StopBtn = Bool("StopBtn", public=True)
EstopOK = Bool("EstopOK", public=True, external=True)
Auto = Bool("Auto", public=True)
Manual = Bool("Manual", public=True)
EntrySensor = Bool("EntrySensor")
DiverterBtn = Bool("DiverterBtn", public=True)
BinASensor = Bool("BinASensor")
BinBSensor = Bool("BinBSensor")

# ---------------------------------------------------------------------------
# Tags — internal
# ---------------------------------------------------------------------------
Running = Bool("Running", public=True)
IsLarge = Bool("IsLarge")
CountReset = Bool("CountReset", public=True)


@named_array(Int, stride=4, readonly=True)
class SortState:
    IDLE = 0
    DETECTING = 1
    SORTING = 2
    RESETTING = 3


SortState = cast(Any, SortState)

State = Int("State", choices=SortState, public=True)

# Timers
DetTimer = Timer.clone("DetTimer")
HoldTimer = Timer.clone("HoldTimer")

# Counters
BinACounter = Counter.clone("BinACounter", public=True)
BinBCounter = Counter.clone("BinBCounter", public=True)

# ---------------------------------------------------------------------------
# Click hardware mapping
# ---------------------------------------------------------------------------
mapping = TagMap(
    [
        # Inputs
        StartBtn.map_to(x[1]),
        StopBtn.map_to(x[2]),
        EstopOK.map_to(x[3]),
        Auto.map_to(x[4]),
        Manual.map_to(x[5]),
        EntrySensor.map_to(x[6]),
        DiverterBtn.map_to(x[7]),
        BinASensor.map_to(x[8]),
        BinBSensor.map_to(x[9]),
        # Outputs
        conv.Motor.map_to(y[1]),
        conv.Diverter.map_to(y[2]),
        conv.StatusLight.map_to(y[3]),
        # Internal relays
        Running.map_to(c[1]),
        IsLarge.map_to(c[2]),
        CountReset.map_to(c[3]),
        # Feedback (mapped to available input points)
        conv.MotorFb.map_to(x[10]),
        conv.DiverterFb.map_to(x[11]),
        # State constants
        *SortState.map_to(ds.select(1, 4)),
        # Data
        State.map_to(ds[5]),
        Size.Reading.map_to(ds[6]),
        Size.Threshold.map_to(ds[7]),
        # Timers
        DetTimer.Done.map_to(t[1]),
        DetTimer.Acc.map_to(td[1]),
        HoldTimer.Done.map_to(t[2]),
        HoldTimer.Acc.map_to(td[2]),
        # Counters
        BinACounter.Done.map_to(ct[1]),
        BinACounter.Acc.map_to(ctd[1]),
        BinBCounter.Done.map_to(ct[2]),
        BinBCounter.Acc.map_to(ctd[2]),
    ],
    include_system=False,
)

# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


@program
def logic():
    comment("Start/stop — NC stop button resets when pressed or wire broken")
    with Rung(StartBtn, Or(Auto, Manual)):
        latch(Running)
    with Rung(~StopBtn):
        reset(Running)
    with Rung(~EstopOK):
        reset(Running)

    comment("Motor output — EstopOK gates all outputs")
    with Rung(EstopOK):
        with branch(Running):
            out(conv.Motor)
        with branch(Running):
            out(conv.StatusLight)

    comment("Sort state machine — IDLE to DETECTING: box arrives")
    with Rung(State == SortState.IDLE, rise(EntrySensor)):
        copy(SortState.DETECTING, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == SortState.DETECTING):
        on_delay(DetTimer, 500)
    with Rung(State == SortState.DETECTING, Size.Reading > Size.Threshold):
        latch(IsLarge)
    with Rung(DetTimer.Done):
        copy(SortState.SORTING, State)

    comment("SORTING: hold diverter for 2 seconds")
    with Rung(State == SortState.SORTING):
        on_delay(HoldTimer, 2000)
    with Rung(HoldTimer.Done):
        copy(SortState.RESETTING, State)

    comment("RESETTING: clean up and return to idle")
    with Rung(State == SortState.RESETTING):
        reset(IsLarge)
        copy(SortState.IDLE, State)

    comment("Diverter output — auto sort OR manual button, gated by EstopOK")
    with Rung(
        EstopOK,
        Or(
            And(State == SortState.SORTING, IsLarge, Auto),
            And(Manual, DiverterBtn),
        ),
    ):
        out(conv.Diverter)

    comment("Bin counters")
    with Rung(rise(BinASensor)):
        count_up(BinACounter, preset=9999).reset(CountReset)
    with Rung(rise(BinBSensor)):
        count_up(BinBCounter, preset=9999).reset(CountReset)


# ---------------------------------------------------------------------------
# Simulation — autoharness synthesizes feedback
# ---------------------------------------------------------------------------


import pytest

from pyrung.core.analysis.simplified import expr_requires, reset_dominance


def test_conv_motor_implies_running():
    # Conv_Motor => Running [dt=0.01]
    plc = PLC(logic, dt=0.01)
    forms = plc.program.simplified()
    assert "Conv_Motor" in forms
    assert expr_requires(forms["Conv_Motor"].expr, "Running")


@pytest.mark.skip(reason="observed in trace, not structurally provable")
def test_running_implies_conv_motor():
    # Running => Conv_Motor [dt=0.01]
    pass


def test_conv_motor_implies_estopok():
    # Conv_Motor => EstopOK [dt=0.01]
    plc = PLC(logic, dt=0.01)
    forms = plc.program.simplified()
    assert "Conv_Motor" in forms
    assert expr_requires(forms["Conv_Motor"].expr, "EstopOK")


def test_running_implies_estopok():
    # Running => EstopOK [dt=0.01]
    plc = PLC(logic, dt=0.01)
    assert reset_dominance(plc.program, "Running", "EstopOK")


def test_conv_statuslight_implies_running():
    # Conv_StatusLight => Running [dt=0.01]
    plc = PLC(logic, dt=0.01)
    forms = plc.program.simplified()
    assert "Conv_StatusLight" in forms
    assert expr_requires(forms["Conv_StatusLight"].expr, "Running")


@pytest.mark.skip(reason="observed in trace, not structurally provable")
def test_running_implies_conv_statuslight():
    # Running => Conv_StatusLight [dt=0.01]
    pass


@pytest.mark.skip(reason="observed in trace, not structurally provable")
def test_conv_diverter_implies_islarge():
    # Conv_Diverter => IsLarge [dt=0.01]
    pass
