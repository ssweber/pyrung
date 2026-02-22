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

## INT Truthiness in Conditions

Direct `INT` tags can be used as rung conditions and are treated as nonzero truthiness:

```python
Step = Int("Step")
with Program() as logic:
    with Rung(Step):  # equivalent to: with Rung(Step != 0)
        out(Bool("Light"))
```

This applies to rung/branch condition composition (`Rung`, `branch`, `any_of`, `all_of`).
It does not change helper-specific condition parameters (`count_*`, `on_delay().reset(...)`,
`shift().clock(...).reset(...)`), which still require BOOL tags or explicit comparisons.

For Click portability, `Program.validate("click", mode="strict", ...)` flags implicit INT
truthiness and requires explicit comparisons.

## Function Call Instructions (`run_function` / `run_enabled_function`)

For logic that does not map cleanly to built-in ladder instructions, pyrung provides
function-call instructions in `pyrung.core`:

- `run_function(fn, ins=None, outs=None, oneshot=False)`:
  - Runs only when rung power is true.
  - Calls `fn(**resolved_inputs)` and maps returned dict keys to output tags.
  - Optional `oneshot=True` uses standard one-shot behavior.
- `run_enabled_function(fn, ins=None, outs=None)`:
  - Runs every scan (including rung-false scans) and passes rung state as `enabled`.
  - Calls `fn(enabled, **resolved_inputs)` and maps returned dict keys to output tags.
  - Useful for async/stateful polling workflows.

Both APIs validate signatures and reject `async def` callables.

### `run_function()` Example

```python
from pyrung.core import Bool, Int, Program, Rung, run_function

Enable = Bool("Enable")
Raw = Int("Raw")
Scaled = Int("Scaled")

def scale(raw):
    return {"scaled": raw * 2 + 5}

with Program() as logic:
    with Rung(Enable):
        run_function(scale, ins={"raw": Raw}, outs={"scaled": Scaled})
```

### `run_enabled_function()` Example

```python
from pyrung.core import Bool, Int, Program, Rung, run_enabled_function

Enable = Bool("Enable")
Busy = Bool("Busy")
Count = Int("Count")

class Worker:
    def __init__(self):
        self.pending = False

    def __call__(self, enabled):
        if not enabled:
            self.pending = False
            return {"busy": False, "count": 0}
        if not self.pending:
            self.pending = True
            return {"busy": True, "count": 0}
        return {"busy": True, "count": 1}

with Program() as logic:
    with Rung(Enable):
        run_enabled_function(Worker(), outs={"busy": Busy, "count": Count})
```

Reference examples:
- `src/pyrung/examples/custom_math.py`
- `src/pyrung/examples/click_email.py`

## Migration Note

Core constructors use IEC names only: `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`.
Click-style aliases moved from `pyrung.core` to `pyrung.click`:
`Bit`, `Int2`, `Float`, `Hex`, `Txt`.

## Auto Naming (Optional)

`AutoTag` provides opt-in class-based auto naming:

```python
from pyrung.core import Bool, Int, AutoTag


class Tags(AutoTag):
    Step1_Event = Bool()
    Count = Int(retentive=True)
```

This binds names from attribute identifiers (`"Step1_Event"`, `"Count"`).
Explicit naming remains supported and unchanged: `Bool("Step1_Event")`.
If you prefer flat module names, export once: `Tags.export(globals())`.
`AutoTag` is for tags only; declare `Block`/`InputBlock`/`OutputBlock` outside the class.

Limitation: `Step1_Event = Bool()` as a plain module/local assignment is intentionally unsupported.

## Structured Tags

`@udt` creates mixed-type structures, `@named_array` creates single-type instance-interleaved arrays:

```python
from pyrung.core import udt, named_array, auto, Field, Int, Bool, Real

@udt(count=3)
class Alarm:
    id: Int = auto()
    active: Bool
    level: Real = Field(retentive=True)

Alarm[1].id       # → LiveTag "Alarm1_id"
Alarm.id          # → Block (all 3 id tags)

@named_array(Int, count=4, stride=2)
class Sensor:
    reading = 0
    offset = auto()

Sensor[1].reading # → LiveTag "Sensor1_reading"
Sensor.map_to(DS.select(1, 8))  # hardware mapping
```

## Documentation

See [CLAUDE.md](CLAUDE.md) for detailed architecture and development information.

