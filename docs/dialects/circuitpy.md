# CircuitPython Dialect

!!! note "Planned — not yet implemented"
    The `pyrung.circuitpy` dialect is designed but not yet implemented.
    This page will be expanded when the dialect ships.

## Overview

The CircuitPython dialect will add:

- A `P1AM` hardware model with slot/channel addressing
- A module catalog for P1AM I/O modules
- `InputBlock` / `OutputBlock` creation from physical slot definitions
- Code generation: `generate_circuitpython()` → deployable `.py` file
- CircuitPython-specific validation rules

## Planned API sketch

```python
from pyrung.core import *
from pyrung.circuitpy import P1AM, generate_circuitpython

hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")    # → InputBlock("Slot1", Bool, range(1, 9))
outputs = hw.slot(2, "P1-08TRS")    # → OutputBlock("Slot2", Bool, range(1, 9))

Button = inputs[1]    # InputTag
Light  = outputs[1]   # OutputTag

with Program() as logic:
    with Rung(Button):
        out(Light)

# Simulate — identical to any other pyrung program
runner = PLCRunner(logic)
runner.patch({Button.name: True})
runner.step()

# Generate deployable CircuitPython code
generate_circuitpython(logic, hw, output="main.py")
```

The core DSL, runner API, and all instructions are identical regardless of dialect.
Only the hardware setup and code-generation step are dialect-specific.

## Status

Internal design notes are in `docs/internal/circuitpy-spec.md`.
