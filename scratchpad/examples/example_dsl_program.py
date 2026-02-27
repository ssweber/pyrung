"""
Example DSL Program - Demonstrating pyrung capabilities

This example shows the current state of the pyrung DSL, including:
- What's currently working
- What's stubbed/not yet implemented (commented out)
"""

from pyrung import (
    PLC,
    Rung,
    branch,
    call,
    copy,
    fall,
    latch,
    math_decimal,
    nc,
    out,
    rise,
    reset,
    subroutine,
)

# fmt: off

def main():
    """Main program demonstrating pyrung DSL features."""
    plc = PLC()

    # ==========================================================================
    # Address Bank Setup (nicknames)
    # ==========================================================================
    # Inputs
    plc.x[1] = "StartButton"
    plc.x[2] = "EmergencyStop"
    plc.x[3] = "CycleStart"

    # Outputs
    plc.y[1] = "MainMotor"
    plc.y[2] = "SecondaryMotor"
    plc.y[3] = "Conveyor"
    plc.y[4] = "Indicator"

    # Control bits
    plc.c[1] = "SystemRunning"
    plc.c[2] = "EStopActive"
    plc.c[3] = "CycleActive"
    plc.c[4] = "ResetTimer"
    plc.c[5] = "ResetCounter"

    # Data store
    plc.ds[1] = "OperationMode"
    plc.ds[2] = "RawValue"
    plc.ds[3] = "CurrentStep"
    plc.ds[4] = "MaxCycleCount"

    # Float data
    plc.df[1] = "ScaledValue"

    # Timers (T bank for done bits, TD for current values)
    plc.t[1] = "PulseTrigger"
    plc.t[2] = "CycleTimer"
    plc.td[1] = "CurrentPulseTriggerVal"

    # Counters (CT bank for done bits, CTD for current values)
    plc.ct[1] = "CycleCounter"

    # Shorthand references
    x, y, c, ds, df = plc.x, plc.y, plc.c, plc.ds, plc.df

    # ==========================================================================
    # WORKING FEATURES - Basic Logic
    # ==========================================================================

    # Read inputs at beginning - basic contact to coil
    with Rung(x.StartButton):
        out(c.SystemRunning)

    # Normally closed contact (XIO)
    with Rung(x.EmergencyStop):
        latch(c.EStopActive)

    # Series conditions (AND logic) with NC contact
    with Rung(c.SystemRunning, nc(c.EStopActive)):
        out(y.MainMotor)
        out(y.SecondaryMotor)

    # ==========================================================================
    # WORKING FEATURES - Edge Detection
    # ==========================================================================

    # Rising edge detection (one-shot on)
    with Rung(rise(x.CycleStart)):
        latch(c.CycleActive)

    # Falling edge detection
    with Rung(fall(x.CycleStart)):
        reset(c.CycleActive)

    # ==========================================================================
    # WORKING FEATURES - Comparisons
    # ==========================================================================

    # Comparison in conditions
    with Rung(ds.OperationMode == 1):
        call("auto_mode")

    with Rung(ds.CurrentStep >= 0, ds.CurrentStep < 10):
        out(y.Indicator)

    # ==========================================================================
    # WORKING FEATURES - Branches (Parallel Logic / OR)
    # ==========================================================================

    with Rung(c.SystemRunning):
        out(y.MainMotor)
        with branch(ds.OperationMode == 2):
            out(y.Conveyor)

    # ==========================================================================
    # WORKING FEATURES - Copy and Math
    # ==========================================================================

    # Copy instruction with oneshot
    with Rung(rise(c.CycleActive)):
        copy(0, ds.CurrentStep, oneshot=True)

    # Math instruction (uses lambda for deferred evaluation)
    with Rung(c.SystemRunning):
        math_decimal(lambda: ds.RawValue.get_value() * 100 / 4095, df.ScaledValue)

    # ==========================================================================
    # NOT YET IMPLEMENTED - Timers
    # ==========================================================================
    # Proposed RTON (Retentive Timer) Syntax:
    # 1) No other instructions can be after the rton/rtof
    # 2) On exit, it leaves an 'open' reset for t.Name
    # 3) The next Rung's first instruction MUST be reset(t.Name)
    #
    # with Rung(c.SystemRunning):
    #     rton(t.CycleTimer, setpoint=ds.CycleTimeMinutes, unit=TimeUnit.Tm,
    #          elapsed_time=td.CurrentPulseTriggerVal)
    #
    # with Rung(c.ResetTimer):
    #     reset(t.CycleTimer)

    # ==========================================================================
    # NOT YET IMPLEMENTED - Counters
    # ==========================================================================
    # Counters follow the Retentive pattern (like rton):
    # 1) Counter instructions (ctu, down) must be the last instruction in the Rung.
    # 2) For CTUD (Up/Down), the order is: Up Rung -> Down Rung -> Reset Rung.
    # 3) The Reset Rung MUST be the immediate next rung after the counter logic.
    #
    # Example 1: Simple Count Up (CTU)
    # with Rung(rise(t.PulseTrigger)):
    #     ctu(ct.CycleCounter, setpoint=ds.MaxCycleCount)
    #
    # with Rung(c.ResetCounter):
    #     reset(ct.CycleCounter)
    #
    # Example 2: Count Up/Down (CTUD)
    # with Rung(rise(x.PartEnter)):
    #     # Primary instruction sets the setpoint and handles increment
    #     ctu(ct.ZoneCount, setpoint=ds.ZoneCapacity)
    #
    # with Rung(rise(x.PartExit)):
    #     # Secondary instruction handles decrement
    #     down(ct.ZoneCount)
    #
    # with Rung(c.ZoneReset):
    #     # Mandatory reset rung
    #     reset(ct.ZoneCount)

    # ==========================================================================
    # NOT YET IMPLEMENTED - Program Control
    # ==========================================================================
    # end() instruction not implemented
    #
    # with Rung():
    #     end()

    # ==========================================================================
    # WORKING FEATURES - Subroutines
    # ==========================================================================

    with subroutine("auto_mode"):
        with Rung(c.CycleActive, ds.CurrentStep == 0):
            out(y.Conveyor)
            copy(1, ds.CurrentStep)

        with Rung(ds.CurrentStep == 1):
            out(y.Indicator)
            copy(2, ds.CurrentStep, oneshot=True)

    return plc


def run_demo():
    """Run the demo program with some simulated inputs."""
    plc = main()

    print("=== pyrung DSL Demo ===\n")
    print("Initial state:")
    print_state(plc)

    # Simulate pressing start button
    print("\n--- Press Start Button ---")
    plc.x.StartButton.set_value(1)
    plc.scan()
    print_state(plc)

    # Another scan with start button released
    print("\n--- Release Start Button ---")
    plc.x.StartButton.set_value(0)
    plc.scan()
    print_state(plc)

    # Set operation mode and trigger cycle
    print("\n--- Set Operation Mode = 1, Press Cycle Start ---")
    plc.ds.OperationMode.set_value(1)
    plc.x.CycleStart.set_value(1)
    plc.scan()
    print_state(plc)

    # Release cycle start (falling edge)
    print("\n--- Release Cycle Start ---")
    plc.x.CycleStart.set_value(0)
    plc.scan()
    print_state(plc)

    # Emergency stop
    print("\n--- Press Emergency Stop ---")
    plc.x.EmergencyStop.set_value(1)
    plc.scan()
    print_state(plc)


def print_state(plc):
    """Print current PLC state."""
    print(f"  SystemRunning = {plc.c.SystemRunning.get_value()}")
    print(f"  EStopActive   = {plc.c.EStopActive.get_value()}")
    print(f"  CycleActive   = {plc.c.CycleActive.get_value()}")
    print(f"  MainMotor     = {plc.y.MainMotor.get_value()}")
    print(f"  Conveyor      = {plc.y.Conveyor.get_value()}")
    print(f"  CurrentStep   = {plc.ds.CurrentStep.get_value()}")
    print(f"  ScaledValue   = {plc.df.ScaledValue.get_value()}")


if __name__ == "__main__":
    run_demo()
