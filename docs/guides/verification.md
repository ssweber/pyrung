# Verification

The [analysis tools](analysis.md) answer questions about recorded history — what happened, why, and did your tests cover the program. Verification answers a different question: **does a property hold across every reachable state, not just the states your tests happened to visit?**

```python
from pyrung import Bool, Program, rung, latch, reset
from pyrung.core.analysis import never, Proven

EstopOK = Bool(external=True)
Start   = Bool(external=True)
Running = Bool()

with Program(strict=False) as logic:
    with rung(Start, EstopOK):
        latch(Running)
    with rung(~EstopOK):
        reset(Running)

result = never(logic, Running, ~EstopOK)
assert isinstance(result, Proven)
```

`never()` proves a bad state is unreachable — `never(logic, Running, ~EstopOK)` means "the motor is never running without the e-stop being OK." It exhaustively explores every reachable state via BFS over the compiled replay kernel. If the property holds everywhere, you get `Proven`. If not, `Counterexample` with a trace you can replay on a real PLC: the verifier has already replayed it on the concrete PLC before reporting it.

`always()` is the general form — `always(logic, Or(~Running, EstopOK))` is equivalent but reads less naturally for safety properties. Use `always()` when the property isn't a "bad state" pattern (e.g., `always(logic, lambda s: s["X"] + s["Y"] < 100)`).

## Condition syntax

`never()` and `always()` accept the same condition expressions as `rung()` and `when()`:

```python
never(logic, Running, ~EstopOK)           # bad-state: "never running without e-stop"
always(logic, ~Running, EstopOK)          # implicit AND (same property, always form)
always(logic, lambda s: s["X"] + s["Y"] < 100)  # callable fallback
```

Condition expressions are preferred — the verifier extracts referenced tags and automatically restricts input enumeration to the upstream cone. A callable names no tags, so the verifier can't tell what it reads: without `scope=` it explores every input on every scan, which is slower. Pass `scope=` with the tags the callable reads and the verifier treats that list as its read set, exploring every input upstream of those tags. A callable that reads a tag outside its `scope=` can miss a violation.

### Result types

```python
from pyrung.core.analysis import Proven, Counterexample, Intractable

result = never(logic, Running, ~EstopOK)

if isinstance(result, Proven):
    print(f"Holds across {result.states_explored} states")

elif isinstance(result, Counterexample):
    # Replay the trace on a real PLC
    with PLC(logic, dt=0.010) as plc:
        for step in result.trace:
            plc.patch(step.inputs)
            for _ in range(step.scans):
                plc.step()
    # The violation is now visible in plc state

elif isinstance(result, Intractable):
    print(result.reason)  # "unbounded domain on Pressure"
    print(result.tags)    # ["Pressure"] — add choices or min/max
    for hint in result.hints:
        print(hint)       # "Pressure: 65536 values (Int, no choices/min/max)"
```

`Intractable` means the verifier could not decide. `result.kind` says why:

- `"search"` — the state space is too large. The `hints` list describes the largest dimensions — which tags are blowing up the state space and why. The fix is usually adding `choices` or `min`/`max` metadata to the unbounded tags — the same metadata you'd declare anyway for Data View dropdowns and static validation.
- `"timing"` — unconfirmed. The search found a violation, but replaying its trace on the concrete PLC (at the verifier's fixed `dt`, 10 ms) did not reach it: the violation needs a timer to finish on a scan the PLC's timing does not give, like a 30 ms timer done before a 1000 ms one started with it. `result.tags` names the timers, `result.trace` keeps the search's trace (each step's `timers` lists the ticks it moved) and `result.diverged_at` is the step where the replay left it. Whether it can happen depends on timer timing, which the verifier does not model (see [How it works](#how-it-works)): check it with a `run_for()` test.
- `"timers"` — so many timers run at once that the ways they can finish on one scan pass the verifier's cap. Narrowing `choices`/`min`/`max` does not help; prove a property whose scope reaches fewer of them, or test the timing with `run_for()`.
- `"property"` — the property's own `rise()`/`fall()`, which the search counts as possibly true on every scan, gave a counterexample the concrete PLC did not reproduce. Latch or one-shot the edge in the program and name that coil in the property instead.

Every `Counterexample` is confirmed the same way before it is reported: its trace is the one the concrete PLC followed to the violation, waits folded into multi-scan steps. The one exception is a trace that only drives free system tags (see [System tags](#system-tags)), which keeps its caveat instead.

### Scoping

With condition expressions, scope is derived automatically from the referenced tags. Override with `scope=` when needed:

```python
never(logic, Running, ~EstopOK, scope=["Running", "EstopOK"])
```

Scoping restricts input enumeration to the upstream cone of the named tags — the verifier only explores inputs that can actually influence the property.

### How it works

The verifier classifies every tag into one of three roles:

- **Combinational** — OTE-only writes, derived from inputs each scan and read only below their writer. Not a state dimension. A coil a rung reads above the rung that writes it sees the previous scan's value, so it is stateful.
- **Stateful** — latch/reset, timer/counter, copy, calc. Tracked in the visited set.
- **Nondeterministic** — external inputs. Enumerated at each state. `InputBlock` tags (e.g., `x[1]`), `receive()` destination tags, and semantic tags mapped to input banks via `TagMap` are all automatically nondeterministic — no `external=True` annotation required.

Value domains come from the expression tree: comparison literals in conditions, `choices` metadata, `min`/`max` bounds. A tag compared against `== 1` and `== 2` gets domain `{1, 2, unmatched}` — three values instead of 65K.

Don't-care pruning skips inputs that are masked by the current state. `And(StateBit, Input)` with `StateBit=False` means `Input` doesn't matter — the verifier skips it entirely.

`run_function` and `run_enabled_function` are opaque to the verifier — it cannot introspect or symbolically execute user-provided Python functions. Output tags are tracked as state-producing writes, but their value domains must come from tag metadata. Without `choices=` or `min=/max=` on the output tags, `always()` returns `Intractable`:

```python
# Intractable — unbounded output domain
result = Int("Result")
run_function(my_func, outs={"r": result})

# Verifiable — domain declared via metadata
result = Int("Result", min=0, max=100)
run_function(my_func, outs={"r": result})
```

Timers are black boxes. A timer's Done may change on any scan its instruction allows: an enabled `on_delay` may finish on any scan and then stays done, a disabled `off_delay` may drop on any scan, a running `time_drum` or `time_event_drum` step may end on any scan, and the Out family's `debounce`, `flasher` and `timed` coils time the same way. Enable, reset and preset logic stay exact, and a tag preset is simply read. That covers everything a real timer can do under any scan time and any preset value, so a `Proven` holds whatever the timing. An accumulator the program compares takes the values its comparisons split at; one read as a value (copied, computed with, read by a callable property) may take any value. Counters count rising edges and are tracked exactly: counting is not time.

The search therefore also finds violations only impossible timing reaches. That is why every counterexample is replayed on the concrete PLC first, and why one that does not replay comes back as an `Intractable` of kind `"timing"` rather than a `Counterexample` (see [Result types](#result-types)).

By default, `always()` checks predicates on every reachable state, including transient states where a timer is still running. For timer-gated alarm properties, pass `settled=True` to evaluate predicates only in states where no timer's Done is still open: every enabled on-delay done, every disabled off-delay off, every running drum step finished:

```python
# Without settled: Counterexample from the transient state before the timer fires
result = never(logic, Cmd, ~Fb, ~Alarm)
assert isinstance(result, Counterexample)

# With settled: judged only once the fault timer is done — the alarm has latched
result = never(logic, Cmd, ~Fb, ~Alarm, settled=True)
assert isinstance(result, Proven)
```

`settled=True` only skips states where a timer could still finish. A violation in a state where no timer is running is still reported, and its replay is accepted only in a state the PLC has settled the same way.

### System tags

System tags read as they do in a fixed-step run at the verifier's `dt`, whether a rung or only the property reads them. `sys.first_scan` is True on the first scan only, and `sys.scan_time_min_ms` and `sys.scan_time_max_ms` read 0 on that scan and the fixed scan time after. `sys.always_on`, `sys.scan_time_current_ms` and `sys.mode_run` are constants, and fault and command bits reset at the start of each scan. When the program writes `sys.cmd_mode_stop`, `sys.mode_run` becomes a free input instead; a stop written from outside the program, over Modbus or from an HMI, is not modeled.

The clocks, `sys.scan_counter`, `sys.scan_clock_toggle` and the RTC fields depend on how long the PLC has run, and `sys.battery_present` is set by the environment, so the verifier explores them as free inputs that can take any value on any scan, within the calendar range for the RTC fields. A `Proven` result holds for every timing; a `Counterexample` that drives one of them carries a caveat, since the PLC may never produce that sequence. A callable property can read the constants; reading a free system tag that no rung reads returns `Intractable`, so write that property as a condition.

### Debugging with journals

Pass `journal=True` to get a per-tag decision trail showing how the verifier classified or elided each tag:

```python
result = always(logic, condition, journal=True)
print(result.journal)
```

The `Journal` object is a mapping from tag name to `TagEntry`. Each entry records the tag's final `outcome` (e.g. `"stateful"`, `"elided:concrete"`, `"excluded:readonly"`), its `domain`, and the chain of `Decision` objects from each pass that touched it. Useful for diagnosing why the verifier returned an unexpected result — especially when optimized and unoptimized runs disagree.

Available on all three result types (`Proven`, `Counterexample`, `Intractable`). When `journal=False` (default), `result.journal` is `None` and there is no overhead.

### Splitting coupled state

When independent zones share a single coupling tag (a mode selector, an auto/manual latch), the verifier can't factor them apart — every zone's inputs look dependent because they all read the shared tag. The state space is the full cross-product.

`split_at` promotes a stateful tag to a nondeterministic input so the verifier explores it at all values independently. The remaining inputs become separable:

```python
result = always(logic, condition, split_at=["AutoMode"])
states = reachable_states(logic, split_at=["AutoMode"])
```

The verifier explores each independent group separately, then composes results via delta merge — `O(shared × sum of groups)` kernel evaluations instead of `O(product of all inputs)`.

Split candidates must be Bool, Done-paired, or `choices=` tags. External inputs (already nondeterministic), `rise()`/`fall()` targets, and unbounded-domain tags are rejected with a clear error.

Because splitting over-approximates (it explores values the tag might never reach in practice), a `Counterexample` from a split run includes a caveat. `Proven` results are sound — if the property holds across all split-tag values, it holds across all reachable values too.

`split_at` is also available in `__lock__`:

```python
__lock__ = {
    "split_at": ["AutoMode"],
}
```

When the verifier returns `Intractable`, the `hints` list will suggest `split_at` candidates it detected — stateful tags whose promotion would enable factoring.

## Fault coverage

The harness knows every device coupling. `Harness.couplings()` iterates them as `Coupling` dataclasses so you can automate fault coverage without maintaining a manual device list:

```python
from pyrung import Coupling, Harness, PLC

plc = PLC(logic, dt=0.001)
harness = Harness(plc)
harness.install()

for coupling in harness.couplings():
    print(coupling.en_name, "→", coupling.fb_name)
```

Each `Coupling` has `en_name`, `fb_name`, `physical`, and `trigger_value` (None for plain bool links, the matched value for `link="Tag:value"` triggers).

Fault coverage decomposes into two passes over the same coupling list:

**Structural coverage** — does a path from the fault to an alarm exist at all? `always()` answers this exhaustively. Batch all conditions into a single call — the verifier shares work across properties:

```python
from pyrung.core.analysis import always, Proven, Counterexample

couplings = list(harness.couplings())
conditions = [
    Or(~plc.tags[c.en_name], plc.tags[c.fb_name], AlarmExtent != 0)
    for c in couplings
]
results = always(logic, conditions, settled=True)

for coupling, result in zip(couplings, results):
    assert isinstance(result, Proven), f"{coupling.fb_name}: no alarm path"
```

Each condition reads: "in every reachable state, either the enable is off, the feedback is healthy, or the alarm caught it." A `Counterexample` means there exists a reachable state where the feedback has failed and no alarm fired — a structural detection gap. `settled=True` suppresses transient violations during timer accumulation — see [How it works](#how-it-works) above.

`always()` treats every timer as a black box that may finish on any scan, which keeps BFS tractable and makes a `Proven` hold under any timing. It's timing-blind by design — it answers "can the alarm fire?" not "does it fire in time?" A property that holds only because one timer finishes before another is not `Proven`: it comes back unconfirmed (`Intractable` of kind `"timing"`), which is the cue for the timing test below.

**Timing coverage** — does the fault timer trip fast enough under real timing? Force-based tests answer this:

```python
for coupling in harness.couplings():
    plc2 = PLC(logic, dt=0.001)
    h2 = Harness(plc2)
    h2.install()
    plc2.force(coupling.en_name, True)
    plc2.run_for(1.5)
    plc2.force(coupling.fb_name, False)
    plc2.run_for(6.0)
    with plc2:
        assert AlarmExtent.value != 0, f"{coupling.fb_name}: too slow"
```

This catches fault timers that exist structurally but are too slow — the alarm path exists but takes longer than the machine can safely tolerate.

Run `always()` first — there's no point testing timing on a coupling that never reaches an alarm. Then run the force-based tests for timing validation on the ones that passed. See `examples/fault_coverage.py` for a complete working example.

## Lock files

The lock file captures your program's full reachable behavior as a committed artifact — same mental model as `uv.lock` or `package-lock.json`.

```bash
pyrung lock my_program        # compute reachable states, write pyrung.lock
pyrung lock my_program --check  # recompute, diff against pyrung.lock, exit 1 if changed
```

The lock projects to tags marked `lock=True` by default — the outputs that define your program's observable behavior. Programs using CLICK `TagMap` get this automatically (output-mapped tags are stamped `lock=True`). Programs without `TagMap` need explicit `lock=True` on output tags, or use `__lock__` or `--project`:

```bash
pyrung lock my_program --project Running MotorOut StatusLight
```

Tags with `choices=` metadata get their labels in the lock file instead of raw integers — `"FAST"` instead of `2`. Tags with `band=` metadata collapse numeric values into categorical labels — see [Bands](#bands-collapsing-numeric-values-into-categories) below.

Timers are black boxes here too (see [How it works](#how-it-works)), so a lock can include rows that only impossible timing reaches: a 2 s watchdog done before the 1 s timer started with it. The lock over-approximates the program's behavior; it never misses a reachable row.

### `__lock__` — per-module projection override

For programs where the `lock=True` default misses something (a pivot that matters behaviorally) or includes something cosmetic, define `__lock__` at module level:

```python
__lock__ = {
    "include": ["AlarmExtent", "BatchCount"],
    "exclude": ["Sts_DisplayText"],
}
```

- `include` adds tags the default misses.
- `exclude` drops tags the default includes.
- `joint` declares joint multi-flip input groups (see below).
- `exclusive` declares mutually exclusive input groups (see below).
- All keys are optional. Most programs won't need `__lock__` at all.
- `--project` on the CLI still overrides everything for one-off checks.

Common patterns:

```python
# Lock down the operator-facing interface too
dv = logic.dataview()
__lock__ = {
    "include": list(dv.public().tags),
}

# Lock Modbus registers
__lock__ = {
    "include": list(dv.contains("Modbus").tags),
}
```

### Bands — collapsing numeric values into categories

The `band` attribute maps ranges of concrete values to categorical labels in the lock file output. Band metadata is purely a post-processing reduction — it is not used during BFS exploration or `always()`:

```python
AlarmExtent = Int(lock=True, band={"ZERO": 0, "POSITIVE": "> 0"})
```

In the lock file, `AlarmExtent=3` becomes `AlarmExtent="POSITIVE"`. The lock captures *which band* the value falls into, not the exact number — so adding a new alarm source doesn't change the lock file as long as the alarm extent stays positive.

Band predicates:

| Predicate | Matches |
|-----------|---------|
| `0` | Exact value 0 |
| `"*"` | Any value (wildcard) |
| `">= 100"` | Comparison (supports `==`, `!=`, `>`, `>=`, `<`, `<=`) |
| `"0..10"` | Inclusive range |

Predicates are checked in declaration order — the first match wins. Use `"*"` as a catch-all last entry.

### Joint inputs — multi-flip combinations

Single-flip BFS only changes one input per successor state. The `joint` key in `__lock__` declares groups whose members are additionally explored jointly — the BFS generates multi-flip combinations on top of the normal single-flip successors.

```python
__lock__ = {
    "joint": {
        "panel": ["SwitchA", "SwitchB"],
    },
}
```

Also available programmatically via `joint_inputs=` on `reachable_states()` and `always()`.

It's generally unwise to ship logic that depends on multiple inputs changing in the exact same scan cycle — real-world I/O doesn't change atomically. If the verifier only finds a property violation via a multi-flip path, that's usually a signal the logic needs fixing, not that single-flip is too conservative.

### Exclusive inputs — at most one True

The `exclusive` key in `__lock__` declares groups where at most one member is True at a time. The BFS only explores all-False and one-hot (single-True) assignments, pruning multi-hot combinations that can't happen in practice. This reduces the state space for selector switches, mode buttons, and similar mutually exclusive physical inputs.

```python
__lock__ = {
    "exclusive": {
        "mode": ["Manual", "Auto", "Step"],
    },
}
```

Also available programmatically via `exclusive_inputs=` on `reachable_states()` and `always()`.

The verifier auto-detects some exclusive patterns (encoder-style one-hot families). Use the `exclusive` key when auto-detection doesn't cover your case — typically for inputs that are directly read in conditions rather than routed through an encoder tag.

### Three levels of lock

**Lock everything** — full state space equality. For purely cosmetic refactoring (renaming tags, reordering rungs that don't interact). Any behavioral change is flagged.

```python
states = reachable_states(logic)  # default: lock=True tags
```

**Lock I/O** — project to inputs and terminals only. For restructuring internal logic where pivots can change freely.

```python
dv = logic.dataview()
states = reachable_states(logic, project=sorted(dv.terminals().tags))
```

**Lock a subset** — scope to specific tags. "I'm changing the diverter logic, but motor control shouldn't be affected."

```python
dv = logic.dataview()
motor_tags = sorted(dv.upstream("Running", "Conv_Motor").tags)
states = reachable_states(logic, scope=["Running", "Conv_Motor"], project=motor_tags)
```

### Diffing

```python
from pyrung.core.analysis import reachable_states, diff_states

before = reachable_states(original, project=["Running", "MotorOut"])
after  = reachable_states(refactored, project=["Running", "MotorOut"])
diff   = diff_states(before, after)

assert not diff.added and not diff.removed  # behavioral equivalence
```

In a PR, the lock file diff tells the story. States omit `false` values — each entry reads as "what's ON":

```diff
  "reachable": [
    {},
    {"Conv_Motor": true, "Running": true},
+   {"Conv_Motor": true}
  ]
```

Reviewer sees: "Conv_Motor can now be on while Running is off." Either intentional (regenerate with `pyrung lock`) or a bug.

### Programmatic use

```python
from pyrung.core.analysis.prove import check_lock, write_lock, program_hash

# Write
states = reachable_states(logic, project=["Running"])
write_lock(Path("pyrung.lock"), states, ["Running"], program_hash(logic))

# Check
diff = check_lock(logic, Path("pyrung.lock"))
assert diff is None  # None means no change
```

### CLI reference

```
pyrung lock <module>              # write pyrung.lock
pyrung lock <module> -o out.lock  # custom output path
pyrung lock <module> --project Running MotorOut  # explicit projection
pyrung lock <module> --depth-budget 100          # explore longer scan sequences
pyrung lock <module> --profile out.prof          # write cProfile stats

pyrung lock <module> --check                  # diff against pyrung.lock, exit 1 on change
pyrung lock <module> --check -o custom.lock   # custom lock path
pyrung lock <module> --check --profile out.prof  # write cProfile stats
```

`--profile` dumps cProfile stats even on `KeyboardInterrupt`. Analyze with `pstats.Stats("out.prof")` or `uvx snakeviz out.prof`.
`--depth-budget` is the BFS depth: the longest sequence of scans explored. A timer may finish on any scan, so waiting for one costs no depth, but a counter or a `calc` count advances one step per scan, so a count past the budget is not reached. A `Proven` cut short by the budget says so in a caveat.

The `<module>` argument is a Python module path (e.g., `my_program` or `examples.conveyor`). The module must contain a `Program` instance.

## Next steps

- [Analysis](analysis.md) — program structure, diagnosis, cause/effect, test coverage
- [Physical Annotations](physical-harness.md) — declare device behavior, autoharness
- [Testing](testing.md) — forces as fixtures, forking, monitors, breakpoints
- [Runner Guide](runner.md) — execution methods, history, time travel
