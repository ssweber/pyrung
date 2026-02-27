# ScanContext Optimization Plan

## Overview

Batch all tag/memory updates within a scan cycle using Pyrsistent evolvers, reducing object allocation from O(instructions) to O(1) per scan while preserving read-after-write visibility.

## Files to Modify

| File | Changes |
|------|---------|
| `src/pyrung/engine/context.py` | **NEW** - ScanContext class |
| `src/pyrung/engine/instruction.py` | Change `execute(state) -> state` to `execute(ctx) -> None` |
| `src/pyrung/engine/condition.py` | Add `evaluate_ctx(ctx)` method to all conditions |
| `src/pyrung/engine/rung.py` | Update `evaluate()` to use ScanContext |
| `src/pyrung/engine/runner.py` | Update `step()` to create/commit ScanContext |
| `src/pyrung/engine/__init__.py` | Export ScanContext |

## Implementation Steps

### Phase 1: Create ScanContext (`context.py`)

```python
class ScanContext:
    """Batched write context for a single scan cycle."""

    def __init__(self, state: SystemState):
        self._state = state
        self._tags_evolver = state.tags.evolver()
        self._memory_evolver = state.memory.evolver()
        self._tags_pending: dict[str, Any] = {}
        self._memory_pending: dict[str, Any] = {}

    # Read with pending visibility
    def get_tag(self, name: str, default: Any = None) -> Any
    def get_memory(self, key: str, default: Any = None) -> Any

    # Write (batched)
    def set_tag(self, name: str, value: Any) -> None
    def set_tags(self, updates: dict[str, Any]) -> None
    def set_memory(self, key: str, value: Any) -> None

    # Passthrough for scan_id, timestamp
    @property scan_id, timestamp

    # Commit all at once
    def commit(self, dt: float) -> SystemState
```

### Phase 2: Update Conditions (`condition.py`)

Add `evaluate_ctx(ctx: ScanContext) -> bool` to all condition classes:

- `BitCondition`: `return bool(ctx.get_tag(self.tag.name, False))`
- `NormallyClosedCondition`: `return not bool(ctx.get_tag(...))`
- `RisingEdgeCondition`: reads from `ctx.get_tag()` and `ctx.get_memory("_prev:...")`
- `FallingEdgeCondition`: same pattern
- `CompareEq/Ne/Lt/Le/Gt/Ge`: use `ctx.get_tag()`
- `IndirectCompare*`: resolve via ctx, then compare
- `AnyCondition`: call `evaluate_ctx()` on sub-conditions
- `CounterCondition`, `TimerCondition`: delegate to object's evaluate method

### Phase 3: Update Instructions (`instruction.py`)

Change signature: `def execute(self, ctx: ScanContext) -> None`

Example migrations:

**OutInstruction:**
```python
def execute(self, ctx: ScanContext) -> None:
    if not self.should_execute():
        return
    ctx.set_tag(self.target.name, True)
```

**IECOnDelayInstruction:**
```python
def execute(self, ctx: ScanContext) -> None:
    # Reset check
    if self.reset_condition and self.reset_condition.evaluate_ctx(ctx):
        ctx.set_memory(frac_key, 0.0)
        ctx.set_tags({done_name: False, acc_name: 0})
        return

    # Timer logic
    dt = ctx.get_memory("_dt", 0.0)
    acc = ctx.get_tag(acc_name, 0)
    # ... compute new values ...
    ctx.set_memory(frac_key, new_frac)
    ctx.set_tags({done_name: done, acc_name: acc_value})
```

**CallInstruction:**
```python
def execute(self, ctx: ScanContext) -> None:
    self._program.call_subroutine_ctx(self.subroutine_name, ctx)
```

Update helper functions:
- `resolve_tag_or_value(source, ctx)` - context-aware version
- `resolve_tag_name(target, ctx)` - context-aware version

### Phase 4: Update Rung (`rung.py`)

```python
def evaluate(self, ctx: ScanContext) -> None:
    conditions_true = self._evaluate_conditions(ctx)
    if conditions_true:
        self._execute_instructions(ctx)
    else:
        self._handle_rung_false(ctx)

def _evaluate_conditions(self, ctx: ScanContext) -> bool:
    for cond in self._conditions:
        if not cond.evaluate_ctx(ctx):
            return False
    return True

def _execute_instructions(self, ctx: ScanContext) -> None:
    for instruction in self._instructions:
        instruction.execute(ctx)
    for branch in self._branches:
        branch.evaluate(ctx)

def _handle_rung_false(self, ctx: ScanContext) -> None:
    # Execute always-execute instructions
    for instruction in self._instructions:
        if instruction.always_execute():
            instruction.execute(ctx)
    # Reset coils
    for tag in self._coils:
        ctx.set_tag(tag.name, tag.default)
    # Reset oneshots
    for instruction in self._instructions:
        if hasattr(instruction, "reset_oneshot"):
            instruction.reset_oneshot()
    # Propagate to branches
    for branch in self._branches:
        branch._handle_rung_false(ctx)
```

### Phase 5: Update PLCRunner (`runner.py`)

```python
def step(self) -> SystemState:
    ctx = ScanContext(self._state)

    # Apply patches
    if self._pending_patches:
        ctx.set_tags(self._pending_patches)
        self._pending_patches = {}

    # Calculate dt
    dt = self._calculate_dt()
    ctx.set_memory("_dt", dt)

    # Evaluate all rungs
    for rung in self._logic:
        rung.evaluate(ctx)

    # Batch _prev:* updates (all tags, not just modified ones)
    for name in self._state.tags:
        ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))
    for name in ctx._tags_pending:
        if name not in self._state.tags:
            ctx.set_memory(f"_prev:{name}", ctx.get_tag(name))

    # Single commit
    self._state = ctx.commit(dt)
    return self._state
```

## Key Design Decisions

1. **Read-after-write visibility**: `get_tag()` checks `_tags_pending` first, then falls back to `_state.tags`. This ensures updates are visible to subsequent instructions in the same scan.

2. **Dual tracking**: Both evolvers (for final commit) and pending dicts (for fast read lookups) are maintained.

3. **_prev:* batching**: Edge detection values are updated in the same commit as logic changes.

4. **No backwards compatibility layer**: This is a breaking change to the internal API. All instructions must be migrated.

## Verification

1. Run `make test` - all existing tests must pass
2. Key test files to verify:
   - `tests/engine/test_edge_detection.py` - edge detection still works
   - `tests/engine/test_counter.py` - counters work with context
   - `tests/engine/test_timer.py` - timers work with context
   - `tests/engine/test_program.py` - subroutines work

3. Add new test: `tests/engine/test_scan_context.py`
   - Read-after-write visibility
   - Multiple writes to same key
   - Commit produces correct final state
   - Original state unchanged after commit
