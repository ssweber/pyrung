# Core Debug — Handoff Brief

> **Status:** Handoff — decisions captured, needs full spec writeup.
> **Depends on:** `core/engine.md` (PLCRunner, SystemState, history)
> **Implementation milestone:** 11 (Advanced Features)

---

## Scope

The debugging, inspection, and time-travel API. This is what makes pyrung more than a PLC simulator — it's a PLC *debugger*. Forces, breakpoints, observers, history navigation, and execution branching.

---

## Decisions Made

### Force (Override Logic Outputs)

```python
# Context manager: force active for duration
with runner.force({"Safety_Guard": True, "Watchdog_OK": True}):
    runner.run(cycles=100)

# Manual toggle (for UI use)
runner.add_force(tag, value)
runner.remove_force(tag)
```

- Forces override logic outputs. The engine evaluates logic normally but the forced value replaces whatever logic produced.
- Forces persist across scans until removed (unlike `patch`, which is one-shot).
- `force` context manager auto-removes on exit.
- Multiple forces can be active simultaneously.

### Breakpoints (Predicate Halts)

```python
runner.when(predicate).pause()             # Halt when condition met
runner.when(predicate).snapshot(label)     # Bookmark history when condition met
```

- `predicate` is `Callable[[SystemState], bool]`.
- `.pause()` stops execution (from `run()` / `run_for()` / `run_until()`).
- `.snapshot(label)` doesn't halt — it tags the history entry with a label for later retrieval.
- Multiple breakpoints can be active simultaneously.

### Observers (Tag Change Monitoring)

```python
runner.monitor(tag_name, callback)
# callback(current_value, previous_value) fires when tag changes
```

- Fires after each scan where the monitored tag's value differs from previous scan.
- Useful for logging, assertions in tests, GUI updates.

### Rung Inspection

```python
runner.inspect(rung_id)
# Returns live object data: conditions evaluated, power state, instruction results
```

- For visualization: a GUI can call `inspect` to render rung state at any historical point.
- Related to the `render(state)` protocol on Rung/Contact/Coil objects (see `dsl.md`).

### History Buffer

```python
runner.history.at(scan_id)            # Retrieve specific snapshot
runner.history.range(start, end)      # Slice of snapshots
runner.history.latest(n)              # Last N snapshots
```

- History is a list of immutable `SystemState` snapshots.
- Grows with every `step()`.
- Can be bounded (ring buffer) for long-running simulations.

### Time Travel (Read-Only Navigation)

```python
runner.seek(scan_id)                  # Move playhead for inspection
runner.rewind(seconds=N)              # Jump back N seconds of simulation time
runner.playhead                       # Current inspection position
```

- `seek` and `rewind` are for inspection only.
- They move the playhead but don't affect execution.
- `step()` always appends to the end of history regardless of playhead position.

### Diff

```python
runner.diff(scan_a, scan_b)
# Returns dict of {tag_name: (old_value, new_value)} for tags that changed
```

### Fork (Branching Execution)

```python
alt_runner = runner.fork_from(scan_id=50)
alt_runner.patch({"X": True})
alt_runner.run(cycles=10)
```

- Creates a new `PLCRunner` with history starting at the specified scan.
- The new runner is independent — changes don't affect the original.
- Same program, fresh execution from a historical state.

---

## Needs Specification

- **Force interaction with `out`:** If `Motor` is forced True and a rung does `out(Motor)` with rung power False, the force wins. But does the engine evaluate the rung at all? (Yes — it evaluates, then the force overrides the output. This matters for `inspect()`.)
- **Force + patch interaction:** What if a tag is both forced and patched? Force wins? Error?
- **Breakpoint removal:** How do you remove a breakpoint? Return a handle from `when()`?
- **History memory management:** Is there a max history size? Ring buffer? Configurable? What happens when it's exceeded?
- **History + labeled snapshots:** How does `snapshot(label)` interact with history retrieval? `runner.history.find(label)`?
- **Monitor cleanup:** How do you remove a monitor? Return a handle? `unmonitor(tag)`?
- **Fork state:** Does `fork_from` copy forces? Breakpoints? Monitors? (Probably: no, no, no — clean slate except for program + state.)
- **Seek + inspect:** When playhead is at scan 50, does `inspect(rung_id)` show the state at scan 50? (Yes — that's the point.)
- **Diff performance:** For large tag sets, diff could be expensive. Optimize with change tracking? Or is this premature?
- **Serialization:** Can history be saved to disk? For replaying sessions, sharing bug reports, etc. This is powerful but can be future work.
