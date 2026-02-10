# Search Instruction Implementation Spec (Markdown Handoff)

## 1. Goal
Implement core `search(...)` with Click-equivalent runtime behavior, while keeping core operand handling permissive. Click-specific strictness is enforced by dialect audit later.

## 2. Locked API
Add to `src/pyrung/core/program.py`:

```python
search(
    condition: str,
    value: Any,
    start: Tag,
    end: Tag,
    result: Tag,
    found: Tag,
    continuous: bool = False,
    oneshot: bool = False,
) -> Tag
```

Rules:
- `found` must be `TagType.BOOL`.
- `result` must be `TagType.INT` or `TagType.DINT`.
- `condition` accepted tokens: `==`, `!=`, `<`, `<=`, `>`, `>=` (unchanged from prior plan).
- `start`/`end` must be same type and same address prefix family (for example `DS...` to `DS...`).

Return value:
- Return `result` tag (same style as `copy`/`math` convenience returns).

## 3. Runtime Semantics
Implement `SearchInstruction(OneShotMixin, Instruction)` in `src/pyrung/core/instruction.py`.

### 3.1 Common
- Inclusive range from `start_addr` to `end_addr`.
- If `start_addr > end_addr`, raise `ValueError`.
- On success: `result = matched_address`, `found = True`.
- On miss: `result = -1`, `found = False`.
- `oneshot=True`: execute only on OFF->ON rung transition.
- Rung false behavior: no writes (preserve previous `result`/`found`).

### 3.2 Continuous mode
- `continuous=False`: always start scan at `start_addr`.
- `continuous=True`:
- If current `result == 0`: restart at `start_addr`.
- If current `result == -1`: treat as exhausted; return miss without rescanning.
- Else start from `max(start_addr, current_result + 1)`.
- If start cursor is already past searchable range, return miss.

### 3.3 Numeric search path (`INT`, `DINT`, `REAL`, `WORD`)
- Resolve RHS via existing resolver (`resolve_tag_or_value_ctx`), allowing literal/tag/indirect/expression.
- Compare each candidate value with operator function mapped from `condition`.
- Candidate default when not present in state: use type default (same type as `start`/`end` tag defaults).

### 3.4 Text search path (`CHAR` / TXT-style)
- Allow only `==` and `!=`; other operators raise `ValueError`.
- Resolve RHS to string (`str(...)`), reject empty string with `ValueError`.
- Window length `N = len(rhs)`.
- Candidate at address `i` is concatenation of addresses `i..i+N-1`.
- Valid start indices: `cursor..(end_addr - N + 1)`.
- On success, store start address of matched text window.
- If `N` exceeds range size, immediate miss.

## 4. Address Resolution Rules
Use helper logic in `src/pyrung/core/instruction.py`:
- Parse tag name with regex: `^([A-Za-z_]+)(\d+)$`.
- Prefix must match between `start` and `end`.
- Generate candidate names with the same prefix and zero-padding width as `start`'s numeric part.
- This supports both `DS1` style and `X001` style naming.

## 5. File-by-File Changes
1. `src/pyrung/core/instruction.py`
- Add `SearchInstruction`.
- Add operator dispatch map and address parse/generate helpers.
- Add text-window search helper.

2. `src/pyrung/core/program.py`
- Import `SearchInstruction`.
- Add DSL `search(...)` wrapper with type checks and rung-context check.

3. `src/pyrung/core/__init__.py`
- Export `search`.

4. `spec/core/instructions.md`
- Replace open "search return semantics" gap with implemented behavior.

5. `spec/audit/core/instructions.md`
- Move `search` from "roadmap/not implemented" to implemented instruction set.

## 6. Tests to Add

### 6.1 Unit tests (`tests/core/test_instruction.py`)
- `found` bool enforcement.
- `result` int/dint enforcement.
- Invalid condition token rejection.
- Prefix/type mismatch between `start` and `end`.
- Reverse range rejection.
- Numeric success (`>`, `<`, etc.).
- Numeric miss writes `-1` + `False`.
- Continuous progression across repeated executes.
- Continuous exhausted state (`result=-1`) no-rescan behavior.
- Continuous restart with `result=0`.
- One-shot behavior.
- Rung-false preserve behavior.
- Text equality search (`"ADC"` over 3 consecutive CHAR registers).
- Text inequality search (`!=`).
- Invalid text operator rejection.
- Empty text value rejection.

### 6.2 Integration tests (`tests/core/test_program.py`)
- DSL call inside `Rung`.
- Search + copy-by-pointer pattern from Click example.
- Continuous mode across `PLCRunner.step()`.
- Text search example end-to-end.
- Export presence (`from pyrung.core import search` callable).

## 7. Non-Goals (Explicit)
- No Click-only restriction enforcement in core (bank legality, pointer policy, etc.).
- No new condition token aliases beyond current plan.
- No dialect validator implementation in this task.

## 8. Acceptance Criteria
- New tests pass.
- Existing suites remain green:
- `uv run pytest tests/core/test_instruction.py tests/core/test_program.py`
- Lint clean:
- `make lint`
- Core docs/audit docs updated to reflect implemented status.
