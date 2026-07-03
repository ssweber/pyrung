# Physical Annotations and Autoharness

Once you have a working program with UDTs, you can annotate the physical behavior of feedback signals. On a feedback field, `physical=` describes the feedback response and `link=` names the command/enable field that drives it. The autoharness reads those annotations and synthesizes feedback patches in tests automatically, so you stop writing boilerplate that toggles inputs by hand.

## The problem

A typical device-heavy test file is 80% feedback toggling:

```python
def test_gripper_cycle():
    with PLC(logic, dt=0.001) as plc:
        Cmd.value = True
        plc.step()                   # En rises
        plc.run_for(0.005)
        Gripper[1].Fb_Contact.value = True   # manual toggle
        plc.run_for(0.075)
        Gripper[1].Fb_Vacuum.value = True    # manual toggle
        plc.run_for(0.050)
        assert Gripper[1].Sts.value is True
```

Twenty devices, twenty feedback loops, twenty blocks of this — maintained by hand, diverging from reality over time, and wrong in subtle ways when someone changes a delay in the test but not the device, or vice versa.

## Declaring physical behavior

`Physical` describes how a feedback signal responds in the real world. There are two kinds:

**Bool feedback** — a signal that asserts or deasserts after a delay (limit switches, proximity sensors, pressure switches):

```python
from pyrung import Physical

LIMIT_SWITCH = Physical("LimitSwitch", on_delay="5ms", off_delay="5ms")
VACUUM_SENSOR = Physical("VacuumSensor", on_delay="80ms", off_delay="50ms")
```

**Profile-driven feedback** — a signal driven by a declarative response spec (thermocouples, pressure transmitters, flow meters, shaft encoders):

```python
from pyrung import Ramp, Approach, Pulse

THERMOCOUPLE = Physical("Thermocouple", profile=Ramp(up=0.5, down=-0.05))
PRESSURE     = Physical("Pressure",     profile=Approach(toward=100.0, rate=0.3))
ENCODER      = Physical("Encoder",      profile=Pulse(on_dwell="8ms", off_dwell="8ms"))
```

A `Physical` has either timing (on_delay/off_delay) or a `profile=` spec, never both. Bool fields accept either form — use timing for simple delayed transitions (contactors, limit switches) and a `Pulse` profile for signals that oscillate like pulse trains; `Ramp`/`Approach` drive analog registers. Delays and dwells accept duration strings: `"5ms"`, `"2s"`, `"1s500ms"`.

## Linking feedback to commands

The `link=` field on a `Field` declaration says "this feedback responds to that command." The `physical=` field says how:

```python
from pyrung import udt, Bool, Real, Field

@udt()
class Gripper:
    Cmd: Bool = Field(public=True)
    Sts: Bool = Field(public=True, final=True)
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")
    Fb_Vacuum: Bool = Field(physical=VACUUM_SENSOR, link="En")
```

`Fb_Contact` and `Fb_Vacuum` both link to `En`. When `En` rises, both feedback signals will respond — each with the timing declared by their `Physical`. The link must refer to a field in the same structure.

Do not put `physical=` on `En` just because `En` represents a real output. The harness discovers couplings from linked feedback fields (`Fb_*` with `link="En"`). An unlinked bool `physical=` annotation is metadata only and does not create a harness loop.

### Standalone tags — linking across the program

`link=` also works on standalone tags, not just UDT fields. This is useful for modeling process physics — responses that happen in the real world but aren't electrical feedback on the same device.

A conveyor sorts large boxes by extending a diverter. After the diverter fires, a box arrives at the bin sensor — that's a physical consequence with a real delay:

```python
from pyrung import Bool, Physical

DiverterCmd = Bool()
BinSensor = Bool(
    physical=Physical("BinSensor", on_delay="2s", off_delay="500ms"),
    link="DiverterCmd",
)
```

When `DiverterCmd` goes True, the harness schedules `BinSensor=True` 2 seconds later. When it drops, `BinSensor` clears after 500ms. No UDT needed — the link names any tag in the program.

The distinction: UDT links model device-level feedback (motor command → motor contactor feedback). Standalone links model process-level physics (diverter fires → box arrives at bin). Both use the same `Physical` timing and the same harness machinery.

### Value triggers — linking to specific states

Plain `link=` watches for bool edges (truthy ↔ falsy). When the enable tag is an Int with a choices map — a state machine, a mode selector — you often want feedback to fire when the tag enters a *specific* value, not just any nonzero value.

The `link="Tag:value"` syntax triggers on a specific value. The part after the colon is either a choices label or a literal integer:

```python
from pyrung import Int, Bool, Field, Physical, udt, named_array

@named_array(Int, count=1)
class SortState:
    IDLE = 0
    RUNNING = 1
    SORTING = 2

@udt()
class Sorter:
    State: Int = Field(choices=SortState)
    BinSensor: Bool = Field(
        physical=Physical("BinSensor", on_delay="2s", off_delay="500ms"),
        link="State:SORTING",
    )
```

When `State` transitions to the value matching `SORTING` (2), the harness schedules `BinSensor=True` after 2 seconds. When `State` transitions away from that value — to anything else — the harness schedules `BinSensor=False` after 500ms.

Both forms are valid:

- `link="State:SORTING"` — resolves `SORTING` through the tag's choices map
- `link="State:2"` — uses the literal integer directly, no choices map needed

If the part after the colon is a valid integer literal, it's used directly. Otherwise it's looked up in the enable tag's choices map. A missing choices map is only an error when the value isn't a numeric literal.

Value triggers also work on Char tags for string matching:

```python
Status = Char()
Ready = Bool(
    physical=Physical("Ready", on_delay="100ms", off_delay="50ms"),
    link="Status:Y",
)
```

Value triggers work with profile-driven feedback too. The profile's enable is active when the enable tag matches the trigger value and inactive otherwise — the same interface as a plain bool link:

```python
THERMOCOUPLE = Physical("Thermocouple", profile=Ramp(up=0.6, down=-0.1))

@udt()
class Oven:
    Mode: Int = Field(choices={0: "OFF", 1: "PREHEAT", 2: "BAKE"})
    Temp: Real = Field(physical=THERMOCOUPLE, link="Mode:BAKE",
                       min=0, max=300, uom="degC")
```

When `Mode` enters `BAKE`, the ramp climbs; when `Mode` leaves `BAKE`, it applies the `down` ambient decay.

Multiple feedback fields can watch the same enable tag with different trigger values:

```python
@udt()
class Station:
    State: Int = Field(choices={0: "IDLE", 1: "RUNNING", 2: "SORTING"})
    RunFb: Bool = Field(physical=FAST_SENSOR, link="State:RUNNING")
    SortFb: Bool = Field(physical=LIMIT_SWITCH, link="State:SORTING")
```

A transition from `RUNNING` to `SORTING` fires `RunFb` off-edge and `SortFb` on-edge simultaneously.

Analog feedback works the same way, with a `profile=` spec on the `Physical`:

```python
from pyrung import Ramp

THERMOCOUPLE = Physical("Thermocouple", profile=Ramp(up=0.5, down=-0.05))

@udt()
class Heater:
    Cmd: Bool = Field(public=True)
    Sts: Bool = Field(public=True, final=True)
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")
    Fb_Temp: Real = Field(physical=THERMOCOUPLE, link="En",
                          min=0, max=250, uom="degC")
```

`Fb_Contact` is bool — the harness drives it with on/off delays. `Fb_Temp` is analog — the harness drives it with a ramp. Both link to the same `En` and respond independently.

## Using the autoharness

Install a `Harness` on a PLC and it synthesizes all feedback patches automatically:

```python
from pyrung import Harness, PLC

with PLC(logic, dt=0.010) as plc:
    harness = Harness(plc)
    harness.install()

    Cmd.value = True
    plc.run_for(0.200)
    assert Gripper[1].Sts.value is True
```

No manual feedback toggling. The harness discovered the `En → Fb_Contact` and `En → Fb_Vacuum` couplings from the UDT declaration, installed edge monitors on `En`, and scheduled `Fb` patches using the declared timing.

### How bool feedback works

Each bool coupling is a real on-delay/off-delay timer pair (a `TON`→`TOF`), scanned as a **plant at the top of each scan** — the input-read phase. The on-delay accumulates while the command is sustained; once it crosses `on_delay`, `Fb` asserts, and the off-delay holds `Fb` for `off_delay` after the command drops. A command pulse shorter than `on_delay` never sustains the timer, so it never fabricates feedback — this is **dwell**, not a transport delay. The on-delay is rounded up to scan ticks based on the PLC's `dt`:

| `on_delay` | `dt` | Ticks |
|-----------|------|-------|
| `20ms` | `0.010` | 2 |
| `20ms` | `0.001` | 20 |
| `20ms` | `0.100` | 1 (minimum) |

Feedback is an **input**: the plant reads the *previous* scan's committed command, so `Fb` lags the command by one scan. A held command's feedback therefore arrives after `ceil(on_delay / dt)` sustained scans plus one input-read scan — with `on_delay == 0`, the program sees `Fb` the very next scan. (This is the physical model: a sensor can't respond to an output before it's written.)

Multiple `Fb` fields linked to the same `En` are independent timers, each with its own `Physical` timing. A vacuum gripper's `Fb_Contact` (5ms) and `Fb_Vacuum` (80ms) assert at different scans from the same `En` edge.

### How profile-driven feedback works

A `profile=` spec is **declarative data**, not a Python function — the harness lowers it to real plant rungs (a guarded `calc` or timer), so it folds, traces, and round-trips through a Click nickname comment like everything else. There are three specs.

**`Ramp`** — a constant-slope analog response. `Fb` moves `up` units per second while `En` is active and `down` units per second otherwise (`down` is usually negative — an ambient decay or bleed-down; `0` means "hold on En fall"). Rates are per **second**, applied against the `sys.dt` system tag, so they're stable across scan periods:

```python
from pyrung import Ramp

BURNER   = Physical("Burner",   profile=Ramp(up=0.8, down=-0.05))  # heat 0.8°/s, cool 0.05°/s
PRESSURE = Physical("Pressure", profile=Ramp(up=10.0, down=-5.0))  # +10 PSI/s, bleed -5 PSI/s
```

The program's own logic controls when `En` drops. A heater turns off `En` when `Fb_Temp` hits the setpoint — the ramp was climbing, but the program cut it off at 180°C. The harness doesn't need to know the settling point; the program does.

**`Approach`** — a first-order (exponential) lag. `Fb` moves toward `toward` by fraction `rate` per second and holds on `En` fall. `toward` is a constant or a setpoint tag name:

```python
from pyrung import Approach

OVEN = Physical("Oven", profile=Approach(toward=180.0, rate=0.3))   # exponential to 180°C
```

**`Pulse`** — a bool pulse train (an astable "flasher") for a discrete pulse sensor like a shaft encoder or flow-meter output. While `En` is active, `Fb` cycles high for `on_dwell` then low for `off_dwell`; it rests low when disabled:

```python
from pyrung import Pulse

ENCODER = Physical("Encoder", profile=Pulse(on_dwell="8ms", off_dwell="8ms"))

@udt()
class Conveyor:
    En: Bool
    Fb_Encoder: Bool = Field(physical=ENCODER, link="En")
```

A counter instruction in the logic counts the rising edges — the harness produces the pulse train, the program counts it.

In a Click nickname comment the spec is a comma-free token: `profile=ramp:up=0.8|down=-0.05`, `profile=approach:toward=180|rate=0.3`, or `profile=pulse:on_dwell=8ms|off_dwell=8ms`.

## Validation

pyrung validates UDT and named-array field annotations at construction time:

- **Bool Fb field + `link=` but no physical** — rejected. A linked bool feedback field must declare either `physical=Physical(..., on_delay=..., off_delay=...)` or `physical=Physical(..., profile=...)`.
- **Physical profile without `link=`** — rejected on tags and fields. A profile defines a response to a linked command; without a link there's nothing to respond to.
- **Trigger value on Bool enable** — rejected. `link="En:1"` where `En` is a Bool field is invalid; use plain `link="En"` for Bool enables.
- **Unknown choices label** — `link="State:MISSING"` raises `ValueError` when `MISSING` is not in the enable field's choices map.
- **Non-numeric trigger without choices** — `link="State:SORTING"` on an Int field with no choices map raises `ValueError`. Use `link="State:2"` for literal values.

`Program.validate()` also checks the full program. In addition to range violations and feedback timing hazards, it reports linked analog feedback that does not declare `physical=Physical(..., profile=...)`.

## Forces override the harness

Forces take precedence over harness patches. If you force a feedback tag to a specific value, the harness patch lands but the force re-applies on top of it:

```python
with PLC(logic, dt=0.010) as plc:
    harness = Harness(plc)
    harness.install()

    plc.force(Gripper[1].Fb_Contact, False)  # hold Fb off
    Cmd.value = True
    plc.run_for(0.050)
    assert Gripper[1].Fb_Contact.value is False  # force wins
```

This is how you test "what happens when feedback never arrives" — force the Fb off and let the program's fault timer trip.

## Tag metadata: min, max, uom

Alongside `physical=` and `link=`, fields accept value-domain metadata:

```python
Fb_Temp: Real = Field(physical=THERMOCOUPLE, link="En",
                      min=0, max=250, uom="degC")
```

The static validator catches literal writes outside these bounds (`TAG_RANGE_VIOLATION`), and the runtime bounds checker flags dynamic writes that land outside the declared range after each scan — see [Testing: Checking bounds](testing.md#checking-bounds). Values are never clamped; the check sets a warning and populates `plc.bounds_violations`. The debugger's Data View shows declared ranges as hints. Profile functions receive only `(cur, en, dt)`, so pass constants explicitly if a profile needs bounds.

## Fault coverage

For fault coverage — proving every device has an alarm path — see [Verification](verification.md#fault-coverage). The workflow uses `harness.couplings()` to iterate device couplings and `always()` to check structural detection paths.

## Next steps

- [Verification](verification.md) — always(), never(), fault coverage, lock files
- [Testing Guide](testing.md) — deterministic testing patterns, forces, monitors
- [Analysis](analysis.md) — program structure, diagnosis, cause/effect, test coverage
- [VS Code Debugger](dap-vscode.md) — Data View, breakpoints, step-through debugging
- [Harness in the debugger](dap-vscode.md#autoharness-in-the-debug-session) — auto-installs when annotations exist, `harness status/remove/install` console verbs, capture provenance
