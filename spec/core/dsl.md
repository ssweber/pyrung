# Core DSL — Handoff Brief

> **Status:** Handoff — decisions captured, needs full spec writeup.
> **Depends on:** `core/types.md` (Tag, Block, TagType)
> **Referenced by:** `core/instructions.md`, `core/engine.md`, dialect specs

---

## Scope

The DSL is the context-manager-based syntax for writing ladder logic as Python. This file covers the structural elements: `Program`, `Rung`, conditions, branching, and subroutines. Individual instructions (`out`, `latch`, `copy`, etc.) are in `core/instructions.md`.

---

## Decisions Made

### Program

```python
with Program() as logic:
    # Rungs go here
```

- Context manager that collects rungs into a logic graph.
- The resulting `logic` object is passed to `PLCRunner`.
- Programs are inert data structures — they describe logic but don't execute it.

### Rung

```python
with Rung(condition1, condition2, ...):
    # Instructions go here
```

- All conditions are ANDed (series circuit).
- Instructions execute only when the combined condition is True.
- Rungs have stable IDs for inspection (`runner.inspect(rung_id)`).
- Rungs implement `render(state)` for visualization/playback.

### Conditions

```python
# Normally open (examine-on)
with Rung(Button):

# Normally closed (examine-off)
with Rung(nc(Button)):

# Rising edge (one-shot on transition False→True)
with Rung(rise(Button)):

# Falling edge (one-shot on transition True→False)
with Rung(fall(Button)):

# Multiple conditions — AND (comma-separated)
with Rung(Button, nc(Fault), AutoMode):

# OR — any_of() function
with Rung(AutoMode, any_of(Start, RemoteStart)):

# OR — pipe operator (alternative syntax)
with Rung(Button | OtherButton):

# Comparisons
with Rung(Step == 0):
with Rung(Temperature >= 100.0):

# Inline expressions (Python-native, validator may flag for hardware)
with Rung((PressureA + PressureB) > 100):
```

### Branching (parallel paths)

```python
with Rung(MainCondition):
    out(MainOutput)
    with branch(BranchCondition):
        out(BranchOutput)
```

- `branch()` creates a parallel path within a rung.
- The branch condition is ANDed with the rung's power rail, not the main condition.
- This maps to Click's branch/diverge structure.

### Subroutines

**Context-manager style** (defined inline within a Program):

```python
with subroutine("my_sub"):
    with Rung(Step == 1):
        out(SubLight)

# In main program:
with Rung(EnableSub):
    call("my_sub")
```

**Decorator style** (defined outside, auto-registered on first `call()`):

```python
@subroutine("init")
def init_sequence():
    with Rung():
        out(SubLight)

with Program() as logic:
    with Rung(Button):
        call(init_sequence)   # auto-registers + creates call instruction

# Also works with @program:
@program
def my_logic():
    with Rung(Button):
        call(init_sequence)
```

- Subroutines are named groups of rungs.
- `call()` is an instruction that transfers execution. It accepts either a string name or a `SubroutineFunc` (decorated function).
- Context-manager subroutines are defined within the `Program` context.
- Decorator subroutines are defined outside and auto-registered with the current Program when first passed to `call()`.

---

## Needs Specification

- **Rung evaluation order:** Top-to-bottom, left-to-right within a rung. Confirm this is the canonical order and document edge cases (branch evaluation order relative to main path).
- **Condition composition internals:** What do conditions return? A `Condition` object? How do AND (comma), OR (`any_of` / `|`), and negation (`nc`) compose? What's the internal representation?
- **Edge detection state:** `rise()` and `fall()` need previous-scan state. Where is this stored? In `SystemState.memory`? Auto-keyed by tag + rung?
- **Rung IDs:** Auto-assigned sequential? User-assignable? How do they survive program edits?
- **Empty rungs:** Is `with Rung(X): pass` valid? What about rungs with only a `call()`?
- **Nested branches:** Can you branch within a branch? What's the depth limit?
- **Subroutine scoping:** Can subroutines call other subroutines? Recursion? What about forward references (call before define)?
- **`render()` protocol:** What does `rung.render(state)` return? A dict? A structured object? This is the visualization protocol for GUI/playback.
- **Program serialization:** Can a `Program` be saved/loaded? Or is it always constructed from Python source?
