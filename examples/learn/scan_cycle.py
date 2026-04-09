"""Lesson 1: The Scan Cycle — docs/learn/scan-cycle.md"""

# --- The ladder logic way ---

from pyrung import Bool, Program, Rung, PLC, out

RunButton     = Bool("RunButton")
ConveyorMotor = Bool("ConveyorMotor")

with Program() as logic:
    with Rung(RunButton):
        out(ConveyorMotor)

# --- Try it ---

with PLC(logic) as plc:
    RunButton.value = True
    plc.step()               # One scan
    assert ConveyorMotor.value is True

    RunButton.value = False
    plc.step()               # Next scan
    assert ConveyorMotor.value is False  # Motor follows button, every scan
