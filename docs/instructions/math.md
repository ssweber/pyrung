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

## Division

- Division by zero produces result = 0 and sets the system fault flag.
- Integer division truncates toward zero: `−7 / 2 = −3`.

## Mode inference

`calc()` infers arithmetic mode from referenced tag types (including destination):

| Family | Inferred mode |
|--------|----------------|
| WORD-only | `"hex"` (unsigned 16-bit wrap) |
| Any non-WORD present | `"decimal"` (signed arithmetic) |

For Click portability, do not mix WORD and non-WORD math in the same `calc()` expression. Click validation reports `CLK_CALC_MODE_MIXED` for mixed-family expressions.

## Numeric behavior summary

| Operation | Out-of-range behavior |
|-----------|----------------------|
| `copy()` | Clamps to destination min/max |
| `calc()` | Wraps (modular arithmetic) |
| Timer accumulator | Clamps at 32,767 |
| Counter accumulator | Clamps at DINT min/max |
| Division by zero | Result = 0, fault flag set |
