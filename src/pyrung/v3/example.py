# Import the main modules
from plc import PLC
from dsl import Rung, out, nc, copy, reset, math_decimal, re, fe, set_instr as set

def setup_plc():
    """Initialize the PLC and define nicknames"""
    plc = PLC()
    
    # Define input nicknames
    plc.x[1] = "Button"
    plc.x[2] = "EmergencyStop"
    
    # Define output nicknames
    plc.y[1] = "Light"
    plc.y[2] = "Indicator"
    plc.y[3] = "Buzzer"
    plc.y[4] = "NestedLight"
    
    # Define control bit nicknames
    plc.c[1] = "AutoMode"
    plc.c[2] = "SystemRunning"
    plc.c[3] = "AlarmActive"
    
    # Define data store nicknames
    plc.ds[1] = "Step"
    plc.ds[2] = "Counter"
    plc.ds[3] = "Timer"
    
    return plc

def main_program(plc):
    """Main PLC program logic using the refactored DSL"""
    # Create local references to avoid plc. prefix
    x = plc.x
    y = plc.y
    c = plc.c
    ds = plc.ds
    
    # Simple rung - output turns on light
    with Rung():
        out(y.Light)
        with Rung(c.AutoMode):
            out(y.NestedLight)

def print_plc_state(plc):
    """Display the current state of key PLC variables"""
    # Create local references to avoid plc. prefix
    x = plc.x
    y = plc.y
    c = plc.c
    ds = plc.ds
    
    print("\nPLC State:")
    print(f"x.Button = {x.Button.get_value()}")
    print(f"x.EmergencyStop = {x.EmergencyStop.get_value()}")
    print(f"y.Light = {y.Light.get_value()}")
    print(f"y.Indicator = {y.Indicator.get_value()}")
    print(f"y.Buzzer = {y.Buzzer.get_value()}")
    print(f"y.NestedLight = {y.NestedLight.get_value()}")
    print(f"c.AutoMode = {c.AutoMode.get_value()}")
    print(f"c.SystemRunning = {c.SystemRunning.get_value()}")
    print(f"c.AlarmActive = {c.AlarmActive.get_value()}")
    print(f"ds.Step = {ds.Step.get_value()}")
    print(f"ds.Counter = {ds.Counter.get_value()}")
    print(f"ds.Timer = {ds.Timer.get_value()}")

def run_example():
    """Run the example PLC program"""
    # Initialize PLC
    plc = setup_plc()
    
    # Create local references to avoid plc. prefix
    x = plc.x
    c = plc.c
    ds = plc.ds
       
    # Set initial values
    c.AutoMode.set_value(0)  # Auto mode off initially
       
    # Print initial state
    print("Initial State:")
    print_plc_state(plc)
    
    # Execute the program once
    print("\nRunning first scan...")
    main_program(plc)
    print_plc_state(plc)
    
    # Execute the program again to show progression
    print("\nRunning second scan...")
    c.AutoMode.set_value(1)  # Turn on
    plc.scan()
    print_plc_state(plc)

if __name__ == "__main__":
    run_example()