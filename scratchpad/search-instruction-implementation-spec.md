# Search Instruction Implementation Spec (Markdown Handoff)

## 1. Goal
Implement core `search(...)` with Click-equivalent runtime behavior, while keeping core operand handling permissive. Use `.select(...)` ranges for addressing (same pattern as `shift`) and allow direction control via `.reverse()`.

## 2. Locked API
Add to `src/pyrung/core/program.py`:

```python
search(
    condition: str,
    value: Any,
    search_range: BlockRange | IndirectBlockRange,
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
- `search_range` must be from `.select(...)` (static or indirect), matching `shift(...)` input style.
- Search direction follows range order:
  - `DS.select(1, 100)` scans low-to-high.
  - `DS.select(1, 100).reverse()` scans high-to-low.

Return value:
- Return `result` tag (same style as `copy`/`calc` convenience returns).

## 3. Runtime Semantics
Implement `SearchInstruction(OneShotMixin, Instruction)` in `src/pyrung/core/instruction.py`.

### 3.1 Common
- Resolve `search_range` per scan (for indirect bounds), then use its ordered addresses/tags as the scan domain.
- If resolved range is empty, treat as miss.
- On success: `result = matched_address`, `found = True`.
- On miss: `result = -1`, `found = False`.
- `oneshot=True`: execute only on OFF->ON rung transition.
- Rung false behavior: no writes (preserve previous `result`/`found`).

### 3.2 Continuous mode
- `continuous=False`: always start at the first address in the ordered range.
- `continuous=True`:
- If current `result == 0`: restart at first address in the ordered range.
- If current `result == -1`: treat as exhausted; return miss without rescanning.
- Else resume at the first address strictly after `current_result` in active direction:
  - forward order: first address `> current_result`
  - reverse order: first address `< current_result`
- If no such address exists, return miss.

### 3.3 Numeric search path (`INT`, `DINT`, `REAL`, `WORD`)
- Resolve RHS via existing resolver (`resolve_tag_or_value_ctx`), allowing literal/tag/indirect/expression.
- Compare each candidate value with operator function mapped from `condition`.
- Candidate default when not present in state: use tag default for each address in the range.

### 3.4 Text search path (`CHAR` / TXT-style)
- Allow only `==` and `!=`; other operators raise `ValueError`.
- Resolve RHS to string (`str(...)`), reject empty string with `ValueError`.
- Window length `N = len(rhs)`.
- Candidate window at index `i` is `N` consecutive tags in resolved range order (`tags[i : i + N]`).
- Valid start indices are `cursor_index..(len(tags) - N)`.
- On success, store the first address of the matched text window.
- If `N` exceeds resolved range size, immediate miss.

## 4. Range Resolution Rules
Use range helpers in `src/pyrung/core/instruction.py`:
- Accept `BlockRange` / `IndirectBlockRange` and resolve at execution time.
- Preserve resolved order (including `.reverse()`).
- Derive candidate addresses directly from resolved range instead of parsing tag name strings.

## 5. File-by-File Changes
1. `src/pyrung/core/instruction.py`
- Add `SearchInstruction`.
- Add operator dispatch map and range/cursor helpers for forward/reverse progression.
- Add text-window search helper.

2. `src/pyrung/core/program.py`
- Import `SearchInstruction`.
- Add DSL `search(...)` wrapper with type checks and rung-context check.
- Update wrapper signature to accept `search_range` from `.select(...)`.

3. `src/pyrung/core/__init__.py`
- Export `search`.

4. `spec/core/instructions.md`
- Replace open "search return semantics" gap with implemented behavior and `.select(...).reverse()` direction model.

## 6. Tests to Add

### 6.1 Unit tests (`tests/core/test_instruction.py`)
- `found` bool enforcement.
- `result` int/dint enforcement.
- Invalid condition token rejection.
- `search_range` type enforcement (must be `BlockRange` / `IndirectBlockRange`).
- Numeric success (`>`, `<`, etc.).
- Numeric miss writes `-1` + `False`.
- Continuous progression across repeated executes.
- Continuous exhausted state (`result=-1`) no-rescan behavior.
- Continuous restart with `result=0`.
- Continuous resume in reverse direction (`.reverse()`).
- One-shot behavior.
- Rung-false preserve behavior.
- Text equality search (`"ADC"` over 3 consecutive CHAR registers).
- Text inequality search (`!=`).
- Text search in reverse range order.
- Invalid text operator rejection.
- Empty text value rejection.
- Empty resolved range miss behavior.

### 6.2 Integration tests (`tests/core/test_program.py`)
- DSL call inside `Rung`.
- Search + copy-by-pointer pattern from Click example.
- Continuous mode across `PLCRunner.step()`.
- Reverse-order search using `.reverse()`.
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
- Core docs updated to reflect implemented status.
