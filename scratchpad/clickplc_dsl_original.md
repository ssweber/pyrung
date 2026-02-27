# Click PLC DSL Specification (v1.1)

## Introduction and Purpose

This document defines a Domain Specific Language (DSL) implemented in Python for describing logic intended for execution on a AutomationDirect Click PLC. The primary goal of this DSL is to provide a structured, readable, and automatable representation of PLC logic, facilitating potential translation into native PLC programming formats or use in simulation and documentation tools. It aims to abstract away some low-level details while staying true to the fundamental execution model of a PLC scan cycle.

## Core Syntax Structure

The DSL leverages Python's syntax as a container and structure for the logic description:

- Standard Python function definitions. Main Program is `def main():`.
- Subroutines are decorated with @sub
- Each rung starts with an `if` condition
- Actions are indented under the condition
- Subset of Python syntax. No use of `elif`, `else`, `and`, `or`, `not`.
- An `if True:` statement explicitly represents an Unconditional Rung
- `pass` for NOP or where necessary to maintain Python syntax.

## Addressing System
All address types support both array notation `type[index]` and dot notation `type.nickname` for named references. Ranges can be specified using Python slice notation `type[start:end]`.

| Type | Data Type | Range | Description | Retentive |
|------|-----------|-------|-------------|-----------|
| `x` | bit | 001-816 | Input bits (0 or 1) | No |
| `y` | bit | 001-816 | Output bits (0 or 1) | No |
| `c` | bit | 1-2000 | Control Relays (Internal bits) | No |
| `t` | bit | 1-500 | Timer bits (0 or 1) | No |
| `ct` | bit | 1-250 | Counter bits (0 or 1) | Yes |
| `sc` | bit | 1-1000 | System Control Relays (Internal bits) | Always No |
| `ds` | int16 | 1-4500 | Data addresses (Single Word Integer) | Yes |
| `dd` | int32 | 1-1000 | Double Data addresses (Double Word Integer) | Yes |
| `dh` | hex16 | 1-500 | Hexadecimal data addresses | Yes |
| `df` | float32 | 1-500 | Floating-point addresses | Yes |
| `xd` | hex16 | 0-8 | Input data addresses (hexadecimal) | Always No |
| `yd` | hex16 | 0-8 | Output data addresses (hexadecimal) | Always No |
| `td` | int16 | 1-500 | Timer Current Values (Single Word Integer) | No |
| `ctd` | int32 | 1-250 | Counter Current Values (Double Word Integer) | Yes |
| `sd` | int16 | 1-1000 | System Data addresses (Single Word Integer) | Always No |
| `txt` | txt | 1-1000 | Text Data addresses (ASCII 7-bit) | Yes |

## Condition: Logic and Comparison

### Logic Functions
- `all(condition1, condition2, ...)` - Series connection (AND logic)
  - Accepts any number of arguments directly (no need for a list)
  - Example: `all(x[1], nc(x[2]))`
- `any(condition1, condition2, ...)` - Parallel branches (OR logic)
  - Accepts any number of arguments directly (no need for a list)
  - Example: `any(x[1], x[2])`

### bit/Contact Instructions
- `address : BitAddress | BitAddressRange` - Normally Open Contact
- `nc(address : BitAddress | BitAddressRange)` - Normally Closed Contact
- `re(address)` - Rising Edge Contact
- `fe(address)` - Falling Edge Contact

### Comparison Functions
**`==`, `!=`, `<`, `<=`, `>`, `>=`**
- No chaining comparisions. 
- Must be matching type of NumericAddress or Numeric value
## Actions

### Output Instructions
- `out(address)` - Output Coil. (Optional) 'oneshot'=True. Turns on the bit for one scan
- `set(address)` - Set Coil
- `reset(address)` - Reset Coil
Note: address can be either a Y/C/SCAddress or Y/C/SCAddressRange

## Timers/Counters
- `ton(t.name, setpoint, unit)` - Timer On Delay
- `tof(t.name, setpoint, unit)` - Timer Off Delay
- `rton(t.name, setpoint, unit, reset=lambda: expression)` - Retentive Timer On with reset condition
  - Example: `rton(t.CycleTimer, setpoint=5, unit=Ts, reset=lambda: c.ResetTimer)`
- `rtof(t.name, setpoint, unit, reset=lambda: expression)` - Retentive Timer Off with reset condition
- `ctu(counter, setpoint, reset=lambda: expression)` - Count Up
- `ctd(counter, setpoint, reset=lambda: expression)` - Count Down
- `ctud(counter, setpoint, count_down=lambda: expression)` - Count Up/Down
Note:
- setpoint can be DSAddress or int16
- Timer/counter usage: `t.name` or `nc(ct.name)`
- units: Td (day), Th (hour), Tm (minute), Ts (second), Tms (millisecond)

## Data Manipulation
### Copy Instructions
- `copy(source, destination, oneshot=False, options=None)`
Note: copy() Allows `Pointer` usage, in the form of type[DSAddress]. Eg `ds[ds.StepVal]`
- `copy_block(source_range, destination_start, oneshot=False, options=None)`
- `copy_fill(source, destination_range, oneshot=False, options=None)`
- `copy_pack(source_range, destination, oneshot=False, options=None)`
- `copy_unpack(source, destination_range, oneshot=False, options=None)`
- `shift(range)` - Shift Data

# Data Type Compatibility for Copy Instructions

## Compatible Data Types
| Data Type | Compatible With | Packing/Unpacking |
|-----------|-----------------|-------------------|
| **int16** (DS, TD, CTD, SD) | int16, int32, float32, hex16 | DS can pack into DD/DF and unpack into C |
| **int32** (DD) | int32, int16, float32, hex16 | DD can unpack into C/DS/DH |
| **float32** (DF) | float32, int16, int32, hex16 | DF can unpack into C/DS |
| **hex16** (XD, YD, DH) | hex16, int16, int32, float32 | DH can unpack into C/Y |
| **bit** (X, Y, C, T, CT, SC) | All bit types | X/Y/T/CT/SC can pack into DH, C can pack into DS/DD/DF/DH |
| **txt** (TXT) | txt | Can pack into DS/DD/DF/DH/TD/CTD |

### Search Instruction
```python
search(condition, search_value, start_address, end_address, result_address, result_flag : C Address, continuous=False, oneshot=False)
```

Searches for values meeting specified conditions within an address range, storing the matching address in the result address.

**Parameters:**
- `condition` (str): Comparison type ("eq", "ne", "gt", "ge", "lt", "le")
- `search_value`: Value to find (constant or address)
- `start_address`: Beginning of search range
- `end_address`: End of search range
- `result_address`: Where to store the found address
- `result_flag`: C Address that indicates successful search
- `continuous` (bool): When True, continues from last found position; when False, starts from beginning
- `oneshot` (bool): When True, executes only on OFF-to-ON transition

**Behavior:**
- Stores matching address in result_address
- Stores "-1" in result_address if no match found
- With `continuous=True`, continues from next address after last match
- Store zero in result_address to restart continuous search
- Sets result flag on successful search

## Math Instructions
### Decimal Math
```python
calc(formula, result_destination, one_shot=False)
```
Solves decimal formulas and stores results in the specified destination. 
**Supported operators:**
- Arithmetic: `+`, `-`, `*`, `/`, `^` (power)
- Grouping: `(` and `)` (up to 8 nested levels)
- Trigonometric: `SIN()`, `COS()`, `TAN()`, `ASIN()`, `ACOS()`, `ATAN()`
- Functions: `LOG()`, `SQRT()`, `LN()`, `SUM(range)`, `MOD`, `RAD()`, `DEG()`
- Constants: `PI` (3.1415927)

### Hex Math
```python
math_hex(formula, result_destination, one_shot=False)
```
Solves hexadecimal formulas with support for: 
- Arithmetic: `/`, `*`, `-`, `+`
- Grouping: `(` and `)`
- Logical: `OR`, `AND`, `XOR`, `NOT`
- Bit operators: `SHL`, `SHR`
- Other:
  - `MOD`, `SUM(address_range)`

## Program Control Instructions

- `call(subroutine_name)` - Calls a Subroutine Program from the Main Program
- `for _ in range(loops):` - Starts a For-Next loop to execute code multiple times in one scan
- `next_loop()` - Next instruction that marks the end of a For-Next loop
- `end()` - End instruction that marks the termination point of program scan
- `return` - Returns to the Main Program from a Subroutine Program

**Notes:**
- Subroutines cannot call other subroutines (nesting level limited to 1)
- Nested For-Next loops are not permitted
- Main program must have at least one unconditional `end()` instruction
- Every Subroutine must have at least one unconditional `return`

## Example Program
```python
def main():
    # Read inputs at beginning
    if x[1]:
        out(c.StartButton)

    if x.EmergencyStop:
        out(c.EStopActive)

    # Basic logic examples
    if all(c.StartButton, nc(c.EStopActive)):
        set(c.SystemRunning)

    if any(x.ResetButton, c.EStopActive):
        reset(c.SystemRunning)

    # Timer examples
    if c.SystemRunning:
        ton(t.StartDelay, setpoint=ds.StartDelayValue, unit=Ts)
        tof(t.StopDelay, setpoint=3, unit=Ts)
        rton(
            t.CycleTimer,
            setpoint=ds.CycleTimeMinutes,
            unit=Tm,
            reset=lambda: c.ResetTimer,
        )

    # Counter examples
    if re(c.PulseTrigger):
        ctu(
            ct.CycleCounter, setpoint=ds.MaxCycleCount, reset=lambda: c.ResetCounter
        )
        ctud(ct.BidirectionalCounter, setpoint=50, count_down=lambda: c.CountDown)

    # Data manipulation
    if c.UpdateAddresses:
        copy(ds[10], ds.ProcessValue)  # Single value copy
        copy_block(ds[1:5], ds[100])  # Block copy
        copy_fill(df.SetpointTemp, df[20:30])  # Fill multiple Addresses
        copy_pack(c[1:17], dh.StatusWord)  # Pack bits into word
        copy_unpack(dd.ConfigWord, c[101:133])  # Unpack word to bits
        copy(ds[ds.IndexPointer], ds.SelectedValue)  # Pointer addressing

    # Math operations
    if True:
        calc("ds.RawValue * 100 / 4095", df.ScaledValue)
        math_hex("dh.InputStatus AND 0x00FF", dh.FilteredStatus)

    # Search instruction
    if c.FindValue:
        search("==", ds.TargetValue, ds[100], ds[200], ds.ResultAddress, c.ValueFound)

    # Program control
    if ds.OperationMode == 1:
        call(auto_mode)

    # For-loop Example
    # ----------------
    if re(c.UpdateAlarmHistory):
        copy(0, ds.AlarmIndex)
        copy(0, ds.AlarmExtent)

    if c.UpdateAlarmHistory:
        for _ in range(ds.MaxAlarms):
            pass  # All logic between this and next_loop will be repeated

    # will loop ds.MaxAlarms
    if True:
        calc("ds.AlarmBase + ds.AlarmIndex", ds.CurrentAlarmAddr)
        calc("ds.AlarmHistoryBase + ds.AlarmIndex", ds.HistoryAddr)
        copy(ds[ds.CurrentAlarmAddr], ds[ds.HistoryAddr])
        calc("ds.AlarmExtent + ds[ds.CurrentAlarmAddr]", ds.AlarmExtent)
        calc("ds.AlarmIndex + 1", ds.AlarmIndex)

    if True:
        next_loop()  # Next instruction indicates the end of a For Next loop
    # End of For-loop Example

    # Write outputs at end
    if c.SystemRunning:
        out(y.MainMotor)
        out(y[2])  # Secondary motor

    if t.StartDelay:
        out(y.ReadyLight)

    # End program
    if True:
        end()


@sub
def auto_mode():
    # Simple subroutine example
    if re(c.CycleStart):
        set(c.CycleActive)

    if all(c.CycleActive, ds.CurrentStep == 0):
        out(y.Conveyor)
        copy(1, ds.CurrentStep)

    if True:
        return
```
