# Click PLC DSL Core Specification (v0.1) - Condensed

## Core Syntax Structure
- Main program: `def main():`
- Subroutines: `@sub` decorator
- Logic structure: `if` conditions with indented actions
- Unconditional rung: `if True:`

## Addressing System
- Supports array notation `type[index]` and dot notation `type.nickname`
- Ranges use slice notation `type[start:end]`
- Only literal values in brackets

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

## Rung Conditions

### Logic Functions
- `all(condition1, condition2, ...)` - Series connection (AND logic)
- `any(condition1, condition2, ...)` - Parallel branches (OR logic)

### Bit/Contacts
- `address : bit` - Normally Open Contact
- `nc(address : bit)` - Normally Closed Contact
- `re()`, `fe()` - Rising/Falling Edge Contact

### Comparison
- `==`, `!=`, `<`, `<=`, `>`, `>=`

## PLC Instructions

### Coil Instructions
- `out(address)` - Output Coil
- `set(address)` - Set Coil
- `reset(address)` - Reset Coil

### Timer Instructions
- `ton(output, setpoint, unit, elapsed_time)` - On-Delay Timer
- `tof(output, setpoint, unit, elapsed_time)` - Off-Delay Timer
- `rton(output, setpoint, unit, elapsed_time, reset=lambda: expression)` - Retentive On-Delay Timer
- `rtof(output, setpoint, unit, elapsed_time, reset=lambda: expression)` - Retentive Off-Delay Timer

### Counter Instructions
- `ctu(output, setpoint, current_value, reset=lambda: expression)` - Count Up
- `ctd(output, setpoint, current_value, reset=lambda: expression)` - Count Down
- `ctud(output, setpoint, current_value, reset=lambda: expression, down=lambda: expression)` - Up/Down Counter

### Copy Instructions
- `copy(source, destination, oneshot=False, options=None)`

### Decimal Math
- `math_decimal(formula, result_destination, one_shot=False)`

## Program Control Instructions
- `call(subroutine_name)` - Calls a Subroutine Program
- `end()` - End instruction
- `return` - Returns to the Main Program