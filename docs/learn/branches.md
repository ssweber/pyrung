# Lesson 8: Branches and OR Logic

## The Python instinct

```python
if auto_mode and state == "sorting" and is_large:
    diverter = True
elif manual_mode and diverter_button:
    diverter = True
```

## The ladder logic way

Ladder logic has two ways to combine conditions. For OR-ing two Bool tags together, use `|`:

```python
from pyrung import Bool, Int, Program, Rung, branch, comment, out, latch, reset, any_of, all_of

Auto          = Bool("Auto")
Manual        = Bool("Manual")
StopBtn       = Bool("StopBtn")     # NC contact
StartBtn      = Bool("StartBtn")
EstopOK       = Bool("EstopOK")     # NC safety relay permission
Running       = Bool("Running")
Light         = Bool("Light")
DiverterBtn   = Bool("DiverterBtn")
DiverterCmd   = Bool("DiverterCmd")
ConveyorMotor = Bool("ConveyorMotor")
StatusLight   = Bool("StatusLight")
Mode          = Int("Mode")

with Program() as logic:
    # Motor runs in either mode when started
    with Rung(Auto | Manual):
        out(Light)                        # Status light: either mode is active

    # any_of for comparisons or more than two conditions
    with Rung(any_of(Mode == 1, Mode == 3, Mode == 5)):
        latch(Running)
```

`|` is binary and works on any condition — Bool tags *and* comparisons. The catch is Python's operator precedence: `|` binds tighter than `==`, `<`, `>`, so comparisons need parentheses: `(Mode == 1) | (Mode == 3)`. `any_of` is variadic and skips the paren noise. Reach for `any_of` when the parens get loud or you have more than two terms. Same shape for `&` vs `all_of` — mirrors a familiar Python pattern: `a or b or c` vs `any([a, b, c])`.

## Branches

A `branch` creates a parallel path within a rung. Think of it as a second wire that ANDs its condition with the parent's.

Here's the conveyor's motor rung. `EstopOK` gates everything — it's a permission input from the safety relay, True when the world is safe to run. Below that, the motor and status light share the same gate:

```
  EstopOK (parent rung — True when safe)
      +-- Running -> out(ConveyorMotor)
      +-- Running -> out(StatusLight)
```

```python
with Program() as logic:
    comment("Start/stop — NC stop resets when pressed or wire broken")
    with Rung(StartBtn, any_of(Auto, Manual)):
        latch(Running)
    with Rung(~StopBtn):
        reset(Running)
    with Rung(~EstopOK):
        reset(Running)

    comment("Motor output — EstopOK gates all outputs")
    with Rung(EstopOK):
        with branch(Running):
            out(ConveyorMotor)
        with branch(Running):
            out(StatusLight)
```

This is the **gate pattern**. The parent rung holds your master condition, and every branch inside inherits that permission automatically. Lose the gate, lose all the outputs — atomically, in one scan. `EstopOK` reads as "safety is satisfied" so the gate uses the raw tag with no `~`. The reset rungs use `~StopBtn` and `~EstopOK` because those fire when the NC circuits open — same `~` convention from [Lesson 3](latch-reset.md).

The gate pattern is *the* textbook ladder structure for any permission or interlock — guard doors, light curtains, machine-enabled flags. Real fail-safe E-stop wiring lives in [Lesson 11](hardware.md); here, the gate is general-purpose.

## Combining branches and `any_of`

The diverter needs to fire in two cases: auto mode during sorting, or manual mode with the button pressed. That's `any_of` with `all_of` — same pattern as `any([all([...]), all([...]])` in Python:

```python
    comment("Diverter output — auto sort OR manual button, gated by EstopOK")
    with Rung(
        EstopOK,
        any_of(
            all_of(State == SORTING, IsLarge, Auto),
            all_of(Manual, DiverterBtn),
        ),
    ):
        out(DiverterCmd)
```

The diverter rung reads `State` and `IsLarge` directly from the state machine in [Lesson 7](state-machines.md) — no intermediate latch needed. Both control sources fold into one rung with a single `out(DiverterCmd)`. Remember "order has meaning" from [Lesson 1](scan-cycle.md)? This is how you escape it: **one coil, one rung.** If two separate rungs both `out` the same tag, the last one evaluated wins — a false manual rung below a true auto rung would de-energize the diverter. Fold every reason the output should energize into one rung and order stops being a side effect.

!!! tip "Key concept: atomic rungs"

    **All conditions evaluate before any instructions execute.** The branch doesn't "see" results of instructions above it in the same rung — every rung is a snapshot of the world, evaluated then acted on as a unit. This is the **atomic rung** property: conditions read from the state as it was when the rung started, not from half-finished instruction results. It ties back to [Lesson 1's scan cycle](scan-cycle.md) and forward to [Testing](testing.md), where deterministic scans make this guarantee testable.

## Seal-in: a branch that holds itself

[Lesson 3](latch-reset.md) used `latch`/`reset` for start/stop control. The classic ladder alternative is a **seal-in** — a single rung where the output feeds back into its own branch:

```python
with Rung(~StopBtn):
    with branch(StartBtn | Running):
        out(Running)
```

`Running` appears in its own branch condition. Press `StartBtn` and `Running` energizes; release it and `Running` still powers the branch — it holds itself in. Open `~StopBtn` and the parent rung drops, breaking the seal. Reach for `latch`/`reset` when clarity matters; expect seal-in in every legacy ladder you inherit.

## Try it

```python
from pyrung import PLC

runner = PLC(logic)
with runner:
    StopBtn.value = True             # NC inputs: True = healthy
    EstopOK.value = True

    Auto.value = True
    StartBtn.value = True
    runner.step()
    assert Running.value is True
    assert ConveyorMotor.value is True
    assert StatusLight.value is True

    StartBtn.value = False
    runner.step()
    assert Running.value is True     # Still running (latched)

    # E-stop kills everything (NC opens)
    EstopOK.value = False
    runner.step()
    assert ConveyorMotor.value is False
    assert StatusLight.value is False
    assert Running.value is False
```

## Exercise

Add a `ManualLight` that is on only in manual mode. Write a test that switches from Auto to Manual mid-run and verifies the diverter control source changes without the motor stopping. Test that the diverter button does nothing in auto mode.

---

Each bin has a count, the diverter has a state, and there's a mode selector. That's a lot of scattered tags. In a real PLC, you'd group them into structures -- UDTs for the bins and the equipment.

!!! info "Also known as..."

    OR is a parallel branch of contacts; AND is contacts in series. `branch()` is "parallel branch" everywhere. The safety-gate pattern is sometimes called "Master Control Reset" (`MCR`). Seal-in is the classic OR-branch with a series stop contact.
