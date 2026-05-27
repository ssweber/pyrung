# Diagnosis

My machine is down. What's wrong? These tools need the program and a snapshot — a tag dump from the faulted machine. No scan history required.

See also: [Program Structure](analysis-structure.md) (static analysis), [Cause & Effect](analysis-causal.md) (richer results with scan history), [Test Coverage](analysis-coverage.md) (test suite surveys).

## `why()` — what happened without history?

`why()` needs only a snapshot — load a tag dump from a faulted machine and get the causal path from program structure alone. For loading Click PLC data dumps, see [Loading PLC state](../dialects/click.md#loading-plc-state).

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
  r0: latch(Running) -- StartBtn, Auto
 *r1: reset(Running) -- blocked StopBtn
 *r2: reset(Running) -- blocked EstopOK
  r3: out(ConveyorMotor) -- Running, EstopOK
```

Each step shows `rN: instruction(tag) -- contacts`. Bool True is implicit (just the tag name), False is explicit (`TagName(False)`). Tags with choices show the label (`State(IDLE)`), other non-Bool tags show the raw value (`SizeReading(185)`).

Steps with a `*` prefix are abnormal — the rung state contradicts what you'd naively expect. `blocked` on a contact means it's preventing a reset rung from firing. `held` means a latch trigger that has since cleared.

### Both directions

`why()` handles "why is this ON?" and "why is this OFF?" equally:

```python
plc.why(ConveyorMotor)  # Motor is OFF — what's blocking it?
```

### Multiple tags

Pass multiple tags to get one unified explanation:

```python
plc.why(FaultAlarm, MotorStall, CoolingPumpOff)
```

When tags share upstream structure (common in fault cascades), the walk merges at shared internal tags — one explanation, not three.

### Confidence

For stateless chains (`out`, `copy`, `calc`) and latches whose trigger is still active, the diagnosis is definitive. For latches where the trigger has cleared and there's only one path through the rung condition, the inference is strong. With OR conditions (multiple paths that could have set the latch), each path is equally plausible and reported separately.

Without history, `why()` can't distinguish triggers from enablers — it reports every contributing contact equally. If you have scan history, prefer [`cause()`](analysis-causal.md#recorded-cause-what-caused-this).

### In a debug session

```
> why Alarm_Horn
> why FaultAlarm MotorStall
```

## `how()` — how do I reach a target state?

`how()` answers the follow-up question: now that I know what's wrong, what's the minimum sequence of external input changes to reach a target state?

```python
plc.explore()
path = plc.how(StateCurrent == S.EXECUTE)
```

`explore()` builds the full transition graph via BFS — call it once, then query as many times as you need. `how()` finds the shortest path through the graph.

```
> how State_Execute
> how Tag1 Tag2
```

See also: [Verification](verification.md) for the underlying state-space exploration.

## Next steps

- [Program Structure](analysis-structure.md) — DataView, simplified forms, static validators
- [Cause & Effect](analysis-causal.md) — `cause()` and `effect()` over scan history
- [Test Coverage](analysis-coverage.md) — cold rungs, stranded bits, pytest plugin
- [Testing Guide](testing.md) — forces as fixtures, forking, monitors, breakpoints
