# Docs-Code Alignment: Time Units, pad, Keywords, Docstrings

## Context

After a documentation pass with Sonnet, we found several places where docs and code diverged. This plan covers: exporting time unit aliases, implementing the `pad` parameter for `as_text()`, making key API parameters keyword-only, fixing counter docstrings, and correcting doc examples.

---

## 1. Export time unit aliases from `__init__.py`

**File:** `src/pyrung/core/__init__.py`

Add `Tms`, `Ts`, `Tm`, `Th`, `Td` as top-level imports from `TimeUnit`:

```python
from pyrung.core.time_mode import TimeMode, TimeUnit

# Time unit aliases for DSL ergonomics
Tms = TimeUnit.Tms
Ts = TimeUnit.Ts
Tm = TimeUnit.Tm
Th = TimeUnit.Th
Td = TimeUnit.Td
```

Add all five to `__all__`.

---

## 2. Implement `pad` parameter on `as_text()`

Data flow: `Tag.as_text()` → `as_text()` → `CopyModifier` → `CopyInstruction._copy_numeric_to_text()` → `_render_text_from_numeric()` → `_format_int_text()`

### Files to modify:

**`src/pyrung/core/copy_modifiers.py`**
- Add `pad: int | None = None` field to `CopyModifier` dataclass
- Add `pad` parameter to `as_text()` function, pass through to `CopyModifier`

**`src/pyrung/core/tag.py`** (~line 235)
- Add `pad: int | None = None` keyword param to `Tag.as_text()`, pass through

**`src/pyrung/core/instruction/data_transfer.py`** (~line 150, `_copy_numeric_to_text`)
- Pass `modifier.pad` through to `_render_text_from_numeric()`

**`src/pyrung/core/instruction/conversions.py`** (~line 96)
- Add `pad: int | None = None` param to `_render_text_from_numeric()`
- When `pad` is not None, use it as the width override in `_format_int_text()` and set `suppress_zero=False` (padding implies leading zeros)
- When `pad` is None, keep current behavior (type-default widths with `suppress_zero`)

Behavior: `DS[1].as_text(pad=5)` with value 123 → `"00123"` (5 chars, zero-padded). `pad` overrides the type-default width and forces zero-fill regardless of `suppress_zero`.

### Tests to add:

**`tests/core/test_instruction.py`** — add tests near existing `test_copy_as_text_*`:
- `test_copy_as_text_with_pad` — INT value 123, pad=7 → "0000123"
- `test_copy_as_text_pad_smaller_than_value` — value 12345, pad=3 → "12345" (no truncation)
- `test_copy_as_text_pad_with_dint` — DINT source with custom pad
- `test_copy_as_text_pad_with_negative` — negative value, pad=6 → "-00123"

---

## 3. Make API parameters keyword-only

### Timer/counter builders (`src/pyrung/core/program/builders.py`)

```python
# Before:
def on_delay(done_bit, accumulator, setpoint, time_unit=TimeUnit.Tms)
# After:
def on_delay(done_bit, accumulator, *, setpoint, time_unit=TimeUnit.Tms)
```

Same pattern for: `off_delay`, `count_up`, `count_down`

### Runner methods (`src/pyrung/core/runner.py`)

```python
# set_time_mode: make dt keyword-only
def set_time_mode(self, mode: TimeMode, *, dt: float = 0.1)

# run_until: make max_cycles keyword-only
def run_until(self, predicate, *, max_cycles: int = 10000)
```

### Test fixups required

- `tests/core/test_validation_walker.py` — 2 calls pass `time_unit` positionally:
  `on_delay(done, acc, 100, TimeUnit.Ts)` → `on_delay(done, acc, setpoint=100, time_unit=TimeUnit.Ts)`
  `off_delay(done, acc, 100, TimeUnit.Tm)` → same pattern
- `tests/core/test_timers.py` — verify all calls; most already use `setpoint=` keyword
- `tests/core/test_counters.py` — verify; most already use `setpoint=` keyword
- Any `set_time_mode` calls with positional `dt` need `dt=` keyword
- Grep for `run_until(` with positional `max_cycles` (likely none)

---

## 4. Fix counter docstrings in `builders.py`

**File:** `src/pyrung/core/program/builders.py`

`count_up()` docstring (~line 359): Change "increments on each rising edge" → "increments every scan while the rung condition is True. Use `rise()` on the condition for edge-triggered counting."

`count_down()` docstring (~line 393): Same pattern — "decrements every scan while the rung condition is True."

---

## 5. Fix doc examples

**`docs/guides/ladder-logic.md`**
- Line 199: `copy(DS[1].as_text(pad=5), Txt[1])` — now valid with pad implemented
- Line 255+: Timer examples already use `time_unit=Tms` keyword; with Tms exported this now works

**`docs/getting-started/concepts.md`**
- Line 202: `runner.run(n)` → `runner.run(cycles)` (or just leave as `runner.run(10)` with no param name shown)

---

## Verification

```bash
make          # full lint + test cycle
```

Specific checks:
- `make test` — all existing tests pass after keyword-only migration
- New pad tests pass
- `python -c "from pyrung.core import Tms; print(Tms)"` — confirms alias works
- `make lint` — ruff + ty happy with new signatures
