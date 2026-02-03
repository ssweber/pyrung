# PLC Integer Overflow Behavior: Research Recap

## Cross-Platform Comparison

| Platform | Math Overflow | Stored Result | Notification Mechanism | Processor Impact |
|---|---|---|---|---|
| **Click** (AutomationDirect) | Wraps (modular) | Low-order bits (two's complement) | SC43 `_Out_of_Range` | Continues running |
| **Do-more / BRX** (AutomationDirect) | Wraps (modular) | Truncated low-order bits | `$Overflow` system bit | Continues running |
| **Productivity 1000/2000/3000** | Wraps (modular) | Truncated to destination type | System tags | Continues running |
| **CODESYS / IEC 61131-3** | Wraps on assignment; intermediates use wide registers | Truncated on store to destination width | None (silent) | Continues running |
| **Allen-Bradley SLC-500** (contrast) | Clamps to ±32767 | Saturated value | S:5/0 overflow bit; **faults processor** if not cleared before end of scan | Can fault/stop |

### Key Observations

All four AutomationDirect families and CODESYS wrap on math overflow — this is the natural result of binary arithmetic on fixed-width registers. Clamping (saturation) requires extra logic and is only implemented where specific instructions justify it.

Allen-Bradley SLC-500 is the notable outlier: it clamps and can fault the processor, requiring the programmer to explicitly clear the overflow bit before end-of-scan. This is a more aggressive safety posture but also more disruptive.

### CODESYS Unique Behavior: Wide Intermediate Registers

CODESYS compiles to native code (x86/ARM), so intermediate math results use the CPU's native register width — at least 32-bit on x86/ARM, always 64-bit on x64. Overflow is **not** truncated during calculation, only on final assignment to the destination variable.

This produces surprising behavior:

```
wVar := 65535;          (* WORD, unsigned 16-bit *)

(* Example 1: Assignment to wider type *)
dwVar := wVar + 1;      (* Result: 65536, NOT 0 — computed in wide register *)

(* Example 2: Comparison without assignment *)
bVar := (wVar + 1) = 0; (* Result: FALSE — 65536 ≠ 0 in wide register *)

(* Example 3: Assignment to same-width type *)
wVar2 := (wVar + 1);    (* Result: 0 — truncated to WORD on store *)
bVar := wVar2 = 0;      (* Result: TRUE *)

(* Example 4: Explicit conversion forces truncation *)
bVar := TO_WORD(wVar + 1) = 0;  (* Result: TRUE *)
```

The IEC 61131-3 standard itself does not mandate specific overflow behavior — it is implementation-defined. Other IEC platforms (Beckhoff TwinCAT, Siemens, B&R) may differ.

---

## Click PLC Deep Dive

### Instruction-Specific Overflow Behavior

| Instruction | Overflow Strategy | Documented Language | Error Flag |
|---|---|---|---|
| **COPY** (Single, Block, Pack) | **Clamp** to destination min/max | "range limit the value" | SC43 |
| **MATH** (Decimal & Hex) | **Wrap** (modular arithmetic) | "adjusted to the data type" | SC43 |
| **Timer** (TON, TOF, RTON) | **Clamp** (empirical, undocumented) | Not documented | None documented |
| **Counter** (CTU, CTD) | **Clamp** (empirical, undocumented) | Not documented | None documented |

The critical distinction: COPY says the destination is "range limited" — explicitly clamped. MATH says the result is "adjusted to the data type" — vague language consistent with modular truncation. Both set SC43, but the stored values differ completely.

### Concrete Example

Starting with DS1 = 32767 (max signed 16-bit):

| Operation | Result in DS1 | SC43 |
|---|---|---|
| `COPY(40000, DS1)` | **32,767** (clamped to max) | ON |
| `MATH(DS1 + 1, DS1)` | **-32,768** (wrapped) | ON |

### COPY Clamping — Documented Examples from casting.md

Copying DD (32-bit) value of 1,000,000 into DS (16-bit) results in 32,767 — clamped to the DS maximum. Copying DD value of 305,419,896 (0x12345678) into DS results in 32,767 (0x7FFF), not a truncated bit pattern.

To preserve the raw bit pattern instead, you must use Unpack Copy (splits 32-bit into two 16-bit registers) rather than Single Copy.

### DH Registers: The Unsigned Exception

All Click registers are signed **except DH** (Hex), which is unsigned (0x0000–0xFFFF). DH serves as an escape hatch for sign-related issues. To treat a signed value as unsigned, route through DH:

```
DS1 = -1          (0xFFFF)
COPY DS1 → DD1    → DD1 = -1       (0xFFFFFFFF, sign-extended)
COPY DS1 → DH1    → DH1 = 0xFFFF   (bit pattern preserved)
COPY DH1 → DD1    → DD1 = 65535    (0x0000FFFF, zero-extended)
```

### System Control Relays — Math-Related Error Flags

| SC Bit | Nickname | Meaning | Triggered By |
|---|---|---|---|
| **SC40** | `_Division_Error` | Division by zero | Math instruction |
| **SC43** | `_Out_of_Range` | Data Overflow, Underflow, or Data Convert Error | Math instruction, Copy instruction |
| **SC44** | `_Address_Error` | Pointer address out of range | Copy (Single with pointer) |
| **SC46** | `_Math_Operation_Error` | Invalid values in formula registers — **fatal: sets SC50, stops PLC** | Math instruction |

SC43 is the shared flag for overflow in both COPY and MATH, but the stored-value behavior differs by instruction. SC46 is the only math flag that halts the processor — reserved for truly invalid data (e.g., corrupted register values), not ordinary arithmetic overflow.

The Math instruction dialog in Click Programming Software explicitly lists all three flags: Division Error, Out of Range, and Math Error.

### Data Types

| Type | Registers | Width | Range | Signed |
|---|---|---|---|---|
| Bit | X, Y, C, T, CT, SC | 1 bit | 0–1 | N/A |
| Integer (Int) | DS, SD, TD | 16-bit | -32,768 to 32,767 | Yes |
| Integer2 (Int2) | DD, CTD | 32-bit | -2,147,483,648 to 2,147,483,647 | Yes |
| Float | DF | 32-bit | ±3.4028235E+38 | Yes |
| Hex | DH, XD, YD | 16-bit | 0x0000 to 0xFFFF | **No** |
| Text | TXT | 8-bit | Single ASCII character | N/A |

Negative values are stored as two's complement. -1 in DS = 0xFFFF; -1 in DD = 0xFFFFFFFF.

### Open Questions

- **SC43 reset behavior**: Does SC43 latch until explicitly cleared, or does it auto-reset at the start of the next instruction/scan? Needs empirical testing.
- **Timer/counter clamping**: Observed empirically but not documented in the timer or counter help pages. May be documented elsewhere in the official Click manual or may be undocumented firmware behavior.
- **Math result on divide-by-zero**: When SC40 fires, what value is stored in the result register? Likely 0 or unchanged — needs testing.

---

## Implications for pyrung Simulator

Two distinct overflow strategies are needed, selected by the instruction handler:

```python
# COPY instruction → clamp
if value > dest_max:
    dest = dest_max; sc43 = True
elif value < dest_min:
    dest = dest_min; sc43 = True

# MATH instruction → wrap (two's complement truncation)
if value outside dest range:
    dest = truncate_to_width(value); sc43 = True
```

Both instruction types set SC43, but produce different stored results.
