# Immutable PLC Engine: Implementation Notes

This document captures design decisions, implementation details, and internal mechanisms that complement the main specification. Use this as a reference when implementing engine internals.

---

## Design Decision: Condition Evaluation

**Problem:** When does `Step == 0` get evaluated?

**Answer: Lazily, at scan time.**

```python
# At definition time: creates a Condition object
cond = Step == 0  # Returns CompareEq(Tag("Step"), 0)

# At scan time: Condition.evaluate(state) reads from state.tags
def evaluate(self, state: SystemState) -> bool:
    return state.tags.get(self.tag.name) == self.value
```

This allows conditions to be defined once and evaluated against any historical state during playback.

---

## Design Decision: Pure Rung Evaluation

**Problem:** POC rungs mutated state directly (`self.target.set_value(1)`).

**Solution: Rungs return state deltas**

```python
class Rung:
    def evaluate(self, state: SystemState) -> SystemState:
        """Pure function: state in, new state out."""
        if not self._conditions_true(state):
            return self._handle_false(state)

        new_state = state
        for instruction in self.instructions:
            new_state = instruction.execute(new_state)
        return new_state
```

This maintains immutability and enables time-travel debugging.

---

## Design Decision: Edge Detection

**Problem:** Rising/Falling edge needs previous state.

**Solution: Previous values stored in `state.memory`**

```python
# Memory stores previous scan values for edge detection
state.memory = {
    "_prev:X1": False,  # Previous value of X1
    "_prev:C1": True,   # Previous value of C1
    ...
}

class RisingEdge(Condition):
    def evaluate(self, state: SystemState) -> bool:
        current = state.tags.get(self.tag.name, False)
        previous = state.memory.get(f"_prev:{self.tag.name}", False)
        return current and not previous
```

The runner updates `_prev:*` entries at the end of each scan, AFTER all logic has executed.

---

## Design Decision: Program Capture

**Problem:** How do rungs get collected into a program?

**Solution:** Support both context manager and decorator styles.

```python
# Option 1: Context manager (explicit capture)
with Program() as logic:
    with Rung(Button):
        out(Light)

    with Rung(Step == 0):
        out(Light)
        with branch(AutoMode):
            copy(1, Step, oneshot=True)

runner = PLCRunner(logic)

# Option 2: Decorator (for reusable program definitions)
@program
def motor_control():
    with Rung(Button):
        out(Motor)
    ...

runner = PLCRunner(motor_control)
```

**Implementation:**

```python
class Program:
    """Context manager that captures rungs."""

    _current: "Program | None" = None  # Thread-local in real impl

    def __init__(self):
        self.rungs: list[Rung] = []

    def __enter__(self) -> "Program":
        Program._current = self
        return self

    def __exit__(self, *args):
        Program._current = None

    def add_rung(self, rung: "Rung"):
        self.rungs.append(rung)

    @classmethod
    def current(cls) -> "Program | None":
        return cls._current


def program(fn: Callable[[], None]) -> Program:
    """Decorator that captures rungs from a function."""
    prog = Program()
    with prog:
        fn()
    return prog
```

---

## Type Validation

The engine validates values against tag types at runtime:

```python
class PLCRunner:
    def _validate_and_convert(self, tag: Tag, value: Any) -> bool | int | float:
        """Validate value against tag type, return normalized value."""
        match tag.type:
            case TagType.BOOL:
                return bool(value)
            case TagType.INT:
                if not -32768 <= int(value) <= 32767:
                    raise ValueError(f"INT overflow: {value}")
                return int(value)
            case TagType.DINT:
                if not -2147483648 <= int(value) <= 2147483647:
                    raise ValueError(f"DINT overflow: {value}")
                return int(value)
            case TagType.REAL:
                return float(value)
            case TagType.WORD:
                if not 0 <= int(value) <= 65535:
                    raise ValueError(f"WORD overflow: {value}")
                return int(value)
            case TagType.CHAR:
                if isinstance(value, str):
                    return ord(value[0]) if value else 0
                return int(value) & 0xFF
```

---

## Timer Implementation Details

### dt Injection

Runner stores `_dt` in `state.memory` BEFORE evaluating logic. Timer instructions read this to update accumulators immediately.

```python
class PLCRunner:
    def step(self) -> SystemState:
        # Inject dt into memory before logic evaluation
        dt = self._calculate_dt()
        new_memory = self._state.memory.copy()
        new_memory['_dt'] = dt
        
        state_with_dt = SystemState(
            scan_id=self._state.scan_id,
            timestamp=self._state.timestamp,
            tags=self._state.tags,
            memory=MappingProxyType(new_memory)
        )
        
        # Now evaluate logic
        new_state = self._logic.evaluate(state_with_dt)
        ...
```

### Time Unit Scaling

Accumulator stores integer in timer's native units. Conversion from `dt` (seconds):

| Unit | Conversion |
|------|------------|
| Tms | `acc += int(dt * 1000 + frac)` |
| Ts | `acc += int(dt + frac)` |
| Tm | `acc += int(dt / 60 + frac)` |
| Th | `acc += int(dt / 3600 + frac)` |
| Td | `acc += int(dt / 86400 + frac)` |

Where `frac` is the carried remainder from previous scans.

### Fractional Time Tracking

Store fractional remainder in `state.memory` under `_frac:{timer_name}` to avoid drift.

**Example:** 2.5ms scan with `Ts` (seconds) unit:
- acc += 0 (can't add partial second yet)
- frac = 0.0025s (stored)
- After 400 scans, frac accumulates to 1.0s
- acc += 1, frac = 0

```python
class OnDelayInstruction:
    def execute(self, state: SystemState) -> SystemState:
        dt = state.memory.get('_dt', 0.0)
        frac_key = f"_frac:{self.timer.name}"
        
        # Get accumulated fraction
        frac = state.memory.get(frac_key, 0.0)
        
        # Convert dt to timer units and add fraction
        scaled = self._scale_dt(dt) + frac
        
        # Integer part goes to accumulator
        increment = int(scaled)
        new_frac = scaled - increment
        
        # Update state
        new_acc = state.memory.get(self.acc_key, 0) + increment
        ...
```

### Timer Behavior Matrix

| Type | Enable True | Enable False | Reset |
|------|-------------|--------------|-------|
| TON | Acc counts up, done when acc >= setpoint | Acc = 0, done = False | N/A (auto) |
| RTON | Acc counts up, done when acc >= setpoint | Acc holds, done holds | Acc = 0, done = False |
| TOF | Acc = 0, done = True | Acc counts up, done = False when acc >= setpoint | N/A (auto) |

### Terminal Instruction Rules

Timer instructions are "terminal" - they must be the last instruction in a rung. Each type has different execution requirements:

- **TON** (no reset chain): Only executes while rung is true. Resets immediately when rung goes false (acc = 0, done = False).
- **TOF**: Must always execute - counts while rung is false to implement off-delay. Auto-resets when rung goes true again.
- **RTON** (with reset chain): Always execute to maintain state. Only resets via explicit reset condition.

### Hardware-Verified Behaviors (Click PLC)

These behaviors were verified on actual Click PLC hardware:

| Test | Result |
|------|--------|
| Mid-scan visibility | Accumulator updates IMMEDIATELY when instruction executes. Later rungs see updated value in same scan. |
| Accumulation | Linear: acc += dt each scan while enabled. With 2ms fixed scan, acc = 2, 4, 6, 8, 10... |
| First scan | Accumulator includes current scan's dt (not 0 on first enable) |
| Done bit | True when acc >= setpoint |

---

## IndirectTag Comparison Operators

All comparison operators on IndirectTag return specialized condition objects:

| Expression | Returns |
|------------|---------|
| `DD[Index] == value` | `IndirectCompareEq` |
| `DD[Index] != value` | `IndirectCompareNe` |
| `DD[Index] < value` | `IndirectCompareLt` |
| `DD[Index] <= value` | `IndirectCompareLe` |
| `DD[Index] > value` | `IndirectCompareGt` |
| `DD[Index] >= value` | `IndirectCompareGe` |

These conditions resolve the pointer at evaluation time:

```python
class IndirectCompareEq(Condition):
    def __init__(self, indirect_tag: IndirectTag, value: Any):
        self.indirect_tag = indirect_tag
        self.value = value
    
    def evaluate(self, state: SystemState) -> bool:
        # Resolve pointer
        index = state.tags.get(self.indirect_tag.pointer.name)
        resolved_name = f"{self.indirect_tag.bank.prefix}{index}"
        actual_value = state.tags.get(resolved_name)
        return actual_value == self.value
```

---

## PLCDialect Protocol

The dialect system provides a validation layer without runtime conditionals:

```python
class PLCDialect(Protocol):
    def validate_comparison(self, left: Tag, right: Tag | Any) -> bool: ...
    def validate_instruction(self, instr: Instruction, context: Rung) -> bool: ...
    def validate_pointer(self, bank: MemoryBank, pointer: Tag, context: str) -> bool: ...
    def coerce_value(self, tag: Tag, value: Any) -> Any: ...
    def get_memory_banks(self) -> dict[str, MemoryBank]: ...

class GeneralDialect:
    """Permissive default - allows everything, coerces Python-style."""
    def validate_comparison(self, left, right): return True
    def validate_pointer(self, bank, pointer, context): return True
    def coerce_value(self, tag, value):
        # Python-style coercion
        return value

class ClickDialect:
    """Click-specific restrictions."""
    def validate_comparison(self, left, right):
        # Check bank pairing rules
        ...
    
    def validate_pointer(self, bank, pointer, context):
        # Only DS can be pointer, only in copy()
        if context != "copy":
            return False
        if pointer.bank.prefix != "DS":
            return False
        return True
```

---

## Click Module Structure

Click module re-exports engine components with ClickDialect applied:

```python
# pyrung/click/__init__.py
from pyrung.click import Program as _Program, Rung as _Rung, ...

class Program(_Program):
    dialect = ClickDialect()

# Pre-built memory banks matching Click PLC hardware
X = MemoryBank("X", TagType.BOOL, range(1, 65), input_only=True)
Y = MemoryBank("Y", TagType.BOOL, range(1, 65))
C = MemoryBank("C", TagType.BOOL, range(1, 2001))
DS = MemoryBank("DS", TagType.INT, range(1, 4501), retentive=True)
DD = MemoryBank("DD", TagType.DINT, range(1, 1001), retentive=True)
DF = MemoryBank("DF", TagType.REAL, range(1, 501), retentive=True)
DH = MemoryBank("DH", TagType.WORD, range(1, 501))
T = MemoryBank("T", TagType.BOOL, range(1, 501))      # Timer done bits
TD = MemoryBank("TD", TagType.DINT, range(1, 501))    # Timer accumulators
CT = MemoryBank("CT", TagType.BOOL, range(1, 251))    # Counter done bits
CTD = MemoryBank("CTD", TagType.DINT, range(1, 251))  # Counter accumulators
SC = MemoryBank("SC", TagType.BOOL, range(1, 1001))   # System control bits
SD = MemoryBank("SD", TagType.INT, range(1, 1001))    # System data
TXT = MemoryBank("TXT", TagType.CHAR, range(1, 1001)) # Text registers
# ... etc
```

---

## History Buffer Implementation

The history buffer stores immutable snapshots for time-travel:

```python
class HistoryBuffer:
    def __init__(self, max_size: int = 10000):
        self._snapshots: list[SystemState] = []
        self._max_size = max_size
    
    def append(self, state: SystemState) -> None:
        if len(self._snapshots) >= self._max_size:
            # Could implement circular buffer or LRU eviction
            self._snapshots.pop(0)
        self._snapshots.append(state)
    
    def at(self, scan_id: int) -> SystemState:
        # Binary search by scan_id
        ...
    
    def range(self, start: int, end: int) -> list[SystemState]:
        ...
    
    def latest(self, n: int) -> list[SystemState]:
        return self._snapshots[-n:]
```

---

## Fork Implementation

Forking creates a new runner with shared history up to the fork point:

```python
class PLCRunner:
    def fork_from(self, scan_id: int) -> 'PLCRunner':
        """Create a new runner branched from historical state."""
        historical_state = self.history.at(scan_id)
        
        new_runner = PLCRunner(
            logic=self._logic,
            initial_state=historical_state
        )
        new_runner.set_time_mode(self._time_mode, dt=self._dt)
        
        # Copy history up to fork point (or just start fresh)
        # Implementation choice: fresh history or copied prefix
        
        return new_runner
```

---

## Breakpoint Implementation

Breakpoints use a predicate registry checked after each scan:

```python
class PLCRunner:
    def __init__(self, ...):
        self._breakpoints: list[Breakpoint] = []
        self._paused = False
    
    def when(self, predicate: Callable[[SystemState], bool]) -> BreakpointBuilder:
        return BreakpointBuilder(self, predicate)
    
    def step(self) -> SystemState:
        if self._paused:
            raise RuntimeError("Runner is paused. Call resume() first.")
        
        new_state = self._execute_scan()
        
        # Check breakpoints
        for bp in self._breakpoints:
            if bp.predicate(new_state):
                if bp.action == "pause":
                    self._paused = True
                    break
                elif bp.action == "snapshot":
                    self._snapshots[bp.label] = new_state
        
        return new_state

class BreakpointBuilder:
    def __init__(self, runner: PLCRunner, predicate: Callable):
        self._runner = runner
        self._predicate = predicate
    
    def pause(self) -> None:
        self._runner._breakpoints.append(
            Breakpoint(self._predicate, action="pause")
        )
    
    def snapshot(self, label: str) -> None:
        self._runner._breakpoints.append(
            Breakpoint(self._predicate, action="snapshot", label=label)
        )
```

---

## Monitor Implementation

Monitors track tag changes and fire callbacks:

```python
class PLCRunner:
    def monitor(self, tag: str, callback: Callable[[Any, Any], None]) -> None:
        """Fire callback(current, previous) when tag changes."""
        self._monitors[tag] = callback
    
    def step(self) -> SystemState:
        prev_state = self._state
        new_state = self._execute_scan()
        
        # Fire monitors for changed tags
        for tag, callback in self._monitors.items():
            prev_val = prev_state.tags.get(tag)
            curr_val = new_state.tags.get(tag)
            if prev_val != curr_val:
                callback(curr_val, prev_val)
        
        return new_state
```

---

## Testing with freezegun

For large time jumps (testing hour-long recipes, etc.), use `freezegun` directly in test code. The engine doesn't wrap this—use the real tool.

```python
from freezegun import freeze_time
from datetime import datetime, timedelta

def test_long_batch_process():
    runner = PLCRunner(logic, initial_state)
    runner.set_time_mode(TimeMode.REALTIME)
    runner.run(cycles=10)  # Warm up
    
    # Force watchdog to prevent timeout during time jump
    with runner.force({'Watchdog_OK': True}):
        # Jump forward 1 hour
        with freeze_time(datetime.now() + timedelta(hours=1)):
            runner.run(cycles=10)
    
    assert runner.current_state.tags['Batch_Complete'] == True
```

Note: For most deterministic tests, prefer `FIXED_STEP` mode which doesn't require freezegun.