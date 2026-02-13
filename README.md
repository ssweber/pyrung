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

## Migration Note

Core constructors use IEC names only: `Bool`, `Int`, `Dint`, `Real`, `Word`, `Char`.
Click-style aliases moved from `pyrung.core` to `pyrung.click`:
`Bit`, `Int2`, `Float`, `Hex`, `Txt`.

## Documentation

See [CLAUDE.md](CLAUDE.md) for detailed architecture and development information.
