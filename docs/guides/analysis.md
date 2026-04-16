# Analysis

pyrung's scan engine records every state snapshot. The analysis tools turn that history into answers: what does my program touch, why did something happen, and is my test suite covering the program?

Three layers, each building on the last:

- **`plc.dataview`** — static structure. What tags exist, how they connect, what role they play.
- **`plc.cause()` / `plc.effect()`** — dynamic behavior. What caused a transition, what it caused downstream, and what-if projections.
- **`plc.query`** — test coverage. Which rungs never fired, which latched bits have no clear path.

All three work in plain pytest. No VS Code required.

## DataView: what does my program touch?

`plc.dataview` returns a chainable query over the program's static dependency graph. No scans needed — it reads the program structure directly.

```python
from pyrung import Bool, PLC, Program, Rung, And, latch, reset, out

StartBtn    = Bool("StartBtn")
StopBtn     = Bool("StopBtn")
Fault       = Bool("Fault")
Running     = Bool("Running")
MotorOut    = Bool("MotorOut")

with Program() as logic:
    with Rung(And(StartBtn, ~Fault)):
        latch(Running)
    with Rung(StopBtn):
        reset(Running)
    with Rung(Running):
        out(MotorOut)

with PLC(logic) as plc:
    dv = plc.dataview
```

### Role filters

Every tag gets a role based on its position in the dependency graph:

```python
dv.inputs()      # only read, never written by logic — your physical inputs
dv.pivots()      # both read and written — internal state
dv.terminals()   # only written, never read — your physical outputs
dv.isolated()    # neither read nor written by any rung
```

Filters chain. `.inputs().contains("btn")` narrows to input tags matching "btn".

### Name matching

`.contains()` does abbreviation-aware fuzzy matching:

```python
dv.contains("cmd")      # finds CommandRun, Cmd_Reset, etc.
dv.contains("motor")    # finds MotorOut, ConveyorMotor, etc.
```

It splits on camelCase and underscores, then expands both sides into consonant abbreviations — `"cmd"` finds `CommandRun`, and `"command"` finds `Cmd_Reset`.

### Dependency slicing

```python
dv.upstream("MotorOut")    # everything that can affect MotorOut
dv.downstream("StartBtn")  # everything StartBtn can affect
```

These return narrowed DataViews, so you can chain further:

```python
dv.inputs().upstream("MotorOut")  # which inputs feed into MotorOut?
```

### Iteration

DataView is iterable and supports `len`, `in`, and `bool`:

```python
for tag_name in dv.inputs():
    print(tag_name)

assert "StartBtn" in dv
assert len(dv.pivots()) > 0
```

`.tags` returns the underlying `frozenset` of tag names. `.roles()` returns a `dict[str, TagRole]`.

### Static use without a runner

`program.dataview()` returns the same thing without needing a `PLC`:

```python
dv = logic.dataview()   # works directly on the Program
```

Useful in test utilities or static analysis scripts that don't need to run scans.

## Cause and effect: why did this happen?

After running some scans, `plc.cause()` and `plc.effect()` explain what happened and why.

### Recorded cause — what caused this?

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
step.proximate_causes       # [Transition(StartBtn, 0→1)]

# What was already holding the path open:
step.enabling_conditions    # [EnablingCondition(Fault, value=False, held_since=None)]
```

**Proximate** means the contact transitioned and flipped the rung. **Enabling** means it was already in the right state — necessary, but not what changed. The engine figures out which is which automatically.

!!! note "How attribution works"
    The engine converts each rung's condition into a series-parallel (SP) tree, then applies a four-rule post-order walk to identify which contacts mattered for the evaluation. Intersecting "mattered" with the transition log produces the proximate/enabling split.

### Recorded effect — what did this cause?

```python
chain = plc.effect(StartBtn, scan=1)
```

Walks forward from `StartBtn`'s transition at scan 1. For each downstream rung, the engine checks whether the transition actually mattered — if the rung would have evaluated the same way without it, the transition is filtered out. Only load-bearing causes propagate forward.

!!! note "Counterfactual evaluation"
    The forward walk uses counterfactual SP evaluation: flip the cause leaf in the rung's SP tree, re-evaluate, and compare to the original result. If the outcome doesn't change, the cause was incidental, not proximate.

### Projected cause — what *would* cause this?

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

### Projected effect — what *would* happen if...?

```python
chain = plc.effect(StartBtn, from_=False)
# What would happen if StartBtn went TRUE right now?
```

What-if analysis without mutating state.

### `recovers()` — can this bit clear?

```python
assert plc.recovers(Running)   # True if a clear path exists
```

Convenience predicate over `cause()`. For the diagnostic on failure, use `cause()` directly:

```python
chain = plc.cause(Running, to=False)
assert chain.mode != "unreachable", chain
```

## Query: is my test suite covering the program?

`plc.query` runs whole-program surveys across recorded history.

### Cold and hot rungs

```python
with PLC(logic) as plc:
    StartBtn.value = True
    plc.run(cycles=10)

    plc.query.cold_rungs()   # rung indices that never fired
    plc.query.hot_rungs()    # rung indices that fired every scan
```

Cold rungs are dead code or untested paths. Hot rungs may indicate always-true conditions worth reviewing.

### Stranded bits

```python
stranded = plc.query.stranded_bits()
```

Returns `CausalChain` objects for each latched tag with no reachable reset path. Each chain carries blocker diagnostics pointing at the specific inputs that would need to transition.

### Coverage reports and merge

Individual test findings are mostly noise — a single test only exercises a slice of the program. The signal emerges when you merge findings across a test suite.

```python
from pyrung.core.analysis.query import CoverageReport

def test_start_stop(plc):
    StartBtn.value = True
    plc.run(cycles=5)
    StopBtn.value = True
    plc.step()
    return plc.query.report()

def test_fault_handling(plc):
    plc.force(Fault, True)
    plc.run(cycles=5)
    return plc.query.report()
```

`CoverageReport.merge()` combines findings across tests:

```python
merged = report_a.merge(report_b)
```

Negative findings (cold rungs, stranded bits) merge by **intersection** — a rung is only cold in the merged view if *no* test fired it. Each test you add can only shrink the residuals. What remains after the full suite is what you actually need to investigate.

Stranded bits merge by chain identity (tag + blocker fingerprint), so "stranded for a different reason" after a refactor is a distinct signal from "still stranded."

### Pytest plugin

The manual merge above works, but the `pyrung.pytest_plugin` handles it automatically. Enable it in your `conftest.py`:

```python
pytest_plugins = ["pyrung.pytest_plugin"]
```

Then wire the `pyrung_coverage` fixture into your PLC fixture:

```python
@pytest.fixture
def plc(pyrung_coverage):
    with PLC(logic, dt=0.1) as p:
        yield p
        pyrung_coverage.collect(p)
```

Every test that uses `plc` contributes a report. At session end, the plugin merges all reports and writes `pyrung_coverage.json`:

```json
{
  "cold_rungs": [22, 91],
  "hot_rungs": [0, 2, 3],
  "stranded_chains": []
}
```

Control the output path with `--pyrung-coverage-json`:

```bash
pytest --pyrung-coverage-json=build/coverage.json   # custom path
pytest --pyrung-coverage-json=                       # disable output
```

### Whitelist and CI gating

A TOML whitelist declares known-acceptable findings — cold rungs you've decided are dormant by design, stranded bits that are operator-only and not testable from software:

```toml
# pyrung_whitelist.toml

[cold_rungs]
allow = [22, 91, 104]

[stranded_chains]
allow = ["Sts_SpecialFault", "Sts_ManualReset"]
```

Pass it with `--pyrung-whitelist`:

```bash
pytest --pyrung-whitelist=pyrung_whitelist.toml
```

New findings not in the whitelist fail the session (exitstatus 1) and print a summary:

```
=============================== pyrung coverage ===============================
New cold rungs not in whitelist: [200, 201]
New stranded bits not in whitelist: ['Sts_NewFault']
```

The whitelist keys stranded bits by tag name only — not by blocker fingerprint. If a refactor changes *why* a bit is stranded, the whitelist still covers it, but the JSON report's chain identity will differ, surfacing the change for review.

With one test, cold rungs and stranded bits are mostly noise. After hundreds of tests, anything still in the residual has had hundreds of chances to be exercised and wasn't. That's where the whitelist becomes a short list of deliberate decisions rather than a pile of false positives.

## Static validators

Separate from the runtime analysis, two static validators check program structure at build time — no scans needed.

### Conflicting outputs

```python
from pyrung.core.validation.duplicate_out import validate_conflicting_outputs

report = validate_conflicting_outputs(logic)
```

Detects when multiple instructions that actively write every scan (`out`, timers, counters, drums, shift registers) target the same tag from non-mutually-exclusive execution paths. These instructions reset their target when disabled, so two `out(Motor)` in the same scope means last-writer-wins stomping every scan.

The only safe pattern is different subroutines whose callers have provably exclusive conditions (e.g., `State == 1` vs `State == 2`).

!!! note "Exclusivity patterns recognized"
    The validator detects `CompareEq` different-constant pairs, `BitCondition`/`NormallyClosedCondition` complements, and range-complement pairs (`Lt`/`Ge`, `Le`/`Gt`) on caller conditions.

### Stuck bits

```python
from pyrung.core.validation.stuck_bits import validate_stuck_bits

report = validate_stuck_bits(logic)
```

Detects latch/reset imbalance: `CORE_STUCK_HIGH` when a tag is latched but never reset anywhere in the program, `CORE_STUCK_LOW` when a tag is reset but never latched. Covers subroutine boundaries. Skips `readonly` and `external` tags.

This is the static complement to `plc.query.stranded_bits()` — stuck-bits checks structure ("is there a reset rung at all?"), while stranded-bits checks reachability ("is there a reset rung *and can it actually fire*?").

### Read-only writes

```python
from pyrung.core.validation.readonly_write import validate_readonly_write

report = validate_readonly_write(logic)
```

Flags any write instruction targeting a `readonly=True` tag as `CORE_READONLY_WRITE`. Read-only tags are initialized from their declared default at power-on and should never be written by ladder logic.

### Choices violations

```python
from pyrung.core.validation.choices_violation import validate_choices_violation

report = validate_choices_violation(logic)
```

Checks every literal-value write against the tag's `choices` key set. If a `copy(5, State)` writes a value not in `State`'s choices map, it's flagged as `CORE_CHOICES_VIOLATION`.

### Final multiple writers

```python
from pyrung.core.validation.final_writers import validate_final_writers

report = validate_final_writers(logic)
```

Counts write sites for `final=True` tags. If more than one instruction writes the same `final` tag, it's flagged as `CORE_FINAL_MULTIPLE_WRITERS` — regardless of whether the writers are mutually exclusive. This is stricter than the conflicting-outputs validator, which allows multi-writers behind exclusive conditions.

## Next steps

- [Testing Guide](testing.md) — forces as fixtures, forking, monitors, breakpoints
- [Runner Guide](runner.md) — execution methods, history, time travel
- [Forces & Debug](forces-debug.md) — force semantics, breakpoints, history
