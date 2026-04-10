# Click PLC Cheatsheet

Quick reference for writing pyrung programs targeting AutomationDirect Click PLCs.

## Imports

```python
from pyrung import (
    Bool, Int, Dint, Real, Word, Char,       # tag types
    Timer, Counter,                          # built-in UDTs
    named_array, Field, auto,                # structures
    PLC, Program, Rung,                      # PLC skeleton
    comment, branch,                         # rung structure
    And, Or, rise, fall, system,             # conditions
    out, latch, reset,                       # coils
    calc,                                    # math
    copy, blockcopy, fill,                   # data movement
    to_value, to_ascii, to_text, to_binary,  # copy converters
    pack_bits, pack_words, pack_text,        # pack
    unpack_to_bits, unpack_to_words,         # unpack
    on_delay, off_delay,                     # timers
    count_up, count_down,                    # counters
    shift, event_drum, time_drum, search,    # drum/shift/search
    call, subroutine, forloop, return_early, # program control
    send, receive, ModbusTcpTarget,          # communication
)
from pyrung.click import x, y, c, ds, dd, dh, df, t, td, ct, ctd, sc, sd, txt, xd, yd, TagMap
```

## Memory banks

| Bank | pyrung | Type | Range | Notes |
|------|--------|------|-------|-------|
| X | `x` | BOOL | 1-816 | **Sparse** — see below |
| Y | `y` | BOOL | 1-816 | **Sparse** — see below |
| C | `c` | BOOL | 1-2000 | Bit memory |
| DS | `ds` | INT | 1-4500 | 16-bit signed |
| DD | `dd` | DINT | 1-1000 | 32-bit signed |
| DH | `dh` | WORD | 1-500 | 16-bit unsigned |
| DF | `df` | REAL | 1-500 | 32-bit float |
| T | `t` | BOOL | 1-500 | Timer done bits |
| TD | `td` | INT | 1-500 | Timer accumulators |
| CT | `ct` | BOOL | 1-250 | Counter done bits |
| CTD | `ctd` | DINT | 1-250 | Counter accumulators |
| SC | `sc` | BOOL | 1-1000 | System control bits |
| SD | `sd` | INT | 1-1000 | System data words |
| TXT | `txt` | CHAR | 1-1000 | Text memory |
| XD | `xd` | WORD | 0-8 | Input word images |
| YD | `yd` | WORD | 0-8 | Output word images |

### Sparse X/Y banks

X and Y use module-based addressing. Valid address ranges:

```
1-16, 21-36, 101-116, 201-216, 301-316,
401-416, 501-516, 601-616, 701-716, 801-816
```

`.select()` automatically filters to valid addresses: `x.select(1, 21)` yields X001..X016 and X021.

## System points

Access via `system.<namespace>.<point>`. Import: `from pyrung import system`.

### Clocks (`system.sys`)

| Point | Click addr | Behavior |
|-------|-----------|----------|
| `system.sys.clock_10ms` | SC4 | Toggles every 10ms |
| `system.sys.clock_100ms` | SC5 | Toggles every 100ms |
| `system.sys.clock_500ms` | SC6 | Toggles every 500ms |
| `system.sys.clock_1s` | SC7 | Toggles every 1s |
| `system.sys.clock_1m` | SC8 | Toggles every 1min |
| `system.sys.clock_1h` | SC9 | Toggles every 1hr |

### Other sys points

| Point | Click addr | Notes |
|-------|-----------|-------|
| `system.sys.always_on` | SC1 | Constant True |
| `system.sys.first_scan` | SC2 | True on scan 0 only |
| `system.sys.scan_clock_toggle` | SC3 | Alternates each scan |
| `system.sys.mode_run` | SC11 | True in RUN mode |
| `system.sys.scan_counter` | SD9 | Current scan count |
| `system.sys.scan_time_current_ms` | SD10 | Current scan time |

### Fault flags (`system.fault`)

| Point | Click addr |
|-------|-----------|
| `system.fault.plc_error` | SC19 |
| `system.fault.division_error` | SC40 |
| `system.fault.out_of_range` | SC43 |
| `system.fault.address_error` | SC44 |
| `system.fault.math_operation_error` | SC46 |
| `system.fault.code` | SD1 |

### Real-time clock (`system.rtc`)

`year4`, `year2`, `month`, `day`, `weekday`, `hour`, `minute`, `second` (read-only, SD19-SD26).

Set via `new_year4`, `new_month`, `new_day`, `new_hour`, `new_minute`, `new_second` + `apply_date` / `apply_time`.

## Conditions

```python
with Rung(Tag):                           # normally open (truthy)
with Rung(~Tag):                          # normally closed (falsy)
with Rung(rise(Tag)):                     # rising edge (one scan)
with Rung(fall(Tag)):                     # falling edge (one scan)
with Rung(Temp > 100):                    # comparison (==  !=  <  <=  >  >=)
with Rung(A, B, C):                       # AND (comma = all must be True)
with Rung(And(A, B, C)):                  # AND (explicit)
with Rung(Or(A, B)):                      # OR
with Rung(Or(Start, And(Auto, Ready))):   # nested AND/OR
```

Click requires explicit comparisons for INT tags — use `Step != 0` instead of bare `Step`.

## Coils

```python
out(Light)                    # follows rung: True when rung True, False when False
out(Light, oneshot=True)      # True for one scan on rising edge
latch(Motor)                  # set and hold until reset
reset(Motor)                  # clear latch
out(ValveB.immediate)         # immediate I/O (bypasses image table)
```

## Math

```python
calc(A + B, Result)           # Result = A + B (wraps on overflow)
calc(A * 2, R, oneshot=True)  # one-shot: execute once per rising edge
calc(DH[1] | DH[2], DH[3])   # WORD-only → hex mode (unsigned)
calc(DS.select(1, 10).sum(), Total)  # sum a range
```

- `calc()` wraps on overflow (modular arithmetic). `copy()` clamps.
- Division by zero → result = 0, fault flag set.
- Integer division truncates toward zero.
- Don't mix WORD and non-WORD tags in one `calc()`.

## Data movement

```python
copy(Source, Dest)                             # single value (clamps)
copy(42, DS[1])                                # literal
copy(DS[1], DS[DS[0]])                         # indirect addressing
blockcopy(DS.select(1, 10), DS.select(11, 20)) # range copy
fill(0, DS.select(1, 100))                     # fill range with constant
```

### Copy converters

```python
copy(CharTag, DS[1], convert=to_value)     # CHAR '5' → 5
copy(CharTag, DS[1], convert=to_ascii)     # CHAR '5' → 53
copy(DS[1], Txt[1], convert=to_text())     # 123 → "123"
copy(DS[1], Txt[1], convert=to_binary)     # raw byte
```

### Pack / unpack

```python
pack_bits(C.select(1, 16), DS[1])          # 16 bools → INT
unpack_to_bits(DS[1], C.select(1, 16))     # INT → 16 bools
pack_words(DS.select(1, 2), DD[1])         # 2 INTs → DINT
unpack_to_words(DD[1], DS.select(1, 2))    # DINT → 2 INTs
```

## Timers

Built-in `Timer` type: `.Done` (Bool) + `.Acc` (Int). Units: `"Tms"`, `"Ts"`, `"Tm"`, `"Th"`, `"Td"`.

```python
MyTimer = Timer.clone("MyTimer")

# TON — auto-reset when rung goes False
on_delay(MyTimer, preset=500, unit="Tms")

# RTON — retentive, needs manual reset
on_delay(MyTimer, preset=500).reset(ResetBtn)

# TOF — off-delay
off_delay(CoolDown, preset=500, unit="Tms")
```

## Counters

Built-in `Counter` type: `.Done` (Bool) + `.Acc` (Dint). Counts every scan while True — use `rise()` for edge-triggered.

```python
PartCounter = Counter.clone("PartCounter")

count_up(PartCounter, preset=100).reset(ResetBtn)
count_down(Dispense, preset=100).reset(ResetBtn)

# Edge-triggered counting
with Rung(rise(Sensor)):
    count_up(PartCounter, preset=9999).reset(CountReset)

# Bidirectional
count_up(ZoneCounter, preset=100).down(DownCondition).reset(ResetBtn)
```

## Program structure

```python
with Program() as logic:
    comment("Section header")
    with Rung(A):
        out(X)
        with branch(B):         # branch ANDs with parent rung
            out(Y)

# Subroutines
with Program() as logic:
    with subroutine("init"):
        with Rung():
            out(InitLight)
    with Rung(AutoMode):
        call("init")

# For loops
with Rung():
    with forloop(5) as loop:
        copy(Src[loop.idx + 1], Dst[loop.idx + 1])
```

## Shift register

```python
with Rung(DataBit):
    shift(C.select(1, 8)).clock(ClockBit).reset(ResetBit)
```

## Search

```python
search(DS.select(1, 100) >= 100, result=FoundAddr, found=FoundFlag)
search(DS.select(1, 100) >= 100, result=Addr, found=Flag, continuous=True)
search(Txt.select(1, 50) == "AB", result=Addr, found=Flag)  # text search
```

## Communication

```python
peer = ModbusTcpTarget("peer", "192.168.1.10")

with Rung(Enable):
    send(target=peer, remote_start="DS1", source=DS.select(1, 10),
         sending=Sending, success=SendOK, error=SendErr, exception_response=ExCode)

with Rung(Enable):
    receive(target=peer, remote_start="DS1", dest=DS.select(11, 20),
            receiving=Receiving, success=RecvOK, error=RecvErr, exception_response=ExCode)
```

## Named arrays

Single-type structures for grouping related registers. The most common way to organize Click data.

```python
from pyrung import named_array, Int, Real

@named_array(Int, count=4)
class Sensor:
    Raw = 0
    Scaled = 0
    Setpoint = 100

Sensor[1].Raw             # first sensor's raw reading
Sensor[3].Setpoint        # third sensor's setpoint
Sensor.select(1, 3)       # fields 1-3 as a BlockRange
Sensor.instance(2)        # all fields for instance 2
Sensor.instance_select(1, 2)  # all fields for instances 1-2
```

Use `.instance()` and `.instance_select()` with range instructions:

```python
blockcopy(Sensor.instance(2), ds.select(201, 203))
fill(0, Sensor.instance_select(1, 4))
```

### Mapping named arrays to hardware

```python
Sensor.map_to(ds.select(101, 112))   # 4 instances * 3 fields = 12 slots
# Sensor[1].raw → DS101, Sensor[1].scaled → DS102, Sensor[1].setpoint → DS103
# Sensor[2].raw → DS104, ...
```

For UDTs (mixed-type structures) and advanced options like stride, cloning, and `auto()` defaults, see [Tag Structures](tag-structures.md).

## Common patterns

### EMA filter (exponential moving average)

Formula:

```python
Avg = Avg + (Raw - Avg) * (FilterFactor / 10)
```

In pyrung:

```python
with Rung(rise(system.sys.clock_500ms)):
    calc(Avg + (Raw - Avg) * (FilterFactor / 10), Avg)
```

`FilterFactor` range: `1-9`

- Low (`1-3`): heavy smoothing, slow response, best for noisy signals
- Mid (`4-6`): balanced smoothing and response
- High (`7-9`): light smoothing, fast response, best for clean signals

Adjust `FilterFactor` based on:

- Sensor noise level
- Required response time
- Process stability needs

### Oneshot on first scan

```python
with Rung(system.sys.first_scan):
    copy(DefaultValue, Parameter)
```

### Timed periodic action

```python
with Rung(rise(system.sys.clock_1s)):
    calc(Counter + 1, Counter)
```

### Timer-driven state machine

Use `on_delay` per state, `copy` to advance on done:

```python
State = Char("State")
GreenTimer  = Timer.clone("GreenTimer")
YellowTimer = Timer.clone("YellowTimer")
RedTimer    = Timer.clone("RedTimer")

with Rung(State == "g"):
    on_delay(GreenTimer, preset=3000, unit="Tms")
with Rung(GreenTimer.Done):
    copy("y", State)

with Rung(State == "y"):
    on_delay(YellowTimer, preset=1000, unit="Tms")
with Rung(YellowTimer.Done):
    copy("r", State)

with Rung(State == "r"):
    on_delay(RedTimer, preset=3000, unit="Tms")
with Rung(RedTimer.Done):
    copy("g", State)
```

### Shift log (history window)

Shift a range up, then write newest into slot 1:

```python
DS = Block("DS", TagType.INT, 1, 5)

with Rung(rise(LogEnable)):
    blockcopy(DS.select(1, 4), DS.select(2, 5))  # shift up
    copy(NewValue, DS[1])                          # newest into slot 1
```

### Task sequencer

For step-based task sequencing with Call/Active/Step/Advance patterns, see [simple_task_example.py](https://github.com/ssweber/pyrung/blob/main/examples/simple_task_example.py) and [task_example.py](https://github.com/ssweber/pyrung/blob/main/examples/task_example.py).

## TagMap

```python
mapping = TagMap({
    StartButton:  x[1],           # single tag → single address
    MotorRunning: y[1],
    Speed:        df[1],
    Alarms:       c.select(101, 200),  # block → hardware range
})

# Validate against Click hardware restrictions
report = mapping.validate(logic, mode="warn")
```

Built-in `Timer` and `Counter` UDTs are automatically mapped — `Timer[n].Done` → T*n*, `Timer[n].Acc` → TD*n*, etc. No explicit entries needed.

Named arrays use `.map_to()` instead of TagMap — see [Named arrays](#named-arrays) above.
