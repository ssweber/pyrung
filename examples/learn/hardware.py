"""Lesson 11: From Simulation to Hardware — docs/learn/hardware.md"""

import tempfile

from pyrung import Bool, Int, Timer, Counter, Program, Rung, PLC
from pyrung import (
    And, Or, branch, comment, copy, count_up, latch, on_delay, out, reset, rise,
)
from pyrung import udt

# -- Tags (full conveyor from lessons 3-9) --

StartBtn      = Bool("StartBtn")
StopBtn       = Bool("StopBtn")
EstopOK       = Bool("EstopOK")
Auto          = Bool("Auto")
Manual        = Bool("Manual")
EntrySensor   = Bool("EntrySensor")
DiverterBtn   = Bool("DiverterBtn")
ConveyorMotor = Bool("ConveyorMotor")
DiverterCmd   = Bool("DiverterCmd")
StatusLight   = Bool("StatusLight")
Running       = Bool("Running")
IsLarge       = Bool("IsLarge")

IDLE      = Int("IDLE",      default=0)
DETECTING = Int("DETECTING", default=1)
SORTING   = Int("SORTING",   default=2)
RESETTING = Int("RESETTING", default=3)
State         = Int("State")
SizeReading   = Int("SizeReading")
SizeThreshold = Int("SizeThreshold")

DetTimer  = Timer.named(1, "DetTimer")
HoldTimer = Timer.named(2, "HoldTimer")

@udt(count=2)
class Bin:
    Sensor: Bool
    Full: Bool

BinACounter = Counter.named(1, "BinACounter")
BinBCounter = Counter.named(2, "BinBCounter")
CountReset  = Bool("CountReset")

# -- Program --

with Program() as logic:
    comment("Start/stop")
    with Rung(StartBtn, Or(Auto, Manual)):
        latch(Running)
    with Rung(~StopBtn):
        reset(Running)
    with Rung(~EstopOK):
        reset(Running)

    comment("State machine")
    with Rung(State == IDLE, rise(EntrySensor)):
        copy(DETECTING, State)
    with Rung(State == DETECTING):
        on_delay(DetTimer, preset=500, unit="Tms")
    with Rung(State == DETECTING, SizeReading > SizeThreshold):
        latch(IsLarge)
    with Rung(DetTimer.Done):
        copy(SORTING, State)
    with Rung(State == SORTING):
        on_delay(HoldTimer, preset=2000, unit="Tms")
    with Rung(HoldTimer.Done):
        copy(RESETTING, State)
    with Rung(State == RESETTING):
        reset(IsLarge)
        copy(IDLE, State)

    comment("Outputs")
    with Rung(EstopOK):
        with branch(Running):
            out(ConveyorMotor)
        with branch(Running):
            out(StatusLight)
    with Rung(
        EstopOK,
        Or(
            And(State == SORTING, IsLarge, Auto),
            And(Manual, DiverterBtn),
        ),
    ):
        out(DiverterCmd)

    comment("Bin counters")
    with Rung(rise(Bin[1].Sensor)):
        count_up(BinACounter, preset=10).reset(CountReset)
    with Rung(rise(Bin[2].Sensor)):
        count_up(BinBCounter, preset=10).reset(CountReset)

# --- Option B: Map to a Click PLC ---

from pyrung.click import x, y, c, ds, TagMap, pyrung_to_ladder

mapping = TagMap({
    StartBtn:       x[1],       # Physical input terminal 1
    StopBtn:        x[2],       # NC stop button
    EstopOK:        x[3],       # NC safety relay permission
    Auto:           x[4],
    Manual:         x[5],
    EntrySensor:    x[6],
    DiverterBtn:    x[7],
    Bin[1].Sensor:  x[8],
    Bin[2].Sensor:  x[9],
    ConveyorMotor:  y[1],       # Physical output terminal 1
    DiverterCmd:    y[2],
    StatusLight:    y[3],
    # Internal relays
    Running:        c[1],
    IsLarge:        c[2],
    CountReset:     c[3],
    Bin[1].Full:    c[4],
    Bin[2].Full:    c[5],
    # Data registers
    IDLE:           ds[1],
    DETECTING:      ds[2],
    SORTING:        ds[3],
    RESETTING:      ds[4],
    State:          ds[5],
    SizeReading:    ds[6],
    SizeThreshold:  ds[7],
})

mapping.validate(logic)                        # Check against Click constraints
bundle = pyrung_to_ladder(logic, mapping)      # Export ladder CSV
print(f"Click export: {len(bundle.main_rows)} ladder rows")

# --- Option C: Generate CircuitPython for a P1AM-200 ---

from pyrung.circuitpy import P1AM, generate_circuitpy

hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")   # 8-ch discrete input
outputs = hw.slot(2, "P1-08TRS")   # 8-ch discrete output

source = generate_circuitpy(logic, hw, target_scan_ms=10.0)
print(f"CircuitPython codegen: {len(source.code.splitlines())} lines")
