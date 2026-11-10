# Diagnosis

My machine is down. What's wrong? And once I know, how do I get it running again?

These tools need the program and a snapshot, which is a tag dump from the faulted machine. They need no scan history, no test suite and no workflow change. `why()` tells you what's blocking. `how()` tells you the steps to reach your target state.

See also: [Program Structure](analysis-structure.md) (static analysis), [Cause & Effect](analysis-causal.md) (richer results with scan history), [Test Coverage](analysis-coverage.md) (test suite surveys).

## Loading a snapshot

The starting point is a tag dump. For CLICK PLCs, export via **PLC > Read Data from PLC > All > Save** in CLICK Programming Software, then load with `TagMap.load_snapshot()`:

```python
state = mapping.load_snapshot("data.csv")
plc = PLC(logic, initial_state=state)
```

In a project generated from CLICK, pass `blocks=blocks` (exported beside `mapping`) so plain nicknamed addresses are seeded as well. See [Loading PLC state](../dialects/click.md#loading-plc-state) for the full CLICK workflow. For other targets, build the state directly:

```python
from pyrung.core.state import SystemState

plc = PLC(logic, initial_state=SystemState().with_tags(tags))
```

## `why()` — what's blocking this?

`why()` walks the program graph backward from a tag and explains how it reached its current value using only the snapshot.

```python
from pyrung import Bool, And, PLC, Program, rung, out, latch, reset
from pyrung.core.state import SystemState

StartBtn    = Bool(external=True)
Auto        = Bool(external=True)
StopBtn     = Bool(external=True)
EstopOK     = Bool(external=True)
Running     = Bool()
ConveyorMotor = Bool()

with Program() as logic:
    with rung(And(StartBtn, Auto)):
        latch(Running)
    with rung(~StopBtn):
        reset(Running)
    with rung(~EstopOK):
        reset(Running)
    with rung(And(Running, EstopOK)):
        out(ConveyorMotor)

tags = {"StartBtn": True, "Auto": True, "StopBtn": True,
        "EstopOK": True, "Running": True, "ConveyorMotor": True}

plc = PLC(logic, initial_state=SystemState().with_tags(tags))
plc.why(ConveyorMotor)
```

```
ConveyorMotor = True  [why]
  roots: StartBtn, Auto, EstopOK, StopBtn blocks reset
  r1: latch(Running) -- StartBtn, Auto
 *r2: reset(Running) -- blocked StopBtn
 *r3: reset(Running) -- blocked EstopOK
  r4: out(ConveyorMotor) -- Running, EstopOK
```

Each step shows `rN: instruction(tag) -- contacts`. A True Bool shows as just the tag name. A False Bool shows as `TagName(False)`. Tags with choices show the label (`State(IDLE)`). Other non-Bool tags show the raw value (`SizeReading(185)`).

Steps marked `*` are where something NOT happening keeps the tag in its current state. `blocked` means a contact is preventing the rung from firing. If that contact changed, the result would change. `held` means a latch trigger has since cleared. The latch fired in the past, but the reason is no longer active.

`why()` works in both directions with the same call and the same format. It answers "why is this ON?" and "why is this OFF?". When no writer is firing (the tag is at its initial value, or a latch is holding), the per-rung detail collapses to a summary:

```
State = IDLE  [why]
  roots: CmdStart(False), CmdStop(False), Fault(False), CmdReset(False)
  no writer is firing now (5 blocked)
```

### Multiple tags

Pass multiple tags to get one unified explanation:

```python
plc.why(FaultAlarm, MotorStall, CoolingPumpOff)
```

When tags share upstream structure (common in fault cascades), the walk merges at shared internal tags and returns one explanation.

### Confidence

For stateless chains (`out`, `copy`, `math`) and latches whose trigger is still active, the diagnosis is definitive. For latches where the trigger has cleared and there's only one path through the rung condition, the inference is strong. With OR conditions (multiple paths that could have set the latch), each path is equally plausible and reported separately.

Without history, `why()` can't distinguish triggers from enablers. It reports every contributing contact equally. If you have scan history, prefer [`cause()`](analysis-causal.md#recorded-cause-what-caused-this).

## Force and re-query

`why()` is stateless. Change the snapshot and you get a new answer. Use `force()` to test hypotheses:

```python
plc.why(ConveyorMotor)    # "blocked EstopOK" — is that the only problem?
plc.force(EstopOK, True)
plc.step()
plc.why(ConveyorMotor)    # updated explanation with EstopOK forced True
```

The core interactive workflow is a loop: load the dump, call `why()`, force a tag, call `why()` again. Each force simulates a field change. Each `why()` shows what remains.

`why()` names the forces in its answer. A forced input reads `roots: EstopOK forced`. A forced tag with rungs gets its own line, and the rungs below it show what the logic is doing:

```
Y001 = False  [why]
  Y001 is forced False: its rungs can change it during a scan, the force restores it before outputs are written
  roots: X001
  r1: out(Y001) -- X001
```

## `how()` — how do I reach a target state?

!!! note "Experimental"

    `how()` is experimental. When it finds a path, the path is a real recording you can replay. On real programs it sometimes stops without one, and it can take a minute. `Stopped` is not a proof that no path exists. See the headline meanings below.

    `how()` returns a path it has checked, not necessarily the shortest. That is what lets it wait through timer dwells and navigate multi-step machines.

`how()` drives the PLC to a target state the way an engineer would. It reads the program backward to find what needs to change, tests each command on a fork, verifies what moved, and adapts when the program pushes back. It waits through timer dwells, navigates multi-step state machines, and returns to the last good state after a regression. Use it after `why()` to turn a diagnosis into action, or on its own to answer "how do I even start this machine?"

Given a state machine with IDLE, RUNNING, and FAULTED states:

```python
plc.how(State == RUNNING)
```

```
Reached State=running in 2 scans (~20ms).

Steps:

1. Set CmdStart=True.
   Observed: State idle -> running.
```

From a faulted state, the path is longer:

```
Reached State=running in 4 scans (~40ms).

Steps:

1. Set CmdReset=True, Fault=False.
   Observed: State faulted -> idle.

2. Set CmdStart=True.
   Observed: State idle -> running.
```

The headline is deliberately specific:

- `Reached` means the returned recording ends at the target.
- `Cannot reach` means `how()` proved a conflict or physical constraint.
- `Stopped` means it could not identify another safe action. It does not call an unknown path impossible.

For a program with `send()` or `receive()`, see [how() and Modbus](../instructions/communication.md#how).

### Condition syntax

`how()` accepts one or more targets. Each is a Tag (Bool shorthand for `== True`) or a comparison. Several targets, or an `And(...)` of them, are an AND goal. `how()` reaches one state where they all hold at once.

```python
plc.how(State == RUNNING)                        # comparison
plc.how(Running)                                  # Bool shorthand — target is True
plc.how(Running, State == RUNNING)               # AND — a state where both hold
```

If the targets can't coexist, the result says `Cannot reach` and names the conflict. Examples are the same register at two values, or two states whose only writers clobber each other:

```python
plc.how(State == IDLE, State == RUNNING)         # Cannot reach: one register, two values
```

### `avoid`

`avoid X` means do not take a path that depends on X. It excludes routes, operator actions, and observed scan states that satisfy the predicate. It uses the same condition syntax:

```python
plc.how(State == RUNNING, avoid=State == FAULTED)
```

Momentary commands are treated as actions, not just settled states. `avoid=C_Complete` will not *press* `C_Complete` even though the command settles back to rest a scan later. A condition-like avoided state that the path enters transiently is excluded too. A Python callable is not checked on the repeated scans of a long wait. Use a condition when every scan must be constrained. `rise()` and `fall()` are transition predicates rather than states and are not accepted by `avoid=`.

Pass more than one condition, as a tuple or list, for a **union of exclusions**. Each is avoided independently.

```python
plc.how(Burner, avoid=(ProdMode, MaintFault))   # avoid ProdMode OR MaintFault
```

To express a composite prohibition, write `avoid=And(A, B)`. It avoids only the combined state, not A or B on their own.

When every path is excluded the returned `Plan` stops with a reason that names the violated avoid condition(s).

If an `avoid=` callable raises, `how()` returns no plan and raises `AvoidCheckError` (from `pyrung.core`), with the callable's exception as its cause. A state may not have every tag yet, so read tags with `snapshot.get(name, default)`.

### The route taken

A target can have more than one route, such as two writers or an `OR` over internal coils. `how()` never asks you to choose between them. It takes a deterministic default route and tells you where it went on `Plan.route`:

```python
plan = plc.how(Burner)
print(plan)
# Reached Burner=True in 3 scans (~30ms).
# Route: ProdMode
#   Other routes: avoid=ProdMode
#
# Steps:
#
# 1. Set ProdCmd=True.
```

You already know your machine, so exclude the reported route with `avoid=` and `how()` will consider the routes that remain in the current state:

```python
plc.how(Burner, avoid=ProdMode)    # exclude production; maintenance remains
```

Any target that asks for one exact value reports its route, such as `Running == False` with two stop rungs:

```python
plc.how(Running == False, avoid=StopA)   # clear the latch via the other stop
```

## In a debug session

`why` takes space-separated tag names. `how` takes one or more conditions separated by commas, then optionally `avoid` and more conditions.

```
> why Alarm_Horn
> why FaultAlarm MotorStall
> how StateCurrent == 6
> how Running
```

## Next steps

- [Program Structure](analysis-structure.md) — DataView and simplified forms
- [Ladder Lints](ladder-lints.md) — static checks for ladder logic
- [Cause & Effect](analysis-causal.md) — `cause()` and `effect()` over scan history
- [Test Coverage](analysis-coverage.md) — cold rungs, stranded bits, pytest plugin
- [Testing Guide](testing.md) — forces as fixtures, forking, monitors, breakpoints
