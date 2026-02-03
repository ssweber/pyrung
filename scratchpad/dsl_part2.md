# Document 2: Click PLC DSL Advanced Features (v0.1)

## Advanced Data Manipulation

### Copy Instructions
- `copy(source, destination, oneshot=False, options=None)`
  - Basic copy operation from one address to another
  - Note: copy() Allows `Pointer` usage, in the form of type[DSAddress]. Eg `ds[ds.StepVal]`
- `copy_block(source_range, destination_start, oneshot=False, options=None)`
  - Copies a range of addresses to a destination range starting at destination_start
- `copy_fill(source, destination_range, oneshot=False, options=None)`
  - Copies a single value to multiple consecutive addresses
- `copy_pack(source_range, destination, oneshot=False, options=None)`
  - Packs bits into a word data type
- `copy_unpack(source, destination_range, oneshot=False, options=None)`
  - Unpacks word data type into bits
- `shift(range)` - Shift Data

## Data Type Compatibility for Copy Instructions

### Compatible Data Types
| Data Type | Compatible With | Packing/Unpacking |
|-----------|-----------------|-------------------|
| **int** (DS, TD, CTD, SD) | int, int2, float, hex | DS can pack into DD/DF and unpack into C |
| **int2** (DD) | int2, int, float, hex | DD can unpack into C/DS/DH |
| **float** (DF) | float, int, int2, hex | DF can unpack into C/DS |
| **hex** (XD, YD, DH) | hex, int, int2, float | DH can unpack into C/Y |
| **bit** (X, Y, C, T, CT, SC) | All bit types | X/Y/T/CT/SC can pack into DH, C can pack into DS/DD/DF/DH |
| **txt** (TXT) | txt | Can pack into DS/DD/DF/DH/TD/CTD |

## Search Instruction
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

## Advanced Math Instructions

### Hex Math
```python
math_hex(formula=lambda : expression, result_destination, one_shot=False)
```
Solves hexadecimal formulas with support for: 
- Arithmetic: `/`, `*`, `-`, `+`
- Grouping: `(` and `)`
- Logical: `OR`, `AND`, `XOR`, `NOT`
- Bit operators: `SHL`, `SHR`
- Other:
  - `MOD`, `SUM(address_range)`

## Advanced Program Control Instructions

- `for_loop(loops)` - Starts a For-Next loop to execute code multiple times in one scan
- `next_loop()` - Next instruction that marks the end of a For-Next loop

**Notes:**
- Nested For-Next loops are not permitted

## Advanced Example

```python
def main():
    # Advanced Data Manipulation Examples
    with Rung(c.UpdateBlockData):
        copy_block(ds[1:5], ds[100])  # Block copy
        copy_fill(df.SetpointTemp, df[20:30])  # Fill multiple Addresses
        copy_pack(c[1:17], dh.StatusWord)  # Pack bits into word
        copy_unpack(dd.ConfigWord, c[101:133])  # Unpack word to bits

    # Hex Math Example
    with Rung(c.UpdateStatusRegisters):
        math_hex("dh.InputStatus AND 0x00FF", dh.FilteredStatus)

    # Search instruction example
    with Rung(c.FindValue):
        search("==", ds.TargetValue, ds[100], ds[200], ds.ResultAddress, c.ValueFound)

    # For-loop Example
    # ----------------
    with Rung(re(c.UpdateAlarmHistory)):
        copy(0, ds.AlarmIndex)
        copy(0, ds.AlarmExtent)

    with Rung(c.UpdateAlarmHistory):
        for_loop(ds.MaxAlarms) # All logic between this and next_loop will be repeated

        # will loop ds.MaxAlarms times
        with Rung():
            math(lambda: ds.AlarmBase + ds.AlarmIndex, ds.CurrentAlarmAddr)
            math(lambda: ds.AlarmHistoryBase + ds.AlarmIndex, ds.HistoryAddr)
            copy(ds[ds.CurrentAlarmAddr], ds[ds.HistoryAddr])
            # Todo, replace this example below. Pointers not allowed in math
#             math(lambda: ds.AlarmExtent + ds[ds.CurrentAlarmAddr], ds.AlarmExtent)
            math(lambda: ds.AlarmIndex + 1, ds.AlarmIndex)

        with Rung():
            next_loop()  # Next instruction indicates the end of a For Next loop
    # End of For-loop Example

    with Rung():
        end()