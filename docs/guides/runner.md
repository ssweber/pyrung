# Runner

`PLCRunner` is the execution engine. It takes a program, holds the current state, and exposes methods to drive execution scan by scan.

## Creating a runner

```python
from pyrung import PLCRunner

runner = PLCRunner(logic)
```

The constructor accepts:

- A `Program` (the common case)
- A list of rungs (`[rung1, rung2]`)
- `None` for an empty program (useful in tests)

Optional keyword arguments:

- `initial_state` — a `SystemState` to start from instead of the default
- `history_limit` — how many state snapshots to retain (default: `None`, meaning no history)

## Time modes

```python
from pyrung import TimeMode

runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10 ms per scan
runner.set_time_mode(TimeMode.REALTIME)                # wall-clock
```

| Mode | Behavior | Use case |
|------|----------|----------|
| `FIXED_STEP` | `timestamp += dt` each scan | Tests, offline simulation |
| `REALTIME` | `timestamp` = actual elapsed time | Live hardware, integration tests |

`FIXED_STEP` is the default. Timer and counter instructions use `timestamp`, so fixed steps give perfectly reproducible results. `REALTIME` is intentionally non-deterministic — scan `dt` follows host elapsed time.

## Real-time clock

Logic that depends on time of day (shift changes, scheduled events) uses the RTC system points (`rtc.year4`, `rtc.month`, `rtc.hour`, etc.). By default, these track wall-clock time.

`set_rtc` pins the RTC to a specific datetime:

```python
from datetime import datetime

runner.set_rtc(datetime(2026, 3, 5, 6, 59, 50))
```

The RTC then advances with simulation time: `rtc = base_datetime + (current_sim_time - sim_time_at_set)`. In `FIXED_STEP`, this makes time-of-day logic fully deterministic. In `REALTIME`, it effectively offsets the wall clock.

## Execution methods

### `step()` — one scan

```python
state = runner.step()
```

Executes one complete scan cycle (all phases) and returns the committed `SystemState`.

### `run(cycles)` — N scans

```python
state = runner.run(cycles=300)
```

Runs exactly N scans, unless a [pause breakpoint](forces-debug.md#condition-breakpoints-and-snapshot-labels) fires first. Returns the final state.

### `run_for(seconds)` — advance by time

```python
state = runner.run_for(1.0)  # advance simulation clock by at least 1 second
```

Keeps stepping until the simulation clock has advanced by the given amount (or a pause breakpoint fires).

### `run_until(*conditions)` — stop on condition

```python
state = runner.run_until(~MotorRunning, max_cycles=10000)
```

Accepts the same condition expressions used inside `Rung()`. Multiple conditions are AND-ed:

```python
runner.run_until(Motor & ~Fault)
runner.run_until(Temp > 150.0)
runner.run_until(any_of(AlarmA, AlarmB, AlarmC))
```

Stops when the condition is true, a pause breakpoint fires, or `max_cycles` is reached — whichever comes first.

### `run_until_fn(predicate)` — callable predicate

For conditions that aren't expressible as tag/condition expressions:

```python
state = runner.run_until_fn(
    lambda s: s.scan_id >= 100,
    max_cycles=10000,
)
```

The predicate receives the committed `SystemState` each scan.

## Injecting inputs

### `patch()` — one-shot

```python
runner.patch({Button: True, Step: 5})
```

Values are applied at the start of the next `step()` and then discarded. Multiple patches before a step merge — last write per tag wins.

### `.value` via `active()`

Inside `with runner.active():`, tag `.value` reads and writes go through the runner's current state:

```python
with runner.active():
    Button.value = True       # queues a patch
    print(Step.value)         # reads current value
    runner.step()             # executes with the queued patch
    assert Motor.value is True
```

### Forces

For persistent overrides that hold across scans, see [Forces](forces-debug.md).

## Mode control

### `stop()` — enter STOP mode

```python
runner.stop()
```

Sets PLC mode to STOP. Does not clear tags. Idempotent.

### Auto-restart from STOP

Any execution method (`step`, `run`, `run_for`, `run_until`) performs a STOP→RUN transition before executing:

- Non-retentive tags reset to defaults
- Retentive tags preserve values
- Runtime scope resets (`scan_id=0`, `timestamp=0.0`, history/patches/forces cleared)

### `reboot()` — power-cycle

```python
runner.reboot()
```

Simulates a power cycle. Tag behavior depends on battery:

- Battery present (default): all tags preserve
- Battery absent: all tags reset to defaults

Runtime scope resets the same as STOP→RUN. Runner returns in RUN mode.

```python
runner.set_battery_present(False)
runner.reboot()  # all tags reset
```

## Inspecting state

```python
runner.current_state    # SystemState snapshot at latest committed scan
runner.simulation_time  # shorthand for current_state.timestamp
runner.time_mode        # current TimeMode
runner.forces           # read-only view of active force overrides
```

`SystemState` fields:

```python
state.scan_id    # int — monotonic scan counter (starts at 0)
state.timestamp  # float — simulation clock in seconds
state.tags       # PMap[str, value] — all tag values
state.memory     # PMap[str, value] — internal engine state
```

Both `scan_id` and `timestamp` reset to 0 on STOP→RUN transition or `reboot()`.

## History

Enable history retention to keep immutable state snapshots:

```python
runner = PLCRunner(logic, history_limit=1000)  # keep latest 1000

runner.history.at(5)          # snapshot at scan 5
runner.history.range(3, 7)    # [scan 3, 4, 5, 6] if retained
runner.history.latest(10)     # up to 10 most recent (oldest → newest)
```

Without `history_limit`, no snapshots are retained. The initial state (scan 0) is always included.

## Time-travel playhead

The playhead is a read-only cursor into retained history. It doesn't affect execution — `step()` always appends at the history tip.

```python
runner.playhead              # current inspection scan_id
runner.seek(scan_id=5)       # jump to retained scan (KeyError if evicted)
runner.rewind(seconds=1.0)   # move backward by simulation time

snapshot = runner.history.at(runner.playhead)
```

`rewind(seconds)` finds the nearest retained snapshot where `timestamp <= target`. If the current playhead's scan gets evicted by `history_limit`, the playhead moves to the oldest retained scan.

## Diff

Compare two retained scans to see what changed:

```python
changes = runner.diff(scan_a=5, scan_b=10)
# {"Motor": (True, False), "Step": (3, 7)}
```

Returns string-keyed dicts — only tags whose values differ. Missing tags appear as `None`.

## Fork

Create an independent runner from a snapshot:

```python
alt = runner.fork()              # from current state (common case)
alt = runner.fork(scan_id=10)    # from a retained historical scan
alt = runner.fork_from(scan_id=10)  # alias
```

The fork starts with the snapshot's state and the same time mode. It has clean runtime state — no forces, patches, breakpoints, or monitors carry over. Only the fork snapshot is in its initial history.

See [Testing — Forking](testing.md#forking-test-alternate-outcomes) for the alternate-outcomes pattern.

## Breakpoints and monitors

`when()` creates condition breakpoints evaluated after each committed scan. `monitor()` watches a tag for value changes. Both return handles with `.remove()`, `.enable()`, `.disable()`.

```python
runner.when(Fault).pause()                   # halt run()/run_for()/run_until()
runner.when(Fault).snapshot("fault_seen")    # label scan in history
runner.monitor(Motor, lambda curr, prev: print(f"{prev} → {curr}"))
```

See [Testing — Monitoring changes](testing.md#monitoring-changes) and [Testing — Predicate breakpoints](testing.md#predicate-breakpoints-and-snapshots) for usage patterns.

## Numeric behavior

| Operation | Out-of-range behavior |
|-----------|----------------------|
| `copy()` | Clamps to destination min/max |
| `calc()` | Wraps (modular arithmetic) |
| Timer accumulator | Clamps at 32,767 |
| Counter accumulator | Clamps at DINT min/max |
| Division by zero | Result = 0, fault flag set |
