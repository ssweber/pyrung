# Runner

`PLC` is the execution engine. It takes a program, holds the current state, and exposes methods to drive execution scan by scan.

## Creating a runner

```python
from pyrung import PLC

runner = PLC(logic)
```

The constructor accepts:

- A `Program` (the common case)
- A list of rungs (`[rung1, rung2]`)
- `None` for an empty program (useful in tests)

Optional keyword arguments:

- `initial_state` — a `SystemState` to start from instead of the default
- `history` — retention window for the scan log and checkpoints. Duration string (`"1h"`, `"30m"`), scan count (int), or `None` (unlimited, default). Prevents unbounded memory growth on long runs.
- `cache` — instant-lookup window for full `SystemState` snapshots. Same formats as `history`. `None` (default) uses byte-budget-only eviction.
- `history_budget` — byte ceiling for the recent-state cache (default: 100 MB; minimum 1 MB). Acts as a safety net when duration-based policies aren't enough.

## Time modes

```python
runner = PLC(logic, dt=0.010)        # fixed-step, 10 ms per scan (default)
runner = PLC(logic, realtime=True)   # wall-clock
```

| Mode | Behavior | Use case |
|------|----------|----------|
| `dt=0.010` | `timestamp += dt` each scan | Tests, offline simulation |
| `realtime=True` | `timestamp` = actual elapsed time | Live hardware, integration tests |

`dt=` is the default. Timer and counter instructions use `timestamp`, so fixed steps give perfectly reproducible results. `realtime=True` is intentionally non-deterministic — scan `dt` follows host elapsed time.

## Real-time clock

Logic that depends on time of day (shift changes, scheduled events) uses the RTC system points (`system.rtc.year4`, `system.rtc.month`, `system.rtc.hour`, etc.). By default, these track wall-clock time. See [System points](../getting-started/concepts.md#system-points) for the full namespace overview.

`set_rtc` pins the RTC to a specific datetime:

```python
from datetime import datetime

runner.set_rtc(datetime(2026, 3, 5, 6, 59, 50))
```

The RTC then advances with simulation time: `rtc = base_datetime + (current_sim_time - sim_time_at_set)`. With a fixed `dt`, this makes time-of-day logic fully deterministic. With `realtime=True`, it effectively offsets the wall clock.

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

Runs exactly N scans, unless a [pause breakpoint](testing.md#predicate-breakpoints-and-snapshots) fires first. Returns the final state.

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
runner.run_until(Or(AlarmA, AlarmB, AlarmC))
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

### `.value` via context manager

Inside `with PLC(...) as plc:` (or `with runner:`), tag `.value` reads and writes go through the runner's current state:

```python
with PLC(logic) as plc:
    Button.value = True       # queues a patch
    print(Step.value)         # reads current value
    plc.step()                # executes with the queued patch
    assert Motor.value is True
```

### Forces

Forces persist across scans, re-applied at two points each scan:

```
Phase 3: APPLY FORCES (pre-logic)    ← sets force values before any rung runs
Phase 4: EXECUTE LOGIC               ← logic may overwrite forced values mid-scan
Phase 5: APPLY FORCES (post-logic)   ← re-asserts force values after all logic
```

This means:

- Forced values are present at scan start and scan end.
- Logic may temporarily change a forced value mid-scan (for example, `latch()` on a forced-False tag sets it True temporarily, but the post-logic force pass restores it).
- Edge detection (`rise`/`fall`) sees the post-force values that carry across scans.

If a tag is both patched and forced in the same scan, the pre-logic force pass overwrites the patched value. The patch is consumed but has no effect.

For force usage patterns in tests, see [Testing — Forces](testing.md#forces).

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
runner.battery_present = False
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
runner = PLC(logic)  # 100 MB default cache, all scans addressable

runner.history.at(5)          # state at scan 5
runner.history.range(3, 7)    # [scan 3, 4, 5, 6]
runner.history.latest(10)     # up to 10 most recent (oldest → newest)
```

Every scan from 0 to the current tip is addressable.  Recent scans are served
from an in-memory state cache (byte-bounded, default 100 MB); older scans are
reconstructed on demand from the scan log and checkpoints.

To bound memory on long runs, set a retention window:

```python
runner = PLC(logic, history="1h")                   # keep 1 hour of replayable history
runner = PLC(logic, history="1h", cache="5m")       # last 5 minutes instant, rest via replay
runner = PLC(logic, history_budget=20 * 1024 * 1024)  # 20 MB byte ceiling
```

`history_budget` must be at least 1 MB (raises `ValueError` below that).

## Time-travel playhead

The playhead is a read-only cursor into history. It doesn't affect execution — `step()` always appends at the history tip.

```python
runner.playhead              # current inspection scan_id
runner.seek(scan_id=5)       # jump to a historical scan
runner.rewind(seconds=1.0)   # move backward by simulation time

snapshot = runner.history.at(runner.playhead)
```

`rewind(seconds)` finds the nearest state where `timestamp <= target`.

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