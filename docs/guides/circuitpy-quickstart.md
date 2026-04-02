# Your First P1AM Program

Wire up a discrete input and output on a P1AM-200, test the logic locally, generate CircuitPython code, and deploy to hardware.

## Configure hardware

```python
from pyrung import Bool, Int, Program, Rung, TimeMode, PLCRunner, out, copy, rise
from pyrung.circuitpy import P1AM, write_circuitpy

hw = P1AM()
inputs  = hw.slot(1, "P1-08SIM")   # 8-ch discrete input
outputs = hw.slot(2, "P1-08TRS")   # 8-ch relay output

Button = inputs[1]
Light  = outputs[1]
PressCount = Int("PressCount", retentive=True)
```

`hw.slot()` returns typed blocks matching the physical module. Slot numbers must be contiguous from 1, matching the wiring order on the DIN rail.

`PressCount` is marked `retentive=True` — its value persists to SD card across power cycles.

## Write logic

```python
with Program() as logic:
    with Rung(Button):
        out(Light)

    with Rung(rise(Button)):
        copy(PressCount + 1, PressCount)
```

Button held → Light on. Each rising edge of Button increments the press counter. Same DSL as any pyrung program — nothing CircuitPython-specific here.

## Test locally

```python
def test_button_press():
    runner = PLCRunner(logic)
    runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

    # Press button — light turns on, counter increments
    runner.patch({Button: True})
    runner.step()
    with runner.active():
        assert Light.value is True
        assert PressCount.value == 1

    # Hold button — light stays on, counter doesn't increment (no new edge)
    runner.run(cycles=5)
    with runner.active():
        assert Light.value is True
        assert PressCount.value == 1

    # Release and press again — second count
    runner.patch({Button: False})
    runner.step()
    runner.patch({Button: True})
    runner.step()
    with runner.active():
        assert PressCount.value == 2

    # Release — light turns off (out de-energizes when rung is false)
    runner.patch({Button: False})
    runner.step()
    with runner.active():
        assert Light.value is False
```

Run with `pytest`. The logic is verified before it touches hardware.

## Generate code

```python
write_circuitpy(logic, hw, target_scan_ms=10.0, watchdog_ms=500, output_dir="./build")
```

This writes two files to `./build/`:

- **`code.py`** — your program compiled to a CircuitPython scan loop. Regenerate every time you change logic.
- **`pyrung_rt.py`** — the pyrung runtime library (Modbus helpers, protocol state machines). Same for every project.

For faster boot and lower memory use, replace `pyrung_rt.py` with the pre-compiled `pyrung_rt.mpy` from the [releases page](https://github.com/ssweber/pyrung/releases).

## Deploy to hardware

### One-time board setup

1. Install [CircuitPython](https://circuitpython.org/board/p1am_200/) on the P1AM-200
2. Install the [CircuitPython P1AM library](https://github.com/facts-engineering/CircuitPython_P1AM) and its dependencies into `CIRCUITPY/lib/`
3. Download `pyrung_rt.mpy` from the [pyrung releases page](https://github.com/ssweber/pyrung/releases) and copy it to `CIRCUITPY/lib/`
4. Insert a FAT-formatted SD card (required for retentive tags like `PressCount`)

### Iterate

Copy `code.py` to the P1AM-200's `CIRCUITPY` drive. It runs automatically on boot.

The board switch works as RUN/STOP out of the box — switch down to stop execution (outputs go off), switch up to resume. Retentive tags are saved automatically on RUN→STOP and every 30 seconds when values change.

## What you get for free

- **RUN/STOP** via the board switch (debounced, default on)
- **Retentive persistence** — tagged values survive power loss via SD card with crash-safe writes
- **Watchdog** — hardware reset if the scan loop stalls beyond `watchdog_ms`
- **Scan pacing** — the loop targets `target_scan_ms` and tracks overruns

## Next steps

- [CircuitPython Dialect](../dialects/circuitpy.md) — hardware model, all 35 modules, validation, board peripherals, SD commands
- [CircuitPython Modbus TCP](../dialects/circuitpy-modbus.md) — expose tags over the network via the P1AM-ETH shield
- [Testing Guide](testing.md) — forces, time travel, forking, pytest patterns
