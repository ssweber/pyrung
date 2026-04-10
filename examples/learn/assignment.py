"""Lesson 4: Assignment — docs/learn/assignment.md"""

# --- The ladder logic way ---

from pyrung import PLC, Bool, Int, Program, Rung, calc, copy, rise

EntrySensor = Bool("EntrySensor")
BoxSize = Int("BoxSize")  # Raw sensor reading
CurrentSize = Int("CurrentSize")  # Snapshot of this box's reading
SortCount = Int("SortCount")  # Total boxes sorted
CycleCount = Int("CycleCount")  # Scans since startup

with Program() as logic:
    with Rung(rise(EntrySensor)):
        copy(BoxSize, CurrentSize)  # Snapshot the size reading
        calc(SortCount + 1, SortCount)  # Increment total

    with Rung():
        calc(CycleCount + 1, CycleCount)  # Always counting (every scan)

# --- Try it ---

with PLC(logic) as plc:
    BoxSize.value = 150
    EntrySensor.value = True
    plc.step()
    assert CurrentSize.value == 150
    assert SortCount.value == 1

    EntrySensor.value = False
    plc.step()
    assert SortCount.value == 1  # rise() only fires once
    assert CycleCount.value == 2  # Unconditional rung runs every scan
