# Pyrung

A Python DSL (Domain Specific Language) for representing and simulating Ladder Logic. Pyrung provides a Pythonic way to write PLC programs for simulation, testing, and documentation purposes.

## Status

**Proof of Concept** - The core execution engine works, but the library is under active development.

## Goals

- Provide a readable, Pythonic syntax for ladder logic
- Enable simulation and testing of PLC programs without physical hardware
- Support documentation and validation of PLC logic

## Quick Example

```python
from pyrung import *

Button = Bool("Button")
Light = Bool("Light")

with Program() as logic:
    with Rung(Button):
        out(Light)

runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
with runner.active():
    Button.value = True
runner.step()
```

## Escape Hatch Callbacks (`custom` / `acustom`)

For logic that does not map well to built-in DSL instructions, pyrung provides two escape-hatch
instructions in `pyrung.core`:

- `custom(fn, oneshot=False)`:
  - Runs only when rung power is true.
  - Callback signature: `fn(ctx) -> None`
  - Optional `oneshot=True` uses standard one-shot behavior.
- `acustom(fn)`:
  - Runs every scan (including rung-false scans) and receives rung state.
  - Callback signature: `fn(ctx, enabled: bool) -> None`
  - Useful for async/stateful polling workflows.

Both APIs validate callback compatibility and reject `async def` callbacks.

### `custom()` Example (synchronous)

```python
from pyrung.core import Bool, Int, Program, Rung, custom

Enable = Bool("Enable")
Raw = Int("Raw")
Scaled = Int("Scaled")

def scale(ctx):
    raw = int(ctx.get_tag(Raw.name, 0))
    ctx.set_tag(Scaled.name, raw * 2 + 5)

with Program() as logic:
    with Rung(Enable):
        custom(scale)
```

### `acustom()` Example (scan-to-scan state machine)

```python
from pyrung.core import Bool, Int, Program, Rung, acustom

Enable = Bool("Enable")
Busy = Bool("Busy")
Count = Int("Count")

def worker(ctx, enabled):
    key = "_custom:worker:busy"
    pending = bool(ctx.get_memory(key, False))
    if enabled and not pending:
        ctx.set_memory(key, True)
        ctx.set_tag(Busy.name, True)
        return
    if enabled and pending:
        n = int(ctx.get_tag(Count.name, 0))
        ctx.set_tag(Count.name, n + 1)
        return
    ctx.set_memory(key, False)
    ctx.set_tag(Busy.name, False)

with Program() as logic:
    with Rung(Enable):
        acustom(worker)
```

Reference examples:
- `src/pyrung/examples/custom_math.py`
- `src/pyrung/examples/click_email.py`

## Migration Note

Core constructors use IEC names only: `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`.
Click-style aliases moved from `pyrung.core` to `pyrung.click`:
`Bit`, `Int2`, `Float`, `Hex`, `Txt`.

## Documentation

See [CLAUDE.md](CLAUDE.md) for detailed architecture and development information.
