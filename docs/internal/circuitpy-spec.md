# CircuitPython Dialect — Handoff Brief

> **Status:** Handoff — earliest stage. Architecture decisions made, everything else is open.
> **Depends on:** All core specs.
> **No implementation milestone yet.**

---

## Scope

The CircuitPython dialect targets ProductivityOpen P1AM-200 (and potentially other Arduino-controlled PLC platforms). The key difference from Click: CircuitPython **generates deployable code** rather than exporting a configuration file. The simulation is identical — only the export path differs.

---

## Decisions Made

### Hardware Model

The P1AM-200 is a base unit with slots for I/O modules. Each slot holds a specific module (discrete input, discrete output, analog input, relay output, etc.).

```python
from pyrung.circuitpy import P1AM

hw = P1AM()
hw.slot(1, "P1-08SIM")      # 8-ch discrete input  → InputBlock
hw.slot(2, "P1-08TRS")      # 8-ch discrete output → OutputBlock
hw.slot(3, "P1-04ADL-1")    # 4-ch analog input    → InputBlock (Int)
hw.slot(4, "P1-04DAL-1")    # 4-ch analog output   → OutputBlock (Int)
```

Each slot call returns an `InputBlock` or `OutputBlock` (core types), sized and typed by the module.

### Internal Memory

CircuitPython programs need internal memory (counters, steps, flags) that isn't tied to a physical slot. These are just core `Block` instances:

```python
Flags = Block("Flags", Bool, range(1, 33))
Registers = Block("Registers", Int, range(1, 101))
```

In generated code, these become global variables or arrays.

### Code Generation

The primary export is Arduino .ino code (or MicroPython .py).

```python
from pyrung.circuitpy import ...
```

The generated code structure:

```
TBD
```

This is the `while True` loop you mentioned — every PLC scan becomes one iteration of `loop()`.

### .immediate in CircuitPython

Generates inline `point = base[1].inputs[2].value` / `allPoints = base[1].di_bitmapped()` calls within the logic section, rather than reading/writing from the scan buffers.

```
TBD
```

### CircuitPython Validation

Separate from Click. Checks things like:
- Slot number is valid for P1AM base (1–15?)
- Module string is in the known catalog
- Channel number is within module's range
- No unsupported instructions for Arduino target (are there any? TBD)
- Timer resolution constraints (Arduino `millis()` is ~1ms granularity)
- Memory usage estimation (RAM constraints on Arduino)

Produces a `ValidationReport` (same core structure as Click).

---

## Open Questions (Many)

This dialect is at the idea stage. Big questions:

- **Module catalog:** Where does the knowledge of "P1-08SIM has 8 discrete inputs" live? A dict in `pyrung.circuitpy`? A separate package (`pyp1am`)? A JSON/YAML data file?
- **Analog scaling:** Analog modules read raw ADC counts. Does CircuitPython handle scaling (engineering units), or is that the user's problem via `math()`?
- **Timer implementation:** Arduino has `millis()`. How do pyrung timers translate? `on_delay` becomes a millis-comparison pattern in generated code?
- **Counter persistence:** Arduino has no persistent storage by default. What does `retentive=True` mean? EEPROM? SD card? Just a warning?
- **Subroutine codegen:** Do pyrung subroutines become C++ functions? Arduino functions? Inline code?
- **Code generation quality:** How readable should generated code be? Comment-heavy? Minimal? Configurable?
- **Other Arduino PLC targets:** Just P1AM, or also Opta, Click (via Arduino adapter), or generic GPIO? How modular is the codegen?
- **Testing workflow:** Write in pyrung, simulate, generate code, flash to hardware. Is there a verification step? (Simulate-vs-hardware comparison?)
- **DSL extensions:** You mentioned possible DSL extensions for CircuitPython. What did you have in mind? Analog-specific instructions? Communication protocols? PID?

---

## Not For Current Scope

CircuitPython is future work. The purpose of this handoff is to prove the dialect boundary is clean — that everything we design in core works for both Click and CircuitPython without compromise. If a core design decision would paint CircuitPython into a corner, we need to know now.

Key architectural test: **can CircuitPython use the core DSL, engine, and debug API without modification?** Based on current design, yes:
- `Program`, `Rung`, conditions, instructions — all core, all work
- `InputBlock` / `OutputBlock` — core types, CircuitPython constructs them from slot config
- `PLCRunner` — simulates CircuitPython programs identically to Click
- `force`, `when().pause()`, history, `fork_from` — all work on CircuitPython programs
- Only the export path differs: `TagMap` + nickname CSV vs `generate_arduino()`
