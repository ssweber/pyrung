# CircuitPython Dialect

`pyrung.circuitpy` adds a P1AM-200 hardware model, module catalog, program validation, and CircuitPython code generation on top of the hardware-agnostic core.

## Installation / Imports

```bash
pip install pyrung
```

```python
from pyrung import Bool, Int, Real, PLCRunner, Program, Rung, TimeMode, out, copy, rise
from pyrung.circuitpy import P1AM, generate_circuitpy, validate_circuitpy_program
```

## Hardware Setup — P1AM

The [ProductivityOpen P1AM-200](https://facts-engineering.github.io/modules/P1AM-200/P1AM-200.html) is a base unit with up to 15 slots for [Productivity1000 I/O modules](https://www.automationdirect.com/adc/overview/catalog/programmable_controllers/productivity1000_plcs_(stackable_micro)). Configure hardware with the `P1AM` class:

```python
hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")       # 8-ch discrete input  → InputBlock(Bool)
outputs = hw.slot(2, "P1-08TRS")       # 8-ch discrete output → OutputBlock(Bool)
analog  = hw.slot(3, "P1-04ADL-1")     # 4-ch analog input    → InputBlock(Int)
```

Each `hw.slot()` call returns:

- **`InputBlock`** for input-only modules
- **`OutputBlock`** for output-only modules
- **`tuple[InputBlock, OutputBlock]`** for combo modules (e.g. `P1-16CDR`)

Slots must be numbered 1–15 and contiguous from 1 (matching physical wiring order). Use the optional `name` keyword to override the default `"Slot{N}"` prefix:

```python
hw.slot(1, "P1-08SIM", name="Sensors")   # tags named Sensors.1 .. Sensors.8
```

### Supported modules

The built-in `MODULE_CATALOG` includes 35 modules from the [Productivity1000 series](https://facts-engineering.github.io/) across six categories:

| Category | Count | Examples |
|----------|------:|---------|
| Discrete input | 7 | P1-08SIM, P1-16ND3, P1-08NA, P1-08NE3 |
| Discrete output | 9 | P1-08TRS, P1-16TR, P1-04TRS, P1-08TA |
| Combo discrete | 3 | P1-16CDR, P1-15CDD1, P1-15CDD2 |
| Analog input | 7 | P1-04AD, P1-04ADL-1, P1-08ADL-1 |
| Analog output | 4 | P1-04DAL-1, P1-04DAL-2, P1-08DAL-1 |
| Temperature input | 3 | P1-04RTD, P1-04THM, P1-04NTC |
| Combo analog | 2 | P1-4ADL2DAL-1, P1-4ADL2DAL-2 |

Type mapping: discrete → `Bool`, analog → `Int`, temperature → `Real`.

### Excluded modules (v2)

[`P1-04PWM`](https://facts-engineering.github.io/modules/P1-04PWM/P1-04PWM.html) (PWM) and [`P1-02HSC`](https://facts-engineering.github.io/modules/P1-02HSC/P1-02HSC.html) (high-speed counter) require a multi-tag channel model and are deferred to v2.

## Writing a CircuitPython Program

Programs use the same DSL as any other pyrung dialect — only the hardware setup and export step are dialect-specific.

```python
from pyrung import Bool, Int, PLCRunner, Program, Rung, TimeMode, out, copy, rise
from pyrung.circuitpy import P1AM, generate_circuitpy

# 1. Configure hardware
hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")
outputs = hw.slot(2, "P1-08TRS")

Button = inputs[1]
Light  = outputs[1]
Counter = Int("Counter")

# 2. Write logic — identical to any other pyrung program
with Program() as logic:
    with Rung(Button):
        out(Light)

# 3. Simulate
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.patch({Button.name: True})
runner.step()
assert runner.state.tags[Light.name] is True

# 4. Generate deployable CircuitPython code
source = generate_circuitpy(logic, hw, target_scan_ms=10.0, watchdog_ms=500)
```

Simulation is identical to any other pyrung program — `PLCRunner`, `patch()`, `force()`, history, breakpoints, and all debug tools work unchanged.

## Code Generation

```python
source = generate_circuitpy(program, hw, *, target_scan_ms, watchdog_ms=None)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `program` | `Program` | Ladder logic program |
| `hw` | `P1AM` | Hardware configuration |
| `target_scan_ms` | `float` | Target scan cycle time in milliseconds (must be > 0) |
| `watchdog_ms` | `int \| None` | Hardware watchdog timeout in ms, or `None` to disable |

Returns a `str` containing a complete, self-contained CircuitPython source file. The generator runs strict validation internally and compiles the output to verify syntax correctness.

### Generated file structure

The output is a single `.py` file organized as:

1. **Imports** — `time`, `json`, `board`, `busio`, `P1AM`, `sdcardio`, `storage`, `microcontroller`
2. **Configuration** — `TARGET_SCAN_MS`, `WATCHDOG_MS`, slot module list, retentive schema hash
3. **Hardware bootstrap** — `P1AM.Base()`, `rollCall()`, optional watchdog init
4. **Tag declarations** — one variable per scalar tag, one list per block
5. **Memory buffers** — edge-detection state, scan timing
6. **SD mount / retentive load/save** — generated when retentive tags exist
7. **Helper functions** — rising edge, type conversions, math helpers
8. **Main scan loop** — reads inputs, executes rungs, writes outputs, paces to target scan time

### Retentive tag persistence

Tags marked `retentive=True` are automatically persisted to an SD card:

- **Storage path:** `/sd/memory.json` (atomic writes via temp file)
- **Schema hash:** SHA-256 of tag names and types. On load, a schema mismatch (e.g. after a firmware change) skips the stale file and starts from defaults.
- **NVM dirty flag:** `microcontroller.nvm[0]` is set to `1` before writing and cleared to `0` after. If the controller restarts mid-write, the dirty flag prevents loading a corrupt file.

### Watchdog

When `watchdog_ms` is set, the generated code calls `base.config_watchdog()` and `base.start_watchdog()` at boot, then `base.pet_watchdog()` each scan. If the scan loop stalls longer than the timeout, the P1AM hardware resets the controller.

### Scan timing and overrun detection

The scan loop paces itself to `target_scan_ms` using `time.monotonic()`. If a scan takes longer than the target, the overrun is counted and optionally printed (controlled by `PRINT_SCAN_OVERRUNS` in the generated code).

## Validation

```python
report = validate_circuitpy_program(program, hw, mode="warn")
print(report.summary())

for finding in report.errors + report.warnings + report.hints:
    print(f"  {finding.severity}: [{finding.code}] {finding.message}")
```

Also accessible as a dialect entry point on `Program`:

```python
report = program.validate("circuitpy", hw=hw, mode="warn")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `program` | `Program` | — | Program to validate |
| `hw` | `P1AM \| None` | `None` | Hardware config for I/O traceability checks |
| `mode` | `"warn" \| "strict"` | `"warn"` | `"warn"` emits hints; `"strict"` upgrades to errors |

### Finding codes

| Code | Trigger | Description |
|------|---------|-------------|
| `CPY_FUNCTION_CALL_VERIFY` | `FunctionCallInstruction` in program | Callable will be embedded via `inspect.getsource()` — verify it uses only CircuitPython-compatible APIs |
| `CPY_IO_BLOCK_UNTRACKED` | I/O tag not traceable to a `P1AM` slot | Tag was created outside `hw.slot()` — it won't be wired to physical I/O in generated code |
| `CPY_TIMER_RESOLUTION` | `on_delay` / `off_delay` with `Tms` timing | Millisecond timer accuracy depends on scan time; effective resolution is one scan |

In `"warn"` mode these produce hints. In `"strict"` mode they become errors. `generate_circuitpy()` runs strict validation internally but ignores `CPY_FUNCTION_CALL_VERIFY` and `CPY_TIMER_RESOLUTION` (non-blocking advisories).

## Deploying to Hardware

The generated code uses the [CircuitPython P1AM library](https://github.com/facts-engineering/CircuitPython_P1AM). Make sure the library is installed on your P1AM-200 before deploying (see [P1AM-200 getting started guide](https://facts-engineering.github.io/modules/P1AM-200/P1AM-200.html)).

1. Call `generate_circuitpy()` to produce the source string
2. Write it to a file (e.g. `code.py`)
3. Copy `code.py` to the P1AM-200's CIRCUITPY drive — it runs automatically on boot
4. Insert an SD card for retentive tag storage (FAT-formatted)

!!! note "Verify embedded functions"
    If your program uses `FunctionCallInstruction`, the callable's source is embedded verbatim. Ensure it only uses CircuitPython-compatible modules and APIs.

## External Resources

- [P1AM-200 documentation](https://facts-engineering.github.io/modules/P1AM-200/P1AM-200.html) — hardware specs, pinout, getting started
- [CircuitPython P1AM library](https://github.com/facts-engineering/CircuitPython_P1AM) — the runtime library used by generated code
- [Productivity1000 I/O module docs](https://facts-engineering.github.io/) — per-module wiring diagrams and specs
- [P1AM-200 on AutomationDirect](https://www.automationdirect.com/adc/shopping/catalog/programmable_controllers/productivity_open_(arduino-compatible)/controllers_-a-_shields/p1am-200) — ordering and datasheets
- [CircuitPython documentation](https://docs.circuitpython.org/) — language reference for the target runtime

## API Reference

- [`P1AM`](../reference/api/circuitpy/hardware.md) — hardware model
- [`MODULE_CATALOG`](../reference/api/circuitpy/catalog.md) — module specifications
- [`generate_circuitpy`](../reference/api/circuitpy/codegen.md) — code generation
- [`validate_circuitpy_program`](../reference/api/circuitpy/validation.md) — program validation
