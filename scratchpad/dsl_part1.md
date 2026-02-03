# Document 1: Click PLC DSL Core Specification (v0.1)

## Introduction and Purpose

A Python-based DSL for representing AutomationDirect Click PLC logic for translation, simulation, and documentation. 

## Core Syntax Structure

The DSL leverages Python's syntax as a container and structure for the logic description:

- Rungs: `with Rung(*conditions):`
- Instructions indented under the condition
- Subroutines: call(function)
- Actions are indented under the condition
- Subset of Python syntax. No use of `if`, `elif`, `else`
- Avoid `and`, `or`, `not`. Use `all([])`, `any([])
- Unconditional rung: `with Rung():`
- `pass` where necessary to maintain Python syntax.

## Addressing System
 - All address types support both array notation `type[index]` and dot notation `type.nickname` for named references. 
 - Ranges can be specified using Python slice notation `type[start:end]`.
 - Only literal values are allowed in brackets. Expressions (e.g., `type[x+1]`) are NOT permitted.

## Addressing
| Type | Range | Description | Retentive |
|------|-------|-------------|-----------|
| `x` | 001-816 | Input bits | No |
| `y` | 001-816 | Output bits | No |
| `c` | 1-2000 | Control Relay bits | No |
| `t/td` | 1-500 | Timer bits (Q) / Elapsed Time int (ET) | No/Depends |
| `ct/ctd` | 1-250 | Counter bits (Q) / Current value int2 (CV) | Yes |
| `sc` | 1-1000 | System Control Relay bits | No |
| `ds` | 1-4500 | Data (int) | Yes |
| `dd` | 1-1000 | Double Data (int2) | Yes |
| `df` | 1-500 | float | Yes |
| `dh` | 1-500 | hex | Yes |
| Additional: `xd`, `yd`, `sd`, `txt (ascii 7-bit)` |

**Data Ranges:**
- bit: 0 or 1
- int: -32,768 to 32,767
- int2: -2,147,483,648 to 2,147,483,647
- float: -3.4028235E+38 to 3.4028235E+38
- hex: 0000h to FFFFh
- txt: Single ASCII Character

## Rung Conditions: Logic and Comparison

### Bit/Contacts
- `address : bit` - Normally Open Contact (eg `with Rung(c.Start):`)
- `nc(address : bit)` - Normally Closed Contact
- `re()`, `fe()` - Rising/Falling Edge Contact

### Comparison
**`==`, `!=`, `<`, `<=`, `>`, `>=`**
- No chaining comparisions. 
- Must be matching type of NumericAddress or Numeric value

## PLC Instructions

### Coil Instructions
- `out(address)` - Output Coil. (Optional) 'oneshot'=True. Turns on the bit for one scan
- `set(address)` - Set Coil
- `reset(address)` - Reset Coil
Note: address can be either a Y/C/SCAddress or Y/C/SCAddressRange

### Timer Instructions
- `ton(output, setpoint, unit, elapsed_time)` - On-Delay Timer (TON)
  - The output (t.address) turns ON only after reaching the setpoint.
  - Example: `ton(t.Delay, setpoint=5, unit=Ts, elapsed_time=t.DelayCurrent)` → `t.Delay` becomes `True` after 5 seconds.
  - Usage: `with Rung(t.Delay0:` (checks if output is ON)
- `tof(output, setpoint, unit, elapsed_time)` - Off-Delay Timer (TOF)
  - The output (t.address) stays ON until the setpoint is reached, then turns OFF.
- `rton(output, setpoint, unit, elapsed_time, reset=lambda: expression)` - Retentive On-Delay Timer
- `rtof(output, setpoint, unit, elapsed_time, reset=lambda: expression)` - Retentive Off-Delay Timer
  - Like ton/tof, but retains elapsed time through rung changes until explicitly reset
Note:
- When rung condition evalutes True, the elapsed time stored in corresponding `td` addresses counts up.
- Time Units: `Td` (days), `Th` (hours), `Tm` (minutes), `Ts` (seconds), `Tms` (milliseconds).
- Setpoint can be DSAddress or int

### Counter Instructions
- `ctu(output, setpoint, current_value, reset=lambda: expression)` - Count Up (CTU)
  - Increments; resets via reset.
- `ctd(output, setpoint, current_value, reset=lambda: expression)` - Count Down (CTD)
  - Decrements; resets via reset.
- `ctud(output, setpoint, current_value, reset=lambda: expression, down=lambda: expression)` - Up/Down Counter (CTUD)
  - Increments; decrements based on count_down condition.
Note: 
- Increments/Decrements each scan if rung evaulates True.
- Setpoint can be DSAddress or int

### Copy Instructions
- `copy(source, destination, oneshot=False, options=None)`
Note: copy() Allows `Pointer` usage, in the form of type[DSAddress]. Eg `ds[ds.StepVal]`

### Decimal Math
```python
math_decimal(formula=lambda : expression, result_destination, one_shot=False)
```
Solves decimal formulas and stores results in the specified destination. 
**Supported operators:**
- Arithmetic: `+`, `-`, `*`, `/`, `^` (power)
- Grouping: `(` and `)` (up to 8 nested levels)
- Trigonometric: `SIN()`, `COS()`, `TAN()`, `ASIN()`, `ACOS()`, `ATAN()`
- Functions: `LOG()`, `SQRT()`, `LN()`, `SUM(range)`, `MOD`, `RAD()`, `DEG()`
- Constants: `PI` (3.1415927)

## Program Control Instructions

- `call(subroutine_name)` - Calls a Subroutine Program from the Main Program
- `end()` - End instruction that marks the termination point of program scan
- `return` - Returns to the Main Program from a Subroutine Program

**Notes:**
- Subroutines cannot call other subroutines (nesting level limited to 1)
- Main program must have at least one unconditional `end()` instruction
- Every Subroutine must have at least one unconditional `return`

## Example Program
```python
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
            t.PulseTrigger, setpoint=ds.PulseTriggerValue, unit=Ts, elapsed_time=td.CurrentPulseTriggerVal
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
        math_decimal(lambda: ds.RawValue * 100 / 4095, df.ScaledValue)

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