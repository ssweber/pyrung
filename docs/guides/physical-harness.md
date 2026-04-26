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

**Profile-driven feedback** — a signal driven by a custom response function (thermocouples, pressure transmitters, flow meters, shaft encoders):

```python
THERMOCOUPLE = Physical("Thermocouple", profile="generic_thermal")
ENCODER = Physical("Encoder", profile="shaft_encoder")
```

A `Physical` has either timing (on_delay/off_delay) or a profile name, never both. Bool fields accept either form — use timing for simple delayed transitions (contactors, limit switches) and profiles for signals that need custom state like pulse trains. Delays accept duration strings: `"5ms"`, `"2s"`, `"1s500ms"`.

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

DiverterCmd = Bool("DiverterCmd")
BinSensor = Bool("BinSensor",
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
Status = Char("Status")
Ready = Bool("Ready",
    physical=Physical("Ready", on_delay="100ms", off_delay="50ms"),
    link="Status:Y",
)
```

Value triggers work with profile-driven feedback too. The profile function receives `en=True` when the enable tag matches the trigger value, `en=False` otherwise — the same interface as a plain bool link:

```python
THERMOCOUPLE = Physical("Thermocouple", profile="zone_thermal")

@udt()
class Oven:
    Mode: Int = Field(choices={0: "OFF", 1: "PREHEAT", 2: "BAKE"})
    Temp: Real = Field(physical=THERMOCOUPLE, link="Mode:BAKE",
                       min=0, max=300, uom="degC")
```

When `Mode` enters `BAKE`, the profile sees `en=True` and ramps up. When `Mode` leaves `BAKE`, it sees `en=False` and can model ambient decay.

Multiple feedback fields can watch the same enable tag with different trigger values:

```python
@udt()
class Station:
    State: Int = Field(choices={0: "IDLE", 1: "RUNNING", 2: "SORTING"})
    RunFb: Bool = Field(physical=FAST_SENSOR, link="State:RUNNING")
    SortFb: Bool = Field(physical=LIMIT_SWITCH, link="State:SORTING")
```

A transition from `RUNNING` to `SORTING` fires `RunFb` off-edge and `SortFb` on-edge simultaneously.

Analog feedback works the same way, with `profile=` on the `Physical`:

```python
THERMOCOUPLE = Physical("Thermocouple", profile="generic_thermal")

@udt()
class Heater:
    Cmd: Bool = Field(public=True)
    Sts: Bool = Field(public=True, final=True)
    En: Bool
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")
    Fb_Temp: Real = Field(physical=THERMOCOUPLE, link="En",
                          min=0, max=250, uom="degC")
```

`Fb_Contact` is bool — the harness drives it with on/off delays. `Fb_Temp` is analog — the harness drives it with a profile function. Both link to the same `En` and respond independently.

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

When the harness sees `En` rise, it schedules `Fb=True` at `now + on_delay`. When `En` falls, it schedules `Fb=False` at `now + off_delay`. Delays are rounded up to scan ticks based on the PLC's `dt`:

| `on_delay` | `dt` | Ticks |
|-----------|------|-------|
| `20ms` | `0.010` | 2 |
| `20ms` | `0.001` | 20 |
| `20ms` | `0.100` | 1 (minimum) |

A scheduled patch always arrives at least 1 tick later — you can't schedule in the past.

Multiple `Fb` fields linked to the same `En` schedule independently, each with its own `Physical` timing. A vacuum gripper's `Fb_Contact` (5ms) and `Fb_Vacuum` (80ms) arrive at different times from the same `En` edge.

### How profile-driven feedback works

Profile-driven feedback delegates to a registered profile function. Register one with the `@profile` decorator:

```python
from pyrung import profile

@profile("generic_thermal")
def generic_thermal(cur, en, dt):
    if en:
        return cur + 0.5 * dt   # 0.5 degrees per second
    return cur                   # hold on En fall
```

The function is called once per scan tick while the coupling is active. It receives:

- `cur` — current value of the Fb tag
- `en` — current state of the linked En (`True`/`False`)
- `dt` — PLC scan period in seconds

Write rate-per-second math; `dt` makes the result stable across scan rates. A profile running at `dt=0.001` and `dt=0.100` should converge to the same value over the same wall-clock duration.

The program's own logic controls when `En` drops. A heater program turns off `En` when `Fb_Temp` hits the setpoint — the profile was ramping upward, but the program cut it off at 180°C. The harness doesn't need to know the settling point; the program does.

```python
@profile("120BTU_burner")
def burner_120btu(cur, en, dt):
    if en:
        return cur + 0.8 * dt    # 0.8 degrees per second
    return cur - 0.05 * dt       # slow ambient decay

@profile("generic_pressure")
def generic_pressure(cur, en, dt):
    if en:
        return cur + 10.0 * dt   # 10 PSI per second
    return cur - 5.0 * dt        # bleed down
```

### Bool fields with profiles

Profiles aren't limited to analog tags. A Bool field can use `profile=` instead of `on_delay`/`off_delay` when the feedback needs custom state — the most common case is a discrete pulse sensor like a shaft encoder or flow meter pulse output.

Since `cur` is a Bool (`True`/`False`), it can't carry phase state. Use a closure:

```python
ENCODER = Physical("Encoder", profile="shaft_encoder")

def make_encoder_profile(rpm=60):
    phase = [0.0]
    period = 60.0 / rpm

    @profile("shaft_encoder")
    def shaft_encoder(cur, en, dt):
        if not en:
            phase[0] = 0.0
            return False
        phase[0] += dt
        return (phase[0] % period) < (period / 2)

    return shaft_encoder

@udt()
class Conveyor:
    En: Bool
    Fb_Encoder: Bool = Field(physical=ENCODER, link="En")
```

The closure holds the accumulated phase; the profile toggles the Bool at the right frequency. A counter instruction in the logic counts the rising edges — the harness produces the pulse train, the program counts it.

One profile registration per name, so two encoders at different RPMs need two profile names.

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

The static validator catches literal writes outside these bounds (`CORE_RANGE_VIOLATION`), and the runtime bounds checker flags dynamic writes that land outside the declared range after each scan — see [Testing: Checking bounds](testing.md#checking-bounds). Values are never clamped; the check sets a warning and populates `plc.bounds_violations`. The debugger's Data View shows declared ranges as hints. Profile functions receive only `(cur, en, dt)`, so pass constants explicitly if a profile needs bounds.

## Fault coverage

For fault coverage — proving every device has an alarm path — see [Verification](verification.md#fault-coverage). The workflow uses `harness.couplings()` to iterate device couplings and `prove()` to check structural detection paths.

## Next steps

- [Verification](verification.md) — prove(), fault coverage, lock files
- [Testing Guide](testing.md) — deterministic testing patterns, forces, monitors
- [Analysis](analysis.md) — dataview, cause/effect, coverage queries, static validators
- [VS Code Debugger](dap-vscode.md) — Data View, breakpoints, step-through debugging
- [Harness in the debugger](dap-vscode.md#autoharness-in-the-debug-session) — auto-installs when annotations exist, `harness status/remove/install` console verbs, capture provenance
