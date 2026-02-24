# Click PLC Dialect

`pyrung.click` adds Click-PLC-specific blocks, type aliases, address mapping, nickname file I/O, validation, and a soft-PLC adapter on top of the hardware-agnostic core.

## Installation

```bash
pip install pyrung
```

`pyrung.click` uses `pyclickplc` for address metadata, nickname CSV I/O, and soft-PLC server/client integration.

## Imports

```python
from pyrung import Bool, Int, PLCRunner, Program, Rung, TimeMode, copy, latch, reset, rise
from pyrung.click import x, y, c, ds, TagMap
```

## Pre-built blocks

`pyrung.click` exports pre-built blocks for every Click memory bank:

| Variable | Bank | Type | Block kind |
|----------|------|------|------------|
| `x` | X (inputs) | BOOL | InputBlock |
| `y` | Y (outputs) | BOOL | OutputBlock |
| `c` | C (bit memory) | BOOL | Block |
| `ds` | DS (int memory) | INT | Block |
| `dd` | DD (double int) | DINT | Block |
| `dh` | DH (hex memory) | WORD | Block |
| `df` | DF (float memory) | REAL | Block |
| `t` | T (timer done) | BOOL | Block |
| `td` | TD (timer acc) | INT | Block |
| `ct` | CT (counter done) | BOOL | Block |
| `ctd` | CTD (counter acc) | DINT | Block |
| `sc` | SC (system control) | BOOL | Block |
| `sd` | SD (system data) | INT | Block |
| `txt` | TXT (text memory) | CHAR | Block |
| `xd` | XD (word image) | WORD | InputBlock |
| `yd` | YD (word image) | WORD | OutputBlock |

Addresses use canonical Click display names:

```python
x[1].name   # "X001"
y[1].name   # "Y001"
c[1].name   # "C1"
ds[1].name  # "DS1"
```

### Sparse banks

X and Y are sparse banks with non-contiguous valid addresses. `.select()` filters to valid addresses automatically:

```python
x.select(1, 21)   # yields X001..X016 and X021 (17 tags, not 21)
```

### Per-slot runtime policy

Pre-built Click blocks can be configured in place with per-slot runtime policy
for retention/default seed values.

```python
from pyrung.click import ds, td

# Configure before first access to the same slot.
ds.configure_slot(200, retentive=True, default=123)
td.configure_range(1, 5, retentive=False, default=0)
```

Effective policy precedence:

- `retentive`: slot override > block default
- `default`: slot override > block `default_factory(addr)` > type default

If a slot is already materialized (`block[n]` accessed), later `configure_*`/`clear_*`
for that slot raise `ValueError`.

## Type aliases

Click-style constructor aliases are available as convenience alternatives to IEC names:

| Click alias | IEC equivalent |
|-------------|----------------|
| `Bit` | `Bool` |
| `Int2` | `Dint` |
| `Float` | `Real` |
| `Hex` | `Word` |
| `Txt` | `Char` |

## DSL naming philosophy

This DSL follows Click PLC instruction naming as closely as possible, departing only when a Python conflict exists **and** the replacement name is genuinely better in a Python-hosted context. Every rename has a reason, and the reason is always "the new name is clearer here," never just "Python forced our hand."

### Principles

1. **Keep the Click name** when it's a clear action verb with no conflict: `out`, `reset`, `fill`, `copy`, `blockcopy`.

2. **Use a domain synonym** when Click's name shadows a Python builtin or standard library module: `set` → `latch`, `math` → `calc`. Both replacements are well-understood PLC terminology and arguably more descriptive of what the instruction does.

3. **Use clarified intent** when Python's execution model changes the semantics: `return` → `return_early`. In Click, every subroutine needs an explicit `RET`. In this DSL, normal subroutine completion is implicit via `with Subfunction("name"):`, so the only use of a return instruction is early exit — and the name should say so.

### Why not trailing underscores?

Names like `math_` or `return_` signal "I wanted the real name but couldn't have it." A DSL should feel like a first-class domain language, not a workaround. Each of our renames stands on its own merits.

### Rename table

| Click instruction | pyrung DSL | Reason |
|-------------------|------------|--------|
| `SET` | `latch` | Shadows Python builtin `set` |
| `MATH` | `calc` | Shadows Python stdlib `math` |
| `RET` | `return_early` | Normal return is implicit; only early exit needs a call |

## Writing a Click program

```python
from pyrung import Bool, Int, PLCRunner, Program, Rung, TimeMode, copy, latch, reset, rise
from pyrung.click import x, y, c, ds, TagMap

# Define semantic tags (hardware-agnostic)
StartButton  = Bool("StartButton")
StopButton   = Bool("StopButton")
MotorRunning = Bool("MotorRunning")
Speed        = Int("Speed")

# Write logic using semantic names
with Program() as logic:
    with Rung(rise(StartButton)):
        latch(MotorRunning)

    with Rung(rise(StopButton)):
        reset(MotorRunning)

    with Rung(MotorRunning):
        copy(Speed, ds[1])

# Simulate — no mapping needed
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)
runner.patch({"StartButton": True})
runner.step()
```

## TagMap — mapping to hardware

`TagMap` links semantic tags and blocks to Click hardware addresses. Mapping is separate from logic — write and simulate first, map when ready.

### Dict constructor

```python
mapping = TagMap({
    StartButton:  x[1],           # BOOL → X001
    StopButton:   x[2],           # BOOL → X002
    MotorRunning: y[1],           # BOOL → Y001
    Speed:        ds[1],          # INT  → DS1
})
```

### Method-call syntax

Tags and blocks support `.map_to()` for an alternative style:

```python
StartButton.map_to(x[1])
Speed.map_to(ds[1])
```

### Mapping a block to a hardware range

```python
Alarms = Block("Alarms", TagType.BOOL, 1, 100)

mapping = TagMap({
    Alarms: c.select(101, 200),    # Alarms[1..100] → C101..C200
})
```

### Type validation at map time

`TagMap` validates that logical and hardware data types match:

```python
TagMap({Speed: c[1]})   # raises: INT cannot map to C (BIT)
```

### From nickname file

Load an existing Click nickname CSV:

```python
mapping = TagMap.from_nickname_file("project.csv")
```

- Block tag pairs (`<Name>` / `</Name>` comments) are reconstructed as `Block` objects.
- Standalone nicknames become individual `Tag` objects.
- Timer/counter `_D` suffix pairs are linked automatically.

### To nickname file

Export to Click nickname CSV for import into CLICK Programming Software:

```python
mapping.to_nickname_file("project.csv")
```

- Mapped tags/blocks emit rows with nickname, initial value, and retentive flag.
- Unmapped tags are omitted.

## Validation

After mapping, validate your program against Click hardware restrictions:

```python
report = mapping.validate(logic, mode="warn")
print(report.summary())

for finding in report.findings:
    print(f"  {finding.level}: {finding.message}")
```

Common findings:

| Issue | pyrung allows | Click requires |
|-------|--------------|----------------|
| Pointer in `copy` source | Any block, arithmetic | DS only, no arithmetic |
| Inline expression in condition | `(A + B) > 100` | Must use `calc()` first |

Findings are hints by default (`mode="warn"`). Use `mode="strict"` to treat hints as errors.

## ClickDataProvider — soft PLC

`ClickDataProvider` implements the `pyclickplc` `DataProvider` protocol, bridging pyrung's `SystemState` to a Modbus TCP server. This lets pyrung act as a soft PLC accessible from Click Programming Software or any Modbus client.

```python
from pyrung.click import ClickDataProvider
from pyclickplc.server import ClickServer

provider = ClickDataProvider(runner, tag_map=mapping)
server = ClickServer(provider, port=502)
```

Reads return the current committed state. Writes queue a `runner.patch()` for the next scan.

### Word-image (XD / YD) addressing

- `XD*` reads compute bit-image words from current X bit state.
- `YD*` reads compute bit-image words from current Y bit state.
- `YD*` writes fan out to the corresponding Y bits.
- `XD*` writes are rejected (read-only).

## Communication instructions

`send` and `receive` implement Modbus TCP communication between Click PLCs:

```python
from pyrung.click import send, receive

send(
    host="192.168.1.20",
    port=502,
    remote_start="DS1",
    source=LocalSetpoint,
    sending=CommSending,
    success=CommSuccess,
    error=CommError,
    exception_response=CommEx,
)

receive(
    host="192.168.1.20",
    port=502,
    remote_start="DS1",
    dest=LocalWords.select(1, 4),
    receiving=CommReceiving,
    success=CommSuccess,
    error=CommError,
    exception_response=CommEx,
)
```

Communication runs asynchronously in a background worker pool — the scan loop stays synchronous. The instruction self-gates on the rung condition.

## API Reference

See the [API Reference](../reference/index.md) for full parameter documentation:

- [`TagMap`](../reference/api/click/tag_map.md)
- [`ClickDataProvider`](../reference/api/click/data_provider.md)
- [`send` / `receive`](../reference/api/click/send_receive.md)
- [`validate_click_program`](../reference/api/click/validation.md)

