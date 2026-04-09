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

from pyrung import (
    Bool,
    Dint,
    Int,
    PLC,
    Rung,

    all_of,
    any_of,
    branch,
    comment,
    copy,
    count_up,
    latch,
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
StartBtn = Bool("StartBtn")  # X001 — NO momentary start button
StopBtn = Bool("StopBtn")  # X002 — NC stop button (healthy at rest)
EstopOK = Bool("EstopOK")  # X003 — NC safety relay permission contact
Auto = Bool("Auto")  # X004 — auto mode selector
Manual = Bool("Manual")  # X005 — manual mode selector
EntrySensor = Bool("EntrySensor")  # X006 — photo-eye at conveyor entry
DiverterBtn = Bool("DiverterBtn")  # X007 — manual diverter button
BinASensor = Bool("BinASensor")  # X008 — small-box bin exit sensor
BinBSensor = Bool("BinBSensor")  # X009 — large-box bin exit sensor

# ---------------------------------------------------------------------------
# Tags — outputs
# ---------------------------------------------------------------------------
ConveyorMotor = Bool("ConveyorMotor")  # Y001 — motor contactor
DiverterCmd = Bool("DiverterCmd")  # Y002 — diverter solenoid
StatusLight = Bool("StatusLight")  # Y003 — running indicator

# ---------------------------------------------------------------------------
# Tags — internal
# ---------------------------------------------------------------------------
Running = Bool("Running")  # C001 — motor run latch
IsLarge = Bool("IsLarge")  # C002 — size classification result
CountReset = Bool("CountReset")  # C003 — counter reset button

# State constants — initialized once, never written
IDLE = Int("IDLE", default=0)
DETECTING = Int("DETECTING", default=1)
SORTING = Int("SORTING", default=2)
RESETTING = Int("RESETTING", default=3)

State = Int("State")  # DS005 — sort sequence state

SizeReading = Int("SizeReading")  # DS006 — analog size sensor value
SizeThreshold = Int("SizeThreshold")  # DS007 — small/large cutoff

# Timers — detection and diverter hold
DetDone = Bool("DetDone")  # T001
DetAcc = Int("DetAcc")  # TD001
HoldDone = Bool("HoldDone")  # T002
HoldAcc = Int("HoldAcc")  # TD002

# Counters — per bin
BinADone = Bool("BinADone")  # CT001
BinAAcc = Dint("BinAAcc")  # CTD001
BinBDone = Bool("BinBDone")  # CT002
BinBAcc = Dint("BinBAcc")  # CTD002

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
        IDLE.map_to(ds[1]),
        DETECTING.map_to(ds[2]),
        SORTING.map_to(ds[3]),
        RESETTING.map_to(ds[4]),
        # Data
        State.map_to(ds[5]),
        SizeReading.map_to(ds[6]),
        SizeThreshold.map_to(ds[7]),
        # Timers
        DetDone.map_to(t[1]),
        DetAcc.map_to(td[1]),
        HoldDone.map_to(t[2]),
        HoldAcc.map_to(td[2]),
        # Counters
        BinADone.map_to(ct[1]),
        BinAAcc.map_to(ctd[1]),
        BinBDone.map_to(ct[2]),
        BinBAcc.map_to(ctd[2]),
    ],
    include_system=False,
)

# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------


@program
def logic():
    comment("Start/stop — NC stop button resets when pressed or wire broken")
    with Rung(StartBtn, any_of(Auto, Manual)):
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
    with Rung(State == IDLE, rise(EntrySensor)):
        copy(DETECTING, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == DETECTING):
        on_delay(DetDone, DetAcc, preset=500, unit="Tms")
    with Rung(State == DETECTING, SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetDone):
        copy(SORTING, State)

    comment("SORTING: hold diverter for 2 seconds")
    with Rung(State == SORTING):
        on_delay(HoldDone, HoldAcc, preset=2000, unit="Tms")
    with Rung(HoldDone):
        copy(RESETTING, State)

    comment("RESETTING: clean up and return to idle")
    with Rung(State == RESETTING):
        reset(IsLarge)
        copy(IDLE, State)

    comment("Diverter output — auto sort OR manual button, gated by EstopOK")
    with Rung(
        EstopOK,
        any_of(
            all_of(State == SORTING, IsLarge, Auto),
            all_of(Manual, DiverterBtn),
        ),
    ):
        out(DiverterCmd)

    comment("Bin counters")
    with Rung(rise(BinASensor)):
        count_up(BinADone, BinAAcc, preset=9999).reset(CountReset)
    with Rung(rise(BinBSensor)):
        count_up(BinBDone, BinBAcc, preset=9999).reset(CountReset)


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
        print(f"Bin A     : {BinAAcc.value} boxes")
        print(f"Bin B     : {BinBAcc.value} boxes")
