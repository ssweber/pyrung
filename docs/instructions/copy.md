# Data Movement

For an introduction to the DSL vocabulary, see [Core Concepts](../getting-started/concepts.md).

## `copy` — copy single value

```python
copy(Setpoint, DS[1])               # Copy tag to tag
copy(42, DS[1])                     # Copy literal to tag
copy(DS[1], DS[DS[0]])              # Indirect addressing: DS[pointer]
copy(DS[1], DS[1], oneshot=True)    # Execute only on rung rising edge
```

`copy()`, `blockcopy()`, `fill()` and `pack_text()` **clamp** an out-of-range value to the destination type's min/max and set `system.fault.out_of_range`. A `Real` or Inf into a `Dint` clamps with no flag. This is different from `math()`, which wraps.

`copy()` does not clamp in two cases. A `Word` into an `Int` keeps its bits (65535 is -1). A negative number from -32768 to -1 into a `Word` keeps its low 16 bits (-1 is 65535). Anything below -32768 stores 32768. CLICK's editor takes no constant into a bit, so the CLICK export refuses `copy(2, C[1])` and `fill(1, C.select(1, 4))`. Set bits with `out()`, `latch()` or `reset()`.

A pointer (`DS[Ptr]`) or an index expression (`DS[Ptr + 1]`) may use only `Int` and `Dint` tags, integer constants and integer operators (`//` rather than `/`). The same holds for the bounds of a `select()` given tags or expressions. Anything else raises `TypeError` when the program is built. Copy a `Word` or `Real` into an `Int` first.

A pointer that falls below the block sets `system.fault.address_error` and the destination keeps its value. This holds whether the bad address is the source or the destination. Past the block's last address a read gives zero, a write is dropped and no flag is set. A `select()` with tag or expression bounds outside the block sets `address_error` and writes nothing.

Each `math()` or copy instruction clears `out_of_range` and `address_error` when it runs, then sets the one it trips. A `math()` that does not run (rung false, or a one-shot that does not fire) clears them too. A copy instruction that does not run leaves them, and so do coils, timers and counters. Read a flag on the rung right after the instruction.

## `blockcopy` — copy a range

```python
blockcopy(DS.select(1, 10), DS.select(11, 20))   # Copy DS1..DS10 → DS11..DS20
```

Named arrays also support whole-instance copies:

```python
blockcopy(RecipeProfile.instance(2), WorkingRecipe.select(1, 3))
```

Source and destination ranges must have the same length.

## `fill` — write constant to range

```python
fill(0, DS.select(1, 100))          # Zero out DS1..DS100
fill(Setpoint, Alarms.select(1, 8)) # Copy tag value to all 8 elements
```

## Type conversion (copy converters)

Copy converters handle conversions between numeric and text registers. They offer the same options as the CLICK PLC Copy Single dialog. Pass them as the `convert` argument to `copy()`.

### Text → Numeric

```python
copy(ModeChar, DS[1], convert=to_value)    # CHAR '5' → numeric 5   (Copy Character Value)
copy(ModeChar, DS[1], convert=to_ascii)    # CHAR '5' → ASCII 53    (Copy ASCII Code Value)
```

`to_value` of a letter or other non-digit sets the out-of-range fault and leaves the destination alone. A blank TXT reads as 0.

A `copy()` of a TXT into a number with no `convert` is `to_value`, and a `copy()` of a `Real` into a TXT with no `convert` is `to_text()`.

### Numeric → Text

```python
copy(DS[1], Txt[1], convert=to_text())                       # "123"           (Suppress zero)
copy(DS[1], Txt[1], convert=to_text(suppress_zero=False))    # "00123"         (Do not Suppress zero)
copy(DF[1], Txt[1], convert=to_text(exponential=True))       # "1.0000000E+04" (Exponential Numbering)
copy(DS[1], Txt[1], convert=to_text(termination_code=0))       # "123" + NUL     (Termination Code)
copy(DS[1], Txt[1], convert=to_text(termination_code="$0D"))   # "123" + CR      (Termination Code, hex)
copy(DS[1], Txt[1], convert=to_binary)                         # raw byte: 123 → '{' (Copy Binary)
```

A `Word` is written in hex, so `to_text()` of 0x00AB is "AB", and "00AB" with `suppress_zero=False`.

`termination_code` appends a single ASCII character after the converted text. Pass an int (0–127), a one-character string, or a `$XX` hex string matching CLICK's native notation (e.g. `"$0D"` for carriage return). This matches the CLICK PLC Termination Code option (C0-1x and C2-x CPUs).

### Leading zeros with string literals

In CLICK's programming software you can type `00026` directly into the source field to copy fixed-width text into text registers. Python won't allow leading zeros on integer literals, so `00026` is a syntax error. Use a string instead:

```python
copy("00026", Txt[1])          # Txt1..Txt5 = "0", "0", "0", "2", "6"
```

### blockcopy and fill

`blockcopy()` supports `convert=` but only for text→numeric conversions (`to_value` and `to_ascii`). This matches CLICK PLC hardware, which limits block copy to those two modes.

```python
blockcopy(CH.select(1, 3), DS.select(1, 3), convert=to_value)
blockcopy(CH.select(1, 3), DS.select(1, 3), convert=to_ascii)
```

`fill()` does not support `convert=`. It is a plain value copy only.

### Converter reference

| Converter | Direction | CLICK PLC equivalent | `copy` | `blockcopy` | `fill` |
|-----------|-----------|---------------------|--------|-------------|--------|
| `to_value` | Text → Numeric | Copy Character Value (Option 4b) | yes | yes | no |
| `to_ascii` | Text → Numeric | Copy ASCII Code Value (Option 4b) | yes | yes | no |
| `to_text()` | Numeric → Text | Copy Option 4a / 4c | yes | no | no |
| `to_binary` | Numeric → Text | Copy Binary (Option 4a) | yes | no | no |

`to_value`, `to_ascii`, and `to_binary` take no arguments. Pass them bare, with no parentheses. `to_binary()` also works. `to_text()` accepts keyword arguments for formatting options.

## Pack / unpack

```python
pack_bits(C.select(1, 16), DS[1])                         # C1 -> bit 0, C16 -> bit 15
unpack_to_bits(DS[1], C.select(1, 16))                    # bit 0 -> C1, bit 15 -> C16

pack_words(DS.select(1, 2), DD[1])                        # DS1 = low word, DS2 = high word
unpack_to_words(DD[1], DS.select(1, 2))                   # DD low word -> DS1, high word -> DS2

pack_text(Txt.select(1, 4), DH[1])                        # "ABCD" -> 0xABCD
pack_text(Txt.select(1, 6), DF[1])                        # "1e-2" -> 0.01
pack_text(Txt.select(1, 3), DS[1], allow_whitespace=True) # " 12" -> 12
```

- `pack_bits()` packs a BOOL range into an `INT`, `WORD`, `DINT`, or `REAL`. Use up to 16 bits for `INT`/`WORD`, up to 32 bits for `DINT`/`REAL`. `REAL` destinations use the raw IEEE-754 bit pattern.
- `unpack_to_bits()` reverses that mapping. `REAL` sources are unpacked from their raw IEEE-754 bit pattern.
- `pack_words()` packs exactly two `INT`/`WORD` tags into one `DINT` or `REAL`. The first source is the low word.
- `unpack_to_words()` reverses that mapping. It requires exactly two `INT`/`WORD` destinations.
- `pack_text()` parses a `TXT`/`CHAR` range into an `INT`, `WORD`, `DINT`, or `REAL`.
    - `INT`/`DINT` parse signed decimal and stop at a decimal point (`"12.5"` is 12). Any other character that is not a digit sets the out-of-range fault and leaves the destination alone.
    - `WORD` parses hex, and `REAL` parses float text including exponential notation.
    - Whitespace around the text sets the same fault unless you pass `allow_whitespace=True`.
- All pack/unpack instructions accept `oneshot=True`.
- There is no `unpack_text()`. To write a text literal into `TXT`/`CHAR` memory, use plain `copy("HELLO", Txt[1])`. It fans out across sequential tags and is cut at the end of the block. For numeric → text, use `copy(..., convert=to_text())` or `copy(..., convert=to_binary)`.
