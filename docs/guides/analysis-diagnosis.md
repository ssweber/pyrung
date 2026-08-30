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

`how()` drives the PLC to a target state the way an engineer would — it reads the program backward to find what needs to change, tests each command on a fork, verifies what moved, and adapts when the program pushes back. It waits through timer dwells, navigates multi-step state machines, and returns to the last good state after a regression. Use it after `why()` to turn a diagnosis into action, or on its own to answer "how do I even start this machine?"

Given a state machine with IDLE, RUNNING, and FAULTED states:

```python
plc.how(State == RUNNING)
```

```
Reached State=running in 2 scans (~20ms).

Steps:

1. Pulse CmdStart=True.
   Observed: State idle -> running.
```

From a faulted state, the path is longer:

```
Reached State=running in 4 scans (~40ms).

Steps:

1. Pulse CmdReset=True, Fault=False.
   Observed: State faulted -> idle.

2. Pulse CmdStart=True.
   Observed: State idle -> running.
```

The headline is deliberately specific:

- `Reached` means the returned recording ends at the target.
- `Cannot reach` means `how()` proved a conflict or physical constraint.
- `Stopped` means it could not identify another safe action. It does not call an unknown path impossible.

The debug console reports long-running work as it happens. Trials and investigations are deliberately streamed as unfinished sentences, then completed by the result event:

```
Pulse CmdStart=True... done.
  State jumped 6 -> 10 Checking... unexpected.
  Preventable? Yes -- with rung(State == 3): latch(DoorClosed).
```

`Pulse CmdStart=True...` is emitted before its trial; ` done.` is appended only after acceptance. `Checking...` is emitted before stable-landing analysis. `Preventable?` is emitted before causal replay, and the replay result is appended on the same line when the investigation returns. A long investigation therefore reads as active work instead of a hung console.

### Condition syntax

Accepts one or more targets — each a Tag (Bool shorthand for `== True`) or a comparison. Several targets are an AND goal: `how()` reaches one state where they all hold at once.

```python
plc.how(State == RUNNING)                        # comparison
plc.how(Running)                                  # Bool shorthand — target is True
plc.how(Running, State == RUNNING)               # AND — a state where both hold
```

If the targets can't coexist — the same register at two values, or two states whose only writers clobber each other — the result says `Cannot reach` and names the conflict:

```python
plc.how(State == IDLE, State == RUNNING)         # Cannot reach: one register, two values
```

`avoid=` works with multiple targets too — the same exclusions apply while
PILOT works toward each target.

### `avoid`

`avoid X` = do not take a path that depends on X. It excludes routes, operator actions, and observed scan states that satisfy the predicate. Uses the same condition syntax:

```python
plc.how(State == RUNNING, avoid=State == FAULTED)
```

Momentary commands are treated as actions, not just settled states — `avoid=C_Complete` will not *press* `C_Complete` even though the command settles back to rest a scan later. A condition-like avoided state that the path enters transiently is excluded too: PILOT carries the condition into folded trial coasts, so there is no two-scan wink where it blips true and settles false again. An opaque Python callable has no readable condition for folding; it is checked at trial endpoints, retained real snapshots, and kernel scans the coast actually executes, but not logical scans skipped by a fold. Use condition syntax when every logical scan must be constrained. `rise()` and `fall()` are transition predicates rather than states and are not accepted by `avoid=`.

Pass more than one condition — a tuple or list — for a **union of exclusions**: each is avoided independently.

```python
plc.how(Burner, avoid=(ProdMode, MaintFault))   # avoid ProdMode OR MaintFault
```

Express a composite prohibition explicitly: `avoid=And(A, B)` avoids only the combined state, not A or B on their own.

When every path is excluded the returned `Plan` stops with a reason that names the violated avoid condition(s).

### The route taken

When a target can be reached more than one way — two writers, or an `OR` over internal coils — `how()` never asks you to disambiguate. It takes a deterministic default route and tells you where it went on `Plan.route`:

```python
plan = plc.how(Burner)
print(plan)
# Reached Burner=True in 3 scans (~30ms).
# Route: ProdMode
#   Other routes: avoid=ProdMode
#
# Steps:
#
# 1. Pulse ProdCmd=True.
```

You already know your machine, so exclude the reported route with `avoid=` and PILOT will read the remaining current-world alternatives:

```python
plc.how(Burner, avoid=ProdMode)    # exclude production; maintenance remains
```

The route report is the same for any concrete value target, not just `Bool == True`. A word target that two modes drive (`copy(5, State)` under `Or(ProdMode, MaintMode)`) and a `Bool == False` target with two reset writers both report a `Plan.route`, and the chosen route can be excluded the same way:

```python
plc.how(Running == False, avoid=StopA)   # clear the latch via the other stop
```

A relational target (`State > 5`) has no frozen value to route over, so it never carries a `Plan.route`.

A fork that's a plain choice of inputs (`Or(Auto, Manual)`) is taken silently — there's nothing to report — so `Plan.route` is `None`.

## In a debug session

`why` takes space-separated tag names. `how` takes a single condition expression with comparisons (`==`/`!=`/`<`/`>`).

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
