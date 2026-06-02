# Diagnosis

My machine is down. What's wrong? And once I know — how do I get it running again?

These tools need the program and a snapshot — a tag dump from the faulted machine. No scan history, no test suite, no workflow change. `why()` tells you what's blocking, `how()` tells you the steps to reach your target state.

See also: [Program Structure](analysis-structure.md) (static analysis), [Cause & Effect](analysis-causal.md) (richer results with scan history), [Test Coverage](analysis-coverage.md) (test suite surveys).

## Loading a snapshot

The starting point is a tag dump. For Click PLCs, export via **Data > Read Data from PLC > All > Save** in Click Programming Software, then load with `TagMap.load_snapshot()`:

```python
state = mapping.load_snapshot("data.csv")
plc = PLC(logic, initial_state=state)
```

See [Loading PLC state](../dialects/click.md#loading-plc-state) for the full Click workflow. For other targets, build the state directly:

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

Each step shows `rN: instruction(tag) -- contacts`. Bool True is implicit (just the tag name), False is explicit (`TagName(False)`). Tags with choices show the label (`State(IDLE)`), other non-Bool tags show the raw value (`SizeReading(185)`).

Steps marked `*` are where something NOT happening keeps the tag in its current state. `blocked` means a contact is preventing the rung from firing — if it changed, so would the result. `held` means a latch trigger that has since cleared — the latch fired in the past but the reason is no longer active.

`why()` works in both directions — "why is this ON?" and "why is this OFF?" — same call, same format. When no writer has fired (the tag is at its initial value), the per-rung detail collapses to a summary:

```
State = IDLE  [why]
  roots: CmdStart(False), CmdStop(False), Fault(False), CmdReset(False)
  no writer has fired (5 blocked)
```

### Multiple tags

Pass multiple tags to get one unified explanation:

```python
plc.why(FaultAlarm, MotorStall, CoolingPumpOff)
```

When tags share upstream structure (common in fault cascades), the walk merges at shared internal tags and returns one explanation.

### Confidence

For stateless chains (`out`, `copy`, `calc`) and latches whose trigger is still active, the diagnosis is definitive. For latches where the trigger has cleared and there's only one path through the rung condition, the inference is strong. With OR conditions (multiple paths that could have set the latch), each path is equally plausible and reported separately.

Without history, `why()` can't distinguish triggers from enablers — it reports every contributing contact equally. If you have scan history, prefer [`cause()`](analysis-causal.md#recorded-cause-what-caused-this).

## Force and re-query

`why()` is stateless — change the snapshot, get a new answer. Use `force()` to test hypotheses:

```python
plc.why(ConveyorMotor)    # "blocked EstopOK" — is that the only problem?
plc.force(EstopOK, True)
plc.step()
plc.why(ConveyorMotor)    # updated explanation with EstopOK forced True
```

This loop — load dump, `why()`, force a tag, `why()` again — is the core interactive workflow. Each force simulates a field change; each `why()` shows what remains.

## `how()` — how do I reach a target state?

`how()` returns the minimum sequence of external input changes to reach a target state. Use it after `why()` to turn a diagnosis into action, or on its own to answer "how do I even start this machine?"

Given a state machine with IDLE, RUNNING, and FAULTED states:

```python
plc.how(State == RUNNING)
```

```
Path (1 step(s), 1 input change(s)):
  Step 1: CmdStart=True  (1 scan(s))
```

From a faulted state, the path is longer:

```
Path (2 step(s), 3 input change(s)):
  Step 1: CmdReset=True, Fault=False  (1 scan(s))
  Step 2: CmdStart=True  (1 scan(s))
```

### Condition syntax

Same grammar as `rung()`, `always()`, `run_until()`. Multiple positional args are implicit AND:

```python
plc.how(State == RUNNING)                        # single condition
plc.how(State == RUNNING, Fault == False)         # implicit AND
plc.how(Running)                                  # Bool shorthand — target is True
```

### `avoid`

Exclude states from the path search. Uses the same condition syntax:

```python
plc.how(State == RUNNING, avoid=State == FAULTED)
```

`avoid` filters stable states — transient states that resolve within a single scan can't be avoided because they're never observable between scans.

## In a debug session

`why` takes space-separated tag names. `how` takes a condition expression: commas for implicit AND, `And()`/`Or()` for grouping, `~` for negation, comparisons with `==`/`!=`/`<`/`>`.

```
> why Alarm_Horn
> why FaultAlarm MotorStall
> how StateCurrent == 6
> how Running, ~Fault
> how Or(StateCurrent == 2, StateCurrent == 6)
```

## Next steps

- [Program Structure](analysis-structure.md) — DataView, simplified forms, static validators
- [Cause & Effect](analysis-causal.md) — `cause()` and `effect()` over scan history
- [Test Coverage](analysis-coverage.md) — cold rungs, stranded bits, pytest plugin
- [Testing Guide](testing.md) — forces as fixtures, forking, monitors, breakpoints
