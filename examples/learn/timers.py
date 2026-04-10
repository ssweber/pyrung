"""Lesson 5: Timers — docs/learn/timers.md"""

# --- The ladder logic way ---

from pyrung import PLC, Bool, Program, Rung, Timer, on_delay, out

EntrySensor = Bool("EntrySensor")
DiverterCmd = Bool("DiverterCmd")
HoldTimer = Timer.clone("HoldTimer")

with Program() as logic:
    with Rung(EntrySensor):
        on_delay(HoldTimer, preset=2000, unit="Tms")  # 2 seconds
    with Rung(EntrySensor, ~HoldTimer.Done):
        out(DiverterCmd)  # Hold diverter open while timing

# --- Test it deterministically ---

with PLC(logic, dt=0.010) as plc:
    EntrySensor.value = True

    plc.run(cycles=199)  # 1.99 seconds
    assert DiverterCmd.value is True  # Diverter still held open

    plc.step()  # 2.00 seconds
    assert DiverterCmd.value is False  # Released -- box has passed
