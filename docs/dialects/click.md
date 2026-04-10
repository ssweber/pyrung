# Click PLC Dialect

`pyrung.click` adds Click-PLC-specific blocks, type aliases, address mapping, nickname file I/O, validation, and a soft-PLC adapter on top of the hardware-agnostic core.

## Installation

```bash
pip install pyrung
```

`pyrung.click` uses `pyclickplc` for address metadata, nickname CSV I/O, and soft-PLC server/client integration.

## Imports

```python
from pyrung import Bool, Int, PLC, Program, Rung, copy, latch, reset, rise
from pyrung.click import x, y, c, ds, TagMap
```

## Workflow: write first, validate later

pyrung is intentionally permissive. Write logic with semantic tag names and native Python expressions — no address mapping required — and simulate freely. Hardware constraints are opt-in.

The natural progression:

1. **Write** — define semantic tags (`StartButton`, `MotorRunning`, `Speed`) and express logic in Python
2. **Simulate** — run tests with a fixed `dt`; set inputs, assert outputs, iterate
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
ds.slot(10, name="RecipeStep")
ds.slot(200, retentive=True, default=123)
td.slot(1, 5, retentive=False, default=0)
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

The CSV ladder export uses Click-facing token names: `calc` emits as `math(...)`, `return_early` as `return()`, and `forloop` as `for(...)`. See the [laddercodec CSV format guide](https://ssweber.github.io/laddercodec/guides/csv-format/) for the full token grammar.

## Writing a Click program

```python
from pyrung import Bool, Real, PLC, Program, Rung, copy, latch, reset, rise
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
with PLC(logic, dt=0.1) as plc:
    StartButton.value = True
    plc.step()
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

### Timer/Counter mapping

`Timer` and `Counter` are built-in UDTs. Map them to Click hardware banks explicitly, just like any other block:

```python
OvenTimer = Timer.clone("OvenTimer")

mapping = TagMap([
    OvenTimer.Done.map_to(t[1]),
    OvenTimer.Acc.map_to(td[1]),
])
```

#### Codegen from nicknames

When importing ladder CSV with nicknames, a nickname on a T or CT address produces a named clone:

```python
# Without nickname → clone named after the operand
T1 = Timer.clone("T1")
on_delay(T1, preset=100, unit="Tms")

# With nickname {"T1": "OvenTimer"} → clone named after the nickname
OvenTimer = Timer.clone("OvenTimer")
on_delay(OvenTimer, preset=100, unit="Tms")
```

The T (done-bit) nickname drives the name — any nickname on the matching TD/CTD address is silently overridden. This keeps `.Done` and `.Acc` fields under a single consistent prefix.

If the nickname already ends with `_Done` or `_Acc`, the suffix is stripped automatically — `"OvenTimer_Done"` becomes `Timer.clone("OvenTimer")`.

Condition references resolve through the clone. A rung conditioned on T1 renders as `OvenTimer.Done`:

```python
with Rung(OvenTimer.Done):
    out(AlarmLight)
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

The importer reconstructs blocks, structures, and standalone tags from CSV comment markers and nickname patterns. Standalone nicknames become individual `Tag` objects. Non-marker address-comment text is preserved on standalone tags and block slots for CSV round-trip export.

For strict grouping validation, pass `mode="strict"` — this fails fast on structure grouping mismatches instead of falling back to plain blocks with a warning.

```python
mapping = TagMap.from_nickname_file("project.csv", mode="strict")
```

Imported structure metadata is available via `mapping.structures` and `mapping.structure_by_name("Base")`.

#### CSV marker format

The comment field on CSV rows carries block and structure boundaries. Three marker types:

| Marker | Example | Meaning |
|--------|---------|---------|
| Opening | `<Alarms:block>` | Start of a semantic plain block |
| Closing | `</Alarms:block>` | End of a semantic plain block |
| Self-closing | `<Config.timeout:udt />` | Single-row semantic marker |

Bare tags are grouping-only comments: `<Alarms>`, `</Alarms>`, `<Base.field>`, and `<Base.field />` do not reconstruct pyrung semantics. They simply group rows visually, and any inner nicknames import as ordinary standalone tags.

**Plain blocks** use explicit `:block` markers: `<Alarms:block>` / `</Alarms:block>`. If the logical start differs from the inferred default, export/import uses `<Alarms:block(n)>` or `<Alarms:block(start=n)>`. If a boundary row has a blank nickname, default retentive/default value, and its comment is only the block tag, pyrung treats that row as boundary metadata rather than a slot rename/config override.

**Named arrays** use `:named_array` markers. Count and stride are optional — the importer infers them from the row span between open/close tags:

```
<Task:named_array>            count=1, stride from row count
<Task:named_array(2)>         count=2, stride = rows / count
<Task:named_array(2,3)>       count=2, stride=3 (fully explicit)
```

When both count and stride are given, the row span must equal `count × stride`. When stride is omitted, the row count must be divisible by count.

**Nickname patterns.** For `count > 1`, nicknames must follow `{Base}{instance}_{field}` with 1-based instance numbers. The instance is derived from position: `position // stride + 1`. Field names are the suffix after the prefix strip (`Channel1_Id` → field `Id`).

For `count = 1`, nicknames default to the compact form `{Base}_{field}` (no instance number). If the CSV already uses numbered names like `Task1_Call`, the importer detects this and sets `always_number=True` automatically. To force numbered names explicitly, add `,always_number` to the marker:

```
<Task:named_array(1,2,always_number)>
</Task:named_array(1,2,always_number)>
```

The `always_number` flag only matters for singletons — `count > 1` is always numbered regardless.

**Instance rules.** Instance 1 defines the field template — all its fields must be explicitly named. Instance 2+ fields must match instance 1's pattern (correct field name and instance number). Unnamed slots in instance 2+ are fine (silently skipped). A field name in instance 2+ that wasn't defined in instance 1 is an error.

Example — `Channel` with 2 instances, 3 fields, no gaps (`stride=3`):

| Address | Nickname | Comment |
|---------|----------|---------|
| DS101 | `Channel1_Id` | `<Channel:named_array(2,3)>` |
| DS102 | `Channel1_Val` | |
| DS103 | `Channel1_Name` | |
| DS104 | `Channel2_Id` | |
| DS105 | `Channel2_Val` | |
| DS106 | `Channel2_Name` | `</Channel:named_array(2,3)>` |

Singleton with compact names (`count=1`):

| Address | Nickname | Comment |
|---------|----------|---------|
| DS501 | `Task_Call` | `<Task:named_array(1,2)>` |
| DS502 | `Task_Done` | `</Task:named_array(1,2)>` |

If stride exceeds the field count, the extra slots are gaps (empty nicknames):

| Address | Nickname | Comment |
|---------|----------|---------|
| DS101 | `Sensor1_Raw` | `<Sensor:named_array(2,3)>` |
| DS102 | `Sensor1_Scaled` | |
| DS103 | | *(gap)* |
| DS104 | `Sensor2_Raw` | |
| DS105 | `Sensor2_Scaled` | |
| DS106 | | `</Sensor:named_array(2,3)>` |

Click codegen can round-trip aligned whole-instance spans back into pyrung as `Name.instance(...)` or `Name.instance_select(...)` instead of raw bank ranges. This works for both dense and sparse layouts:

```python
blockcopy(RecipeProfile.instance(2), WorkingRecipe.select(1, 3))
fill(0, RecipeProfile.instance_select(1, 2))
```

**UDTs** use explicit `:udt` markers per field and memory bank. Each attribute range is a separate marker:

```text
<Motor.Speed:udt>
</Motor.Speed:udt>
<Config.Timeout:udt />
```

The importer collects all `Base.Field:udt` ranges that share the same base name and assembles them into a single `@udt`. Field attribute ranges must have matching hardware span lengths across all attributes.

Bare dotted tags such as `<Motor.Speed>` are grouping-only and do not reconstruct a UDT.

Nesting is not supported — a UDT field cannot itself be a named array (e.g. `Sts.Recipes:named_array(20,50)` won't parse). Flatten the name instead: `StsRecipes:named_array(20,50)`.

**Conflict rules.** The same base name cannot be used across different marker kinds. These combinations are all errors:

- Same name as both `:named_array` and `.attr:udt`
- Same name as both `:block` and `:named_array`
- Same name as both `:block` and `.attr:udt`
- Duplicate `:named_array` or `:block` markers for the same name

### To nickname file

Export to Click nickname CSV for import into CLICK Programming Software:

```python
mapping.to_nickname_file("project.csv")
```

Mapped tags and blocks emit rows with canonical logical names, initial values, retentive flags, and preserved address comments. If a row needs both a block marker and user comment text, both are emitted in the same CSV comment field. Unmapped tags are omitted.

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

### Timer preset limits

Click timer accumulators are 16-bit signed INT (max 32,767). A literal preset exceeding this range silently clamps at runtime. The validator reports `CLK_TIMER_PRESET_OVERFLOW` for out-of-range presets — use a larger time unit instead.

| Unit | Max preset | Max duration |
|------|-----------|--------------|
| `Tms` | 32,767 | 32.7 seconds |
| `Ts` | 32,767 | 9.1 hours |
| `Tm` | 32,767 | 22.7 days |
| `Th` | 32,767 | 3.7 years |
| `Td` | 32,767 | 89 years |

```python
# Wrong — clamps silently to 32.7 seconds
on_delay(MyTimer, preset=60000, unit="Tms")

# Right — use seconds
on_delay(MyTimer, preset=60, unit="Ts")
```

Findings are hints by default (`mode="warn"`). Use `mode="strict"` to treat hints as errors.

## Ladder CSV export

`pyrung_to_ladder(program, tag_map)` emits deterministic Click ladder CSV row matrices via `LadderBundle`.

```python
from pyrung.click import pyrung_to_ladder

bundle = pyrung_to_ladder(logic, mapping)
bundle.main_rows          # inspect rows in-memory
bundle.write("./output")  # write main.csv + subroutines/*.csv to disk
```

For the consumer-facing CSV decode contract (files, row semantics, token formats, branch wiring, and supported tokens), see the [laddercodec CSV format guide](https://ssweber.github.io/laddercodec/guides/csv-format/).

To convert ladder CSV back into pyrung Python source, see [Click Python Codegen](click-codegen.md).

### Empty and comment-only rungs

Empty rungs survive the round-trip. A `with Rung(): pass` in pyrung exports as `NOP` in the Click CSV AF column and imports back as `pass`.

```python
comment("--- Motor Control Section ---")
with Rung():
    pass  # becomes NOP in Click ladder CSV
```

For Click programs that want to be explicit, `pyrung.click` also provides `nop()`:

```python
from pyrung.click import nop

comment("Section header")
with Rung():
    nop()  # one per rung, must be the sole instruction
```

Both forms produce identical CSV output.

## Loading PLC state

Use Click Programming Software's **Data > Read Data from PLC** to dump the live state of a Click PLC to CSV, then load that snapshot into a pyrung runner so it starts right where the PLC was.

```python
from pyclickplc import read_plc_data
from pyrung.core import PLC, SystemState

data = read_plc_data("data.csv", skip_default=True)
tags = mapping.tags_from_plc_data(data)
runner = PLC(logic, initial_state=SystemState().with_tags(tags))
```

`read_plc_data` (from `pyclickplc`) parses the CSV and returns `{hardware_address: value}`. `tags_from_plc_data` translates the hardware keys to logical tag names using the TagMap, silently skipping any addresses that aren't mapped. The result is ready for `SystemState.with_tags()` or `runner.patch()`.

`skip_default=True` omits zero/false/empty values — useful since PLC dumps are exhaustive and most addresses are at their default.

You can also inject a PLC snapshot mid-run:

```python
data = read_plc_data("data.csv", skip_default=True)
runner.patch(mapping.tags_from_plc_data(data))
runner.step()  # applied at the start of this scan
```

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
