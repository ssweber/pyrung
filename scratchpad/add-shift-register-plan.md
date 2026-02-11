# Implement `shift` (Range-First API, Reset-Required Chain)

## Summary
Add a new core instruction and DSL builder for `shift` using block ranges directly:
- Data bit is rung power.
- Shift happens on clock OFF->ON edge.
- Reset is required in the chain and clears the full register range.
- Direction comes from range order, so `shift(c.select(1, 10).reverse())` is explicit and needs no tag-name parsing.

## Public API / Interface Changes
1. Add `BlockRange.reverse()` in `src/pyrung/core/memory_block.py` (returns same addresses in reverse order).
2. Add `IndirectBlockRange.reverse()` in `src/pyrung/core/memory_block.py` (propagates reverse ordering to resolved range).
3. Add `shift(bit_range)` to `src/pyrung/core/program.py`.
4. Add `ShiftBuilder` in `src/pyrung/core/program.py` with:
   - `.clock(condition)` required first
   - `.reset(condition)` required to finalize/add instruction
5. Add `ShiftInstruction` in `src/pyrung/core/instruction.py`.
6. Export `shift` from `src/pyrung/core/__init__.py`.

## Implementation Plan
1. Extend `BlockRange` with order metadata and a `.reverse()` helper.
2. Extend `IndirectBlockRange` with matching order metadata and `.reverse()`.
3. Ensure `BlockRange.addresses` and `.tags()` preserve selected order (forward by default, reversed when requested).
4. Implement `ShiftInstruction` in `src/pyrung/core/instruction.py`.
5. Make it terminal-style (`always_execute() -> True`) so clock/reset are evaluated even when rung power is false.
6. Capture/evaluate three logical inputs in instruction runtime:
   - `data_condition` from rung combined condition (data bit source)
   - `clock_condition` (edge-detected in instruction memory)
   - `reset_condition` (level-triggered clear)
7. Use instruction-local memory key for previous clock state (e.g., based on `id(self)`).
8. On each scan:
   - Evaluate clock and detect rising edge (`curr and not prev`).
   - If rising edge, shift along the resolved tag order and insert current data bit at index `0`.
   - If reset is active, force all bits in the resolved range OFF for this scan (reset overwrite).
   - Persist current clock state for next scan.
9. Validation rules:
   - `bit_range` must resolve to `BlockRange`/`IndirectBlockRange`.
   - All resolved tags must be BOOL.
   - Empty resolved ranges are invalid.
10. Implement `ShiftBuilder` in `src/pyrung/core/program.py`:
   - `shift(bit_range)` captures rung combined condition as data input.
   - `.clock(...)` stores clock condition.
   - `.reset(...)` requires clock already set, then creates/adds instruction.
   - Missing `.reset(...)` means no instruction is added (consistent with existing required-chain builder style).
11. Add `shift` import/export wiring in `src/pyrung/core/__init__.py`.
12. Update spec text to match range-first source-of-truth:
   - `spec/core/instructions.md` shift signature and direction wording

## Tests and Scenarios
1. Add `tests/core/test_shift.py` (new feature-focused file).
2. Add `tests/core/test_memory_bank.py` coverage for `.reverse()` on `BlockRange` and `IndirectBlockRange`.
3. Unit-level behavior tests:
   - Rising-edge shift only.
   - No shift while clock stays high/low.
   - Reset clears full range.
   - Reset overwrite dominates output on simultaneous clock edge.
4. Direction tests:
   - `shift(c.select(2, 7))` shifts low->high.
   - `shift(c.select(2, 7).reverse())` shifts high->low.
5. Data source tests:
   - Rung true shifts in `True`.
   - Rung false shifts in `False` (verifies terminal execution behavior).
6. Validation/error tests:
   - Non-BOOL block raises.
   - Empty/invalid resolved range raises.
7. Builder API tests:
   - `.clock().reset()` adds and executes instruction.
   - `.reset()` before `.clock()` raises clear runtime error.
   - `clock` without final `reset` adds nothing.

## Assumptions and Defaults
1. The shift register data input is rung power at the scan where the clock edge occurs.
2. `.reset(...)` is required by API contract for `shift` finalization.
3. Reset is treated as level-active clear (all bits OFF while reset is ON), implemented as an output overwrite in the same scan.
4. Uninitialized range bits read as their tag default (`False`).
5. Existing instructions that use block ranges continue to observe current forward order unless `.reverse()` is explicitly used.
6. Simultaneous clock-edge + reset behavior should be hardware-validated with a focused Click test case and reconciled if observed behavior differs.
