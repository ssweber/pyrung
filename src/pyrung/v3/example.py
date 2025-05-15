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

def test_nested_rungs():
    """Test the nested rung functionality"""
    plc = setup_plc()
    x = plc.x
    y = plc.y
    c = plc.c
    
    # Set initial values
    c.AutoMode.set_value(0)  # Auto mode off initially
    
    # Define the program with nested rungs
    def program():
        with Rung():  # Unconditional rung
            out(y.Light)
            with Rung(re(c.AutoMode)):  # Nested rung that depends on AutoMode
                out(y.NestedLight)
    
    # Run the program directly
    program()
    
    # Print state - Light should be ON, NestedLight should be OFF
    print("Initial run:")
    print(f"Light = {y.Light.get_value()}")
    print(f"NestedLight = {y.NestedLight.get_value()}")
    print(f"AutoMode = {c.AutoMode.get_value()}")
    
    # Turn on AutoMode and run scan
    c.AutoMode.set_value(1)
    plc.scan()
    
    # Print state - Now both should be ON
    print("\nAfter setting AutoMode and scanning:")
    print(f"Light = {y.Light.get_value()}")
    print(f"NestedLight = {y.NestedLight.get_value()}")
    print(f"AutoMode = {c.AutoMode.get_value()}")
    
    # Turn off AutoMode and run scan again
    c.AutoMode.set_value(0)
    plc.scan()
    
    # Print state - NestedLight should now be OFF
    print("\nAfter turning off AutoMode and scanning:")
    print(f"Light = {y.Light.get_value()}")
    print(f"NestedLight = {y.NestedLight.get_value()}")
    print(f"AutoMode = {c.AutoMode.get_value()}")

if __name__ == "__main__":
    test_nested_rungs()