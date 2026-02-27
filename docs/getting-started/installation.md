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
from pyrung import PLCRunner, Program

print("pyrung", version("pyrung"))
print("imports ok:", PLCRunner, Program)
```

## Optional: Click dialect

`pyrung.click` is available in the base install. It depends on `pyclickplc` for Click address metadata, nickname CSV I/O, and Modbus server/client support.

## Development install

Clone the repository and install in editable mode:

```bash
git clone https://github.com/ssweber/pyrung
cd pyrung
uv sync --group dev --group docs
make          # lint + test
```
