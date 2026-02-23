# Source Location Capture for DSL Elements

## Context

Phase 1 (Force) of the debug spec is complete. Phase 2 begins with source location capture — annotating Rungs, conditions, and instructions with `source_file` and `source_line` from the user's Python source. This metadata enables the VS Code DAP extension (Phase 2 part 2) to map engine state back to source lines for inline decorations, breakpoints, and trace display.

This is orthogonal to the walker's `ProgramLocation` (structural position in program graph) and `ForbiddenControlFlowError` (AST-based build-time validation). No overlap or duplication.

## Plan

### 1. Add default source attrs to base classes

**`condition.py` — `Condition` ABC:**
- Add class-level defaults: `source_file: str | None = None`, `source_line: int | None = None`
- These are just class-level defaults that instance attrs override

**`instruction.py` — `Instruction` ABC:**
- Same: `source_file: str | None = None`, `source_line: int | None = None`

**`rung.py` — `Rung` (RungLogic):**
- Add `source_file`, `source_line`, `end_line` params to `__init__` (default `None`)
- Store as instance attrs

### 2. Add capture helper in `program.py`

```python
def _capture_source(depth: int = 2) -> tuple[str | None, int | None]:
    frame = inspect.currentframe()
    try:
        for _ in range(depth):
            if frame is None:
                return (None, None)
            frame = frame.f_back
        if frame is None:
            return (None, None)
        return (frame.f_code.co_filename, frame.f_lineno)
    finally:
        del frame
```

`depth=2` is the default: helper → DSL function → caller.

### 3. Capture in DSL `Rung` wrapper (`program.py`)

- **`Rung.__init__`**: Capture `(source_file, source_line)` at `depth=1` (init → caller). Pass to `RungLogic(...)`.
- **`Rung.__exit__`**: Capture caller's `f_lineno` as `end_line` on the `RungLogic` instance.

### 4. Capture in DSL condition functions (`program.py`)

These functions already use `inspect` imports. After creating the condition object, set its source attrs:

- `nc()` → `NormallyClosedCondition`
- `rise()` → `RisingEdgeCondition`
- `fall()` → `FallingEdgeCondition`
- `any_of()` → `AnyCondition`
- `all_of()` → `AllCondition`

Pattern:
```python
def rise(tag: Tag) -> RisingEdgeCondition:
    cond = RisingEdgeCondition(tag)
    cond.source_file, cond.source_line = _capture_source()
    return cond
```

**Also captured in operators:** Conditions created via operators (`Tag.__eq__`, `Expression.__eq__`, `Condition.__or__`, etc.) also get source locations. `inspect.currentframe()` is essentially free (returns a frame pointer), and this runs at build time only. Precise per-condition line numbers matter for multi-line rung declarations.

### 5. Capture in operator-created conditions

These create conditions outside DSL functions. Add `_capture_source(depth=1)` after construction in each:

**`tag.py` — Tag comparison operators:**
- `__eq__`, `__ne__`, `__lt__`, `__le__`, `__gt__`, `__ge__` → `CompareEq`, `CompareNe`, etc.
- `__or__`, `__ror__` → `AnyCondition` (BOOL path)
- `__and__` (if BOOL path exists)

**`expression.py` — Expression comparison operators:**
- `__eq__`, `__ne__`, `__lt__`, `__le__`, `__gt__`, `__ge__` → `ExprCompareEq`, etc.

**`condition.py` — Condition combining operators:**
- `__or__`, `__ror__` → `AnyCondition`
- `__and__`, `__rand__` → `AllCondition`

Pattern (in tag.py):
```python
def __eq__(self, other):
    from pyrung.core.condition import CompareEq
    cond = CompareEq(self, other)
    cond.source_file, cond.source_line = _capture_source(depth=1)
    return cond
```

Note: `_capture_source` is defined in program.py. To avoid circular imports, either:
- Duplicate the 5-line helper in tag.py/condition.py/expression.py, or
- Move it to a tiny `_source.py` utility module (preferred — single source of truth)

**Not captured:** Expression arithmetic operators (`Tag + 5` → `AddExpr`). Sub-expression value display is handled by Phase 3 rung trace data, not source locations.

### 6. Capture in DSL instruction functions (`program.py`)

Same pattern for all instruction-emitting functions:

`out()`, `latch()`, `reset()`, `copy()`, `calc()`, `blockcopy()`, `fill()`, `search()`, `shift()`, `pack_bits()`, `pack_words()`, `pack_text()`, `unpack_to_bits()`, `unpack_to_words()`, `run_function()`, `run_enabled_function()`, `call()`, `return_()`

Pattern:
```python
def out(target, oneshot=False):
    ctx = _require_rung_context("out")
    for coil_tag in _iter_coil_tags(target):
        ctx._rung.register_coil(coil_tag)
    instr = OutInstruction(target, oneshot)
    instr.source_file, instr.source_line = _capture_source()
    ctx._rung.add_instruction(instr)
    return target
```

### 7. Tests

New test file: `tests/core/test_source_location.py`

- Rung captures `source_file` and `source_line` pointing to the `with Rung(...)` line
- Rung captures `end_line` pointing to end of with block
- Conditions from DSL functions (`rise()`, `nc()`, etc.) have source locations
- Instructions from DSL functions (`out()`, `copy()`, etc.) have source locations
- Conditions from operators (`|`, `&`, `==`, `<`, etc.) have source locations
- Expression arithmetic operators (`Tag + 5`) have `None` source (expected)
- Source info survives through to Program's rung list (accessible for inspection)

## Files to modify

- `src/pyrung/core/_source.py` — **new** tiny helper module (`_capture_source()`)
- `src/pyrung/core/condition.py` — add class-level source defaults to `Condition`, capture in `__or__`/`__and__`/`__ror__`/`__rand__`
- `src/pyrung/core/instruction.py` — add class-level source defaults to `Instruction`
- `src/pyrung/core/rung.py` — add source params to `Rung.__init__`
- `src/pyrung/core/tag.py` — capture in `__eq__`/`__ne__`/`__lt__`/`__le__`/`__gt__`/`__ge__`/`__or__`/`__ror__`/`__and__`
- `src/pyrung/core/expression.py` — capture in `__eq__`/`__ne__`/`__lt__`/`__le__`/`__gt__`/`__ge__`
- `src/pyrung/core/program.py` — update DSL `Rung`, all DSL condition/instruction functions
- `tests/core/test_source_location.py` — new test file

## Verification

```bash
make test   # All existing tests pass + new source location tests
make lint   # Clean
```

