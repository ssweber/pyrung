# Forces and Debug Overrides

Forces let you override tag values independently of logic. They are the simulation equivalent of PLC "override" (OVR) mode. Use forces for edge-case testing, known-state setup, and debugging.

## Force vs patch

| | `patch()` | `add_force()` |
|---|---|---|
| Duration | One scan | Until explicitly removed |
| Applied | Pre-logic, once | Pre-logic AND post-logic, every scan |
| Use case | Momentary inputs, test steps | Persistent overrides, test fixtures |

## `add_force` - persistent override

```python
runner.add_force("Button", True)        # by name
runner.add_force(Button, True)          # by Tag object
runner.add_force("Temperature", 75.5)   # any writable type
```

The force persists across every subsequent scan until removed.

## `remove_force` - remove a single override

```python
runner.remove_force("Button")
runner.remove_force(Button)
```

## `clear_forces` - remove all overrides

```python
runner.clear_forces()
```

## `force()` - temporary context manager

```python
with runner.force({"Button": True, "Fault": False}):
    runner.run(5)
# Forces exactly restored to pre-context state here
```

The context manager saves the previous force map and restores it on exit, making it safe to nest:

```python
with runner.force({"AutoMode": True}):
    runner.run(3)
    with runner.force({"Fault": True}):   # adds Fault while AutoMode stays forced
        runner.run(2)
    # Fault removed; AutoMode still True
# AutoMode removed
```

## `forces` - inspect active forces

```python
print(runner.forces)   # Mapping[str, value] - read-only view
```

## Force semantics in the scan cycle

Forces are applied at two points in each scan cycle:

```
Phase 3: APPLY FORCES (pre-logic)    <- sets force values before any rung runs
Phase 4: EXECUTE LOGIC               <- logic may overwrite forced values mid-scan
Phase 5: APPLY FORCES (post-logic)   <- re-asserts force values after all logic
```

This means:

- Forced values are present at scan start and scan end.
- Logic may temporarily diverge a forced value mid-scan (for example, `latch()` on a forced-False tag may set it True temporarily, but the post-logic force pass restores it).
- Edge detection (`rise`/`fall`) sees the post-force values that carry across scans.

## Force and patch interaction

If a tag is both patched and forced in the same scan, the pre-logic force pass overwrites the patched value. The patch is consumed but its value has no effect.

## Supported tag types

Any writable tag (`BOOL`, `INT`, `DINT`, `REAL`, `WORD`, `CHAR`) can be forced. Read-only system tags cannot be forced and raise `ValueError`.

---

## History and comparison

History retention is available on `PLCRunner`:

```python
runner = PLCRunner(logic, history_limit=1000)  # None means unbounded

runner.history.at(scan_id)
runner.history.range(start_scan_id, end_scan_id)
runner.history.latest(10)
```

You can compare retained scans by tag value:

```python
runner.diff(scan_a=5, scan_b=10)  # -> {tag_name: (old_value, new_value)}
```

Missing tags are treated as `None`, and only changed keys are returned.

## Forking from history

Create a new independent runner from a retained snapshot:

```python
alt = runner.fork_from(scan_id=5)
```

The fork:

- starts from that historical `SystemState`
- keeps the same time mode configuration
- has clean runtime debug state (no active forces, patches, breakpoints, monitors, or labels)
- retains only the fork snapshot initially in its own history

## Time-travel playhead

History can be navigated without changing execution tip:

```python
runner.playhead
runner.seek(scan_id=5)
runner.rewind(seconds=1.0)
snapshot = runner.history.at(runner.playhead)
```

`seek()`/`rewind()` are inspection-only navigation APIs. Calling `step()` still appends a new scan at the history tip.

## Rung inspection

You can inspect retained rung-level debug trace data by scan and rung index:

```python
trace = runner.inspect(rung_id=0)            # uses runner.playhead
trace = runner.inspect(rung_id=0, scan_id=5) # explicit retained scan
```

`trace` is a `RungTrace` with:

- `scan_id`
- `rung_id`
- `events` (`tuple[RungTraceEvent, ...]`)

If the scan is missing/evicted, `inspect()` raises `KeyError(scan_id)`.
If the scan exists but no trace is retained for that rung, it raises `KeyError(rung_id)`.

Current incremental limitation:

- `inspect()` trace retention is currently populated through `scan_steps_debug()` (including DAP stepping flows).
- Scans produced only with `step()`/`run()` may not yet have retained inspect trace.

## Predicate breakpoints and snapshot labels

Predicate breakpoints evaluate on each committed `SystemState` snapshot.

```python
pause_handle = runner.when(lambda s: s.tags.get("Fault")).pause()
snapshot_handle = runner.when(lambda s: s.tags.get("Fault")).snapshot("fault_triggered")
```

`pause()` behavior:

- Halts `run()`, `run_for()`, or `run_until()` after committing the triggering scan.
- `step()` still executes exactly one scan.

`snapshot(label)` behavior:

- Tags the triggering scan in history and does not halt execution.
- Query scan states via `runner.history.find(label)` and `runner.history.find_all(label)`.
- Query snapshot metadata (including RTC capture) via
  `runner.history.find_labeled(label)` and `runner.history.find_all_labeled(label)`.

Both methods return a handle with:

- `id`
- `remove()`
- `enable()`
- `disable()`

## Monitors

Monitors fire on committed value changes:

```python
handle = runner.monitor("Button", lambda curr, prev: print(f"{prev} -> {curr}"))
```

- Callback signature is `callback(current_value, previous_value)`.
- Callbacks run after each commit only when the value changed.
- Callback exceptions propagate to the caller.
- Monitor handles also support `id/remove/enable/disable`.
