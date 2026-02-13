# Plan: Implement `forloop(n)` Instruction

## Context

The Click PLC has a For-Next loop construct that repeats a block of rungs N times within a single scan. pyrung's spec (`spec/core/instructions.md`) has a placeholder for `loop()` but it's not implemented yet. The user wants `with forloop(n):` syntax, operating like a branch context manager, with implicit `next` (no separate instruction). Only flat instructions inside (no nested branches or nested forloops). Supports oneshot mode per Click reference.

## Files to Modify

| File | Change |
|------|--------|
| `src/pyrung/core/instruction.py` | Add `ForLoopInstruction` class |
| `src/pyrung/core/program.py` | Add `ForLoop` context manager, `forloop()` function, import |
| `src/pyrung/core/validation/walker.py` | Add `ForLoopInstruction` to `_INSTRUCTION_FIELDS` + recurse into child instructions |
| `src/pyrung/click/validation.py` | Update `_iter_instruction_sites` to recurse into ForLoopInstruction children |
| `tests/core/test_forloop.py` | New test file |

## Implementation

### 1. `ForLoopInstruction` in `instruction.py`

```python
class ForLoopInstruction(OneShotMixin, Instruction):
    def __init__(self, count, idx_tag, instructions, coils, oneshot=False):
        OneShotMixin.__init__(self, oneshot)
        self.count = count          # literal int or Tag
        self.idx_tag = idx_tag      # Tag("_forloop_idx", DINT)
        self.instructions = instructions  # list[Instruction]
        self.coils = coils          # set[Tag] - coils from captured instructions

    def execute(self, ctx):
        if not self.should_execute():
            return
        n = resolve_tag_or_value_ctx(self.count, ctx)
        n = max(0, int(n))
        for i in range(n):
            ctx.set_tag(self.idx_tag.name, i)
            for instr in self.instructions:
                instr.execute(ctx)

    def reset_oneshot(self):
        # Reset self + propagate to child instructions
        self._has_executed = False
        for instr in self.instructions:
            reset_fn = getattr(instr, "reset_oneshot", None)
            if callable(reset_fn):
                reset_fn()
```

Key details:
- Uses `ctx.set_tag()` (not `set_memory`) so `IndirectRef` resolution via `ctx.get_tag()` works for `loop.idx`
- `reset_oneshot()` propagates to children for proper rung-false handling
- `coils` stored so ForLoop.__exit__ can transfer them to parent rung for reset-on-false behavior

### 2. `ForLoop` context manager + `forloop()` in `program.py`

```python
_forloop_active = False  # Module-level nesting guard

class ForLoop:
    def __init__(self, count, oneshot=False):
        self.count = count
        self.oneshot = oneshot
        self.idx = Tag("_forloop_idx", TagType.DINT)

    def __enter__(self):
        global _forloop_active
        if _forloop_active:
            raise RuntimeError("Nested forloop is not permitted")
        _forloop_active = True
        self._parent_ctx = _require_rung_context("forloop")

        # Capture instructions into a temporary rung (like Branch does)
        self._capture_rung = RungLogic()
        fake = Rung.__new__(Rung)
        fake._rung = self._capture_rung
        _rung_stack.append(fake)
        return self  # user accesses self.idx

    def __exit__(self, *args):
        global _forloop_active
        _forloop_active = False
        _rung_stack.pop()

        instr = ForLoopInstruction(
            self.count, self.idx,
            self._capture_rung._instructions,
            self._capture_rung._coils,
            self.oneshot,
        )
        self._parent_ctx._rung.add_instruction(instr)

        # Transfer coils to parent so rung-false resets them
        for coil in self._capture_rung._coils:
            self._parent_ctx._rung.register_coil(coil)

def forloop(count, oneshot=False):
    return ForLoop(count, oneshot=oneshot)
```

DSL usage:
```python
with Rung(Enable):
    with forloop(10) as loop:
        copy(Source[loop.idx], Dest[loop.idx])
```

### 3. Walker update (`src/pyrung/core/validation/walker.py`)

The walker treats instructions as leaf nodes. ForLoopInstruction has child instructions that must be walked.

**a. Add to `_INSTRUCTION_FIELDS`** (line ~106):
```python
"ForLoopInstruction": ("count", "idx_tag"),
```
Only the direct fields — child instructions get special handling below.

**b. Recurse into child instructions** in `_walk_instruction()` (after field walking, ~line 441):
```python
# Recurse into ForLoopInstruction child instructions
if hasattr(instr, 'instructions'):
    for child_idx, child_instr in enumerate(instr.instructions):
        self._walk_instruction(
            child_instr, scope, subroutine, rung_index, branch_path, instr_idx
        )
```

### 4. Click validation update (`src/pyrung/click/validation.py`)

**Update `_iter_instruction_sites()`** (~line 510) to recurse into ForLoopInstruction children:
```python
for instruction_index, instruction in enumerate(rung._instructions):
    sites.append((...))
    # Recurse into forloop child instructions
    if hasattr(instruction, 'instructions'):
        for child_idx, child_instr in enumerate(instruction.instructions):
            sites.append((child_instr, ProgramLocation(...)))
```

### 5. Wiring

- Add `ForLoopInstruction` to instruction.py exports
- Add `from pyrung.core.instruction import ForLoopInstruction` in program.py
- Add `forloop` and `ForLoop` exports from program.py
- Update `src/pyrung/core/__init__.py` if it re-exports DSL functions

### 6. Strict DSL checker

`program.py`'s `_check_statement_list` already allows `with` statements and bare function calls inside `with` blocks. Since `forloop` uses `with forloop(n) as loop:`, it will pass the strict checker with no changes needed.

## Edge Cases

- **Count = 0 or negative**: no iterations, body skipped
- **Count from Tag**: resolved at scan time via `resolve_tag_or_value_ctx`
- **Nested forloop**: `_forloop_active` flag raises `RuntimeError` at DSL time
- **Oneshot**: `OneShotMixin` handles it; `reset_oneshot()` propagates to children
- **Coils inside forloop**: transferred to parent rung for proper reset-on-false
- **`loop.idx` after loop**: stale value remains in ctx until scan ends (harmless)

## Verification

```bash
make test    # Run full test suite
make lint    # Ensure clean linting
```

Test scenarios in `tests/core/test_forloop.py`:
1. Literal count (5 iterations) - verify instruction executes 5 times
2. Tag count (INT/DINT) - verify dynamic count resolution
3. `loop.idx` with indirect addressing - `copy(Source[loop.idx], Dest[loop.idx])`
4. Oneshot - executes once on rung activation, not again until re-enabled
5. Zero/negative count - body does not execute
6. Nested forloop - raises RuntimeError
7. Rung-false - coils reset, oneshots reset
8. Multiple forloops in different rungs - independent execution
