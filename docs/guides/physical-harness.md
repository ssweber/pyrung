# Physical Annotations and Autoharness

Once you have a working program with UDTs, you can annotate the physical behavior of feedback signals. Two fields on a `Field` declaration — `physical=` and `link=` — tell pyrung how a real device responds. The autoharness reads those annotations and synthesizes feedback patches in tests automatically, so you stop writing boilerplate that toggles inputs by hand.

## The problem

A typical device-heavy test file is 80% feedback toggling:

```python
def test_gripper_cycle():
    with PLC(logic, dt=0.010) as plc:
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
SLOW_VALVE   = Physical("SlowValve", on_delay="20ms", off_delay="80ms")
```

**Analog feedback** — a signal that follows a continuous response curve (thermocouples, pressure transmitters, flow meters):

```python
THERMOCOUPLE = Physical("Thermocouple", profile="generic_thermal")
```

A `Physical` is either bool (has timing) or analog (has a profile name), never both. Delays accept duration strings: `"5ms"`, `"2s"`, `"1s500ms"`.

## Linking feedback to commands

The `link=` field on a `Field` declaration says "this feedback responds to that command." The `physical=` field says how:

```python
from pyrung import udt, Bool, Real, Field

@udt()
class Gripper:
    Cmd: Bool = Field(public=True)
    Sts: Bool = Field(public=True, final=True)
    En: Bool = Field(physical=SLOW_VALVE)
    Fb_Contact: Bool = Field(physical=LIMIT_SWITCH, link="En")
    Fb_Vacuum: Bool = Field(physical=LIMIT_SWITCH, link="En")
```

`Fb_Contact` and `Fb_Vacuum` both link to `En`. When `En` rises, both feedback signals will respond — each with the timing declared by their `Physical`. The link must refer to a field in the same structure.

Analog feedback works the same way, with `profile=` on the `Physical` or overridden on the `Field`:

```python
THERMOCOUPLE = Physical("Thermocouple", profile="generic_thermal")

@udt()
class Heater:
    Cmd: Bool = Field(public=True)
    Sts: Bool = Field(public=True, final=True)
    En: Bool = Field(physical=SLOW_VALVE)
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

When the harness sees `En` rise, it schedules `Fb=True` at `now + on_delay`. When `En` falls, it schedules `Fb=False` at `now + off_delay`. Delays are converted to scan ticks based on the PLC's `dt`:

| `on_delay` | `dt` | Ticks |
|-----------|------|-------|
| `20ms` | `0.010` | 2 |
| `20ms` | `0.001` | 20 |
| `20ms` | `0.100` | 1 (floor) |

A scheduled patch always arrives at least 1 tick later — you can't schedule in the past.

Multiple `Fb` fields linked to the same `En` schedule independently, each with its own `Physical` timing. A vacuum gripper's `Fb_Contact` (5ms) and `Fb_Vacuum` (80ms) arrive at different times from the same `En` edge.

### How analog feedback works

Analog feedback delegates to a registered profile function. Register one with the `@profile` decorator:

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

The program's own logic controls when `En` drops. A heater program turns off `En` when `Fb_Temp` hits the setpoint — the profile was ramping toward max, but the program cut it off at 180°C. The harness doesn't need to know the settling point; the program does.

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

## Validation

pyrung validates physical annotations at construction time:

- **Bool Fb + `link=` but no `physical=`** — rejected. A linked bool feedback must declare its response timing.
- **`profile=` without `link=`** — rejected. A profile defines a response to a linked command; without a link there's nothing to respond to.
- **Bool Fb with `profile=`** — rejected. Bool feedback uses `on_delay`/`off_delay`; profiles are for analog only.

`Program.validate()` also checks physical annotations across the full program and reports findings.

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

These are descriptive — they don't clamp values at runtime. The validator uses them (out-of-range findings), the debugger's Data View shows them as range hints, and profile functions can reference them if needed.

## Next steps

- [Testing Guide](testing.md) — deterministic testing patterns, forces, monitors
- [Analysis](analysis.md) — validation findings, dataview, cause/effect
- [VS Code Debugger](dap-vscode.md) — Data View, breakpoints, step-through debugging
