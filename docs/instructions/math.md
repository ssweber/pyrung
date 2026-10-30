# Math

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `calc()` — evaluate expression

```python
calc(DS[1] + DS[2], DS[3])              # DS3 = DS1 + DS2 (wraps to INT range)
calc(DS[1] * 2, DS[3], oneshot=True)    # One-shot: execute once per rung rising edge
calc(DH[1] | DH[2], DH[3])              # WORD-only math infers hex mode
```

## Overflow behavior

**Math wraps** — overflow truncates to the destination type's bit width (modular arithmetic). This differs from `copy()` which clamps.

| Expression | Destination | Result |
|------------|-------------|--------|
| `DS1 + 1` (DS1=32767) | INT (16-bit signed) | −32768 (wraps) |
| `50000 * 50000` | DINT (32-bit signed) | −1,794,967,296 (wraps) |
| `40000` → `copy()` | INT | 32767 (clamped) |

## REAL range and precision

A `Real` never leaves CLICK's range, ±3.4028235E+38. A `calc()` result past it saturates at the limit and sets `system.fault.out_of_range`; `copy()` saturates the same way. A default, literal, `patch()` or `force()` past it raises `ValueError`.

Inside the range pyrung keeps a `Real` at Python's 64-bit precision, where a CLICK REAL is a 32-bit float: `16777216.0 + 1` is 16777217 in pyrung and 16777216 on a CLICK. Compare a `Real` with a tolerance, not `==`, when the last digits matter.

## Division

- Division by zero produces result = 0 and sets `system.fault.division_error`.
- Integer division truncates toward zero: `−7 / 2 = −3`.

## Math errors

```python
calc(sqrt(Level), Root)     # Level = -4.0
```

A math function given an operand outside its domain — `sqrt` of a negative, `log`/`log10` of 0 or a negative, `asin`/`acos` outside −1..1 — stores 0 in the destination and sets `system.fault.math_operation_error` (CLICK's SC46). The rest of the scan runs, so a later rung can see the flag; the next scan starts with it off, like the other fault flags. The PLC stays in RUN. That is what a CLICK does, though its help for SC46 says "SC50 is turned to ON, the PLC is put in Stop Mode": on the hardware, SC50 stays off and the PLC keeps scanning.

An operand that is already infinite (from a division by zero earlier in the expression) is not a domain error: the result is not finite, so it is 0 with `division_error`.

## Mode inference

`calc()` infers arithmetic mode from referenced tag types (including destination):

| Family | Inferred mode |
|--------|----------------|
| WORD-only | `"hex"` (unsigned 16-bit wrap) |
| Any non-WORD present | `"decimal"` (signed arithmetic) |

For CLICK portability, do not mix WORD and non-WORD math in the same `calc()` expression. CLICK validation reports `CLK_CALC_MODE_MIXED` for mixed-family expressions.

## Numeric behavior summary

| Operation | Out-of-range behavior |
|-----------|----------------------|
| `copy()` | Clamps to destination min/max |
| `calc()` | Wraps (modular arithmetic) |
| Timer accumulator | Clamps at 32,767 |
| Counter accumulator | Clamps at DINT min/max |
| REAL past ±3.4028235E+38 | Saturates; `calc()` sets `system.fault.out_of_range` |
| Division by zero | Result = 0, `system.fault.division_error` set |
| Math domain error (`sqrt(-1)`, `log(0)`, `asin(2)`) | Result = 0, `system.fault.math_operation_error` set for the scan |

## Range aggregates

```python
calc(DS.select(1, 10).sum(), Total)      # DS1 + DS2 + … + DS10
calc(DS.select(1, 10).max(), Peak)       # largest value
calc(DS.select(1, 10).min(), Low)        # smallest value
calc(DF.select(1, 8).median(), Mid)      # middle value
calc(DF.select(1, 8).mean(), Avg)        # average
calc(C.select(1, 16).sum(), OnCount)     # Bool range: counts True bits
```

Each method returns a lazy expression over the range, read at scan time. They compose with the rest of the expression:

```python
calc(DS.select(1, 10).max() + abs(Offset), Result)
calc(DS.select(1, 10).sum() / 10, Result)
```

`median()` of an even count is the mean of the two middle values; over integers, as on a CLICK, that mean is truncated toward zero (`{9, 2, 10, 1}` gives 5, not 5.5), and over a `Real` range it keeps the fraction. `mean()` and `median()` are computed exactly; the destination decides what is stored, so an `Int` destination truncates toward zero (`2.5` → `2`, `-4.5` → `-4`) and a `Real` keeps the fraction. On a WORD range (hex mode) values are unsigned 16-bit, so `max()` of `0x0001, 0xFFFF` is `0xFFFF`.

The integer `MDN` rule is CLICK's (hardware, 2026-10-29). CLICK's own rounding for `AVG`, `MDN` of a `Real` range, and whether `MAX`/`MIN` treat DH as unsigned are **unconfirmed on hardware**.

Mode inference applies normally: WORD-only ranges infer hex mode, anything else infers decimal.

## `abs()`

```python
calc(abs(Error), Magnitude)
```

Python's built-in `abs` works on any expression. CLICK offers `ABS` in decimal mode only, so `abs()` of WORD tags is a portability finding.

## `randint()`

```python
from pyrung.core import randint

calc(randint(1, 6), Die)                 # 1..6 inclusive, a new draw every scan
calc(randint(Lo, Hi) * 10, Setpoint)     # bounds may be tags, read as the scan starts
```

`randint(lo, hi)` includes both ends, like Python's `random.randint`. If `lo` is above `hi`, it draws between them.

Draws are deterministic, so the simulation stays replayable. Each `randint` call in the program is one site, and its value is a fixed function of the site and the scan number. Two runs of the same program draw the same sequence; replay, `history.at()`, seek and `fork()` see the values the original run saw; and a fork draws what its parent would have. Restarting (STOP -> RUN, `reboot()`) resets the scan number, so the sequence starts over.

A site's draw is an input: the scan writes it into a hidden tag (`expr.tag`, named `_rand.<n>`) before logic runs. A test chooses a draw the way it sets any input:

```python
roll = randint(1, 6)
# ... calc(roll, Die) in the program ...
plc.patch({roll.tag: 6})                 # next scan draws 6
plc.force(roll.tag, 3)                   # every scan draws 3 until unforced
```

The verifier explores every value a draw can take: `lo..hi` for literal bounds, or the range a bound tag declares (`min`/`max` or `choices`). A bound tag with no declared range makes the result `Intractable`, with a hint naming the draw. `run_for()` and `run_until()` do not skip scans in a program that uses `randint`. `how()` treats a draw like any other input, so a plan may say to wait for a lucky draw or to patch the site's tag.

`randint` is not allowed inside a `forloop()`: every iteration would read the scan's one draw, while CLICK draws again on each execution. On CircuitPython the board calls `random.randint` directly, so its draws are not replayable.

## Range aggregates on CLICK

| pyrung | CLICK formula | Hex mode |
|--------|---------------|----------|
| `rng.sum()` | `SUM ( DS1 : DS10 )` | yes |
| `rng.max()` | `MAX ( DS1 : DS10 )` | yes |
| `rng.min()` | `MIN ( DS1 : DS10 )` | yes |
| `rng.median()` | `MDN ( DS1 : DS10 )` | yes |
| `rng.mean()` | `AVG ( DS1 : DS10 )` | yes |
| `abs(x)` | `ABS ( x )` | no |
| `randint(lo, hi)` | `RAND ( lo , hi )` | yes |

pyrung spells these the way Python does (`statistics.median`, `statistics.mean`); the exporter writes CLICK's name and the importer reads it back. A sum over bits exports as plain `SUM ( C1 : C16 )`.

The exporter spaces every paren and separator the way CLICK's formula editor does (`SQRT ( DS1 )`, `LSH ( DH1 , 2 )`, `DS1 * ( DS2 + 1 )`), so an exported CSV matches what a copy-back from CLICK decodes to. The importer also reads the unspaced form (`ABS(DS1)`) for hand-written CSVs.

In pyrung the aggregates work anywhere an expression does (`copy()` sources, rung comparisons). CLICK accepts them only inside Math, and validation says so (`CLK_EXPR_ONLY_IN_CALC`). CLICK validation also checks each range:

| Finding | Rule |
|---------|------|
| `CLK_CALC_RANGE_NOT_CONTIGUOUS` | The range must map to consecutive addresses, low to high (drop `.reverse()`). |
| `CLK_CALC_RANGE_MIXED_BANKS` | Both ends must be in the same bank. |
| `CLK_CALC_MEDIAN_RANGE_TOO_LARGE` | `MDN` covers at most 1024 bytes: 512 DS/DH or 256 DD/DF. |
| `CLK_CALC_BIT_RANGE_AGGREGATE` | Only `SUM` accepts a bit range (X, Y, C, T, CT, SC). |
