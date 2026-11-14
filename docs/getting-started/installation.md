# Installation

## Requirements

- Python 3.11 or later

## Install

=== "pip"

    ```bash
    pip install pyrung
    ```

=== "uv"

    ```bash
    uv add pyrung
    ```

=== "uv (dev)"

    ```bash
    uv sync --group dev
    ```

## Verify

```python
from importlib.metadata import version
from pyrung import PLC, Program

print("pyrung", version("pyrung"))
print("imports ok:", PLC, Program)
```

## Optional: CLICK dialect

`pyrung.click` is available in the base install. It depends on `pyclickplc` for CLICK address metadata, nickname CSV I/O, and Modbus server/client support.

## Development install

Clone the repository and install in editable mode:

```bash
git clone https://github.com/ssweber/pyrung
cd pyrung
uv sync --group dev --group docs
make          # lint + test
```

## Limits

pyrung is not a certified simulator. It models the program (scans, instructions, tags, and timers). It does not model the wiring, the sensors, or the firmware. The [conformance suite](../guides/conformance.md) checks pyrung against a real CLICK, case by case, and lists where they differ. Its results come from one CPU (a C2-03CPU with CLICK V4.00), and yours may differ in ways the suite doesn't reach. pyrung is not affiliated with or endorsed by AutomationDirect. If your program behaves differently in pyrung than on a CLICK PLC, that's a bug we want to know about. Validate on real hardware before deploying to production. The CircuitPython target runs on a garbage-collected runtime, so sub-millisecond scan timing is not realistic. Modbus TCP has no built-in authentication, so keep it on isolated networks.

## For LLM agents

A docs index for agents is at <https://pyrung.com/pyrung/llms.txt>.
