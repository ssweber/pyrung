# Click Text Copy/Pack Design Plan (Decision-Complete)

## Summary
1. We will model text behavior with canonical `copy()+modifiers` APIs (no `copy_text(...)` wrapper commands).
2. We will add a dedicated `pack_text(...)` command for Click Pack Copy scenario 4 (TXT range -> numeric parse).
3. We will not add `unpack_text(...)`; numeric->text stays `copy(as_text(...), dest)`.
4. We will enforce core `CHAR` values as blank `""` or single ASCII character.
5. We will implement SC43/SC44-equivalent fault signaling through `system.fault.out_of_range` and `system.fault.address_error` in text/copy paths.

## Click Text Handling Catalog (from `docs/click_reference`)
1. `copy_single.md`: text source strings write across consecutive destination registers.
2. `copy_single.md`: TXT->numeric has two modes: Character Value and ASCII Code Value.
3. `copy_single.md`: numeric->TXT has Suppress Zero, Do Not Suppress Zero, Copy Binary.
4. `copy_single.md`: REAL->TXT has Real Numbering and Exponential Numbering.
5. `copy_single.md`, `copy_block.md`, `copy_fill.md`: text destinations can append Termination Code.
6. `copy_block.md`: TXT range -> numeric range supports Character Value or ASCII Code Value.
7. `copy_fill.md`: fill option field is unused; text fill is direct text write semantics.
8. `copy_pack.md`: TXT range -> numeric register parse path exists (integer, float, hex target families).
9. `copy_unpack.md`: no text-specific unpack mode exists.
10. `casting.md`: confirms numeric->text max lengths and TXT parse examples.
11. `search.md`: text search is windowed string matching over consecutive TXT/CHAR registers.
12. `contact_compare.md` + `data_compatibility.md`: TXT compares only with TXT/text constants.
13. `memory_addresses.md` + `ascii_table.md`: TXT is single 7-bit ASCII character per register.

## Public API Changes
1. Add conversion modifier constructors in `src/pyrung/core/copy_modifiers.py`:
`as_value(source)`, `as_ascii(source)`, `as_text(source, *, pad=None, suppress_zero=True, exponential=False, termination_code=None)`, `as_binary(source)`.
2. Add `Tag` instance methods in `src/pyrung/core/tag.py` delegating to these constructors:
`Tag.as_value()`, `Tag.as_ascii()`, `Tag.as_text(...)`, `Tag.as_binary()`.
3. Add `BlockRange`/`IndirectBlockRange` methods in `src/pyrung/core/memory_block.py` for text->numeric block modes:
`as_value()`, `as_ascii()`.
4. Add `pack_text(...)` DSL entrypoint in `src/pyrung/core/program.py` and export it via `src/pyrung/core/__init__.py`.
5. No `copy_text(...)` API and no `unpack_text(...)` API.

## Runtime Implementation Plan
1. Introduce a typed modifier payload model in `src/pyrung/core/copy_modifiers.py` that preserves:
`mode`, `source`, and mode-specific options.
2. Update `CopyInstruction` in `src/pyrung/core/instruction.py`:
detect modifier payloads and route to text conversion handlers.
3. Implement sequential destination expansion for multi-character writes from a start tag:
`DS1 -> DS2 -> DS3` and `X001 -> X002` style numbering preserved by suffix width.
4. Update `BlockCopyInstruction` in `src/pyrung/core/instruction.py`:
support wrapped text source ranges for Character Value and ASCII Code block modes.
5. Keep `FillInstruction` option-less; enforce CHAR validity when destination type is `CHAR`.
6. Add `PackTextInstruction` in `src/pyrung/core/instruction.py`:
source must be CHAR range; destination must be INT/DINT/WORD/REAL typed tag.
7. Implement destination-type parser families for `pack_text`:
INT/DINT as signed integer, REAL as float/exponential, WORD as hex.
8. Implement `allow_whitespace` behavior in `pack_text` parser path.
9. Implement termination code append behavior for `as_text(..., termination_code=...)`.
10. Preserve “no partial write on failed conversion” behavior for text parse/conversion failures.

## Fault Flag Semantics Plan
1. Add internal helpers in `src/pyrung/core/instruction.py` to set:
`system.fault.out_of_range` and `system.fault.address_error`.
2. Set `fault.out_of_range` on text conversion/parse failures and out-of-range text formatting cases.
3. Set `fault.address_error` on pointer/address resolution failures in `copy` text/copy paths.
4. Abort instruction write on these failures instead of raising for normal runtime parity behavior.
5. Keep non-text math/system fault behavior unchanged in this scope.

## Validation and Walker Updates
1. Update `src/pyrung/core/validation/walker.py` to classify modifier payloads as first-class operands.
2. Ensure walker still recursively emits facts for wrapped inner sources (tags, indirect refs, expressions).
3. Update `src/pyrung/click/validation.py` operand resolution to unwrap modifier payloads.
4. Add explicit `pack_text` bank compatibility check in Click validation:
source bank `TXT`; destination bank one of `DS/DD/DH/DF/TD/CTD`.
5. Keep existing R8 checks for `copy/block/fill/pack_bits/pack_words/unpack_*` unchanged.

## Test Cases and Scenarios
1. `tests/core/test_instruction.py`:
copy modifier success/failure, multi-register writes, termination code, block text modes, `pack_text` parse matrix, oneshot, no-partial-write behavior.
2. `tests/core/test_program.py`:
DSL integration for modifier-based copy and `pack_text`, export checks, no `unpack_text`.
3. `tests/core/test_tag.py`:
new `Tag.as_*` methods and option validation.
4. `tests/core/test_validation_walker.py`:
wrapped operands still produce expected fact metadata and expression detection.
5. `tests/click/test_validation_stage3.py`:
`pack_text` compatibility and wrapped-copy portability checks.
6. Add runner-based tests for fault flags (`fault.out_of_range`, `fault.address_error`) and scan-to-scan auto-clear behavior.

## Acceptance Criteria
1. All new text pathways from the catalog map to a concrete API and instruction path.
2. `copy()+modifiers` is the canonical interface for text conversion modes.
3. `pack_text(...)` is implemented and validated as the TXT parse pack path.
4. No `copy_text(...)`/`unpack_text(...)` public commands exist.
5. New tests pass with existing suites (`make test`) and no regression in current copy/pack/unpack behavior.

## Assumptions and Defaults
1. `as_value` accepts only digit characters (`0-9`) per character for text->numeric character-value conversion.
2. `as_ascii` accepts ASCII characters; non-ASCII sets out-of-range behavior.
3. `CHAR` legal runtime value is `""` or one ASCII character.
4. `pack_text(..., allow_whitespace=True)` trims edges and still sets `fault.out_of_range` when whitespace was present.
5. Conversion/parse failure causes no destination write for that instruction execution.
6. No `unpack_text` mode is introduced because Click Unpack Copy has no text path.
