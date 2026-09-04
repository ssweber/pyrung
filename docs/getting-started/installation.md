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

pyrung simulates CLICK PLC behavior as faithfully as possible, but it is not a certified simulator. It models the program, meaning scans, instructions, tags, and timers, not the wiring, the sensors, or the firmware. If your program behaves differently in pyrung than on a CLICK PLC, that's a bug we want to know about, but you should always validate on real hardware before deploying to production. The CircuitPython target runs on a garbage-collected runtime, so sub-millisecond scan timing is not realistic. Modbus TCP has no built-in authentication; keep it on isolated networks.

## For LLM agents

A docs index for agents is at <https://pyrung.com/pyrung/llms.txt>.
