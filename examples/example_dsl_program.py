from clickplc_dsl import Addresses, Conditions, Actions, Td, Th, Tm, Ts, Tms, sub

# fmt: off
# Get address references
x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, td, ctd, sd, txt = Addresses.get()

# Get condition functions
no, nc, re, fe, all, any, eq, ne, lt, le, gt, ge = Conditions.get()

# Get action functions
out, set, reset, ton, tof, rton, rtof, ctu, ctd, ctud, copy, copy_block, copy_fill, copy_pack, copy_unpack, shift, search, math_decimal, math_hex, call, next_loop, end = Actions.get()
# fmt: on


def main():
    # Read inputs at beginning
    with Rung(x[1]):
        out(c.StartButton)

    with Rung(x.EmergencyStop):
        out(c.EStopActive)

    # Basic logic examples
    with Rung(c.StartButton, nc(c.EStopActive)):
        out(c.SystemRunning)

    # Timer examples
    with Rung(c.SystemRunning):
        ton(
            t.PulseTrigger,
            setpoint=ds.PulseTriggerValue,
            unit=Ts,
            elapsed_time=td.CurrentPulseTriggerVal,
        )
        rton(
            t.CycleTimer,
            setpoint=ds.CycleTimeMinutes,
            unit=Tm,
            reset=lambda: c.ResetTimer,
        )

    # Counter & Copy examples
    with Rung(re(t.PulseTrigger)):
        ctu(ct.CycleCounter, setpoint=ds.MaxCycleCount, reset=lambda: c.ResetCounter)
        copy(0, td.CurrentPulseTriggerVal)

    # Math operations
    with Rung():
        math_decimal("ds.RawValue * 100 / 4095", df.ScaledValue)

    # Program control
    with Rung(ds.OperationMode == 1):
        call(auto_mode)

    # Write outputs at end
    with Rung(c.SystemRunning):
        out(y.MainMotor)
        out(y[2])  # Secondary motor

    # End program
    with Rung():
        end()


@sub
def auto_mode():
    # Simple subroutine example
    with Rung(re(c.CycleStart)):
        set(c.CycleActive)

    with Rung(c.CycleActive, ds.CurrentStep == 0):
        out(y.Conveyor)
        copy(1, ds.CurrentStep)

    with Rung():
        return
