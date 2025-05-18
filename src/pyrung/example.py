# Import the main modules
from pyrung import (
    PLC,
    Rung,
    branch,
    subroutine,
    call,
    out,
    nc,
    copy,
    copy_block,
    copy_fill,
    copy_pack,
    copy_unpack,
    reset,
    math_decimal,
    re,
    fe,
    latch,
)


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
    ds = plc.ds

    # Set initial values
    c.AutoMode.set_value(0)  # Auto mode on initially
    ds.Step.set_value(0)  # Set step to 0

    # Define the program with nested rungs
    def program():
        with Rung(ds.Step == 0):
            out(y.Light)
            with branch(c.AutoMode):  # Nested rung that depends on AutoMode
                out(y.NestedLight)
                copy_fill(1, ds.Step, 5, oneshot=True)
                call("sub")
        #         with Rung(ds.Step == 1):
        #             reset(y.Light)
        with subroutine("sub"):
            with Rung(ds.Step == 1):
                out(c.AlarmActive)

    # Define the program structure (without execution)
    program()

    # Initial scan - now all execution happens here
    plc.scan()

    print("Initial run:")
    print(f"Light = {y.Light.get_value()}")
    print(f"NestedLight = {y.NestedLight.get_value()}")
    print(f"AutoMode = {c.AutoMode.get_value()}")
    print(f"Step = {ds.Step.get_value()}")
    print(f"AlarmActive = {c.AlarmActive.get_value()}")

    plc.scan()

    print(f"\nLight = {y.Light.get_value()}")
    print(f"NestedLight = {y.NestedLight.get_value()}")
    print(f"AutoMode = {c.AutoMode.get_value()}")
    print(f"Step = {ds.Step.get_value()}")
    print(f"AlarmActive = {c.AlarmActive.get_value()}")

    c.AutoMode.set_value(1)
    plc.scan()

    print(f"\nLight = {y.Light.get_value()}")
    print(f"NestedLight = {y.NestedLight.get_value()}")
    print(f"AutoMode = {c.AutoMode.get_value()}")
    print(f"Step = {ds.Step.get_value()}")
    print(f"AlarmActive = {c.AlarmActive.get_value()}")


if __name__ == "__main__":
    test_nested_rungs()
