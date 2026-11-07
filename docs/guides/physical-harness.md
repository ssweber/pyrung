# Physical Annotations and Autoharness

Once you have a working program with UDTs, you can annotate the physical behavior of feedback signals. On a feedback field, `physical=` describes the feedback response and `link=` names the command/enable field that drives it. The autoharness reads those annotations and synthesizes feedback patches in tests automatically, so you stop writing boilerplate that toggles inputs by hand.

## The problem

Much of a device-heavy test file is feedback toggling:

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

Twenty devices means twenty feedback loops and twenty blocks of this. You maintain them by hand. They diverge from reality over time. They go wrong in subtle ways when someone changes a delay in the test but not the device, or the other way round.

## Declaring physical behavior

`Physical` describes how a feedback signal responds in the real world. There are two kinds:

**Bool feedback** — a signal that asserts or deasserts after a delay (limit switches, proximity sensors, pressure switches):

```python
from pyrung import Physical

LIMIT_SWITCH = Physical("LimitSwitch", on_delay="5ms", off_delay="5ms")
VACUUM_SENSOR = Physical("VacuumSensor", on_delay="80ms", off_delay="50ms")
```

**Pulse trains and analog values** (thermocouples, pressure transmitters, flow meters, shaft encoders):

```python
THERMOCOUPLE = Physical("Thermocouple", ramp_on=0.5, ramp_off=-0.05)
PRESSURE     = Physical("Pressure",     approach_to=100.0, approach_rate=0.3)
ENCODER      = Physical("Encoder",      flasher_on="8ms", flasher_off="8ms")
```

A `Physical` takes the keywords of one kind. `on_delay`/`off_delay` suit simple delayed transitions (contactors, limit switches). `flasher_on`/`flasher_off` make a Bool pulse train. `ramp_on`/`ramp_off` and `approach_to`/`approach_rate` drive analog registers. Times are strings with units, such as `"2s"` or `"1s500ms"`. A bare number raises. Rates are numbers per second. The same keywords go in a CLICK nickname comment: `[link=Heat, ramp_on=0.5, ramp_off=-0.05]`.

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

`Fb_Contact` and `Fb_Vacuum` both link to `En`. When `En` rises, both feedback signals will respond. Each uses the timing declared by its `Physical`. The link must refer to a field in the same structure.

Do not put `physical=` on `En` just because `En` represents a real output. The harness discovers couplings from linked feedback fields (`Fb_*` with `link="En"`). An unlinked bool `physical=` annotation is metadata only and does not create a harness loop.

### Standalone tags — linking across the program

`link=` also works on standalone tags, not just UDT fields. This is useful for modeling process physics. These are responses that happen in the real world but aren't electrical feedback on the same device.

A conveyor sorts large boxes by extending a diverter. After the diverter fires, a box arrives at the bin sensor. That's a physical consequence with a real delay:

```python
from pyrung import Bool, Physical

DiverterCmd = Bool()
BinSensor = Bool(
    physical=Physical("BinSensor", on_delay="2s", off_delay="500ms"),
    link="DiverterCmd",
)
```

When `DiverterCmd` goes True, the harness schedules `BinSensor=True` 2 seconds later. When `DiverterCmd` drops, `BinSensor` clears after 500ms. No UDT is needed. The link names any tag in the program.

UDT links model device-level feedback (motor command → motor contactor feedback). Standalone links model process-level physics (diverter fires → box arrives at bin). Both use the same `Physical` timing and the same harness machinery.

### Normally closed — `~`

Prefix the enable with `~` when the feedback responds while a bit is **off**, like a `~Tag` contact on a rung. A spring-return valve opens when its solenoid drops out:

```python
VentSol = Bool()
VentOpen = Bool(
    physical=Physical("VentOpen", on_delay="300ms", off_delay="200ms"),
    link="~VentSol",
)
```

`~` only applies to Bool enables. To drive while an Int equals zero, write `link="Mode:0"`.

### Value triggers — linking to specific states

Plain `link=` watches for bool edges (truthy ↔ falsy). When the enable tag is an Int with a choices map (a state machine, a mode selector), you often want feedback to fire when the tag enters a *specific* value, not just any nonzero value.

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

When `State` transitions to the value matching `SORTING` (2), the harness schedules `BinSensor=True` after 2 seconds. When `State` changes to any other value, the harness schedules `BinSensor=False` after 500ms.

Both forms are valid:

- `link="State:SORTING"` — resolves `SORTING` through the tag's choices map
- `link="State:2"` — uses the literal integer directly, no choices map needed

If the part after the colon is a valid integer literal, it's used directly. Otherwise it's looked up in the enable tag's choices map.

Value triggers also work on Char tags for string matching:

```python
Status = Char()
Ready = Bool(
    physical=Physical("Ready", on_delay="100ms", off_delay="50ms"),
    link="Status:Y",
)
```

Value triggers work with analog feedback too. The enable is active when the enable tag matches the trigger value and inactive otherwise. This is the same interface as a plain bool link:

```python
THERMOCOUPLE = Physical("Thermocouple", ramp_on=0.6, ramp_off=-0.1)

@udt()
class Oven:
    Mode: Int = Field(choices={0: "OFF", 1: "PREHEAT", 2: "BAKE"})
    Temp: Real = Field(physical=THERMOCOUPLE, link="Mode:BAKE",
                       min=0, max=300, uom="degC")
```

When `Mode` enters `BAKE`, the ramp climbs. When `Mode` leaves `BAKE`, the value moves at `ramp_off`.

Multiple feedback fields can watch the same enable tag with different trigger values:

```python
@udt()
class Station:
    State: Int = Field(choices={0: "IDLE", 1: "RUNNING", 2: "SORTING"})
    RunFb: Bool = Field(physical=FAST_SENSOR, link="State:RUNNING")
    SortFb: Bool = Field(physical=LIMIT_SWITCH, link="State:SORTING")
```

A transition from `RUNNING` to `SORTING` fires `RunFb` off-edge and `SortFb` on-edge simultaneously.

Analog feedback works the same way, with `ramp_on` or `approach_to` on the `Physical`:

```python
THERMOCOUPLE = Physical("Thermocouple", ramp_on=0.5, ramp_off=-0.05)

@udt()
class Heater:
    Cmd: Bool = Field(public=True)
    Sts: Bool = Field(public=True, final=True)
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")
    Fb_Temp: Real = Field(physical=THERMOCOUPLE, link="En",
                          min=0, max=250, uom="degC")
```

`Fb_Contact` is a bool, so the harness drives it with on/off delays. `Fb_Temp` is analog, so the harness drives it with a ramp. Both link to the same `En` and respond independently.

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

The test never toggles the feedback by hand.

### How bool feedback works

Each bool coupling runs as a CLICK `debounce` coil on its link, scanned as a **plant** (the harness's model of the device) at the top of each scan (the input-read phase). `Fb` turns on once the command has been on for `on_delay`. It turns off once the command has been off for `off_delay`. A command pulse shorter than `on_delay` never turns `Fb` on. A dip shorter than `off_delay` never turns `Fb` off. Both delays are rounded up to whole scans of the PLC's `dt`.

Feedback is an **input**. The plant reads the *previous* scan's committed command, so `Fb` lags the command by one scan. A held command's feedback therefore arrives after `ceil(on_delay / dt)` sustained scans plus one input-read scan. With `on_delay == 0`, the program sees `Fb` the very next scan. (This is the physical model. A sensor can't respond to an output before it's written.) A dropped command clears `Fb` one scan later than the same delay would set it, as `debounce` does on a CLICK.

A delay longer than 32.767 s counts in whole seconds, rounded up. A delay longer than about 9 hours counts in whole minutes. The same goes for `flasher_on` and `flasher_off`. Both delays of one `Physical` count in the same unit, so `on_delay="100ms", off_delay="40s"` turns `Fb` on after 1 s.

Multiple `Fb` fields linked to the same `En` are independent timers, each with its own `Physical` timing. A vacuum gripper's `Fb_Contact` (5ms) and `Fb_Vacuum` (80ms) assert at different scans from the same `En` edge.

### How flasher and analog feedback work

These keywords are **declarative data**, not Python functions. There are three kinds.

**Ramp** (`ramp_on`, `ramp_off`) — a constant-slope analog response. `Fb` moves `ramp_on` units per second while `En` is active and `ramp_off` units per second otherwise. `ramp_off` is usually negative, for an ambient decay or bleed-down. Left out, `ramp_off` is `0`, which holds the value when `En` drops. Rates are per **second**, applied against the `sys.dt` system tag, so they're stable across scan periods:

```python
BURNER   = Physical("Burner",   ramp_on=0.8, ramp_off=-0.05)  # heat 0.8°/s, cool 0.05°/s
PRESSURE = Physical("Pressure", ramp_on=10.0, ramp_off=-5.0)  # +10 PSI/s, bleed -5 PSI/s
```

The program's own logic controls when `En` drops. A heater turns off `En` when `Fb_Temp` hits the setpoint. The ramp was climbing, but the program cut it off at 180°C. The harness doesn't need to know the settling point. The program does.

**Approach** (`approach_to`, `approach_rate`) — a first-order (exponential) lag. `Fb` moves toward `approach_to` by fraction `approach_rate` per second and holds on `En` fall. `approach_to` is a number or the name of a setpoint tag. A number in quotes (`"180"`) raises. If no rung uses that tag and nothing has written it, pyrung warns at install, and the tag reads 0 until something writes it.

```python
OVEN = Physical("Oven", approach_to=180.0, approach_rate=0.3)   # exponential to 180°C
```

**Flasher** (`flasher_on`, `flasher_off`) — a bool pulse train for a discrete pulse sensor like a shaft encoder or flow-meter output. It runs as a CLICK `flasher` coil. While `En` is active, `Fb` cycles high for `flasher_on` then low for `flasher_off`. It rests low when disabled:

```python
ENCODER = Physical("Encoder", flasher_on="8ms", flasher_off="8ms")

@udt()
class Conveyor:
    En: Bool
    Fb_Encoder: Bool = Field(physical=ENCODER, link="En")
```

A counter instruction in the logic counts the rising edges. The harness produces the pulse train and the program counts it.

`Fb` goes high the scan after `En` turns on. That first high phase is one scan short. A `flasher_on` or `flasher_off` of 0 keeps `Fb` low.

In a comment, `link=` names the enable by nickname or by CLICK address, with the same optional `~` or trigger: `[on_delay=2s, off_delay=500ms, link=Motor_Run]`, `[on_delay=2s, link=Y001]`, `[on_delay=300ms, link=~Vent_Sol]`, or `[on_delay=1s, link=State:SORTING]`. An address reads as the nickname at that address. An address without a nickname keeps its address name (`Y005`).

## Validation

pyrung validates UDT and named-array field annotations at construction time:

- **Bool Fb field + `link=` but no physical** — rejected. A linked bool feedback field must declare a `physical=Physical(...)`, such as `Physical(..., on_delay=..., off_delay=...)`.
- **A flasher, ramp or approach without `link=`** — rejected on tags and fields. It defines a response to a linked command. Without a link there's nothing to respond to.
- **Trigger value on Bool enable** — rejected. `link="En:1"` where `En` is a Bool field is invalid. Use `link="En"` to drive while it is on, or `link="~En"` while it is off.
- **`~` on a non-Bool enable** — rejected. `link="~Mode"` where `Mode` is an Int raises `ValueError`. Write `link="Mode:0"`.
- **Unknown choices label** — `link="State:MISSING"` raises `ValueError` when `MISSING` is not in the enable field's choices map.
- **Non-numeric trigger without choices** — `link="State:SORTING"` on an Int field with no choices map raises `ValueError`. Use `link="State:2"` for literal values.

`Program.check()` also checks the full program. In addition to range violations and feedback timing hazards, it reports linked analog feedback that does not declare `ramp_on` or `approach_to`, and a `link=` whose enable the program never uses (`PHYS_LINK_UNKNOWN`). For that link, the harness has nothing to drive the feedback from.

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

This is how you test "what happens when feedback never arrives". Force the Fb off and let the program's fault timer trip.

## Tag metadata: min, max, uom

Alongside `physical=` and `link=`, fields accept value-domain metadata:

```python
Fb_Temp: Real = Field(physical=THERMOCOUPLE, link="En",
                      min=0, max=250, uom="degC")
```

The [`TAG_RANGE_VIOLATION` ladder lint](ladder-lints.md#rule-reference) catches literal writes outside these bounds, and the runtime bounds checker flags dynamic writes that land outside the declared range after each scan. See [Testing: Checking bounds](testing.md#checking-bounds). Values are never clamped. The check sets a warning and populates `plc.bounds_violations`. The debugger's Data View shows declared ranges as hints.

## Fault coverage

For fault coverage (proving every device has an alarm path), see [Verification](verification.md#fault-coverage). The workflow uses `harness.couplings()` to iterate device couplings and `always()` to check structural detection paths.

## Next steps

- [Verification](verification.md) — always(), never(), fault coverage, lock files
- [Testing Guide](testing.md) — deterministic testing patterns, forces, monitors
- [Analysis](analysis.md) — program structure, diagnosis, cause/effect, test coverage
- [VS Code Debugger](dap-vscode.md) — Data View, breakpoints, step-through debugging
- [Harness in the debugger](dap-vscode.md#autoharness-in-the-debug-session) — auto-installs when annotations exist, `harness status/remove/install` console verbs, capture provenance
