"""Conveyor sorting station — CircuitPython port of click_conveyor for P1AM-200.

Same logic as click_conveyor.py, targeting P1AM hardware instead of Click PLC:
  1. Start/stop/E-stop motor control with safety interlocking
  2. State-machine sort sequence (idle → detecting → sorting → counting)
  3. Auto/manual mode with branch-based diverter control
  4. Edge-triggered bin counters
  5. Code generation with force_runtime for starter bundle

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
    Tms,
    all_of,
    any_of,
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
Start = inputs[1]  # momentary start button
Stop = inputs[2]  # momentary stop button
Estop = inputs[3]  # emergency stop
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

State = Int("State")  # sort sequence (0=idle, 1=detecting, 2=sorting, 3=counting)

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
    comment("Start/stop/E-stop - process control inputs first")
    with Rung(Start, any_of(Auto, Manual)):
        latch(Running)
    with Rung(Stop):
        reset(Running)
    with Rung(Estop):
        reset(Running)

    comment("Motor output with E-stop safety gating")
    with Rung(~Estop):
        with branch(Running):
            out(ConveyorMotor)
        with branch(Running):
            out(StatusLight)

    comment("Sort state machine - IDLE to DETECTING: box arrives")
    with Rung(State == 0, rise(EntrySensor)):
        copy(1, State)

    comment("DETECTING: read size for 0.5 seconds")
    with Rung(State == 1):
        on_delay(DetDone, DetAcc, preset=500, unit=Tms)
    with Rung(State == 1, SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetDone):
        copy(2, State)

    comment("SORTING: hold diverter for 2 seconds")
    with Rung(State == 2):
        on_delay(HoldDone, HoldAcc, preset=2000, unit=Tms)
    with Rung(HoldDone):
        copy(3, State)

    comment("COUNTING: reset and return to idle")
    with Rung(State == 3):
        reset(IsLarge)
        copy(0, State)

    comment("Diverter output - auto sort OR manual button")
    with Rung(
        ~Estop,
        any_of(
            all_of(State == 2, IsLarge, Auto),
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
