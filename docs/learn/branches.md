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
AutoDivert    = Bool("AutoDivert")    # Set by state machine in auto mode
Mode          = Int("Mode")

with Program() as logic:
    # Motor runs in either mode when started
    with Rung(Auto | Manual):
        out(Light)                        # Status light: either mode is active

    # any_of for comparisons or more than two conditions
    with Rung(any_of(Mode == 1, Mode == 3, Mode == 5)):
        latch(Running)
```

Use `|` when you're OR-ing two Bool tags. Use `any_of` when you're OR-ing comparisons or have more than two conditions.

## Branches

A `branch` creates a parallel path within a rung. Think of it as a second wire that ANDs its condition with the parent's.

Here's the conveyor's control rung. `EstopOK` gates everything — it's a permission input from the safety relay, True when the world is safe to run. Below that, the motor runs when started, and the diverter responds to either auto logic or the manual button:

```
  EstopOK (parent rung — True when safe)
      +-- Running -----------------------------> out(ConveyorMotor)
      +-- any_of(Auto+AutoDivert, Manual+Btn) -> out(DiverterCmd)
```

```python
Light = Bool("Light")

with Program() as logic:
    comment("Motor and diverter outputs — EstopOK gates everything")
    with Rung(EstopOK):
        with branch(Running):
            out(ConveyorMotor)
        with branch(any_of(
            all_of(Auto, AutoDivert),
            all_of(Manual, DiverterBtn),
        )):
            out(DiverterCmd)

    comment("Start/stop — NC stop resets when pressed or wire broken")
    with Rung(StartBtn, any_of(Auto, Manual)):
        latch(Running)
    with Rung(~StopBtn):
        reset(Running)
    with Rung(~EstopOK):
        reset(Running)
```

The `EstopOK` parent rung gates everything — if the safety relay drops permission, no branch has power, so the motor stops and the diverter closes regardless of mode. Notice the naming: `EstopOK` reads as "safety is satisfied" so the gate uses the raw tag with no `~`. The reset rungs use `~StopBtn` and `~EstopOK` because those fire when the NC circuits open.

The diverter branch combines both control sources with `any_of` so there's a single `out(DiverterCmd)`. This matters: if two separate rungs both `out` the same tag, the last one evaluated wins. A false manual rung below a true auto rung would de-energize the diverter. One `out` per output avoids the problem.

Important: **all conditions evaluate before any instructions execute.** The branch doesn't "see" results of instructions above it in the same rung because each rung starts from a clean snapshot.

## Try it

```python
from pyrung import PLCRunner

runner = PLCRunner(logic)
with runner.active():
    StopBtn.value = True             # NC inputs: True = healthy
    EstopOK.value = True

    Auto.value = True
    StartBtn.value = True
    runner.step()
    assert Running.value is True
    assert ConveyorMotor.value is True

    StartBtn.value = False

    # Auto divert signal (from state machine)
    AutoDivert.value = True
    runner.step()
    assert DiverterCmd.value is True

    # E-stop kills everything (NC opens)
    EstopOK.value = False
    runner.step()
    assert ConveyorMotor.value is False
    assert DiverterCmd.value is False
    assert Running.value is False
```

!!! info "Also known as..."

    OR is a parallel branch of contacts; AND is contacts in series. `branch()` is "parallel branch" everywhere (sometimes with explicit `BST`/`BND` markers). The safety-gate pattern is "Master Control Reset" (`MCR`) in some editors. Seal-in is the classic OR-branch with a series stop contact — every legacy ladder opens with one.

## Exercise

Add a `StatusLight` that is on whenever the conveyor is running in any mode, and a `ManualLight` that is on only in manual mode. Write a test that switches from Auto to Manual mid-run and verifies the diverter control source changes without the motor stopping. Test that the diverter button does nothing in auto mode.

---

Each bin has a count, the diverter has a state, and there's a mode selector. That's a lot of scattered tags. In a real PLC, you'd group them into structures -- UDTs for the bins and the equipment.
