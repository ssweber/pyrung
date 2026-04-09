"""Conveyor sorting station — CircuitPython port of click_conveyor for P1AM-200.

Same logic as click_conveyor.py, targeting P1AM hardware instead of Click PLC:
  1. Start/stop motor control with NC stop button convention
  2. E-stop safety gating via EstopOK permission input
  3. State-machine sort sequence (idle -> detecting -> sorting -> resetting)
  4. Auto/manual mode with branch-based diverter control
  5. Edge-triggered bin counters
  6. Code generation with force_runtime for starter bundle

Hardware:
  Slot 1: P1-16ND3  (16-ch discrete input, 24V sink)
  Slot 2: P1-08TRS  (8-ch relay output)
"""

import os

from pyrung import (
    Bool,
    Dint,
    Int,
    Program,
    Rung,

    And,
    Or,
    branch,
    comment,
    copy,
    count_up,
    latch,
    on_delay,
    out,
    reset,
    rise,
)
from pyrung.circuitpy import P1AM, RunStopConfig, generate_circuitpy

# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------
hw = P1AM()
inputs = hw.slot(1, "P1-16ND3")  # 16-ch discrete input (24V sink)
outputs = hw.slot(2, "P1-08TRS")  # 8-ch relay output

# Inputs (channels 1-9)
StartBtn = inputs[1]  # NO momentary start button
StopBtn = inputs[2]  # NC stop button (healthy at rest)
EstopOK = inputs[3]  # NC safety relay permission contact
Auto = inputs[4]  # auto mode selector
Manual = inputs[5]  # manual mode selector
EntrySensor = inputs[6]  # photo-eye at conveyor entry
DiverterBtn = inputs[7]  # manual diverter button
BinASensor = inputs[8]  # small-box bin exit sensor
BinBSensor = inputs[9]  # large-box bin exit sensor

# Outputs (channels 1-3)
ConveyorMotor = outputs[1]  # motor contactor
DiverterCmd = outputs[2]  # diverter solenoid
StatusLight = outputs[3]  # running indicator

# ---------------------------------------------------------------------------
# Tags — internal
# ---------------------------------------------------------------------------
Running = Bool("Running")  # motor run latch
IsLarge = Bool("IsLarge")  # size classification result
CountReset = Bool("CountReset")  # counter reset

# State constants — initialized once, never written
IDLE = Int("IDLE", default=0)
DETECTING = Int("DETECTING", default=1)
SORTING = Int("SORTING", default=2)
RESETTING = Int("RESETTING", default=3)

State = Int("State")  # sort sequence state

SizeReading = Int("SizeReading")  # analog size sensor value
SizeThreshold = Int("SizeThreshold")  # small/large cutoff

# Timers — detection and diverter hold
DetDone = Bool("DetDone")
DetAcc = Int("DetAcc")
HoldDone = Bool("HoldDone")
HoldAcc = Int("HoldAcc")

# Counters — per bin
BinADone = Bool("BinADone")
BinAAcc = Dint("BinAAcc")
BinBDone = Bool("BinBDone")
BinBAcc = Dint("BinBAcc")

# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------
with Program() as logic:
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
        Or(
            And(State == SORTING, IsLarge, Auto),
            And(Manual, DiverterBtn),
        ),
    ):
        out(DiverterCmd)

    comment("Bin counters")
    with Rung(rise(BinASensor)):
        count_up(BinADone, BinAAcc, preset=9999).reset(CountReset)
    with Rung(rise(BinBSensor)):
        count_up(BinBDone, BinBAcc, preset=9999).reset(CountReset)

# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------
if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    result = generate_circuitpy(
        logic,
        hw,
        target_scan_ms=10.0,
        watchdog_ms=5000,
        runstop=RunStopConfig(),
        force_runtime=True,
    )
    print(result.code)
