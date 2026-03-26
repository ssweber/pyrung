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

## Workflow: write first, validate later

pyrung is intentionally permissive. Write logic with semantic tag names and native Python expressions — no address mapping required — and simulate freely. Hardware constraints are opt-in.

The natural progression:

1. **Write** — define semantic tags (`StartButton`, `MotorRunning`, `Speed`) and express logic in Python
2. **Simulate** — run tests with `FIXED_STEP`; patch inputs, assert outputs, iterate
3. **Map** — create a `TagMap` linking semantic tags to Click hardware addresses
4. **Validate** — `mapping.validate(logic, mode="warn")` surfaces Click-incompatible patterns
5. **Iterate** — fix findings, tighten to `mode="strict"` when the program is clean

The validator tells you exactly what Click can't do — inline expressions, unsupported pointer modes, type mismatches — before you discover it at deploy time.

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

### Per-slot configuration

Pre-built blocks support per-slot runtime policy for retention, default values, and naming. Configure before first access to a slot:

```python
ds.rename_slot(10, "RecipeStep")
ds.configure_slot(200, retentive=True, default=123)
td.configure_range(1, 5, retentive=False, default=0)
```

If a slot is already materialized (`block[n]` accessed), later configuration for that slot raises `ValueError`.

## Type aliases

Click-style constructor aliases as alternatives to IEC names:

| Click alias | IEC equivalent |
|-------------|----------------|
| `Bit` | `Bool` |
| `Int2` | `Dint` |
| `Float` | `Real` |
| `Hex` | `Word` |
| `Txt` | `Char` |

## DSL naming philosophy

This DSL follows Click PLC instruction naming as closely as possible, departing only when a Python conflict exists **and** the replacement name is genuinely better in a Python-hosted context.

1. **Keep the Click name** when it's a clear action verb with no conflict: `out`, `reset`, `fill`, `copy`, `blockcopy`.
2. **Use a domain synonym** when Click's name shadows a Python builtin or standard library module: `set` → `latch`, `math` → `calc`. Both are well-understood PLC terminology.
3. **Use clarified intent** when Python's execution model changes the semantics: `return` → `return_early`. In Click, every subroutine needs an explicit `RET`. In this DSL, normal subroutine completion is implicit, so the only use is early exit — and the name should say so.

| Click instruction | pyrung DSL | Reason |
|-------------------|------------|--------|
| `SET` | `latch` | Shadows Python builtin `set` |
| `MATH` | `calc` | Shadows Python stdlib `math` |
| `RET` | `return_early` | Normal return is implicit; only early exit needs a call |

The CSV ladder export uses Click-facing token names: `calc` emits as `math(...)`, `return_early` as `return()`, and `forloop` as `for(...)`. See the [CSV contract](click-ladder-csv.md) for full token grammar.

## Writing a Click program

```python
from pyrung import Bool, Real, PLCRunner, Program, Rung, TimeMode, copy, latch, reset, rise
from pyrung.click import x, y, c, ds, df, TagMap

# Define semantic tags (hardware-agnostic)
StartButton  = Bool("StartButton")
StopButton   = Bool("StopButton")
MotorRunning = Bool("MotorRunning")
RawSpeed     = Real("RawSpeed")
Speed        = Real("Speed")

# Write logic using semantic names
with Program() as logic:
    with Rung(rise(StartButton)):
        latch(MotorRunning)

    with Rung(rise(StopButton)):
        reset(MotorRunning)

    with Rung(MotorRunning):
        copy(RawSpeed, Speed)

# Simulate — no mapping needed
runner = PLCRunner(logic)
runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.1)

with runner.active():
    StartButton.value = True
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
    RawSpeed:     df[1],          # REAL → DF1 (analog input)
    Speed:        df[11],         # REAL → DF11
})
```

### Method-call syntax

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

The importer reconstructs blocks from paired `<Name>`/`</Name>` markers, infers block start indices from hardware spans, and groups dotted names (`Base.field`) into UDT structures. Standalone nicknames become individual `Tag` objects.

For strict grouping validation, pass `mode="strict"` — this fails fast on dotted UDT grouping mismatches instead of falling back to plain blocks with a warning.

```python
mapping = TagMap.from_nickname_file("project.csv", mode="strict")
```

Imported structure metadata is available via `mapping.structures` and `mapping.structure_by_name("Base")`.

### To nickname file

Export to Click nickname CSV for import into CLICK Programming Software:

```python
mapping.to_nickname_file("project.csv")
```

Mapped tags and blocks emit rows with canonical logical names, initial values, and retentive flags. Unmapped tags are omitted.

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

## Ladder CSV export

`to_ladder(program, tag_map)` emits deterministic Click ladder CSV row matrices via `LadderBundle`.

```python
from pyrung.click import to_ladder

bundle = to_ladder(logic, mapping)
bundle.main_rows          # inspect rows in-memory
bundle.write("./output")  # write main.csv + sub_*.csv to disk
```

For the consumer-facing CSV decode contract (files, row semantics, token formats, branch wiring, and supported tokens), see:

- [Click Ladder CSV Contract](click-ladder-csv.md)

## Python codegen

`to_pyrung()` converts Click ladder data back into executable pyrung Python source. Accepts a file path (to a CSV or directory) or a `LadderBundle` for in-memory round-trip without disk I/O.

```python
from pyrung.click import to_pyrung

code = to_pyrung("main.csv")                    # from CSV file
code = to_pyrung("ladder_dir/")                  # from directory with sub_*.csv
code = to_pyrung(bundle)                         # from LadderBundle (no disk)
code = to_pyrung("main.csv", output_path="generated.py")  # write to file
```

### Round-trip

```python
from pyrung.click import to_ladder, to_pyrung

bundle = to_ladder(logic, mapping)
code = to_pyrung(bundle)          # no CSV files needed
```

### Nickname substitution

Three ways to provide nicknames for readable variable names:

1. `nickname_csv=` — path to a Click nickname CSV (Address.csv). Recommended, because it also enables structured type inference (see below).
2. `nicknames=` — pre-parsed `{operand: nickname}` dict (e.g. `{"X001": "start_button"}`).
3. Neither — raw operand names used as-is (`X001`, `DS1`, etc.).

Cannot provide both `nickname_csv` and `nicknames`.

```python
code = to_pyrung("main.csv", nickname_csv="Address.csv")

code = to_pyrung("main.csv", nicknames={"X001": "start_button", "Y001": "motor"})
```

### Structured type inference

When `nickname_csv=` is provided, codegen calls `TagMap.from_nickname_file()` internally, which reconstructs `@named_array` and `@udt` metadata from the CSV markers. The generated code emits idiomatic structure declarations instead of hundreds of flat tags.

Without `nickname_csv`, a named-array group comes back flat:

```python
Channel1_id = Int("Channel1_id")
Channel1_val = Int("Channel1_val")
Channel2_id = Int("Channel2_id")
Channel2_val = Int("Channel2_val")

# in the program:
copy(Channel1_id, Channel2_val)

# in TagMap:
mapping = TagMap({
    Channel1_id: ds[101],
    Channel1_val: ds[102],
    ...
})
```

With `nickname_csv=` pointing to a CSV that has named-array markers:

```python
@named_array(Int, count=2)
class Channel:
    id = 0
    val = 0

# in the program:
copy(Channel[1].id, Channel[2].val)

# in TagMap:
mapping = TagMap([
    *Channel.map_to(ds.select(101, 104)),
], include_system=False)
```

For UDTs (fields spanning different memory banks), per-field `map_to` is emitted:

```python
@udt(count=2)
class Motor:
    running: Bool = False
    speed: Int = 0

mapping = TagMap([
    Motor.running.map_to(c.select(101, 102)),
    Motor.speed.map_to(ds.select(1001, 1002)),
], include_system=False)
```

Singleton structures (count=1) use dotted access without indexing: `Config.timeout`, not `Config[1].timeout`.

For details on `@named_array` and `@udt` syntax, see the [Tag Structures guide](../guides/tag-structures.md).

### What codegen infers

Tag types from operand prefixes (`X`→Bool, `DS`→Int, etc.), block ranges from `DS100..DS102` notation, OR expansion via `any_of()`, branch conditions, timer/counter pin chains, `for`/`next` loops, and comments.

For the CSV format that codegen reads, see the [Click Ladder CSV Contract](click-ladder-csv.md).

### Round-trip guarantee

The generated code is designed to round-trip: `exec()` the output, then `to_ladder(logic, mapping)` reproduces the original CSV. This is tested extensively.

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

`send` and `receive` implement Modbus communication with remote devices. Two addressing modes are supported:

### Click addresses (Click-to-Click)

Use a Click address string for `remote_start` when talking to another Click PLC:

```python
from pyrung.click import ModbusTcpTarget, send, receive

plc = ModbusTcpTarget("plc1", "192.168.1.20")

send(
    target=plc,
    remote_start="DS1",
    source=LocalSetpoint,
    sending=CommSending,
    success=CommSuccess,
    error=CommError,
    exception_response=CommEx,
)

receive(
    target=plc,
    remote_start="DS1",
    dest=LocalWords.select(1, 4),
    receiving=CommReceiving,
    success=CommSuccess,
    error=CommError,
    exception_response=CommEx,
)
```

Click handles word swap and character order natively — no configuration needed on the pyrung side.

### Raw Modbus addresses (any device)

Use `ModbusAddress` for `remote_start` when talking to non-Click Modbus devices (VFDs, meters, sensors, etc.):

```python
from pyrung import ModbusAddress, ModbusTcpTarget, ModbusRtuTarget, RegisterType, send, receive

vfd = ModbusTcpTarget("vfd", "192.168.1.50")

send(
    target=vfd,
    remote_start=ModbusAddress(400001),
    source=SpeedSetpoint,
    sending=VfdSending,
    success=VfdSuccess,
    error=VfdError,
    exception_response=VfdEx,
)

meter = ModbusRtuTarget("meter", "/dev/ttyUSB0", device_id=3, baudrate=19200)

receive(
    target=meter,
    remote_start=ModbusAddress(300001),
    dest=MeterReading,
    receiving=MeterReceiving,
    success=MeterSuccess,
    error=MeterError,
    exception_response=MeterEx,
    word_swap=True,
)
```

`ModbusAddress` accepts MODBUS 984 addresses (e.g. `400001` for holding, `300001` for input, `100001` for discrete input) or hex strings with an `h` suffix (e.g. `"0h"`, `"FFFEh"`). For 984 addresses, the register type is inferred from the prefix. Hex addresses need an explicit `RegisterType` since the offset alone is ambiguous.

`word_swap` controls how 32-bit values (DINT, REAL) are packed across register pairs. `False` (default) = high word first, `True` = low word first. Only relevant for 32-bit Click types (DD, DF, etc.).

`RegisterType` selects the Modbus function code: `HOLDING` (FC 3/6/16, default), `INPUT` (FC 4, read-only), `COIL` (FC 1/5/15), `DISCRETE_INPUT` (FC 2, read-only). Sending to `INPUT` or `DISCRETE_INPUT` raises `ValueError`.

### Target types

| Type | Transport | Live I/O | Codegen |
|------|-----------|----------|---------|
| `ModbusTcpTarget` | Ethernet | Yes (pymodbus for raw, pyclickplc for Click addresses) | Yes |
| `ModbusRtuTarget` | Serial | Yes (pymodbus) | Not yet |
| `str` (name only) | — | No (inert) | Yes (resolved via `ModbusClientConfig`) |

When `target` is a `ModbusTcpTarget` or `ModbusRtuTarget`, communication runs asynchronously in a background worker pool — the scan loop stays synchronous. When `target` is a plain string, the instruction is inert during simulation and exists only for CircuitPython code generation.
