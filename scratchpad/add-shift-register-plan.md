# Implement `shift_register` (Click-Literal Direction, Reset-Required Chain)

## Summary
Add a new core instruction and DSL builder for `shift_register` with Click semantics:
- Data bit is rung power.
- Shift happens on clock OFF→ON edge.
- Reset is required in the chain and clears the full register range.
- Reverse argument order is allowed and follows Click page wording (`end -> start`), which results in low-address to high-address movement.

## Public API / Interface Changes
1. Add `shift_register(start, end)` to `src/pyrung/core/program.py`.
2. Add `ShiftRegisterBuilder` in `src/pyrung/core/program.py` with:
   - `.clock(condition)` required first
   - `.reset(condition)` required to finalize/add instruction
3. Add `ShiftRegisterInstruction` in `src/pyrung/core/instruction.py`.
4. Export `shift_register` from `src/pyrung/core/__init__.py`.

## Implementation Plan
1. Implement `ShiftRegisterInstruction` in `src/pyrung/core/instruction.py`.
2. Make it terminal-style (`always_execute() -> True`) so clock/reset are evaluated even when rung power is false.
3. Capture/evaluate three logical inputs in instruction runtime:
   - `data_condition` from rung combined condition (data bit source)
   - `clock_condition` (edge-detected in instruction memory)
   - `reset_condition` (level-triggered clear)
4. Use instruction-local memory key for previous clock state (e.g., based on `id(self)`).
5. On each scan:
   - Evaluate reset first; if true, clear all bits in range and skip shifting.
   - Evaluate clock and detect rising edge (`curr and not prev`).
   - If rising edge, shift range and insert current data bit at entry side.
   - Persist current clock state for next scan.
6. Address/range resolution rules:
   - `start` and `end` must be BOOL tags.
   - Must resolve as block-address style names (`<prefix><digits>`) with same prefix.
   - Current pass supports block-address names only (no semantic alias resolution yet).
7. Direction semantics (per Click page literal):
   - If `start < end`: shift `start -> end`, insert data at `start`.
   - If `start > end`: shift `end -> start`, insert data at `end`.
8. Implement `ShiftRegisterBuilder` in `src/pyrung/core/program.py`:
   - `shift_register(start, end)` captures rung combined condition as data input.
   - `.clock(...)` stores clock condition.
   - `.reset(...)` requires clock already set, then creates/adds instruction.
   - Missing `.reset(...)` means no instruction is added (consistent with existing required-chain builder style).
9. Add `shift_register` import/export wiring in `src/pyrung/core/__init__.py`.
10. Update spec text to match implemented direction source-of-truth:
   - `spec/core/instructions.md` shift direction wording

## Tests and Scenarios
1. Add `tests/core/test_shift_register.py` (new feature-focused file).
2. Unit-level behavior tests:
   - Rising-edge shift only.
   - No shift while clock stays high/low.
   - Reset clears full range.
   - Reset has priority over simultaneous clock edge.
3. Direction tests:
   - Forward args (`C2..C7`) shift low→high.
   - Reverse args (`C7..C2`) produce the same low→high movement per Click literal.
4. Data source tests:
   - Rung true shifts in `True`.
   - Rung false shifts in `False` (verifies terminal execution behavior).
5. Validation/error tests:
   - Non-BOOL start/end raises.
   - Different prefix blocks raises.
   - Non-address-style names raise.
6. Builder API tests:
   - `.clock().reset()` adds and executes instruction.
   - `.reset()` before `.clock()` raises clear runtime error.
   - `clock` without final `reset` adds nothing.

## Assumptions and Defaults
1. Authoritative reverse-direction behavior is the Click reference wording in `docs/click_reference/shift_register.md`.
2. Alias/tagmap-based address resolution is out of scope for this pass; only block-address-style tag names are supported.
3. `.reset(...)` is required by API contract for `shift_register` finalization.
4. Reset dominates shift when both are active in the same scan.
5. Uninitialized range bits read as their tag default (`False`).
