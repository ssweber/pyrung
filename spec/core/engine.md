# Core Engine — Handoff Brief

> **Status:** Handoff — decisions captured, needs full spec writeup.
> **Depends on:** `core/types.md`, `core/dsl.md`, `core/instructions.md`
> **Referenced by:** `core/debug.md`, dialect specs

---

## Scope

The execution engine: `SystemState`, `PLCRunner`, `TimeMode`, and the scan cycle. This is the "Redux store" of pyrung — immutable state, pure function evaluation, generator-driven execution.

---

## Decisions Made

### SystemState

```python
from pyrsistent import PRecord, field, pmap, PMap

class SystemState(PRecord):
    scan_id   = field(type=int, initial=0)
    timestamp = field(type=float, initial=0.0)    # Simulation clock
    tags      = field(type=PMap, initial=pmap())   # Tag values (bool, int, float)
    memory    = field(type=PMap, initial=pmap())   # Internal state (edge detection, timer internals)
```

- Immutable. Every scan produces a new `SystemState`.
- `tags` holds all tag values keyed by name string.
- `memory` holds internal engine state: edge detection bits for `rise`/`fall`/`oneshot`, timer accumulators (internal tracking), etc. This is the "hidden" state users don't directly manipulate.

### PLCRunner

```python
runner = PLCRunner(logic, initial_state=None)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.simulation_time                    # Property: current timestamp

runner.step()                             # Advance one scan
runner.scan_steps()                       # Generator: yield after each rung in one scan
runner.run(cycles=N)                      # Run N scans
runner.run_for(seconds=N)                # Run until sim clock advances N seconds
runner.run_until(predicate)               # Run until predicate(state) is True
runner.current_state                      # Property: snapshot at tip of history
```

- Inversion of control: the runner is driven by the consumer, not the other way around.
- Each `step()` executes one complete scan cycle and appends the resulting state to history.
- `scan_steps()` yields `(rung_index, rung, ctx)` at each rung boundary within one scan.
- `scan_steps()` commits only when fully exhausted.
- `run()`, `run_for()`, `run_until()` are convenience wrappers around `step()`.

### Tag Manipulation

State is immutable. Mutations are queued and applied at scan boundaries.

```python
runner.patch(tags={...})          # One-shot: applied to next scan, then released

with runner.active():
    StartButton.value = True      # Equivalent staged one-shot write
    print(StartButton.value)      # Reads pending value before step()
```

- `patch` sets values that take effect at the start of the next scan.
- Patch values are consumed after one scan (they don't persist).
- Multiple patches before a `step()` merge (last write wins per tag).
- `patch` accepts both string keys and `Tag` keys (`{StartButton: True}`).
- `.value` requires an explicit active scope (`with runner.active(): ...`) and
  raises `RuntimeError` when used outside that scope.

Force is specified in `core/debug.md`. Engine-level integration is:

- Force can be active for any writable tag.
- Force is applied pre-logic and post-logic in each scan.
- IEC assignments may temporarily diverge forced variables during logic execution.

### Time Modes

```python
class TimeMode(Enum):
    REALTIME   = "realtime"       # timestamp = wall clock
    FIXED_STEP = "fixed_step"     # timestamp += dt each scan
```

| Mode | Use Case | Behavior |
|------|----------|----------|
| `REALTIME` | Integration tests, hardware-in-loop, live GUI | `timestamp` = actual elapsed time |
| `FIXED_STEP` | Unit tests, deterministic timing | `timestamp += dt` each scan |

### Scan Cycle Phases

Every `step()` executes:

```
0. SCAN START       Dialect-specific resets (e.g., Click clears SC40/SC43/SC44)
1. APPLY PATCH      Pending patch values written to tags
2. READ INPUTS      InputBlock: copy from external source (or from patch for simulation)
3. APPLY FORCES     Pre-logic force pass (debug override behavior)
4. EXECUTE LOGIC    Evaluate rungs top-to-bottom
   - Each rung: evaluate conditions, execute instructions if True
   - All writes to tags/memory are immediately visible to subsequent rungs
5. APPLY FORCES     Post-logic force pass (re-assert prepared force values)
6. WRITE OUTPUTS    OutputBlock: push values to external sink (no-op in pure simulation)
7. ADVANCE CLOCK    scan_id += 1, timestamp updated per TimeMode
8. SNAPSHOT         New SystemState appended to history
```

### Numeric Handling (Hardware-Verified)

| Context | Behavior |
|---------|----------|
| Timer accumulator | Clamps at max value (no overflow) |
| Counter accumulator | Clamps at min/max value (no overflow) |
| Copy operations | **Clamps** value to destination min/max |
| Math operations | **Wraps** (modular arithmetic, wide intermediates) |

**Copy vs Math distinction:** Both operations handle out-of-range values, but differently:

```python
# COPY: Clamps to destination range
copy(40000, DS1)      # DS1 = 32,767 (clamped to max signed 16-bit)
copy(-50000, DS1)     # DS1 = -32,768 (clamped to min)

# MATH: Wraps via modular arithmetic
math(DS1 + 1, DS1)    # If DS1=32767, result = -32,768 (wrapped)
```

**Math uses 32-bit intermediates:** Hardware uses 32-bit signed intermediates with standard two's complement wrap. pyrung uses Python arbitrary-precision integers with truncation on store.

**Division by zero:** Result is 0 (not unchanged, not max). Hardware enforces this.

---

## Needs Specification

- **Initial state:** When `initial_state` is None, what's the default? All tags False/0? Or do retentive tags need explicit initialization?
- **Patch semantics detail:** Does patching an InputBlock tag bypass the "read inputs" phase? Or does the patch become the external source? (Probably the latter — in pure simulation, `patch` IS the external source.)
- **Run_until timeout:** Does `run_until` have a max cycle guard to prevent infinite loops?
- **Generator protocol:** The original spec mentions the engine as a generator. Is `PLCRunner` literally a Python generator (`yield` after each scan)? Or is it an object with a `step()` method? The current API suggests the latter. Clarify.
- **Multiple programs:** Can a runner execute multiple programs? Or is it always one program per runner?
- **Reset:** Is there a `runner.reset()` that returns to initial state? What about retentive tags — do they survive reset? (That's what "retentive" should mean.)
- **External I/O adapter:** How would hardware-in-the-loop plug in? A callback? An interface? This can be future work but the scan cycle should have clear hooks.
- **Concurrent tag writes:** If two rungs both `out(Light)`, last-rung-wins is implied but should be documented explicitly.
- **Memory key convention:** How are `SystemState.memory` keys structured? `"rise:Button:rung_3"`? This affects serialization and debugging.
