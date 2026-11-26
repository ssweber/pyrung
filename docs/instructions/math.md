# Math

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `math()` — evaluate expression

```python
math(DS[1] + DS[2], DS[3])              # DS3 = DS1 + DS2 (wraps to INT range)
math(DS[1] * 2, DS[3], oneshot=True)    # One-shot: execute once per rung rising edge
math(DH[1] | DH[2], DH[3])              # WORD-only math infers hex mode
```

The expression comes first and the destination second, as in every pyrung instruction (`copy(source, dest)`, `blockcopy(source, dest)`). CLICK's Math dialog puts the result first.

`math()` refuses a TXT as the destination. A TXT holds only text. Write into a number tag, then `copy()` that tag into the TXT. The copy writes the number's text.

## Overflow behavior

**Math wraps.** Overflow truncates to the destination type's bit width (modular arithmetic). This differs from `copy()`, which clamps.

| Expression | Destination | Result |
|------------|-------------|--------|
| `DS1 + 1` (DS1=32767) | INT (16-bit signed) | −32768 (wraps) |
| `DD1 + 1` (DD1=2147483647) | DINT (32-bit signed) | −2,147,483,648 (wraps) |
| `40000` → `copy()` | INT | 32767 (clamped) |

## Wide constants

CLICK's editor rejects a whole-number constant outside −2147483648..2147483647 in any Math. A hex-mode Math (only `Word` tags) accepts a whole number from 0 to 65535. For −32768..−1 in hex mode, pyrung writes the constant as its 16 bits, so −1 exports as FFFFh. CLICK validation reports any other constant (`math-constant-out-of-range`). The export rejects any other hex-mode constant, even when you allow that finding, because no CLICK hex constant can hold it.

## REAL range and precision

A CLICK REAL is a 32-bit float. A `math()` or `copy()` result past ±3.4028235E+38 becomes +Inf or −Inf. A `math()` also sets `system.fault.out_of_range` (SC43). Later arithmetic on Inf and NaN follows IEEE 754. A default or literal must be finite and in range. A `patch()` or `force()` of a finite value past the range raises `ValueError`.

If a `math()` result of ±Inf goes into an `Int`, `Dint` or `Word`, it stores 0 and sets `out_of_range`. A NaN result stores 0 and sets no flag. A `copy()` of ±Inf clamps instead (see [Copy](copy.md)).

A NaN result in a `Real` reads 0xFFFF:0xFFFF over Modbus, as on a CLICK. A NaN that a client writes keeps its own bits, and a `copy()` of that tag keeps them too. The P1AM sends every NaN as 0xFFFF:0xFFFF (see [CircuitPython Modbus](../dialects/circuitpy-modbus.md#which-registers-the-server-serves)).

`math()` checks the range when it stores its result, not at each step of the formula.

Inside the range, pyrung keeps a `Real` at Python's 64-bit precision. A CLICK REAL is a 32-bit float. For example, `16777216.0 + 1` is 16777217 in pyrung and 16777216 on a CLICK. Compare a `Real` with a tolerance, not `==`, when the last digits matter.

## Division

- A division by zero gives 0 and sets `system.fault.division_error`. The rest of the formula runs, so `math(X / Zero + 5, D)` stores 5.
- Every later `math()` clears the flag. This includes a `math()` on a false rung and a one-shot that does not fire. A `copy()`, coil, timer or counter leaves the flag alone. Read it on the next rung.
- Integer division truncates toward zero: `−7 / 2 = −3`.
- In a hex-mode `math()` (only `Word` tags), each `/` truncates its own result. `DH1 / DH2 * DH2` with 7 and 2 stores 6, the same as on a CLICK. A decimal `math()` keeps the fraction until it stores the result, so the same formula on `Int` tags stores 7.
- In a `math()`, `%` (CLICK's MOD) truncates too, so the remainder takes the dividend's sign. `−7 % 2 = −1`, where Python's `%` gives 1.
- A MOD by zero gives 0 and sets `system.fault.math_operation_error` (CLICK's SC46). The rest of the formula runs.
- Outside a `math()`, `%` is Python's. An index such as `DS[(P - 1) % 10 + 1]` wraps 0 to 10.

## Math errors

```python
math(sqrt(Level), Root)     # Level = -4.0
```

A math function that receives an operand outside its domain gives 0 and sets `system.fault.math_operation_error` (CLICK's SC46). Examples are `sqrt` of a negative and `log` of 0. The rest of the formula runs, so `sqrt(Level) + 5` stores 5. The PLC stays in RUN, as a real CLICK does.

## Math outside `math()`

An expression outside `math()` is permissive. That covers a rung condition, a `copy()` source, a `forloop()` count and an index expression such as `DS[I + 1]`. If an operation in it causes a math error (a division by zero, or a math function given an operand outside its domain), the operation gives 0. It sets no fault flag and never stops the scan. This is pyrung's own rule. A CLICK has no expressions outside Math, and the CLICK validator flags them. Use `math()` when you need the flag.

## Mode inference

`math()` infers arithmetic mode from referenced tag types (including destination):

| Family | Inferred mode |
|--------|----------------|
| WORD-only | `"hex"` (unsigned 16-bit wrap) |
| Any non-WORD present | `"decimal"` (signed arithmetic) |

```python
math(-DH[1], DH[2])         # DH1 = 2: stores FFFEh, sets out_of_range
math(-DH[1] | 0, DH[3])     # stores FFFEh, sets no flag
```

A hex-mode result below 0 or above FFFFh keeps its low 16 bits and sets `system.fault.out_of_range` (CLICK's SC43). AND, OR and XOR give 16 unsigned bits, so a formula whose last step is one of them never sets the flag. A CLICK does the same.

For CLICK portability, do not mix WORD and non-WORD math in the same `math()` expression. pyrung exports such a `math()` in decimal mode. A decimal Math refuses a `Word` tag. CLICK validation reports `math-operand-bank` for each `Word` tag in the formula. It reports `bank-not-allowed` for a DH or YD destination of a formula without `Word` tags.

CLICK's editor accepts only a tag or a constant as an argument of `lsh`, `rsh`, `lro` and `rro`. It accepts only a tag or a constant on either side of `<<` and `>>`. CLICK validation reports `math-arg-expression` for `lsh(W + 1, 2)` or `(W & 0xFF) << 2`. Compute the value into a tag in a `math()` above. The same rule applies to `randint()` bounds.

## Range aggregates

```python
math(DS.select(1, 10).sum(), Total)      # DS1 + DS2 + … + DS10
math(DS.select(1, 10).max(), Peak)       # largest value
math(DS.select(1, 10).min(), Low)        # smallest value
math(DF.select(1, 8).median(), Mid)      # middle value
math(DF.select(1, 8).mean(), Avg)        # average
math(C.select(1, 16).sum(), OnCount)     # Bool range: counts True bits
```

Each method returns a lazy expression over the range. pyrung reads the range at scan time. You can combine these expressions with the rest of the formula:

```python
math(DS.select(1, 10).max() + abs(Offset), Result)
math(DS.select(1, 10).sum() / 10, Result)
```

`median()` of an even count is the mean of the two middle values. Over integers, that mean is truncated toward zero (`{9, 2, 10, 1}` gives 5), as on a CLICK. On a WORD range (hex mode), values are unsigned 16-bit.

Mode inference applies normally. WORD-only ranges infer hex mode, and anything else infers decimal.

## `abs()`

```python
math(abs(Error), Magnitude)
```

Python's built-in `abs` works on any expression. CLICK offers `ABS` in decimal mode only. Validation therefore reports `abs()` of WORD tags as a portability finding.

## `randint()`

```python
from pyrung import randint

math(randint(1, 6), Die)                 # 1..6 inclusive, a new draw every scan
math(randint(Lo, Hi) * 10, Setpoint)     # bounds may be tags, read as the scan starts
```

`randint(lo, hi)` includes both ends, like Python's `random.randint`. If `lo` is above `hi`, it still draws between them.

- Equal bounds are a math error. The `randint()` returns 0 and the `math()` sets `system.fault.math_operation_error`.
- A float or expression bound is read as an integer, so `randint(2.5, 2.9)` has equal bounds.
- A draw outside the destination's range wraps and sets `system.fault.out_of_range`, like any other `math()` result.

A decimal Math accepts an integer constant or a DS or DD tag as a bound. A hex Math accepts an integer constant or a DH tag. CLICK validation rejects:

- any other bound: a `Real` tag, a float constant, a bit, a TD, CTD, SD, XD or YD tag, a DH tag in a decimal Math, or text (`random-bound-type`)
- equal bounds, such as `randint(5, 5)` or `randint(N, N)` (`random-equal-bounds`)
- a bound that is an expression, such as `randint(0, N / 2)` (`math-arg-expression`)

Use constants or a DS or DD tag as bounds. Compute an expression bound into a tag in a `math()` above. For a one-value draw, use a plain constant.

Draws are deterministic, so a run replays the same sequence. In a test, you choose a draw through the call's hidden tag, the same way you set any input:

```python
roll = randint(1, 6)
# ... math(roll, Die) in the program ...
plc.patch({roll.tag: 6})                 # next scan draws 6
plc.force(roll.tag, 3)                   # every scan draws 3 until unforced
```

For a hex-mode draw whose bounds are cut to 16 bits, use `roll.ucut_tag` instead.

You cannot use `randint` inside a `forloop()`. Every iteration would read the scan's one draw, while CLICK draws again on each execution. On CircuitPython the board calls `random.randint` directly, so you cannot replay its draws.

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

In pyrung, the aggregates work anywhere an expression does (`copy()` sources, rung comparisons). CLICK accepts them only inside Math, and validation reports this (`CLK_EXPR_ONLY_IN_MATH`). CLICK validation also checks each range:

| Finding | Rule |
|---------|------|
| `CLK_MATH_RANGE_NOT_CONTIGUOUS` | The range must map to consecutive addresses, low to high (drop `.reverse()`). A range that starts at XD0 or YD0 must end there, since CLICK's XD0:XD1 also holds XD0u. |
| `CLK_MATH_RANGE_MIXED_BANKS` | Both ends must be in the same bank. |
| `median-range-too-large` | `MDN` covers at most 1024 bytes: 512 DS/DH or 256 DD/DF. |
| `math-operand-bank` | `MAX`, `MIN`, `MDN` and `AVG` reject a range of bits (X, Y, C, T, CT, SC) or of TXT. In a decimal Math they also reject a `Word` range. `SUM` accepts a bit range. |
| `sum-bank-unavailable` | In decimal mode, `SUM` reads X, Y, C, T, SC, DS, DD, DF, TD, CTD or SD. In hex mode, it reads DH, XD or YD. |

The export writes a range as its first and last address, such as `SUM ( DS1 : DS9 )`. CLICK reads every address from the first to the last. So the export rejects a range that `CLK_MATH_RANGE_NOT_CONTIGUOUS` or `CLK_MATH_RANGE_MIXED_BANKS` reports, even with `allow=`.
