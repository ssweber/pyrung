# pyrung → Click Paste Pipeline — Handoff

## Goal

Enable pasting pyrung programs directly into Click Programming Software. A pyrung `Program` exports to a CSV file describing the ladder grid layout. ClickNick reads that CSV, ensures operands exist in the project, converts to clipboard bytes, and pastes into Click.

## Architecture Split

### pyrung.click

- `Program.export_ladder()` — walks the Program structure, emits a fully expanded 32-column CSV
- Pure Python, cross-platform, no Windows dependency
- Knows ladder logic semantics but nothing about Click's byte format or clipboard mechanism
- Responsible for spatial layout of conditions, wires, and output instructions

### ClickNick

- Reads the ladder CSV
- Expands shorthand (`->`, `...`) if present in hand-authored CSVs
- Normalizes vertical wire rows as needed for Click's grid
- Validates operand addresses against the project `.mdb` via ODBC (already has this)
- Adds missing addresses/nicknames to the project (already has this)
- Construct-based codec: CSV → 8192-byte clipboard buffer
- Pastes into Click via HWND clipboard spoofing (format 522)
- Owns all reverse-engineering tooling, captures, debug dumps, and byte-level iteration

### The CSV is the stable contract between them.

## CSV Format Specification

### Physical Model

One column = one condition in Click's GUI. This is 1:1 with Click's physical grid — no expansion or spatial remapping needed. The 2-cell/4-cell byte spillover in Click's clipboard encoding is purely a codec concern inside ClickNick.

- 32 columns total: A through AE (31 condition columns) + AF (output column)
- One row per horizontal path in the rung

### Row Markers

| First Column | Meaning |
|---|---|
| `R` | New rung. New left power rail. |
| Blank | Continuation row of the current rung (branch or builder pin). |

### Condition Cell Values

| Cell Value | Meaning |
|---|---|
| `X1` | NO contact (examine-on) |
| `~X1` | NC contact (examine-off) |
| `rise(X1)` | Rising edge contact |
| `fall(X1)` | Falling edge contact |
| `Temp > 100` | Comparison condition |
| `-` | Horizontal wire |
| `T` | Horizontal wire + vertical connection down |
| `+` | Horizontal wire + vertical pass-through + vertical down |
| (blank) | Empty cell (no wire, no instruction) |
| `->` | Shorthand: wire-fill remaining columns to AF |
| `...` | Shorthand: empty-fill remaining columns to AF |

### Output Column (AF) Values

| Cell Value | Meaning |
|---|---|
| `out(Y1)` | OTE coil |
| `latch(Y2)` | OTL coil (set) |
| `reset(Y2)` | OTU coil (reset) |
| `on_delay(Done,Acc,100)` | Timer instruction |
| `calc(Acc+1,Acc)` | Math instruction |
| `.reset()` | Builder pin: connects to box instruction above (reset pin) |
| `.down()` | Builder pin: connects to counter above (down pin) |
| `.clock()` | Builder pin: connects to shift register above (clock pin) |

### Builder Pin Rows (Dot Prefix Convention)

Terminal builders in pyrung (`on_delay(...).reset(ResetBtn)`) are expressed as two CSV rows within the same rung. The dot prefix in column AF (`.reset()`, `.down()`, `.clock()`) signals:

1. This row belongs to the same rung (no `R` marker)
2. The row's conditions connect independently from the left power rail (NOT ANDed through the parent row's conditions)
3. The output connects to the corresponding pin on the box instruction in the row above

```csv
R,Start,->,on_delay(Done,Acc,100)
,ResetBtn,->,.reset()
```

```csv
R,Sensor,->,count_up(CntDone,CntAcc,100)
,DownSensor,->,.down()
,ResetBtn,->,.reset()
```

This mirrors the Click GUI where the timer/counter box has multiple input pins, each with its own condition path from the left rail.

### Branch Rows

Branches (`with branch(...)`) are also continuation rows (blank first column), but they differ from builder pin rows:

- Branch conditions ARE ANDed with the parent rung's conditions
- Column AF contains a normal instruction, not a dot-prefixed pin
- Vertical wires (`T`, `+`) connect the branch to the parent row

```csv
R,X1,T,->,out(Y1)
,X2,-,...,
```

```csv
R,X1,X2,T,->,out(Y1)
,,,-,X3,->,out(Y2)
```

### Three Row Types Summary

| First Col | Column AF | Conditions | Meaning |
|---|---|---|---|
| `R` | instruction | from rail | New rung |
| blank | instruction | ANDed with parent | Branch (`with branch(...)`) |
| blank | `.pin()` | from rail (independent) | Builder pin (`.reset()`, `.down()`, `.clock()`) |

### Expanded vs Shorthand

**pyrung always exports fully expanded** 32-column rows. Every cell is explicit — no `->` or `...`. This ensures stable diffs and unambiguous column positions.

The shorthand (`->`, `...`) exists in the format spec for hand-authored CSVs. ClickNick expands shorthand on ingest if present.

## CLI

### pyrung

```
pyrung export csv program.py         # emit fully expanded ladder.csv
pyrung export check program.py       # validate Click compatibility + layout feasibility
```

### ClickNick

```
clicknick paste ladder.csv           # validate .mdb → add addresses → generate bytes → paste
clicknick paste --dry-run ladder.csv  # preview without pasting
clicknick validate ladder.csv        # check CSV against project .mdb
```

## Examples

### Simple NO Contact + Coil

pyrung:
```python
with Rung(x[1]):
    out(y[1])
```

CSV (expanded, 32 cols shown truncated):
```csv
R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
```

### OR Logic (Parallel Branches)

pyrung:
```python
with Rung(x[1]):
    out(y[1])
    with branch(x[2]):
        pass
```

CSV:
```csv
R,X001,T,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
,X002,-,...,
```

### Series AND

pyrung:
```python
with Rung(x[1], x[2]):
    out(y[1])
```

CSV:
```csv
R,X001,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,out(Y001)
```

### Timer with Reset

pyrung:
```python
with Rung(x[1]):
    on_delay(TimerDone, TimerAcc, preset=3000, unit=Tms).reset(x[2])
```

CSV:
```csv
R,X001,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,on_delay(TimerDone,TimerAcc,3000)
,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,.reset()
```

### Counter with Down and Reset

pyrung:
```python
with Rung(rise(x[1])):
    count_up(CountDone, CountAcc, preset=100).down(x[2]).reset(x[3])
```

CSV:
```csv
R,rise(X001),-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,count_up(CountDone,CountAcc,100)
,X002,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,.down()
,X003,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,.reset()
```

### Comparison + Latch

pyrung:
```python
with Rung(Temp > 150.0):
    latch(OverTempAlarm)
```

CSV:
```csv
R,Temp > 150.0,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,latch(OverTempAlarm)
```

## Byte Encoding (ClickNick's Domain)

The clipboard byte format is documented in the existing `exploring/HANDOFF.md`. Key points relevant to this pipeline:

- Fixed 8192-byte buffer, 64-byte cells, 32 columns per row
- One GUI column may span multiple 64-byte cells in the binary encoding (2-cell spillover for contacts, 4-cell for coils)
- Instruction type IDs, operand strings (UTF-16LE), and function codes are at known offsets
- Currently template-based generation works for NO/NC contacts + Out coils
- Timer, counter, math, and comparison byte patterns still need captures
- ClickNick will use Construct for declarative struct definitions as the codec matures

## Open Questions

1. **Comparison conditions in the grid** — `Temp > 100` is one column in Click's GUI. How does pyrung export the tag names? Does it use Click addresses (`DS1 > 100`) or semantic names (`Temp > 100`)? If semantic, ClickNick needs the TagMap to resolve addresses.

2. **Vertical wire semantics** — When `T` appears on row 0 and row 1 is a branch, the vertical is clear. When there are 3+ rows (multiple branches or branches + builder pins), the `+` pass-through rules need explicit documentation. Current understanding: `T` = "start vertical down", `+` = "continue vertical through + branch down".

3. **Rung numbering** — `R` marks a new rung but carries no index. Should it? Click's GUI shows rung numbers. Could add `R1`, `R2` etc. but this creates fragile ordering. Current decision: just `R`, ClickNick assigns numbers on ingest.

4. **Empty continuation rows** — How are blank rows within a rung represented? A row with just a blank first column and empty cells? Or are they omitted?

5. **Subroutine boundaries** — pyrung has subroutines (`call("startup")`). Does the CSV represent these as flat rungs, or does it need subroutine markers?

## Links

- pyrung docs: https://ssweber.github.io/pyrung/
- pyrung llms.txt: https://ssweber.github.io/pyrung/llms.txt
- Click dialect: https://ssweber.github.io/pyrung/dialects/click/index.md
- Ladder logic reference: https://ssweber.github.io/pyrung/guides/ladder-logic/index.md
- Byte format details: see `exploring/HANDOFF.md` in clickplc-tools repo
