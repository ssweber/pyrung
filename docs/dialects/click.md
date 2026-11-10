# CLICK PLC Dialect

`pyrung.click` adds CLICK-PLC-specific blocks, type aliases, address mapping, nickname file I/O, validation, and a soft-PLC adapter on top of the hardware-agnostic core.

## Installation

```bash
pip install pyrung
```

`pyrung.click` uses `pyclickplc` for address metadata, nickname CSV I/O, and soft-PLC server/client integration.

## Imports

```python
from pyrung import Bool, Int, PLC, Program, rung, copy, latch, reset, rise
from pyrung.click import ClickBlocks, TagMap

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()
```

## Workflow: write first, validate later

pyrung is intentionally permissive. Write logic with semantic tag names and native Python expressions, and simulate freely. No address mapping is required. Hardware constraints are opt-in.

The natural progression:

1. **Write** — define semantic tags (`StartButton`, `MotorRunning`, `Speed`) and express logic in Python
2. **Simulate** — run tests with a fixed `dt` that set inputs and assert outputs
3. **Map** — create a `TagMap` linking semantic tags to CLICK hardware addresses
4. **Validate** — `mapping.validate(logic, mode="warn")` surfaces CLICK-incompatible patterns
5. **Iterate** — fix findings, tighten to `mode="strict"` when the program is clean

The validator tells you what CLICK can't do before you discover it at deploy time. It reports inline expressions, unsupported pointer modes and type mismatches.

## Pre-built blocks

`pyrung.click` exports pre-built blocks for every CLICK memory bank:

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

Addresses use canonical CLICK display names:

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

Each call to `ClickBlocks()` returns fresh blocks with no shared state, so multi-project workflows don't need manual cleanup.

Retentive registers ignore their CSV initial value, matching hardware. The CLICK PLC applies the initial value at project download only. A retentive register keeps its last value afterward. pyrung therefore models retentive slots with their type's zero value, and codegen omits `default=` for them.

## Type aliases

CLICK-style constructor aliases as alternatives to IEC names:

| CLICK alias | IEC equivalent |
|-------------|----------------|
| `Bit` | `Bool` |
| `Int2` | `Dint` |
| `Float` | `Real` |
| `Hex` | `Word` |
| `Txt` | `Char` |

## DSL naming philosophy

This DSL follows CLICK PLC instruction naming as closely as possible, departing only when a Python conflict exists **and** the replacement name is genuinely better in a Python-hosted context.

1. **Keep the CLICK name** when it's a clear action verb with no conflict: `out`, `reset`, `fill`, `copy`, `blockcopy`.
2. **Use a domain synonym** when CLICK's name shadows a Python builtin. `set` becomes `latch`, which is well-understood PLC terminology. `math` keeps CLICK's name, although pyrung's `math` hides Python's `math` module in a file that imports it. Import Python's under another name (`import math as pymath`).
3. **Use clarified intent** when Python's execution model changes the semantics. `return` becomes `return_early`. In CLICK, every subroutine needs an explicit `RET`. In this DSL, normal subroutine completion is implicit, so the only use is early exit. The name says so.

| CLICK instruction | pyrung DSL | Reason |
|-------------------|------------|--------|
| `SET` | `latch` | Shadows Python builtin `set` |
| `RET` | `return_early` | Normal return is implicit; only early exit needs a call |
| `BREAK` | `break_loop` | `break` is a Python keyword |
| `CONTINUE` | `continue_loop` | `continue` is a Python keyword |

Math formula functions use the Python name where one exists: `LOG` → `log10`, `LN` → `log`, `ABS` → `abs`, `AVG` → `rng.mean()`, `MDN` → `rng.median()`. See [Range aggregates on CLICK](../instructions/math.md#range-aggregates-on-click).

The CSV ladder export uses CLICK-facing token names: `return_early` as `return()`, `forloop` as `for(...)` with its body rungs and `next()`, and `break_loop` / `continue_loop` as `break()` / `continue()`. See the [laddercodec CSV format guide](https://pyrung.com/laddercodec/guides/csv-format/) for the full token grammar.

## Writing a CLICK program

```python
from pyrung import Bool, Real, PLC, Program, rung, copy, latch, reset, rise
from pyrung.click import ClickBlocks, TagMap

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

# Define semantic tags (hardware-agnostic)
StartButton  = Bool()
StopButton   = Bool()
MotorRunning = Bool()
RawSpeed     = Real()
Speed        = Real()

# Write logic using semantic names
with Program() as logic:
    with rung(rise(StartButton)):
        latch(MotorRunning)

    with rung(rise(StopButton)):
        reset(MotorRunning)

    with rung(MotorRunning):
        copy(RawSpeed, Speed)

# Simulate — no mapping needed
with PLC(logic, dt=0.1) as plc:
    StartButton.value = True
    plc.step()
```

## TagMap — mapping to hardware

`TagMap` links semantic tags and blocks to CLICK hardware addresses. Mapping is separate from logic. Write and simulate first, then map.

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

`Timer` and `Counter` are built-in UDTs. Map them to CLICK hardware banks explicitly, just like any other block:

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
# Without nickname → clone named after the operand, fields named after their addresses
T1 = Timer.clone("T1", nicknames={'Done': 'T1', 'Acc': 'TD1'})
on_delay(T1, preset=100)

# With nickname {"T1": "OvenTimer"} → clone named after the nickname
OvenTimer = Timer.clone("OvenTimer", nicknames={'Done': 'OvenTimer', 'Acc': 'TD1'})
on_delay(OvenTimer, preset=100)
```

The clone is named after the T (done-bit) nickname, or after the TD/CTD nickname if the T has none.

If the nickname already ends with `_Done` or `_Acc`, the suffix is stripped automatically. `"OvenTimer_Done"` becomes `Timer.clone("OvenTimer")`.

The clone takes the retentive setting of its addresses. The built-in `Timer` and `Counter` already match CLICK's bank defaults (T and TD are non-retentive, CT and CTD are retentive). Only an address whose retentive setting differs from its bank default gets an override, such as `Timer.clone("T1", ..., retentive={'Acc': True})` for a TD set retentive.

Condition references resolve through the clone. A rung conditioned on T1 renders as `OvenTimer.Done`:

```python
with rung(OvenTimer.Done):
    out(AlarmLight)
```

### Input inference

When a tag is mapped to an input bank (`x` or `xd`), `TagMap` automatically marks it `external=True`. This tells the verifier that the tag's value comes from outside the ladder. `always()` and `pyrung lock` then treat it as a nondeterministic input, so you don't need to declare `external=True` yourself.

```python
StartButton = Bool()                       # external=False initially
mapping = TagMap({StartButton: x[1]})
assert StartButton.external                # now True — stamped by TagMap
```

Tags mapped to output or memory banks (`y`, `c`, `ds`, etc.) are not stamped `external`. Tags that are `readonly` are skipped, because `readonly` and `external` are mutually exclusive.

### Output inference

When a tag is mapped to an output bank (`y` or `yd`), `TagMap` automatically marks it `lock=True`. This adds the tag to what `pyrung lock` tracks by default, so the lock file tracks physical outputs. You don't need to declare `lock=True` yourself.

```python
MotorOut = Bool()                              # lock=False initially
mapping = TagMap({MotorOut: y[1]})
assert MotorOut.lock                           # now True — stamped by TagMap
```

Tags mapped to input or memory banks (`x`, `c`, `ds`, etc.) are not affected.

### Type validation at map time

`TagMap` validates that logical and hardware data types match:

```python
TagMap({Speed: c[1]})   # raises: INT cannot map to C (BIT)
```

### From nickname file

Load an existing CLICK nickname CSV:

```python
mapping = TagMap.from_nickname_file("project.csv")
```

The importer reconstructs blocks, structures, and standalone tags from CSV comment markers and nickname patterns. Standalone nicknames become individual `Tag` objects. Non-marker address-comment text is preserved on standalone tags and block slots for CSV round-trip export.

For strict grouping validation, pass `mode="strict"`. A structure grouping mismatch then fails immediately (it would otherwise fall back to plain blocks with a warning).

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

Bare tags such as `<Alarms>`, `</Alarms>`, `<Base.field>` and `<Base.field />` are grouping-only comments. They do not reconstruct pyrung semantics. They only group rows visually. Any inner nicknames import as ordinary standalone tags.

Bracket metadata can appear alongside markers or ordinary comments. It preserves tag flags, choices, range bounds, units, and physical annotations through nickname CSV import/export. For int-backed boolean values in register memory, use the built-in shorthand `[choices=Bool]` instead of spelling out `[choices=False:0|True:1]`.

**Plain blocks** use explicit `:block` markers: `<Alarms:block>` / `</Alarms:block>`. If the logical start differs from the inferred default, export/import uses `<Alarms:block(n)>` or `<Alarms:block(start=n)>`.

**Named arrays** use `:named_array` markers. Count and stride are optional. The importer infers them from the row span between the open and close tags:

```
<Task:named_array>            count=1, stride from row count
<Task:named_array(2)>         count=2, stride = rows / count
<Task:named_array(2,3)>       count=2, stride=3 (fully explicit)
```

When both count and stride are given, the row span must equal `count × stride`. When stride is omitted, the row count must be divisible by count.

**Nickname patterns.** For `count > 1`, nicknames must follow `{Base}{instance}_{field}` with 1-based instance numbers. The instance is derived from position: `position // stride + 1`. The field name is what follows the `{Base}{instance}_` prefix (`Channel1_Id` → field `Id`).

For `count = 1`, nicknames default to the compact form `{Base}_{field}` (no instance number). If the CSV already uses numbered names like `Task1_Call`, the importer detects this and sets `always_number=True` automatically. To force numbered names explicitly, add `,always_number` to the marker:

```
<Task:named_array(1,2,always_number)>
</Task:named_array(1,2,always_number)>
```

The `always_number` flag only matters for singletons. A named array with `count > 1` is always numbered.

**Instance rules.** Instance 1 defines the field template, so all its fields must be explicitly named. Instance 2+ fields must match instance 1's pattern (correct field name and instance number). Unnamed slots in instance 2+ are fine (silently skipped). A field name in instance 2+ that wasn't defined in instance 1 is an error.

Example: `Channel` with 2 instances, 3 fields, no gaps (`stride=3`):

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

CLICK codegen can round-trip aligned whole-instance spans back into pyrung as `Name.instance(...)` or `Name.instance_select(...)` instead of raw bank ranges. This works for both dense and sparse layouts:

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

Nesting is not supported. A UDT field cannot itself be a named array (for example, `Sts.Recipes:named_array(20,50)` won't parse). Flatten the name instead, as `StsRecipes:named_array(20,50)`.

**Conflict rules.** The same base name cannot be used across different marker kinds. These combinations are all errors:

- Same name as both `:named_array` and `.attr:udt`
- Same name as both `:block` and `:named_array`
- Same name as both `:block` and `.attr:udt`
- Duplicate `:named_array` or `:block` markers for the same name

### To nickname file

Export to CLICK nickname CSV for import into CLICK Programming Software:

```python
mapping.to_nickname_file("project.csv")
```

Mapped tags and blocks emit rows with canonical logical names, initial values, retentive flags, and preserved address comments. Unmapped tags are omitted. CLICK nicknames hold at most 24 characters. Validation reports a longer one as `CLK_NICKNAME_TOO_LONG`, and ladder export refuses it.

## Validation

After mapping, validate your program against CLICK hardware restrictions:

```python
report = mapping.validate(logic, mode="warn")
print(report.summary())

for finding in report.findings:
    print(f"  {finding.level}: {finding.message}")
```

Common findings:

| Issue | pyrung allows | CLICK requires |
|-------|--------------|----------------|
| Pointer in `copy` source | Any block, arithmetic | DS only, no arithmetic |
| Inline expression in condition | `(A + B) > 100` | Must use `math()` first |
| `out` / `latch` / `reset` target | Any tag (`reset(DS1)`, `reset(TXT1)`) | Bit memory only: X, Y, C, T, CT, SC (`CLK_BANK_WRONG_ROLE`); clear a register with `copy()` or `fill()` |
| Whole-number constant in `math()` | Any, cut to 16 bits as on a CLICK | −32768..32767. Stage a wider one in a DINT tag (`CLK_MATH_WIDE_CONSTANT`) |
| Text compare on a `Char` (`Name == "AB"`) | Compares the Chars that follow it in its block | Those Chars mapped to consecutive TXT addresses in order (`CLK_TEXT_WINDOW_NOT_CONTIGUOUS`) |
| Text `copy()` that runs past the end of its bank | Drops what runs past | Start it low enough to keep every character (`CLK_TEXT_PAST_BANK`) |
| Text `copy()` that runs past the end of its block | Drops what runs past | A CLICK writes on into the bank. Map the block onto more of the bank (`CLK_TEXT_PAST_BLOCK`) |

### Timer preset limits

CLICK timer accumulators are 16-bit signed INT (max 32,767). A literal preset exceeding this range silently clamps at runtime. The validator reports `CLK_TIMER_PRESET_OVERFLOW` for out-of-range presets. Use a larger time unit instead.

| Unit | Max preset | Max duration |
|------|-----------|--------------|
| `Tms` | 32,767 | 32.7 seconds |
| `Ts` | 32,767 | 9.1 hours |
| `Tm` | 32,767 | 22.7 days |
| `Th` | 32,767 | 3.7 years |
| `Td` | 32,767 | 89 years |

```python
# Wrong — clamps silently to 32.7 seconds
on_delay(MyTimer, preset=60000)

# Right — use seconds
on_delay(MyTimer, preset=60, unit="sec")
```

### Out coil variants

`toggle`, `debounce`, `flasher` and `timed` are CLICK v4's Out coil types. Each phase timer must map to `T<n>` with `TD<n>` of the same number, and a register preset to `DS`. Validation reports `CLK_TIMER_PAIR_MISMATCH` for any timer or counter whose Done and Acc map to different numbers. This includes `on_delay` and the counters. CLICK's Out dialog offers fewer units than its timers. `debounce` and `flasher` take `ms` or `sec`, and `timed` takes `ms`, `sec` or `min`. Any other unit is `CLK_OUT_UNIT_UNSUPPORTED`. Only `toggle` takes `immediate(...)`.

### For/Next

A `forloop` exports as its For rung, one rung per body item, and `next()`. A bare instruction is a wire-only rung. A `with rung(...)` inside the loop keeps its contacts. A nested loop is a For rung of its own. `break_loop()` and `continue_loop()` export as `break()` and `continue()`.

CLICK allows:

- three levels of loops, counting the loops of a subroutine called inside a loop (`CLK_FORLOOP_NESTING_DEPTH`)
- a one-shot only on the outermost For (`CLK_FORLOOP_INNER_ONESHOT`)
- no `return_early()` between For and Next (`CLK_FORLOOP_RETURN_INSIDE`)
- no nested subroutine calls, so a `call()` inside a subroutine is flagged (`CLK_NESTED_CALL`)

`loop.idx` has no CLICK address, so count iterations with a `DS` pointer instead. A false For rung skips its body. A `DS` count is read as unsigned 16-bit, so -1 runs 65535 iterations. A `DD` count is signed, so -1 runs none. A count of 0 runs none.

Findings are hints by default (`mode="warn"`). Use `mode="strict"` to treat hints as errors.

### Program steps

A CLICK project holds at most 8,000 program steps. `estimate_steps(pyrung_to_ladder(logic, mapping))` estimates a program's count, and validation reports `CLK_STEP_LIMIT` as a program nears or passes the limit. Export still writes such a program, with the finding as a `UserWarning`.

### Pointers past a bank

```python
ptr = Int("Ptr", max=1010)
copy(dd[ptr], out)  # CLK_PTR_PAST_BANK: DD ends at DD1000
```

Validation warns with `CLK_PTR_PAST_BANK` when a pointer can address memory past the end of its bank. The values a pointer can take are its `choices`, its `min` and `max` (a `max` alone counts as an upper bound), and every value a `copy()` or `math()` in the program can store in it. A `For` loop index with a constant count takes 0 to count - 1. A pointer built from a value the program cannot bound is not reported.

A CLICK `copy()` through a `DD` pointer past `DD1000` writes into the memory there and sets no flag. pyrung drops that write and reads 0 there. Keep the pointer inside the bank, or give the tag `min`/`max` or `choices`.

## Ladder CSV export

`pyrung_to_ladder(program, tag_map)` emits deterministic CLICK ladder CSV row matrices via `LadderBundle`.

```python
from pyrung.click import pyrung_to_ladder

bundle = pyrung_to_ladder(logic, mapping)
bundle.main_rows          # inspect rows in-memory
bundle.write("./output")  # write main.csv + subroutines/*.csv to disk
```

For the consumer-facing CSV decode contract (files, row semantics, token formats, branch wiring, and supported tokens), see the [laddercodec CSV format guide](https://pyrung.com/laddercodec/guides/csv-format/).

To convert ladder CSV back into pyrung Python source, see [CLICK Python Codegen](click-codegen.md).

A blank TXT holds NUL. A CLICK text constant writes one TXT per character, so `""` writes nothing. CLICK ends a quoted constant at NUL. A blank text constant (`""` or `"\x00"`, in `copy`, `fill`, a compare or a `search`) therefore exports as `$00`, CLICK's ASCII-code constant for NUL. `$00` imports back as the blank.

### Empty and comment-only rungs

Empty rungs survive the round-trip. A `with rung(): pass` in pyrung exports as `NOP` in the CLICK CSV AF column and imports back as `pass`.

```python
comment("--- Motor Control Section ---")
with rung():
    pass  # becomes NOP in CLICK ladder CSV
```

For CLICK programs that want to be explicit, `pyrung.click` also provides `nop()`:

```python
from pyrung.click import nop

comment("Section header")
with rung():
    nop()  # one per rung, must be the sole instruction
```

Both forms produce identical CSV output.

A NOP has to sit alone on a bare wire. The exporter therefore refuses two shapes that CLICK will not hold. One is an empty rung with conditions (`with Rung(C1): pass`). The other is an empty rung next to a `.continued()` rung.

### Boxes with pins

A box with pins (a counter, a retentive timer's `.reset()`, a shift register, a drum) ends its rung. CLICK takes all of a box's inputs (its pins) from one branch. If the box's input wire also feeds another output, CLICK draws the rung but won't compile it (error C000A). pyrung refuses that rung when the program is built.

Put the outputs that come before the box in a rung above it. Put the outputs that follow it in a `.continued()` rung, which is another wire of the same CLICK rung:

```python
with Rung(Start):
    latch(Running)
with Rung(Start).continued():
    count_up(Parts, preset=10).reset(Clear)
```

A box with no pins, such as a TON, can share the wire.

On import, each wire of a CLICK rung comes back as a rung of its own, with `.continued()` on every wire after the first. Outputs share a `with rung(...)` or `with branch(...)` only where the CLICK rows branch them off one contact. So the generated code reads the way the CLICK rung does. Two contacts with the same label stay two contacts.

## Loading PLC state

Use CLICK Programming Software's **PLC > Read Data from PLC** to dump the live state of a CLICK PLC to CSV, then load that snapshot into a pyrung runner so it starts right where the PLC was.

```python
from pyclickplc import read_plc_data
from pyrung.core import PLC, SystemState

data = read_plc_data("data.csv", skip_default=True)
tags = mapping.tags_from_plc_data(data)
runner = PLC(logic, initial_state=SystemState().with_tags(tags))
```

`read_plc_data` (from `pyclickplc`) parses the CSV and returns `{hardware_address: value}`. `tags_from_plc_data` translates the hardware keys to logical tag names using the TagMap, silently skipping any addresses that aren't mapped. The result is ready for `SystemState.with_tags()` or `runner.patch()`.

`skip_default=True` omits zero, false and empty values. A PLC dump lists every address, and most sit at their default, so this keeps the result small.

Some programs use CLICK bank slots directly, such as `x.slot(1, name="Button")` or a bare `ds[5]`. Generated projects do this for plain nicknames. These addresses have no TagMap entry. Pass the bank set as `blocks` to seed them too:

```python
tags = mapping.tags_from_plc_data(data, blocks=blocks)
```

You can also inject a PLC snapshot mid-run:

```python
data = read_plc_data("data.csv", skip_default=True)
runner.patch(mapping.tags_from_plc_data(data))
runner.step()  # applied at the start of this scan
```

## ClickDataProvider — soft PLC

`ClickDataProvider` implements the `pyclickplc` `DataProvider` protocol, bridging pyrung's `SystemState` to a Modbus TCP server. This lets pyrung act as a soft PLC accessible from CLICK Programming Software or any Modbus client.

```python
from pyrung.click import ClickDataProvider
from pyclickplc.server import ClickServer

provider = ClickDataProvider(runner, tag_map=mapping)
server = ClickServer(provider, port=502)
```

Reads return the current committed state. Writes queue a `runner.patch()` for the next scan.

If another device on the LAN can't reach the soft PLC, check the Windows Firewall. It must allow both the TCP port (default 502) and the Python interpreter.

### Word-image (XD / YD) addressing

- `XD*` reads compute bit-image words from current X bit state.
- `YD*` reads compute bit-image words from current Y bit state.
- `YD*` writes fan out to the corresponding Y bits.
- `XD*` writes are rejected (read-only).

## Communication instructions

`send` and `receive` implement Modbus communication with remote devices. Two addressing modes are supported:

### CLICK addresses (CLICK-to-CLICK)

Use a CLICK address string for `remote_start` when talking to another CLICK PLC:

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

CLICK handles word swap and character order natively. Nothing needs configuring on the pyrung side.

### Raw Modbus addresses (any device)

Use `ModbusAddress` for `remote_start` when talking to non-CLICK Modbus devices (VFDs, meters, sensors, etc.):

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

`word_swap` controls how 32-bit values (DINT, REAL) are packed across register pairs. `False` (the default) puts the high word first. `True` puts the low word first. This matters only for 32-bit CLICK types (DD, DF, etc.).

`RegisterType` selects the Modbus function code: `HOLDING` (FC 3/6/16, default), `INPUT` (FC 4, read-only), `COIL` (FC 1/5/15), `DISCRETE_INPUT` (FC 2, read-only). Sending to `INPUT` or `DISCRETE_INPUT` raises `ValueError`.

### Target types

| Type | Transport | Live I/O | Codegen |
|------|-----------|----------|---------|
| `ModbusTcpTarget` | Ethernet | Yes (pymodbus for raw, pyclickplc for CLICK addresses) | Yes |
| `ModbusRtuTarget` | Serial | Yes (pymodbus) | Not yet |
| `str` (name only) | — | No (inert) | Yes (resolved via `ModbusClientConfig`) |

When `target` is a `ModbusTcpTarget` or `ModbusRtuTarget`, communication runs asynchronously in a background worker pool. The scan loop stays synchronous. When `target` is a plain string, the instruction is inert during simulation and exists only for CircuitPython code generation.
