"""Lesson 2: Tags — docs/learn/tags.md"""

# --- Setting values from outside the program ---

from pyrung import PLC, Bool, Int, Program, Rung, out

ConveyorSpeed = Int("ConveyorSpeed")
SpeedLimit = Int("SpeedLimit")
OverSpeed = Bool("OverSpeed")

with Program() as logic:
    with Rung(ConveyorSpeed > SpeedLimit):
        out(OverSpeed)

with PLC(logic) as plc:
    SpeedLimit.value = 500  # Like typing into a dataview
    ConveyorSpeed.value = 300
    plc.step()
    assert OverSpeed.value is False

    ConveyorSpeed.value = 600  # Speed exceeds limit
    plc.step()
    assert OverSpeed.value is True  # Program reacts on the next scan
