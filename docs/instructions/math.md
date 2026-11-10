# Math

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `math()` — evaluate expression

```python
math(DS[1] + DS[2], DS[3])              # DS3 = DS1 + DS2 (wraps to INT range)
math(DS[1] * 2, DS[3], oneshot=True)    # One-shot: execute once per rung rising edge
math(DH[1] | DH[2], DH[3])              # WORD-only math infers hex mode
```

The expression comes first and the destination second, as in every pyrung instruction (`copy(source, dest)`, `blockcopy(source, dest)`). CLICK's Math dialog puts the result first.

## Overflow behavior

**Math wraps.** Overflow truncates to the destination type's bit width (modular arithmetic). This differs from `copy()`, which clamps.

| Expression | Destination | Result |
|------------|-------------|--------|
| `DS1 + 1` (DS1=32767) | INT (16-bit signed) | −32768 (wraps) |
| `DD1 + 1` (DD1=2147483647) | DINT (32-bit signed) | −2,147,483,648 (wraps) |
| `40000` → `copy()` | INT | 32767 (clamped) |

## Constants past 16 bits

A CLICK Math keeps every whole-number constant in its formula in 16 bits, whatever the destination, and pyrung does the same. A constant outside −32768..32767 is read as its low 16 bits, signed (40000 is −25536), or unsigned in hex mode. `math(DD[1] / 65536, DD[2])` divides by 0 (65536 is cut to 0), so it writes 0 and sets `system.fault.division_error`. The same 65536 read from a tag (`math(DD[1] / DD[3], DD[2])`) divides as written. Stage a wide constant in a DINT tag in a rung above and use the tag. CLICK validation reports such a constant (`CLK_MATH_WIDE_CONSTANT`).

## REAL range and precision

A CLICK REAL is a 32-bit IEEE 754 float, so a finite `Real` never leaves ±3.4028235E+38. A `math()` result past it is +Inf or −Inf, as a CLICK stores it, and sets `system.fault.out_of_range` (SC43). A `copy()` past it is ±Inf too. A default or literal must be finite and in range. A `patch()` or `force()` of a finite value past it raises `ValueError`. A `patch()` or `force()` may set ±Inf or NaN.

```python
math(Big * 10, Result)      # Big = 3.0E+38: Result = +Inf, out_of_range on
math(Result / 2, Half)      # Half = +Inf: Inf stays Inf
```

An Inf or NaN operand follows IEEE 754:

- `Inf / 2` is Inf.
- `Inf − Inf` and `sin(Inf)` are NaN.
- `Inf > 1.0E+38` is true.
- NaN compares false with everything (`R == R` is false for a NaN).

A `math()` whose result is NaN sets no flag.

Where Inf or NaN meets an integer:

- A `math()` result of ±Inf stores 0 into an `Int`, `Dint` or `Word` and sets `out_of_range`. NaN stores 0 with no flag.
- A `copy()` of ±Inf into an `Int` clamps to its limit and sets `out_of_range`, and into a `Dint` clamps with no flag. NaN stores 0 with no flag. Into a `Word`, +Inf stores 65535 and −Inf 32768, with no flag.

Inside the range pyrung keeps a `Real` at Python's 64-bit precision, while a CLICK REAL is a 32-bit float. For example, `16777216.0 + 1` is 16777217 in pyrung and 16777216 on a CLICK. Compare a `Real` with a tolerance, not `==`, when the last digits matter.

pyrung checks the range when `math()` stores its result, not at each step of the formula. Inside one `math()` each intermediate result is a 64-bit float, as on a CLICK, so `math(Big * 10 / 100, Result)` with `Big` = 3.0E+38 stores 3.0E+37 with no flag.

## Division

- A division by zero is 0 and sets `system.fault.division_error`. The rest of the formula runs, so `math(X / Zero + 5, D)` stores 5. The next `math()` to run sets the flag again only if it divides by zero too, so read the flag on the next rung. A `copy()` leaves it.
- Integer division truncates toward zero: `−7 / 2 = −3`.
- In a `math()`, `%` (CLICK's MOD) truncates too, so the remainder takes the dividend's sign. `−7 % 2 = −1`, where Python's `%` gives 1. A MOD by zero is 0 with no flag, and the rest of the formula runs. Anywhere else `%` is Python's, so an index such as `DS[(P - 1) % 10 + 1]` wraps 0 to 10.

## Math errors

```python
math(sqrt(Level), Root)     # Level = -4.0
```

A math function given an operand outside its domain is 0 and sets `system.fault.math_operation_error` (CLICK's SC46). Examples are `sqrt` of a negative, `log`/`log10` of 0 or a negative, and `asin`/`acos` outside −1..1. The rest of the formula runs, so `sqrt(Level) + 5` stores 5. The rest of the scan runs too, so a later rung can see the flag. The next scan starts with the flag off, like the other fault flags. The PLC stays in RUN, as a real CLICK does, even though CLICK's help for SC46 says it goes to STOP.

A negative base to a fractional power (`(-8.0) ** 0.5`) is a math error too.

## Math outside `math()`

An expression outside `math()` is permissive. That covers a rung condition, a `copy()` source, a `forloop()` count and an index expression such as `DS[I + 1]`. An operation in it that hits a math error (a division by zero, a math function outside its domain) gives 0, sets no fault flag and never stops the scan. This is pyrung's own rule. A CLICK has no expressions outside Math, and the CLICK validator flags them. Use `math()` when the flag matters.

## Mode inference

`math()` infers arithmetic mode from referenced tag types (including destination):

| Family | Inferred mode |
|--------|----------------|
| WORD-only | `"hex"` (unsigned 16-bit wrap) |
| Any non-WORD present | `"decimal"` (signed arithmetic) |

For CLICK portability, do not mix WORD and non-WORD math in the same `math()` expression. CLICK validation reports `CLK_MATH_MODE_MIXED` for mixed-family expressions.

CLICK's editor takes only a tag or a constant as an argument of `lsh`, `rsh`, `lro` and `rro`, and as either side of `<<` and `>>`. CLICK validation reports `CLK_MATH_ARG_NOT_PLAIN` for `lsh(W + 1, 2)` or `(W & 0xFF) << 2`. Compute the value into a tag in a `math()` above. The same holds for `randint()` bounds.

## Numeric behavior summary

| Operation | Out-of-range behavior |
|-----------|----------------------|
| `copy()` | Clamps to the destination's limits (exceptions in [Copy](copy.md)) |
| `math()` | Wraps (modular arithmetic) |
| Timer accumulator | Clamps at 32,767 |
| Counter accumulator | Clamps at DINT min/max |
| REAL past ±3.4028235E+38 | ±Inf; `math()` sets `system.fault.out_of_range` |
| Division by zero | That division = 0, the rest of the formula runs, `system.fault.division_error` set |
| Indexed source below its block (`math(DS[Ptr], D)`) | Nothing written, `system.fault.address_error` set |
| Indexed source past its block | Reads the type's zero value, no flag |
| Math domain error (`sqrt(-1)`, `log(0)`, `asin(2)`) | That function = 0, the rest of the formula runs, `system.fault.math_operation_error` set for the scan |

## Range aggregates

```python
math(DS.select(1, 10).sum(), Total)      # DS1 + DS2 + … + DS10
math(DS.select(1, 10).max(), Peak)       # largest value
math(DS.select(1, 10).min(), Low)        # smallest value
math(DF.select(1, 8).median(), Mid)      # middle value
math(DF.select(1, 8).mean(), Avg)        # average
math(C.select(1, 16).sum(), OnCount)     # Bool range: counts True bits
```

Each method returns a lazy expression over the range, read at scan time. They compose with the rest of the expression:

```python
math(DS.select(1, 10).max() + abs(Offset), Result)
math(DS.select(1, 10).sum() / 10, Result)
```

`median()` of an even count is the mean of the two middle values. Over integers, as on a CLICK, that mean is truncated toward zero (`{9, 2, 10, 1}` gives 5, not 5.5). Over a `Real` range it keeps the fraction. `mean()` and `median()` are computed exactly, and the destination decides what is stored. An `Int` destination truncates toward zero (`2.5` → `2`, `-4.5` → `-4`) and a `Real` keeps the fraction. On a WORD range (hex mode) values are unsigned 16-bit, so `max()` of `0x0001, 0xFFFF` is `0xFFFF`.

Mode inference applies normally. WORD-only ranges infer hex mode, and anything else infers decimal.

## `abs()`

```python
math(abs(Error), Magnitude)
```

Python's built-in `abs` works on any expression. CLICK offers `ABS` in decimal mode only, so `abs()` of WORD tags is a portability finding.

## `randint()`

```python
from pyrung.core import randint

math(randint(1, 6), Die)                 # 1..6 inclusive, a new draw every scan
math(randint(Lo, Hi) * 10, Setpoint)     # bounds may be tags, read as the scan starts
```

`randint(lo, hi)` includes both ends, like Python's `random.randint`. If `lo` is above `hi`, it draws between them.

- Equal bounds are a math error. Examples are `randint(7, 7)` and two `Int`/`Dint` tags that hold the same value. The `randint()` reads 0 and the rest of the formula runs (`randint(7, 7) + 5` stores 5). The `math()` sets `system.fault.math_operation_error` (SC46).
- An expression or a float literal as a bound is read as an integer (`randint(0, N / 2)`, `sqrt(N)`), so `randint(2.5, 2.9)` has equal bounds (2 and 2) and is a math error.
- Into an `Int`, and in hex mode into a `Word`, each constant bound is cut to 16 bits like any constant in the formula ([Constants past 16 bits](#constants-past-16-bits)). `randint(70000, 70002)` draws 4464..4466. Into a `Dint` or `Real` the bounds stay whole.

CLICK's editor takes only an integer constant or an `Int`/`Dint` address as a bound. CLICK validation refuses:

- any other bound: a `Real` tag, a float constant, a bit, a `Word` or text (`CLK_RAND_BOUND_TYPE`)
- equal constant bounds (`CLK_RAND_EQUAL_BOUNDS`)
- a bound that is an expression, such as `randint(0, N / 2)` (`CLK_MATH_ARG_NOT_PLAIN`)

Use constants or an `Int`/`Dint` tag as bounds. Compute an expression bound into a tag in a `math()` above. For a one-value draw, use a plain constant.

Draws are deterministic, so the simulation stays replayable. Each `randint` call in the program draws a value that is a fixed function of that call and the scan number. Two runs of the same program draw the same sequence. Replay, `history.at()`, seek and `fork()` see the values the original run saw. A fork draws what its parent would have. Restarting (STOP -> RUN, `reboot()`) resets the scan number, so the sequence starts over.

Each `randint` call's draw is an input. The scan writes it into a hidden tag (`expr.tag`, named `_rand.<n>`) before logic runs. A test chooses a draw the way it sets any input:

```python
roll = randint(1, 6)
# ... math(roll, Die) in the program ...
plc.patch({roll.tag: 6})                 # next scan draws 6
plc.force(roll.tag, 3)                   # every scan draws 3 until unforced
```

A draw cut into an `Int` is chosen through `expr.cut_tag` instead.

The verifier explores every value a draw can take: `lo..hi` for literal bounds, or the range a bound tag declares (`min`/`max` or `choices`). A bound tag with no declared range, or equal constants, makes the result `Intractable`, with a hint naming the draw. `how()` treats a draw like any other input, so a plan may say to wait for a lucky draw or to patch the draw's tag.

`randint` is not allowed inside a `forloop()`. Every iteration would read the scan's one draw, while CLICK draws again on each execution. On CircuitPython the board calls `random.randint` directly, so its draws are not replayable.

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

In pyrung the aggregates work anywhere an expression does (`copy()` sources, rung comparisons). CLICK accepts them only inside Math, and validation says so (`CLK_EXPR_ONLY_IN_MATH`). CLICK validation also checks each range:

| Finding | Rule |
|---------|------|
| `CLK_MATH_RANGE_NOT_CONTIGUOUS` | The range must map to consecutive addresses, low to high (drop `.reverse()`). |
| `CLK_MATH_RANGE_MIXED_BANKS` | Both ends must be in the same bank. |
| `CLK_MATH_MEDIAN_RANGE_TOO_LARGE` | `MDN` covers at most 1024 bytes: 512 DS/DH or 256 DD/DF. |
| `CLK_MATH_BIT_RANGE_AGGREGATE` | `MAX`, `MIN`, `MDN` and `AVG` refuse a bit range (X, Y, C, T, CT, SC); `SUM` accepts one. |
| `CLK_MATH_SUM_BANK_NOT_AVAILABLE` | `SUM` reads X, Y, C, T, SC, DS, DD, DF, TD, CTD, or SD in decimal mode, and DH, XD, or YD in hex mode. |
