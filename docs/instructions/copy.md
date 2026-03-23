# Copy

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `copy` — copy single value

```python
copy(Setpoint, DS[1])               # Copy tag to tag
copy(42, DS[1])                     # Copy literal to tag
copy(DS[1], DS[DS[0]])              # Indirect addressing: DS[pointer]
copy(DS[1], DS[1], oneshot=True)    # Execute only on rung rising edge
```

Out-of-range values are **clamped** to the destination type's min/max. This is different from `calc()`, which wraps.

## `blockcopy` — copy a range

```python
blockcopy(DS.select(1, 10), DS.select(11, 20))   # Copy DS1..DS10 → DS11..DS20
```

Source and destination ranges must have the same length.

## `fill` — write constant to range

```python
fill(0, DS.select(1, 100))          # Zero out DS1..DS100
fill(Setpoint, Alarms.select(1, 8)) # Copy tag value to all 8 elements
```

## Type conversion (copy converters)

Copy converters handle conversions between numeric and text registers — the same options you see in the Click PLC Copy Single dialog. Pass them as the `convert` argument to `copy()`.

### Text → Numeric

```python
copy(ModeChar, DS[1], convert=to_value)    # CHAR '5' → numeric 5   (Copy Character Value)
copy(ModeChar, DS[1], convert=to_ascii)    # CHAR '5' → ASCII 53    (Copy ASCII Code Value)
```

### Numeric → Text

```python
copy(DS[1], Txt[1], convert=to_text())                       # "123"           (Suppress zero)
copy(DS[1], Txt[1], convert=to_text(suppress_zero=False))    # "00123"         (Do not Suppress zero)
copy(DF[1], Txt[1], convert=to_text(exponential=True))       # "1.0000000E+04" (Exponential Numbering)
copy(DS[1], Txt[1], convert=to_text(termination_code=0))       # "123" + NUL     (Termination Code)
copy(DS[1], Txt[1], convert=to_text(termination_code="$0D"))   # "123" + CR      (Termination Code, hex)
copy(DS[1], Txt[1], convert=to_binary)                         # raw byte: 123 → '{' (Copy Binary)
```

`termination_code` appends a single ASCII character after the converted text. Pass an int (0–127), a one-character string, or a `$XX` hex string matching Click's native notation (e.g. `"$0D"` for carriage return). This matches the Click PLC Termination Code option (C0-1x and C2-x CPUs).

### Leading zeros with string literals

In Click's programming software you can type `00026` directly into the source field to copy fixed-width text into text registers. Python won't allow leading zeros on integer literals — `00026` is a syntax error. Use a string instead:

```python
copy("00026", Txt[1])          # Txt1..Txt5 = "0", "0", "0", "2", "6"
```

### blockcopy and fill

`blockcopy()` supports `convert=` but only for text→numeric conversions (`to_value` and `to_ascii`). This matches Click PLC hardware, which limits block copy to those two modes.

```python
blockcopy(CH.select(1, 3), DS.select(1, 3), convert=to_value)
blockcopy(CH.select(1, 3), DS.select(1, 3), convert=to_ascii)
```

`fill()` does not support `convert=` — it is plain value copy only.

### Converter reference

| Converter | Direction | Click PLC equivalent | `copy` | `blockcopy` | `fill` |
|-----------|-----------|---------------------|--------|-------------|--------|
| `to_value` | Text → Numeric | Copy Character Value (Option 4b) | yes | yes | no |
| `to_ascii` | Text → Numeric | Copy ASCII Code Value (Option 4b) | yes | yes | no |
| `to_text()` | Numeric → Text | Copy Option 4a / 4c | yes | no | no |
| `to_binary` | Numeric → Text | Copy Binary (Option 4a) | yes | no | no |

`to_value`, `to_ascii`, and `to_binary` take no arguments — pass them bare (no parentheses needed, though `to_binary()` also works). `to_text()` accepts keyword arguments for formatting options.

## Pack / unpack

```python
pack_bits(C.select(1, 16), DS[1])          # Pack 16 BOOLs into one WORD
unpack_to_bits(DS[1], C.select(1, 16))     # Unpack WORD into 16 BOOLs

pack_words(DS.select(1, 2), DD[1])         # Pack two INTs into DINT (low-word first)
unpack_to_words(DD[1], DS.select(1, 2))    # Unpack DINT into two INTs
```
