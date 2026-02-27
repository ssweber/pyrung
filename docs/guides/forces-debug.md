# Forces

Forces override tag values independently of logic — the simulation equivalent of PLC "override" mode. Use them for edge-case testing, known-state setup, and debugging.

## Force vs patch

| | `patch()` | `add_force()` |
|---|---|---|
| Duration | One scan | Until explicitly removed |
| Applied | Pre-logic, once | Pre-logic AND post-logic, every scan |
| Use case | Momentary inputs, test steps | Persistent overrides, test fixtures |

## Adding and removing forces

```python
runner.add_force(Button, True)
runner.add_force(Temperature, 75.5)

runner.remove_force(Button)
runner.clear_forces()           # remove all
```

> Forces also accept string keys (`runner.add_force("Button", True)`).

## `force()` context manager

Scoped forces that restore automatically on exit:

```python
with runner.force({Button: True, Fault: False}):
    runner.run(cycles=5)
# Forces restored to pre-context state
```

Safe to nest — inner blocks add to (and restore from) the outer block's forces:

```python
with runner.force({AutoMode: True}):
    runner.run(cycles=3)
    with runner.force({Fault: True}):   # adds Fault while AutoMode stays forced
        runner.run(cycles=2)
    # Fault removed; AutoMode still True
# AutoMode removed
```

See [Testing — Forces as fixtures](testing.md#using-forces-as-test-fixtures) for testing patterns.

## Inspecting active forces

```python
runner.forces   # read-only Mapping[str, value]
```

## How forces work in the scan cycle

Forces are applied at two points each scan:

```
Phase 3: APPLY FORCES (pre-logic)    ← sets force values before any rung runs
Phase 4: EXECUTE LOGIC               ← logic may overwrite forced values mid-scan
Phase 5: APPLY FORCES (post-logic)   ← re-asserts force values after all logic
```

This means:

- Forced values are present at scan start and scan end.
- Logic may temporarily change a forced value mid-scan (for example, `latch()` on a forced-False tag sets it True temporarily, but the post-logic force pass restores it).
- Edge detection (`rise`/`fall`) sees the post-force values that carry across scans.

## Force and patch interaction

If a tag is both patched and forced in the same scan, the pre-logic force pass overwrites the patched value. The patch is consumed but has no effect.

## Supported tag types

Any writable tag (`BOOL`, `INT`, `DINT`, `REAL`, `WORD`, `CHAR`) can be forced. Read-only system tags cannot be forced and raise `ValueError`.

## Next steps

- [Testing Guide](testing.md) — forces as fixtures, forking, monitors, breakpoints
- [Runner Guide](runner.md) — execution methods, history, time travel, diff, fork
