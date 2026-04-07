"""Conveyor sorting station — the tutorial's progressive project as a Click example.

Demonstrates the full pyrung → Click workflow:
  1. Start/stop/E-stop motor control with safety interlocking
  2. State-machine sort sequence (idle → detecting → sorting → counting)
  3. Auto/manual mode with branch-based diverter control
  4. Edge-triggered bin counters
  5. TagMap linking logical tags to Click hardware addresses
  6. Simulation with PLCRunner in FIXED_STEP mode

This is the completed version of the conveyor built across the
"Know Python? Learn Ladder Logic" tutorial.  See ``devtools/build_release_assets.py``.
"""

import os

from pyrung import (
    Bool,
    Dint,
    Int,
    PLCRunner,
    Rung,
    TimeMode,
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
    program,
    reset,
    rise,
)
from pyrung.click import TagMap, c, ct, ctd, ds, t, td, x, y

# ---------------------------------------------------------------------------
# Tags — inputs
# ---------------------------------------------------------------------------
Start = Bool("Start")  # X001 — momentary start button
Stop = Bool("Stop")  # X002 — momentary stop button
Estop = Bool("Estop")  # X003 — emergency stop
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

State = Int("State")  # DS003 — sort sequence state (0=idle, 1=detecting, 2=sorting, 3=counting)

SizeReading = Int("SizeReading")  # DS001 — analog size sensor value
SizeThreshold = Int("SizeThreshold")  # DS002 — small/large cutoff

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
        Start.map_to(x[1]),
        Stop.map_to(x[2]),
        Estop.map_to(x[3]),
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
        # Data
        State.map_to(ds[3]),
        SizeReading.map_to(ds[1]),
        SizeThreshold.map_to(ds[2]),
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
# Simulation
# ---------------------------------------------------------------------------
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan

if os.getenv("PYRUNG_DAP_ACTIVE") != "1":
    with runner.active():
        State.value = 0
        Auto.value = True
        SizeThreshold.value = 100

        # Press Start
        runner.patch({Start.name: True})
        runner.step()
        runner.patch({Start.name: False})

    # Simulate a large box arriving
    runner.add_force(EntrySensor, True)
    runner.add_force(SizeReading, 150)
    runner.run(cycles=300)  # Through detection + sorting
    runner.remove_force(EntrySensor)
    runner.remove_force(SizeReading)
    runner.run(cycles=10)

    # Report
    with runner.active():
        print(f"Motor     : {'ON' if ConveyorMotor.value else 'OFF'}")
        print(f"State     : {State.value!r}")
        print(f"Diverter  : {'EXTENDED' if DiverterCmd.value else 'retracted'}")
        print(f"IsLarge   : {IsLarge.value}")
        print(f"Bin A     : {BinAAcc.value} boxes")
        print(f"Bin B     : {BinBAcc.value} boxes")
