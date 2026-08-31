"""Lesson 3: Latch and Reset — docs/learn/latch-reset.md"""

# --- The ladder logic way ---

from pyrung import PLC, Bool, Program, rung, comment, latch, reset

StartBtn = Bool()  # NO momentary contact
StopCircuitOK = Bool()  # NC stop circuit: True when healthy
Running = Bool()

with Program() as logic:
    with rung(StartBtn):
        latch(Running)  # SET: Running = True, stays True
    with rung(~StopCircuitOK):
        reset(Running)  # RESET when stop pressed or wire broken

# --- Try it ---

with PLC(logic) as plc:
    StopCircuitOK.value = True  # Stop circuit is healthy

    StartBtn.value = True
    plc.step()
    assert Running.value is True

    StartBtn.value = False  # Finger off the button
    plc.step()
    assert Running.value is True  # Still running!

    StopCircuitOK.value = False  # Stop pressed (NC opens)
    plc.step()
    assert Running.value is False

# --- Labeling your rungs ---

with Program() as logic:
    comment("Start the conveyor")
    with rung(StartBtn):
        latch(Running)
    comment("Stop — NC contact resets when pressed or wire broken")
    with rung(~StopCircuitOK):
        reset(Running)
