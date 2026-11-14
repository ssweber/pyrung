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

## Wide constants

CLICK's editor refuses a whole-number constant outside −2147483648..2147483647 in any Math. A hex-mode Math (only `Word` tags) reads a constant as its low 16 bits, so write it as 0..65535. CLICK validation reports both (`CLK_MATH_WIDE_CONSTANT`).

## REAL range and precision

A CLICK REAL is a 32-bit float, so a `math()` or `copy()` result past ±3.4028235E+38 is +Inf or −Inf. A `math()` also sets `system.fault.out_of_range` (SC43). Inf and NaN follow IEEE 754 in later arithmetic. A default or literal must be finite and in range. A `patch()` or `force()` of a finite value past the range raises `ValueError`.

Into an `Int`, `Dint` or `Word`, a `math()` result of ±Inf stores 0 and sets `out_of_range`, and NaN stores 0 with no flag. A `copy()` of ±Inf clamps instead (see [Copy](copy.md)).

The range is checked when `math()` stores its result, not at each step of the formula.

Inside the range pyrung keeps a `Real` at Python's 64-bit precision, while a CLICK REAL is a 32-bit float. For example, `16777216.0 + 1` is 16777217 in pyrung and 16777216 on a CLICK. Compare a `Real` with a tolerance, not `==`, when the last digits matter.

## Division

- A division by zero is 0 and sets `system.fault.division_error`. The rest of the formula runs, so `math(X / Zero + 5, D)` stores 5. Every later `math()` clears the flag, including one on a false rung and a one-shot that does not fire. A `copy()`, coil, timer or counter leaves it. Read it on the next rung.
- Integer division truncates toward zero: `−7 / 2 = −3`.
- In a `math()`, `%` (CLICK's MOD) truncates too, so the remainder takes the dividend's sign. `−7 % 2 = −1`, where Python's `%` gives 1. A MOD by zero is 0 with no flag, and the rest of the formula runs. Anywhere else `%` is Python's, so an index such as `DS[(P - 1) % 10 + 1]` wraps 0 to 10.

## Math errors

```python
math(sqrt(Level), Root)     # Level = -4.0
```

A math function given an operand outside its domain is 0 and sets `system.fault.math_operation_error` (CLICK's SC46). Examples are `sqrt` of a negative and `log` of 0. The rest of the formula runs, so `sqrt(Level) + 5` stores 5. The PLC stays in RUN, as a real CLICK does.

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

`median()` of an even count is the mean of the two middle values. Over integers that mean is truncated toward zero (`{9, 2, 10, 1}` gives 5), as on a CLICK. On a WORD range (hex mode) values are unsigned 16-bit.

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

- Equal bounds are a math error. The `randint()` reads 0 and the `math()` sets `system.fault.math_operation_error`.
- A float or expression bound is read as an integer, so `randint(2.5, 2.9)` has equal bounds.
- A draw outside the destination's range wraps and sets `system.fault.out_of_range`, as any `math()` result does.

CLICK's editor takes only an integer constant or an `Int`/`Dint` address as a bound. CLICK validation refuses:

- any other bound: a `Real` tag, a float constant, a bit, a `Word` or text (`CLK_RAND_BOUND_TYPE`)
- equal constant bounds (`CLK_RAND_EQUAL_BOUNDS`)
- a bound that is an expression, such as `randint(0, N / 2)` (`CLK_MATH_ARG_NOT_PLAIN`)

Use constants or an `Int`/`Dint` tag as bounds. Compute an expression bound into a tag in a `math()` above. For a one-value draw, use a plain constant.

Draws are deterministic, so a run replays the same sequence. A test chooses a draw through the call's hidden tag, as it sets any input:

```python
roll = randint(1, 6)
# ... math(roll, Die) in the program ...
plc.patch({roll.tag: 6})                 # next scan draws 6
plc.force(roll.tag, 3)                   # every scan draws 3 until unforced
```

A hex-mode draw whose bounds are cut to 16 bits is chosen through `roll.ucut_tag` instead. The verifier explores every value a draw can take. A bound tag with no `min`/`max` or `choices` makes the answer `Intractable`, so declare a range.

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
