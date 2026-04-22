"""Conveyor sorting station — the tutorial's progressive project as a Click example.

Demonstrates the full pyrung -> Click workflow:
  1. Start/stop motor control with NC stop button convention
  2. E-stop safety gating via EstopOK permission input
  3. State-machine sort sequence (idle -> detecting -> sorting -> resetting)
  4. Auto/manual mode with branch-based diverter control
  5. Edge-triggered bin counters
  6. TagMap linking logical tags to Click hardware addresses
  7. Simulation with PLC in FIXED_STEP mode

This is the completed version of the conveyor built across the
"Know Python? Learn Ladder Logic" tutorial.  See ``devtools/build_release_assets.py``.
"""

import os
from typing import Any, cast

from pyrung import (
    PLC,
    And,
    Bool,
    Counter,
    Int,
    Or,
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
)
from pyrung.click import TagMap, c, ct, ctd, ds, t, td, x, y

# ---------------------------------------------------------------------------
# Tags — inputs
# ---------------------------------------------------------------------------
StartBtn = Bool("StartBtn", public=True)  # X001 — NO momentary start button
StopBtn = Bool("StopBtn", public=True)  # X002 — NC stop button (healthy at rest)
EstopOK = Bool("EstopOK", public=True)  # X003 — NC safety relay permission contact
Auto = Bool("Auto", public=True)  # X004 — auto mode selector
Manual = Bool("Manual", public=True)  # X005 — manual mode selector
EntrySensor = Bool("EntrySensor")  # X006 — photo-eye at conveyor entry
DiverterBtn = Bool("DiverterBtn", public=True)  # X007 — manual diverter button
BinASensor = Bool("BinASensor")  # X008 — small-box bin exit sensor
BinBSensor = Bool("BinBSensor")  # X009 — large-box bin exit sensor

# ---------------------------------------------------------------------------
# Tags — outputs
# ---------------------------------------------------------------------------
ConveyorMotor = Bool("ConveyorMotor", public=True)  # Y001 — motor contactor
DiverterCmd = Bool("DiverterCmd", public=True)  # Y002 — diverter solenoid
StatusLight = Bool("StatusLight", public=True)  # Y003 — running indicator

# ---------------------------------------------------------------------------
# Tags — internal
# ---------------------------------------------------------------------------
Running = Bool("Running", public=True)  # C001 — motor run latch
IsLarge = Bool("IsLarge")  # C002 — size classification result
CountReset = Bool("CountReset", public=True)  # C003 — counter reset button

# State constants — read-only named array, never written
@named_array(Int, stride=4, readonly=True)
class SortState:
    IDLE = 0
    DETECTING = 1
    SORTING = 2
    RESETTING = 3

SortState = cast(Any, SortState)

State = Int("State", choices=SortState, public=True)  # DS005 — sort sequence state

SizeReading = Int("SizeReading")  # DS006 — analog size sensor value
SizeThreshold = Int("SizeThreshold", public=True)  # DS007 — small/large cutoff

# Timers — detection and diverter hold
DetTimer = Timer.clone("DetTimer")  # T001 / TD001
HoldTimer = Timer.clone("HoldTimer")  # T002 / TD002

# Counters — per bin
BinACounter = Counter.clone("BinACounter", public=True)  # CT001 / CTD001
BinBCounter = Counter.clone("BinBCounter", public=True)  # CT002 / CTD002

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
        ConveyorMotor.map_to(y[1]),
        DiverterCmd.map_to(y[2]),
        StatusLight.map_to(y[3]),
        # Internal relays
        Running.map_to(c[1]),
        IsLarge.map_to(c[2]),
        CountReset.map_to(c[3]),
        # State constants
        *SortState.map_to(ds.select(1, 4)),
        # Data
        State.map_to(ds[5]),
        SizeReading.map_to(ds[6]),
        SizeThreshold.map_to(ds[7]),
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
            out(ConveyorMotor)
        with branch(Running):
            out(StatusLight)

    comment("Sort state machine — IDLE to DETECTING: box arrives")
    with Rung(State == SortState.IDLE, rise(EntrySensor)):
        copy(SortState.DETECTING, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == SortState.DETECTING):
        on_delay(DetTimer, 500)
    with Rung(State == SortState.DETECTING, SizeReading > SizeThreshold):
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
        out(DiverterCmd)

    comment("Bin counters")
    with Rung(rise(BinASensor)):
        count_up(BinACounter, preset=9999).reset(CountReset)
    with Rung(rise(BinBSensor)):
        count_up(BinBCounter, preset=9999).reset(CountReset)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
runner = PLC(logic, dt=0.010)

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    with runner:
        # NC inputs: True simulates healthy wiring
        StopBtn.value = True
        EstopOK.value = True
        Auto.value = True
        SizeThreshold.value = 100

        # Momentary start press
        StartBtn.value = True
        runner.step()
        StartBtn.value = False

    # Simulate a large box arriving
    runner.force(EntrySensor, True)
    runner.force(SizeReading, 150)
    runner.run(cycles=300)  # Through detection + sorting
    runner.unforce(EntrySensor)
    runner.unforce(SizeReading)
    runner.run(cycles=10)

    # Report
    with runner:
        print(f"Motor     : {'ON' if ConveyorMotor.value else 'OFF'}")
        print(f"State     : {State.value!r}")
        print(f"Diverter  : {'EXTENDED' if DiverterCmd.value else 'retracted'}")
        print(f"IsLarge   : {IsLarge.value}")
        print(f"Bin A     : {BinACounter.Acc.value} boxes")
        print(f"Bin B     : {BinBCounter.Acc.value} boxes")
