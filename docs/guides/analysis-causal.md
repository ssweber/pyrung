# Cause & Effect

What happened, why, and what would happen if? These tools need the program and scan history (or projected mode).

See also: [Program Structure](analysis-structure.md) (static analysis), [Diagnosis](analysis-diagnosis.md) (snapshot-only debugging with `why()` and `how()`), [Test Coverage](analysis-coverage.md) (test suite surveys).

## Recorded cause — what caused this?

```python
with PLC(logic) as plc:
    StartBtn.value = True
    plc.step()

    chain = plc.cause(Running)
```

`cause()` walks backward from `Running`'s most recent transition and returns a `CausalChain`:

```python
chain.mode                  # 'recorded'
chain.effect.tag_name       # 'Running'
chain.effect.to_value       # True
chain.effect.scan_id        # 1

step = chain.steps[0]
step.rung_index             # 0

# What flipped the rung:
step.triggers               # [Transition(StartBtn, 0→1)]

# What was already holding the path open:
step.enablers               # [EnablingCondition(Fault, value=False, held_since=None)]
```

**Trigger** means the contact transitioned and flipped the rung. **Enabler** means it was already in the right state — necessary, but not what changed. The engine figures out which is which automatically.

!!! note "How attribution works"
    The engine converts each rung's condition into a series-parallel (SP) tree, then applies a four-rule post-order walk to identify which contacts mattered for the evaluation. Intersecting "mattered" with the transition log produces the trigger/enabler split.

Recorded steps use `fidelity="full"`. Historical states outside the recent-state cache are reconstructed from retained history, so `history_budget` affects analysis cost rather than the answer. When an interpreted replay journal is available, the chain also preserves exact same-scan occurrence ordering: multiple writes to one tag and parent/subroutine interleaving are attributed to the dynamic writer that produced the committed value.

## Recorded effect — what did this cause?

```python
chain = plc.effect(StartBtn, scan=1)
```

Walks forward from `StartBtn`'s transition at scan 1. Exact replay journals follow the reads and writes in literal execution order. When that journal is unavailable, the engine checks whether the transition mattered to each downstream rung — if the rung would have evaluated the same way without it, the transition is filtered out. Only load-bearing causes propagate forward.

!!! note "Counterfactual evaluation"
    The forward walk uses counterfactual SP evaluation: flip the cause leaf in the rung's SP tree, re-evaluate, and compare to the original result. If the outcome doesn't change, the cause was incidental, not a trigger.

## Projected cause — what *would* cause this?

Add `to=` to switch from "what happened" to "what would need to happen":

```python
with PLC(logic) as plc:
    StartBtn.value = True
    plc.step()

    # Running is now latched TRUE. How could it clear?
    chain = plc.cause(Running, to=False)

    chain.mode   # 'projected' — a reachable path exists
    # StopBtn would need to transition 0→1
```

Projected cause finds rungs that could produce the requested value, checks what conditions would need to hold, and verifies whether the required input transitions have actually been observed in recorded history. When no reachable path exists:

!!! note "Reachability rules"
    Tags that no rung writes to (inputs in the dependency graph sense — buttons, sensors, HMI commands) are always considered reachable, since their value comes from outside the ladder. Tags that the ladder *does* write to are reachable only if they've taken the needed value in recorded history. This catches the common bug ("we wrote a clear rung but never fed it the conditions to fire") without false alarms about hypothetical input sequences.

```python
chain.mode     # 'unreachable'
chain.blockers # [BlockingCondition(rung=1, blocked_contact=StopBtn,
               #   reason=BlockerReason.NO_OBSERVED_TRANSITION)]
```

The blockers explain exactly which inputs the test suite has never demonstrated — either a coverage gap (write the test) or a deliberate omission (operator-only input, not testable from software).

## Projected effect — what *would* happen if...?

```python
chain = plc.effect(StartBtn, from_=False)
# What would happen if StartBtn went TRUE right now?
```

What-if analysis without mutating state.

## `assume={}` — scenario pinning

All three projected methods accept `assume=` to pin tags to specific values during analysis:

```python
plc.cause(Running, to=False, assume={"ResetReady": True})
plc.effect(StartBtn, from_=False, assume={"Guard": True})
plc.recovers(Fault, assume={"ResetBtn": True})
```

The assumed values override the state snapshot before the walker runs, and assumed tags are treated as reachable regardless of history. Three uses:

**Exploration.** REPL sweeps to discover which tests are worth writing:

```python
for tag in fault_tags:
    if not plc.recovers(tag, assume={"ResetBtn": True}):
        print(f"Reset doesn't clear {tag}")
```

**Causal assertions in tests.** Assert the ladder actually connects inputs to outputs:

```python
assert plc.cause("Motor_Running",
                 assume={"StartBtn": True, "EStop": False})
assert not plc.cause("Motor_Running",
                     assume={"EStop": True})
```

**External tag reasoning.** Tags marked `external=True` normally return `True` from `recovers()` by declaration. With `assume=`, the shortcut is skipped and the analysis runs, so you can verify the recovery path works with specific inputs:

```python
assert plc.recovers("Alarm_Ack", assume={"Alarm_Ack": False})
```

`assume=` on a `readonly` tag raises `ValueError` — the tag is declared constant, so pinning it to a different value contradicts the declaration. `external` and `final` tags are fine to assume.

`assume=` requires projected mode. Using it without `to=` on `cause()` or without `from_=` on `effect()` raises `ValueError`.

## `recovers()` — can this bit clear?

```python
assert plc.recovers(Running)   # True if a clear path exists
```

Convenience predicate over `cause()`. For the diagnostic on failure, use `cause()` directly:

```python
chain = plc.cause(Running, to=False)
assert chain.mode != "unreachable", chain
```

## Next steps

- [Program Structure](analysis-structure.md) — DataView, simplified forms, static validators
- [Diagnosis](analysis-diagnosis.md) — snapshot-only debugging with `why()` and `how()`
- [Test Coverage](analysis-coverage.md) — cold rungs, stranded bits, pytest plugin
- [Runner Guide](runner.md) — execution methods, history, time travel
