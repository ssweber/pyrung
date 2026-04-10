"""Lesson 3: Latch and Reset — docs/learn/latch-reset.md"""

# --- The ladder logic way ---

from pyrung import PLC, Bool, Program, Rung, comment, latch, reset

StartBtn = Bool("StartBtn")  # NO momentary contact
StopBtn = Bool("StopBtn")  # NC contact: conductive at rest
Running = Bool("Running")

with Program() as logic:
    with Rung(StartBtn):
        latch(Running)  # SET: Running = True, stays True
    with Rung(~StopBtn):
        reset(Running)  # RESET when stop pressed or wire broken

# --- Try it ---

with PLC(logic) as plc:
    StopBtn.value = True  # NC input: True = healthy wiring

    StartBtn.value = True
    plc.step()
    assert Running.value is True

    StartBtn.value = False  # Finger off the button
    plc.step()
    assert Running.value is True  # Still running!

    StopBtn.value = False  # Stop pressed (NC opens)
    plc.step()
    assert Running.value is False

# --- Labeling your rungs ---

with Program() as logic:
    comment("Start the conveyor")
    with Rung(StartBtn):
        latch(Running)
    comment("Stop — NC contact resets when pressed or wire broken")
    with Rung(~StopBtn):
        reset(Running)
