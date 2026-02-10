# Plan: Implement pack/unpack Instructions

## Context

The spec (`spec/core/instructions.md`) defines four pack/unpack instructions under "Copy & Block Operations" that are not yet implemented. These mirror Click PLC's Pack Copy and Unpack Copy modes, which combine or separate bit-level and word-level data. The Click reference docs (`docs/click_reference/copy_pack.md`, `copy_unpack.md`) provide the hardware behavior. TXT→numeric conversion (pack scenario 4) is deferred.

**Goal:** Implement `pack_bits`, `pack_words`, `unpack_to_bits`, and `unpack_to_words` as core instructions following the existing patterns.

---

## Click Reference Scenarios Covered

**Pack Copy (3 of 4 scenarios):**
1. Up to 16 bit sources → DS (INT) or DH (WORD)
2. Up to 32 bit sources → DD (DINT) or DF (REAL)
3. Two word sources (DS/DH) → DD (DINT) or DF (REAL)
4. ~~TXT → numeric~~ (deferred)

**Unpack Copy (all 3 scenarios):**
1. DS (INT) or DH (WORD) → up to 16 bit destinations
2. DD (DINT) or DF (REAL) → up to 32 bit destinations
3. DD (DINT) or DF (REAL) → two word destinations (DS/DH)

---

## Instructions to Implement

### 1. `pack_bits(bit_block, dest)`
Pack N BOOL tags from a BlockRange into a destination register.
- Dest can be INT/WORD (16-bit), DINT (32-bit), or REAL (32-bit IEEE 754)
- Bit 0 (LSB) = first tag in the range
- Each BOOL tag maps to one bit position
- For REAL dest: assembled 32-bit unsigned int is reinterpreted as IEEE 754 float via `struct`
- For INT/WORD/DINT dest: use `_truncate_to_tag_type()` (bit pattern preservation, not saturating clamp)

### 2. `pack_words(word_block, dest)`
Pack two 16-bit values from a 2-element BlockRange into a 32-bit destination.
- `word_block` from `.select()` — must contain exactly 2 tags
- Dest can be DINT or REAL (per Click: DD or DF)
- **Low-word-first ordering** (matches Click hardware convention per `casting.md` Example 2): first tag = lower 16 bits, second tag = upper 16 bits
- Result integer: `(int(second_value) << 16) | (int(first_value) & 0xFFFF)`
- For REAL dest: 32-bit int pattern reinterpreted as IEEE 754 float via `struct`
- For DINT dest: use `_truncate_to_tag_type()` (bit pattern preservation)

### 3. `unpack_to_bits(source, bit_block)`
Unpack a source register into individual BOOL tags.
- Source can be INT/WORD (16-bit), DINT (32-bit), or REAL (32-bit IEEE 754)
- Bit 0 (LSB) → first tag in the range
- Each bit position maps to one BOOL tag (True/False)
- For REAL source: float is converted to its IEEE 754 32-bit pattern via `struct`, then bits extracted
- For INT/WORD source: read as int, mask to 16-bit unsigned for bit extraction
- For DINT source: read as int, mask to 32-bit unsigned for bit extraction

### 4. `unpack_to_words(source, word_block)`
Unpack a 32-bit source into two 16-bit destination tags via a 2-element BlockRange.
- Source can be DINT or REAL (per Click: DD or DF)
- `word_block` from `.select()` — must contain exactly 2 tags
- **Low-word-first ordering** (matches Click hardware convention per `casting.md` Example 2): first tag receives lower 16 bits (`bits & 0xFFFF`), second tag receives upper 16 bits (`(bits >> 16) & 0xFFFF`)
- For REAL source: float converted to 32-bit IEEE 754 pattern via `struct` before splitting
- Store each half-word with `_truncate_to_tag_type()` (bit pattern preservation into INT/WORD dest)

### Range Validation

Bit-range instructions must validate block size against destination/source width at execute time:
- **`pack_bits`**: If dest is INT/WORD (16-bit), block length must be ≤ 16. If dest is DINT/REAL (32-bit), block length must be ≤ 32. Raise `ValueError` if exceeded.
- **`unpack_to_bits`**: If source is INT/WORD (16-bit), block length must be ≤ 16. If source is DINT/REAL (32-bit), block length must be ≤ 32. Raise `ValueError` if exceeded.
- **`pack_words` / `unpack_to_words`**: Block length must be exactly 2. Raise `ValueError` otherwise.

This matches Click's "up to 16" / "up to 32" limits (`copy_pack.md`, `copy_unpack.md`).

### Type-Matrix Validation

Invalid source/dest type combinations must raise `TypeError` at execute time:
- **`pack_bits`**: dest must be INT, WORD, DINT, or REAL. Any other type raises `TypeError`.
- **`pack_words`**: word_block tags must be INT or WORD; dest must be DINT or REAL. Mismatch raises `TypeError`.
- **`unpack_to_bits`**: source must be INT, WORD, DINT, or REAL. Any other type raises `TypeError`.
- **`unpack_to_words`**: source must be DINT or REAL; word_block tags must be INT or WORD. Mismatch raises `TypeError`.

### Store Semantics Note

Pack/unpack are **bit pattern operations**, not numeric copy operations. They use `_truncate_to_tag_type()` (modular wrapping) rather than `_store_copy_value_to_tag_type()` (saturating clamp). Example: packing 16 bits with bit 15 set into INT must give -32768, not clamp to 32767.

### IEEE 754 Helpers

Add a small helper pair (private to `instruction.py`):
- `_int_to_float_bits(n)` → reinterpret 32-bit unsigned int as float: `struct.unpack('<f', struct.pack('<I', n & 0xFFFFFFFF))[0]`
- `_float_to_int_bits(f)` → reinterpret float as 32-bit unsigned int: `struct.unpack('<I', struct.pack('<f', f))[0]`

Used by pack (when dest is REAL) and unpack (when source is REAL).

---

## Implementation Steps

### Step 1: Add instruction classes to `src/pyrung/core/instruction.py`

Add four classes after `FillInstruction` (line ~842), following the `OneShotMixin + Instruction` pattern used by `BlockCopyInstruction` and `FillInstruction`:

**`PackBitsInstruction(OneShotMixin, Instruction)`**
- `__init__(self, bit_block, dest, oneshot=False)`
- `execute()`: Resolve bit_block via `resolve_block_range_tags_ctx()`, read each BOOL tag, assemble unsigned integer (bit 0 = first tag). Resolve dest via `resolve_tag_ctx()`. If dest is REAL, store `_int_to_float_bits(value)`. Otherwise store `_truncate_to_tag_type(value, dest)`. Write via `ctx.set_tag()`.

**`PackWordsInstruction(OneShotMixin, Instruction)`**
- `__init__(self, word_block, dest, oneshot=False)`
- `execute()`: Resolve `word_block` via `resolve_block_range_tags_ctx()` (must be length 2). Read tag values: first tag = lo_value, second tag = hi_value (low-word-first). Compute `(int(hi_value) << 16) | (int(lo_value) & 0xFFFF)`. Resolve dest. If dest is REAL, store `_int_to_float_bits(value)`. Otherwise store `_truncate_to_tag_type(value, dest)`. Write via `ctx.set_tag()`.

**`UnpackToBitsInstruction(OneShotMixin, Instruction)`**
- `__init__(self, source, bit_block, oneshot=False)`
- `execute()`: Resolve source tag via `resolve_tag_ctx()`, read value. If source is REAL, `bits = _float_to_int_bits(value)`. If INT/WORD, `bits = int(value) & 0xFFFF`. If DINT, `bits = int(value) & 0xFFFFFFFF`. Resolve bit_block tags, extract each bit as `bool((bits >> i) & 1)`, batch write via `ctx.set_tags()`.

**`UnpackToWordsInstruction(OneShotMixin, Instruction)`**
- `__init__(self, source, word_block, oneshot=False)`
- `execute()`: Resolve source tag, read value. If REAL, `bits = _float_to_int_bits(value)`. If DINT, `bits = int(value) & 0xFFFFFFFF`. Resolve `word_block` via `resolve_block_range_tags_ctx()` (must be length 2). First tag receives lo = `bits & 0xFFFF`, second tag receives hi = `(bits >> 16) & 0xFFFF` (low-word-first). Apply `_truncate_to_tag_type()` to each dest, write both via `ctx.set_tags()`.

### Step 2: Add DSL functions to `src/pyrung/core/program.py`

Add four explicit functions after `fill()` (~line 262), matching the strict spec API:

```python
def pack_bits(bit_block, dest, oneshot=False): ...
def pack_words(word_block, dest, oneshot=False): ...
def unpack_to_bits(source, bit_block, oneshot=False): ...
def unpack_to_words(source, word_block, oneshot=False): ...
```

Each calls `_require_rung_context()` and adds the corresponding instruction class directly (no type-inferred dispatch wrapper).

### Step 3: Update imports and exports

**`src/pyrung/core/program.py`** — Add imports of the four new instruction classes.

**`src/pyrung/core/__init__.py`** — Add `pack_bits`, `pack_words`, `unpack_to_bits`, and `unpack_to_words` to both the import block and `__all__`.

### Step 4: Add tests to `tests/core/test_instruction.py`

Add test classes following existing patterns (using `execute()` helper from `conftest.py`). Tests target instruction classes directly.

**`TestPackBitsInstruction`**
- Pack 8 bits into INT (basic case)
- Pack 16 bits into WORD
- Pack 16 bits into INT with bit 15 set → negative value (verifies wrapping, not clamping)
- Pack 32 bits into DINT
- Pack 32 bits into REAL (IEEE 754 reinterpretation)
- Missing/default BOOL tags → 0 bits
- 17 bits into INT/WORD raises ValueError (exceeds 16-bit limit)
- 33 bits into DINT/REAL raises ValueError (exceeds 32-bit limit)
- Dest is BOOL → TypeError
- Oneshot behavior
- Immutability check

**`TestPackWordsInstruction`**
- Pack two INT values (via `.select()`) into DINT — verify low-word-first ordering
- Pack two WORD values into DINT
- Pack two words into REAL (IEEE 754 reinterpretation)
- Negative value handling (in low or high word position)
- Length != 2 raises ValueError
- Dest is INT → TypeError (must be DINT or REAL)
- Source tags are BOOL → TypeError (must be INT or WORD)
- Oneshot behavior

**`TestUnpackToBitsInstruction`**
- Unpack INT to 16 bits (known bit pattern)
- Unpack negative INT → bit 15 is True
- Unpack DINT to 32 bits
- Unpack REAL to 32 bits (IEEE 754 bit extraction)
- Value 0 → all False
- All-ones → all True
- 17 bit destinations from INT/WORD source raises ValueError
- 33 bit destinations from DINT/REAL source raises ValueError
- Source is BOOL → TypeError
- Oneshot behavior
- Immutability check

**`TestUnpackToWordsInstruction`**
- Unpack DINT to two word destinations — verify low-word-first ordering
- Unpack REAL to two words (IEEE 754 bit pattern split)
- Negative DINT source
- Store semantics (wrapping to dest type)
- Length != 2 raises ValueError
- Source is INT → TypeError (must be DINT or REAL)
- Dest tags are BOOL → TypeError (must be INT or WORD)
- Oneshot behavior

**Round-trip tests** (can be in any of the above or separate):
- `pack_bits` → `unpack_to_bits` recovers original bools
- `pack_words` → `unpack_to_words` recovers original values (including through REAL)

### Step 5: Add DSL and export integration tests

**In `tests/core/test_program.py`** — add tests that exercise the DSL functions via `Program`/`Rung` context (following the existing `test_rung_with_copy` pattern):
- `test_rung_with_pack_bits` — verify `pack_bits()` DSL function creates correct instruction and executes through `evaluate_rung()`
- `test_rung_with_pack_words` — verify `pack_words()` DSL function with low-word-first ordering
- `test_rung_with_unpack_to_bits` — verify `unpack_to_bits()` DSL function
- `test_rung_with_unpack_to_words` — verify `unpack_to_words()` DSL function

**In `tests/core/test_program.py` or a dedicated export test** — verify public API surface:
- `test_pack_unpack_exports` — verify `pack_bits`, `pack_words`, `unpack_to_bits`, `unpack_to_words` are importable from `pyrung.core`

---

## Files Modified

| File | Change |
|------|--------|
| `src/pyrung/core/instruction.py` | Add 4 instruction classes |
| `src/pyrung/core/program.py` | Add `pack_bits()`, `pack_words()`, `unpack_to_bits()`, `unpack_to_words()` DSL functions + imports |
| `src/pyrung/core/__init__.py` | Add pack/unpack function exports |
| `tests/core/test_instruction.py` | Add 4 test classes (instruction-level) |
| `tests/core/test_program.py` | Add DSL dispatch tests + export verification |

## Existing Utilities to Reuse

- `OneShotMixin` — `instruction.py:150`
- `resolve_block_range_tags_ctx()` — `instruction.py:127`
- `resolve_tag_or_value_ctx()` — `instruction.py:36`
- `resolve_tag_ctx()` — `instruction.py:69`
- `_truncate_to_tag_type()` — `instruction.py:712` (bit pattern preservation, NOT `_store_copy_value_to_tag_type`)
- `_require_rung_context()` — `program.py:56`

---

## Verification

1. `make lint` — passes ruff check + format + ty
2. `make test` — all existing + new tests pass
3. Verify immutability: original state unchanged after each instruction
4. Verify oneshot: second execution is a no-op
5. Verify round-trip: `pack_bits` → `unpack_to_bits` recovers original bits
6. Verify round-trip: `pack_words` → `unpack_to_words` recovers original words (including through REAL)
