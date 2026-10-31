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

## Constants into an INT

A CLICK Math whose destination is an INT keeps every whole-number constant in its formula in 16 bits, and pyrung does the same: a constant outside −32768..32767 is read as its low 16 bits, signed. `calc(DD[1] / 65536, DS[1])` divides by 0 (65536 is cut to 0), so it writes 0 and sets `system.fault.division_error`; the same 65536 read from a tag (`calc(DD[1] / DD[2], DS[1])`) divides as written, and a DINT or REAL destination keeps the constant whole. Stage a wide constant in a DINT, or write the result to a DINT, when it must stay wide. The CLICK export writes the formula as you wrote it. Measured on a CLICK for 65536; for a constant between 32768 and 65535, pyrung's signed reading (40000 is −25536) has not been checked on hardware.

## REAL range and precision

A CLICK REAL is a 32-bit IEEE 754 float, so a finite `Real` never leaves ±3.4028235E+38. A `calc()` result past it is +Inf or −Inf, as a CLICK stores it, and sets `system.fault.out_of_range` (SC43); a `copy()` past it is ±Inf too. A default or literal must be finite and in range, and a `patch()` or `force()` of a finite value past it raises `ValueError`; a `patch()` or `force()` may set ±Inf or NaN.

```python
calc(Big * 10, Result)      # Big = 3.0E+38: Result = +Inf, out_of_range on
calc(Result / 2, Half)      # Half = +Inf: Inf stays Inf
```

An Inf or NaN operand follows IEEE 754: `Inf / 2` is Inf, `Inf − Inf` and `sin(Inf)` are NaN, `Inf > 1.0E+38` is true and NaN compares false with everything (`R == R` is false for a NaN). A `calc()` whose result is Inf or NaN sets `out_of_range`.

Where Inf or NaN meets an integer, pyrung makes a choice of its own, not yet measured on a CLICK:

- A `calc()` result that is Inf or NaN stores 0 into an `Int`, `Dint` or `Word`.
- A `copy()` of ±Inf into an `Int` or `Dint` clamps to its limit; NaN, or anything not finite into a `Word`, stores 0.
- A bitwise or shift operation (`&`, `|`, `^`, `<<`, `>>`, `~`, `lsh`, `rsh`, `lro`, `rro`) on an Inf or NaN operand, or a shift (`<<`, `>>`, `lsh`, `rsh`) by a negative count, makes the `calc()` store 0 and set `out_of_range`.
- A [`randint()`](#randint) bound that is Inf or NaN makes the `calc()` store 0 and set `out_of_range`.
- A [`forloop()`](program-control.md) whose `Real` count is Inf or NaN runs no iteration and sets `out_of_range`.
- `unpack_to_words()` of an Inf or NaN `Real` writes its bit pattern: +Inf is 0x7F800000, and a NaN keeps the bits of the operation that made it (`Inf − Inf` on a PC gives 0xFFC00000, a high word of 65472).

Inside the range pyrung keeps a `Real` at Python's 64-bit precision, where a CLICK REAL is a 32-bit float: `16777216.0 + 1` is 16777217 in pyrung and 16777216 on a CLICK. Compare a `Real` with a tolerance, not `==`, when the last digits matter.

pyrung checks the range when `calc()` stores its result, not at each step of the formula. Inside one `calc()` a step is a 64-bit float (finite up to about ±1.8E+308, then ±Inf; `**` too) or an exact integer, so `calc(Big * 10 / 100, Result)` with `Big` = 3.0E+38 stores 3.0E+37 with no flag, where a CLICK computing in 32 bits would reach Inf at `Big * 10` and store Inf (not yet measured on a CLICK). Split such a formula across two `calc()`s when that intermediate Inf matters. An integer step is exact however large it grows (`2 ** E` with `E` = 1100), until it meets a float: past about 1.8E+308 that step (`* 0.5`, `/`, `sin`) makes the `calc()` store 0 and set `out_of_range`.

## Division

- Division by zero produces result = 0 and sets `system.fault.division_error`.
- Integer division truncates toward zero: `−7 / 2 = −3`.

## Math errors

```python
calc(sqrt(Level), Root)     # Level = -4.0
```

A math function given an operand outside its domain — `sqrt` of a negative, `log`/`log10` of 0 or a negative, `asin`/`acos` outside −1..1 — stores 0 in the destination and sets `system.fault.math_operation_error` (CLICK's SC46). The rest of the scan runs, so a later rung can see the flag; the next scan starts with it off, like the other fault flags. The PLC stays in RUN. That is what a CLICK does, though its help for SC46 says "SC50 is turned to ON, the PLC is put in Stop Mode": on the hardware, SC50 stays off and the PLC keeps scanning.

A division by zero earlier in the expression wins: `sqrt(X / 0)` is 0 with `division_error`, not a domain error. An infinite operand outside the domain is one: `sqrt` of −Inf and `asin` of Inf are 0 with `math_operation_error`; `sqrt` and `log` of +Inf are +Inf.

A negative base to a fractional power (`(-8.0) ** 0.5`, or `N ** H` with `H` = 0.5) has no real value, so pyrung treats it as a math error, as it does `sqrt(-1)`: the `calc()` stores 0 and sets `math_operation_error`. This is pyrung's choice, not yet measured on a CLICK.

## Math outside `calc()`

An expression evaluated anywhere but `calc()` (a rung condition, a `copy()` source, a `forloop()` count, an index expression such as `DS[I + 1]`) is permissive: an operation that hits a math error gives 0, sets no fault flag and never stops the scan. That covers a division by zero (`/`, `//` or `%`, where `calc()` sets `division_error`), a math function outside its domain, a negative base to a fractional power, a bitwise or shift operation on Inf or NaN or with a negative count, an integer too large for a float meeting a float (`(2 ** E) * 0.5` with `E` = 1100), and a `randint()` bound holding Inf or NaN. A result that IEEE 754 defines passes through: `Big * 10 > 1.0E+38` is true when the product is +Inf. This is pyrung's own rule; a CLICK has no expressions outside Math, and the CLICK validator flags them. Use `calc()` when the flag matters.

When a value stored as Inf or NaN reaches an instruction's own integer conversion, that instruction's rule applies: a `forloop()` over a `Real` count holding Inf runs no iteration and sets `out_of_range`.

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
| REAL past ±3.4028235E+38 | ±Inf; `calc()` sets `system.fault.out_of_range` |
| Division by zero | Result = 0, `system.fault.division_error` set |
| Indexed source outside its block (`calc(DS[Ptr], D)`) | Nothing written, `system.fault.address_error` set |
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

`randint(lo, hi)` includes both ends, like Python's `random.randint`. If `lo` is above `hi`, it draws between them. A `Real` bound is cut toward zero; one holding Inf or NaN makes the `calc()` store 0 and set `system.fault.out_of_range` (SC43), pyrung's choice, not yet measured on a CLICK. Outside `calc()` such a draw is 0 with no flag ([Math outside `calc()`](#math-outside-calc)).

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
