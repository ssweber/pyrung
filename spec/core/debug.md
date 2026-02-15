# Core Debug - Specification

> **Status:** Draft specification (milestone 11 scope)
> **Depends on:** `core/engine.md`, `core/dsl.md`

---

## Scope

Debugging and inspection APIs on top of `PLCRunner`:

- force (Click "override")
- breakpoints and snapshot labels
- monitors
- history and playhead navigation
- rung inspection
- diff and fork

---

## Terminology

- API term is **force**.
- In Click UI/docs this corresponds to **override (OVR)**.

---

## Public API

```python
# force
runner.add_force(tag, value)
runner.remove_force(tag)
runner.clear_forces()
with runner.force({tag_or_name: value, ...}):
    ...

# breakpoints
runner.when(predicate).pause()
runner.when(predicate).snapshot("label")

# monitors
runner.monitor(tag, callback)

# history
runner.history.at(scan_id)
runner.history.range(start_scan_id, end_scan_id)
runner.history.latest(n)
runner.history.find(label)
runner.history.find_all(label)

# time travel
runner.seek(scan_id)
runner.rewind(seconds)
runner.playhead

# inspection / diff / fork
runner.inspect(rung_id, scan_id=None)
runner.diff(scan_a, scan_b)
runner.fork_from(scan_id)
```

`tag` accepts `str` or `Tag`.

---

## Force Semantics

### Supported targets

- Any writable tag may be forced (`bool`, `int`, `float`, `str`).
- Read-only system points cannot be forced (`ValueError`).

### Persistence

- Forces persist across scans until removed.
- Multiple forces may be active.
- `with runner.force({...})` is temporary and restores the exact previous force map on exit (nested-safe).

### Scan cycle integration

Force is applied before and after code execution:

1. Read inputs
2. Apply force values
3. Process code
4. Apply force values
5. Write outputs

At each force pass, all prepared force values are written by the runtime system,
regardless of whether those variables are used in the task/program.

Mapped to core engine phases:

```
0. SCAN START
1. APPLY PATCH
2. READ INPUTS
3. APPLY FORCES (pre-logic)
4. EXECUTE LOGIC
5. APPLY FORCES (post-logic)
6. WRITE OUTPUTS
7. ADVANCE CLOCK
8. SNAPSHOT/HISTORY
```

### In-cycle behavior

- Pre-logic force pass writes prepared values before IEC code begins.
- IEC code may assign different values during processing.
- External client writes (if present in a runtime integration) may also overwrite values mid-cycle.
- Mid-cycle reads observe the current in-cycle value (including IEC assignments).
- Post-logic force pass writes prepared values again before output write.
- Therefore, force does **not** lock a variable to one value for the entire cycle; it reasserts at cycle boundaries.

### Force and patch

- `patch` remains one-shot and is consumed as usual.
- If a tag is both patched and forced in the same scan, the pre-logic force pass overwrites the patched value.

### Force and edge detection

- `rise()` / `fall()` evaluate whatever value is present when that condition is evaluated.
- Since forced variables may diverge mid-cycle, edges can reflect IEC assignments during the scan.
- Across scans, the committed post-force value is what carries into the next cycle.

---

## Breakpoints and Snapshot Labels

- Predicate type: `Callable[[SystemState], bool]`.
- Predicate is evaluated on each committed scan snapshot.
- `pause()` halts `run()`, `run_for()`, or `run_until()` after committing the triggering scan.
- `snapshot(label)` tags the triggering scan and does not halt.
- Each registration returns a handle with `remove()`, `enable()`, `disable()`, and `id`.
- Multiple breakpoints can be active simultaneously.

Label lookup:

- `history.find(label)` returns the most recent labeled snapshot or `None`.
- `history.find_all(label)` returns all matches oldest-to-newest.

---

## Monitors

```python
runner.monitor(tag, callback)
# callback(current_value, previous_value)
```

- Fires after each committed scan when value changed vs previous committed scan.
- Multiple monitors per tag are allowed.
- Returns a handle with `remove()`, `enable()`, `disable()`, and `id`.
- Callback exceptions propagate to caller.

---

## Rung Inspection

```python
runner.inspect(rung_id, scan_id=None) -> RungTrace
```

- If `scan_id` is omitted, inspect uses `runner.playhead`.
- `rung_id` is rung order index (0-based) in the compiled program.
- Inspection is read-only.

`RungTrace` includes at minimum:

- `scan_id`, `rung_id`, `powered`
- condition results in evaluation order
- instruction results in execution order
- attempted writes and force re-apply events (pre-logic and post-logic)

Missing scan/rung trace raises `KeyError`.

---

## History and Playhead

History stores immutable `SystemState` snapshots (including initial state).

### Access

- `at(scan_id)` returns one snapshot (`KeyError` if absent/evicted).
- `range(start, end)` returns snapshots where `start <= scan_id < end`.
- `latest(n)` returns oldest-to-newest among the last `n`.

### Capacity

- Configurable `history_limit: int | None` (`None` = unbounded).
- When limit is exceeded, oldest scans are evicted (ring buffer behavior).
- Labels and traces for evicted scans are evicted too.
- If playhead points to an evicted scan, it moves to oldest retained scan.

### Time travel

- `playhead` is the current inspection `scan_id`.
- `seek(scan_id)` moves playhead to retained scan.
- `rewind(seconds)` moves backward from playhead timestamp to nearest retained scan with `timestamp <= target`.
- Execution is independent of playhead: `step()` always appends at history tip.

---

## Diff

```python
runner.diff(scan_a, scan_b) -> dict[str, tuple[Any, Any]]
```

- Compares tag values only (`tags`, not `memory`).
- Includes keys with changed values.
- Missing keys are treated as `None`.
- Deterministic key order (sorted by tag name).
- Missing scans raise `KeyError`.

---

## Fork

```python
alt_runner = runner.fork_from(scan_id)
```

Creates an independent runner with:

- same program logic
- same time-mode configuration
- initial state from the selected snapshot
- clean debug runtime state (no forces, breakpoints, monitors, labels, or pending patches)
- history containing only the fork snapshot initially

---

## Out of Scope (Milestone 11)

- on-disk history/session serialization
- remote debugger protocol
- GUI-specific rendering schema beyond `inspect()` trace data
