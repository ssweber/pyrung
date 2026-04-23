"""Conveyor sorting station with Physical annotations and autoharness.

Same conveyor logic as click_conveyor.py, but tags carry physical metadata:
  - Physical feedback declarations (sensor timing, analog profiles)
  - min/max/uom operating ranges
  - Tag flags (readonly, external, final, public)
  - The autoharness synthesizes feedback patches automatically

Run under DAP to see autoharness feedback, tag flag badges, and
runtime bounds checking in action.
"""

import os
from typing import Any, cast

from pyrung import (
    And,
    Bool,
    Counter,
    Field,
    Harness,
    Int,
    Or,
    Physical,
    PLC,
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
runner = PLC(logic, dt=0.010)

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    harness = Harness(runner)
    harness.install()

    with runner:
        # NC inputs: True simulates healthy wiring
        StopBtn.value = True
        EstopOK.value = True
        Auto.value = True
        Size.Threshold.value = 100

        # Momentary start press
        StartBtn.value = True
        runner.step()
        StartBtn.value = False

    # Let autoharness deliver motor feedback
    runner.run(cycles=60)

    # Simulate a large box arriving
    runner.force(EntrySensor, True)
    runner.force(Size.Reading, 150)
    runner.run(cycles=300)
    runner.unforce(EntrySensor)
    runner.unforce(Size.Reading)
    runner.run(cycles=10)

    with runner:
        print(f"Motor     : {'ON' if conv.Motor.value else 'OFF'}")
        print(f"Motor Fb  : {'ON' if conv.MotorFb.value else 'OFF'}")
        print(f"State     : {State.value!r}")
        print(f"Diverter  : {'EXTENDED' if conv.Diverter.value else 'retracted'}")
        print(f"Diverter Fb: {'ON' if conv.DiverterFb.value else 'OFF'}")
        print(f"IsLarge   : {IsLarge.value}")
        print(f"Bin A     : {BinACounter.Acc.value} boxes")
        print(f"Bin B     : {BinBCounter.Acc.value} boxes")

    violations = runner.bounds_violations
    if violations:
        print(f"\nBounds violations: {violations}")
