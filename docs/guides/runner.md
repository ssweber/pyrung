# Running and Stepping

`PLCRunner` is the execution engine. It accepts a `Program`, holds the current `SystemState`, and exposes methods to drive execution step by step.

## Creating a runner

```python
from pyrung import PLCRunner

runner = PLCRunner(logic)
```

`PLCRunner` also accepts:

- a list of rungs (`[rung1, rung2]`)
- `None` for an empty program (useful in tests)
- an `initial_state` keyword argument for custom starting state
- a `history_limit` keyword argument (`int | None`) for retained snapshots

## Time modes

Before stepping, choose a time mode:

```python
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)  # 100ms per scan
runner.set_time_mode(TimeMode.REALTIME)              # wall-clock
```

| Mode | Use case | Behavior |
|------|----------|----------|
| `FIXED_STEP` | Tests, offline simulation | `timestamp += dt` each scan |
| `REALTIME` | Live hardware, integration tests | `timestamp` = actual elapsed time |

`FIXED_STEP` is the default and the right choice for most work. Timer and counter instructions use `timestamp`, so `FIXED_STEP` gives perfectly reproducible results.
`REALTIME` is intentionally non-deterministic because scan `dt` follows host elapsed wall-clock time.

Note: RTC system points (`rtc.year4`, `rtc.month`, `rtc.hour`, etc.) are wall-clock-derived (`datetime.now()` + RTC offset). `FIXED_STEP` does not freeze RTC by itself.

## Execution methods

### `step()` — one complete scan

```python
state = runner.step()
```

Executes one full scan cycle (all phases 0–8) and returns the committed `SystemState`.

### `run(n)` — N scans

```python
state = runner.run(10)    # Run exactly 10 scans
```

### `run_for(seconds)` — run until time advances

```python
state = runner.run_for(1.0)   # Advance simulation clock by at least 1 second
```

### `run_until(condition, ...)` — run until condition is met

```python
state = runner.run_until(
    ~MotorRunning,
    AutoMode,
    max_cycles=10000,
)
```

Multiple conditions are combined with implicit AND.

If `max_cycles` is reached before the condition evaluates True, execution stops and the final state is returned.

### `run_until_fn(predicate)` — advanced callable predicates

Use `run_until_fn` when the stop condition is not expressible as a `Tag`/`Condition` expression:

```python
state = runner.run_until_fn(
    lambda s: s.scan_id >= 100,
    max_cycles=10000,
)
```

## Mode control and reboot

### `stop()` - enter STOP mode

```python
runner.stop()
```

- Sets PLC mode to STOP (`sys.mode_run == False`).
- Does not clear tags by itself.
- Idempotent: calling `stop()` again is a no-op.

### Auto restart from STOP

When stopped, any execution method (`step`, `run`, `run_for`, `run_until`, `scan_steps`) performs a STOP->RUN transition before executing scans.

STOP->RUN transition behavior:

- Non-retentive known tags reset to defaults.
- Retentive known tags preserve values.
- Runtime scope resets (`scan_id=0`, `timestamp=0.0`, memory/history/patches/forces/debug-trace caches cleared).

### `reboot()` - power-cycle simulation

```python
runner.reboot()
```

- Battery present (`sys.battery_present=True`, default): all known tags preserve.
- Battery absent (`sys.battery_present=False`): all known tags reset to defaults.
- Runtime scope resets like STOP->RUN.
- Runner returns in RUN mode.

Battery status is simulation-controlled:

```python
runner.set_battery_present(False)
```

## Inspecting state

```python
runner.current_state           # SystemState snapshot at latest committed scan
runner.simulation_time         # Shorthand for current_state.timestamp
runner.time_mode               # Current TimeMode
```

`SystemState` fields:

```python
state.scan_id    # int — monotonic scan counter (starts at 0)
state.timestamp  # float — simulation clock in seconds
state.tags       # PMap[str, value] — all tag values
state.memory     # PMap[str, value] - internal engine state
```

`scan_id` and `timestamp` reset to `0` when STOP->RUN transition or `reboot()` rebuilds runtime scope.

## History, diff, and fork

`PLCRunner` retains immutable `SystemState` snapshots, including the initial state.

```python
runner = PLCRunner(logic, history_limit=1000)  # keep latest 1000 snapshots

runner.history.at(5)         # one retained snapshot
runner.history.range(3, 7)   # [scan_id 3, 4, 5, 6] if retained
runner.history.latest(10)    # up to 10 snapshots (oldest -> newest)
```

You can compare two retained scans by tag value:

```python
changes = runner.diff(scan_a=5, scan_b=10)
# {"TagName": (old_value, new_value), ...}
```

And branch execution from a historical scan:

```python
fork = runner.fork_from(scan_id=10)
fork.step()   # advances independently of parent runner
```

## Time-travel playhead

You can navigate retained history for read-only inspection:

```python
runner.playhead              # current inspection scan_id
runner.seek(scan_id=5)       # move playhead to retained scan
runner.rewind(seconds=1.0)   # move playhead backward by simulation time

snapshot = runner.history.at(runner.playhead)
```

Behavior:

- `seek(scan_id)` raises `KeyError` if that scan is not retained.
- `rewind(seconds)` raises `ValueError` for negative values.
- `rewind(seconds)` moves to the nearest retained snapshot where `timestamp <= target`.
- `step()` is independent of playhead and always appends at history tip.
- If `history_limit` eviction removes the current playhead scan, playhead moves to the oldest retained scan.

## Rung inspection (`inspect`)

`PLCRunner.inspect()` returns retained rung-level debug trace data for a specific scan:

```python
trace = runner.inspect(rung_id=0)            # defaults to runner.playhead
trace = runner.inspect(rung_id=0, scan_id=5) # explicit retained scan
```

Returned object model:

- `RungTrace.scan_id`: committed scan id
- `RungTrace.rung_id`: top-level rung index
- `RungTrace.events`: ordered tuple of `RungTraceEvent`

Each `RungTraceEvent` captures one debug boundary event with:

- `kind`: `"rung" | "branch" | "subroutine" | "instruction"`
- `source_file`, `source_line`, `end_line`
- `subroutine_name`, `depth`, `call_stack`
- `enabled_state`, `instruction_kind`
- `trace`: `TraceEvent | None`

Error behavior:

- Missing/evicted scan id raises `KeyError(scan_id)`.
- Existing scan with no retained rung trace raises `KeyError(rung_id)`.

Current limitation (Phase 3 incremental):

- Trace retention for `inspect()` is currently populated by `scan_steps_debug()` (including DAP stepping paths).
- Scans produced only by `step()`/`run()`/`run_for()`/`run_until()` may not have retained rung trace yet.

`PLCRunner.inspect_event()` returns the latest debug-trace event using a unified core path:

```python
event = runner.inspect_event()
if event is not None:
    scan_id, rung_id, rung_event = event
```

Behavior:

- Returns latest in-flight event while a `scan_steps_debug()` scan is active and has yielded at least one step.
- Otherwise returns latest committed retained debug-path event.
- Returns `None` if no debug trace context exists.
- Returned event is immutable `RungTraceEvent` model data.

Debug-path-only scope:

- `inspect_event()` and `inspect()` are populated through `scan_steps_debug()` (including DAP stepping flows).
- Scans produced only by `step()`/`run()`/`run_for()`/`run_until()` are intentionally out of scope for trace retention in this phase.

## Injecting inputs

### `patch()` — one-shot inputs

```python
runner.patch({"Button": True})
```

Values are applied at the start of the **next** `step()` and then discarded. Use for momentary button presses, sensor reads, or test scenarios.

`patch()` accepts both string keys and `Tag` keys:

```python
runner.patch({Button: True, Step: 5})
```

Multiple patches before a `step()` merge — last write per tag wins.

### `.value` via `active()` scope

Inside `with runner.active():`, tag `.value` reads and writes go through the runner's pending state:

```python
with runner.active():
    Button.value = True      # equivalent to runner.patch({"Button": True})
    print(Step.value)        # reads pending value before next step
    runner.step()            # valid inside active(); applies pending patch
```

## scan_steps() — rung-boundary stepping

For DAP debugging and custom step granularity, `scan_steps()` yields after each top-level rung evaluation:

```python
for rung_index, rung, ctx in runner.scan_steps():
    # ctx has all writes batched so far in this scan
    print(f"After rung {rung_index}: {dict(ctx._tags_pending)}")
# scan is committed after the generator is exhausted
```

!!! warning
    The scan is committed atomically when the `scan_steps()` generator is **fully exhausted**.
    Partially consuming the generator leaves the runner in a partially-evaluated state.

## scan_steps_debug() — DAP-level stepping

```python
for step in runner.scan_steps_debug():
    print(step.rung_index, step.kind, step.source_line, step.enabled_state)
```

`scan_steps_debug()` yields `ScanStep` objects at top-level rung, branch, subroutine, and instruction boundaries. This is the API used by the DAP adapter to drive rung-by-rung execution with source location information.

See [DAP Debugger in VS Code](dap-vscode.md) for details.

## Numeric behavior summary

| Operation | Behavior on out-of-range |
|-----------|--------------------------|
| `copy()` | Clamps to destination min/max |
| `calc()` | Wraps (modular arithmetic) |
| Timer accumulator | Clamps at 32 767 |
| Counter accumulator | Clamps at DINT min/max |
| Division by zero | Result = 0, fault flag set |

