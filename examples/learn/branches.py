"""Lesson 8: Branches and OR Logic — docs/learn/branches.md"""

# --- The ladder logic way ---

from pyrung import Bool, Int, Program, Rung, PLC, branch, comment, out, latch, reset, Or, And

Auto          = Bool("Auto")
Manual        = Bool("Manual")
StopBtn       = Bool("StopBtn")     # NC contact
StartBtn      = Bool("StartBtn")
EstopOK       = Bool("EstopOK")     # NC safety relay permission
Running       = Bool("Running")
Light         = Bool("Light")
DiverterBtn   = Bool("DiverterBtn")
DiverterCmd   = Bool("DiverterCmd")
ConveyorMotor = Bool("ConveyorMotor")
StatusLight   = Bool("StatusLight")
Mode          = Int("Mode")

with Program() as logic:
    # Motor runs in either mode when started
    with Rung(Or(Auto, Manual)):
        out(Light)                        # Status light: either mode is active

    # Or works with comparisons and any number of conditions
    with Rung(Or(Mode == 1, Mode == 3, Mode == 5)):
        latch(Running)

# --- Motor rung with branches ---

with Program() as logic:
    comment("Start/stop — NC stop resets when pressed or wire broken")
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

# --- Try it ---

with PLC(logic) as plc:
    StopBtn.value = True             # NC inputs: True = healthy
    EstopOK.value = True

    Auto.value = True
    StartBtn.value = True
    plc.step()
    assert Running.value is True
    assert ConveyorMotor.value is True
    assert StatusLight.value is True

    StartBtn.value = False
    plc.step()
    assert Running.value is True     # Still running (latched)

    # E-stop kills everything (NC opens)
    EstopOK.value = False
    plc.step()
    assert ConveyorMotor.value is False
    assert StatusLight.value is False
    assert Running.value is False
