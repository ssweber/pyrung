# Pyrung

A Python DSL (Domain Specific Language) for representing and simulating Ladder Logic. Pyrung provides a Pythonic way to write PLC programs for simulation, testing, and documentation purposes.

## Status

PLANNING ONLY * INITIAL COMMIT

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
runner.patch({"Button": True})
runner.step()
```
