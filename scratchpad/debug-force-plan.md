# Phase 1 — Force (Debug Override)

## Context

First phase of the debug API (milestone 11). Forces allow manual override of tag values during scan execution — the foundation for all debugging. Unlike `patch()` (one-shot), forces persist across scans until explicitly removed.

## API

Added to `PLCRunner`:

```python
runner.add_force(tag, value)       # persistent override (str or Tag)
runner.remove_force(tag)           # remove single force
runner.clear_forces()              # remove all
with runner.force({tag: value}):   # temporary, nested-safe context manager
    ...
runner.forces                      # read-only view of active forces
```

## Changes

### 1. `src/pyrung/core/runner.py` — PLCRunner

**New instance state** in `__init__`:
```python
self._forces: dict[str, bool | int | float | str] = {}
```

**New methods:**

- `add_force(tag, value)` — normalize tag (str/Tag), reject read-only system points (same guard as `patch()`), store in `_forces`.
- `remove_force(tag)` — normalize, `KeyError` if not forced.
- `clear_forces()` — reset `_forces` to `{}`.
- `force(overrides)` — `@contextmanager`. Snapshot `_forces.copy()`, apply overrides via `add_force`, yield, restore snapshot on exit. Nested-safe because each layer saves/restores the full dict.
- `forces` property — returns a read-only `MappingProxyType` view of `_forces`.

**Modify `step()`** — insert two force application passes:

```
...existing patches application (line 187-189)...
# APPLY FORCES (pre-logic)
if self._forces:
    ctx.set_tags(self._forces)
...existing dt + logic (lines 191-206)...
# APPLY FORCES (post-logic)
if self._forces:
    ctx.set_tags(self._forces)
...existing edge detection + commit...
```

Uses `ctx.set_tags()` (public API with read-only guard) since we already validate at `add_force()` time — belt and suspenders.

**Modify `_peek_live_tag_value()`** — check `_forces` after `_pending_patches` so live tag reads outside a scan reflect forced values:

```python
if name in self._pending_patches:
    return self._pending_patches[name]
if name in self._forces:
    return self._forces[name]
```

### 2. `tests/core/test_force.py` — New test file

Test class: `TestPLCRunnerForce`

| Test | What it verifies |
|---|---|
| `test_add_force_persists_across_scans` | Forced value present after multiple `step()` calls |
| `test_add_force_accepts_tag_object` | `Tag` and `str` both work as keys |
| `test_add_force_rejects_read_only_system_point` | `ValueError` on read-only system tag |
| `test_remove_force` | Value no longer forced after removal |
| `test_remove_force_nonexistent_raises` | `KeyError` when tag not forced |
| `test_clear_forces` | All forces removed |
| `test_force_overwrites_patch` | Pre-logic force overwrites a patched value |
| `test_force_reasserts_after_logic` | Post-logic force overwrites logic assignment |
| `test_force_does_not_lock_midcycle` | Logic *can* see its own writes during evaluation (force only reasserts at boundaries) |
| `test_force_context_manager_temporary` | Forces active inside `with`, removed after |
| `test_force_context_manager_nested` | Inner context restores outer's forces on exit |
| `test_force_context_manager_exception_safe` | Forces restored even if body raises |
| `test_forces_property_readonly` | `.forces` returns current map, not mutatable |
| `test_force_and_edge_detection` | `rise()` triggers correctly with forced pre-logic value across scans |
| `test_force_multiple_tags` | Multiple simultaneous forces |
| `test_peek_live_reflects_force` | `Tag.value` via live binding returns forced value |

### 3. No changes needed to:
- `context.py` — existing `set_tags()` is sufficient
- `state.py` — no structural changes
- `__init__.py` — force API lives on `PLCRunner`, already exported

## Verification

```bash
make test          # all existing + new tests pass
make lint          # no regressions
```
