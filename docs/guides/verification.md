# Verification

The [analysis tools](analysis.md) answer questions about recorded history — what happened, why, and did your tests cover the program. Verification answers a different question: **does a property hold across every reachable state, not just the states your tests happened to visit?**

```python
from pyrung import Bool, Or, Program, Rung, latch, reset
from pyrung.core.analysis import prove, Proven

EstopOK = Bool("EstopOK", external=True)
Start   = Bool("Start", external=True)
Running = Bool("Running")

with Program(strict=False) as logic:
    with Rung(Start, EstopOK):
        latch(Running)
    with Rung(~EstopOK):
        reset(Running)

result = prove(logic, Or(~Running, EstopOK))
assert isinstance(result, Proven)
```

`prove()` exhaustively explores every reachable state via BFS over the compiled replay kernel. If the property holds everywhere, you get `Proven`. If not, `Counterexample` with a trace you can replay on a real PLC.

## Condition syntax

`prove()` accepts the same condition expressions as `Rung()` and `when()`:

```python
prove(logic, Or(~Running, EstopOK))     # condition expression
prove(logic, ~Running, EstopOK)          # implicit AND
prove(logic, lambda s: s["X"] + s["Y"] < 100)  # callable fallback
```

Condition expressions are preferred — the verifier extracts referenced tags and automatically restricts input enumeration to the upstream cone. Callable predicates work but don't get auto-scoping.

### Result types

```python
from pyrung.core.analysis import Proven, Counterexample, Intractable

result = prove(logic, Or(~Running, EstopOK))

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
```

`Intractable` means the state space is too large. The fix is usually adding `choices` or `min`/`max` metadata to the unbounded tags — the same metadata you'd declare anyway for Data View dropdowns and static validation.

### Scoping

With condition expressions, scope is derived automatically from the referenced tags. Override with `scope=` when needed:

```python
prove(logic, Or(~Running, EstopOK), scope=["Running", "EstopOK"])
```

Scoping restricts input enumeration to the upstream cone of the named tags — the verifier only explores inputs that can actually influence the property.

### How it works

The verifier classifies every tag into one of three roles:

- **Combinational** — OTE-only writes, derived from inputs each scan. Not a state dimension.
- **Stateful** — latch/reset, timer/counter, copy, calc. Tracked in the visited set.
- **Nondeterministic** — external inputs. Enumerated at each state.

Value domains come from the expression tree: comparison literals in conditions, `choices` metadata, `min`/`max` bounds. A tag compared against `== 1` and `== 2` gets domain `{1, 2, unmatched}` — three values instead of 65K.

Don't-care pruning skips inputs that are masked by the current state. `And(StateBit, Input)` with `StateBit=False` means `Input` doesn't matter — the verifier skips it entirely.

Timer and counter Done bits use a three-valued abstraction: `False`, `Pending` (accumulating), and `True` (done). The verifier fast-forwards through accumulation rather than stepping one tick at a time. When evaluating a property, the verifier settles all pending timers/counters to a stable state first — a timer-gated alarm that is structurally reachable but hasn't elapsed yet won't produce a spurious counterexample.

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

**Structural coverage** — does a path from the fault to an alarm exist at all? `prove()` answers this exhaustively. Batch all conditions into a single call — the verifier shares work across properties:

```python
from pyrung.core.analysis import prove, Proven, Counterexample

couplings = list(harness.couplings())
conditions = [
    Or(~plc.tags[c.en_name], plc.tags[c.fb_name], AlarmExtent != 0)
    for c in couplings
]
results = prove(logic, conditions)

for coupling, result in zip(couplings, results):
    assert isinstance(result, Proven), f"{coupling.fb_name}: no alarm path"
```

Each condition reads: "in every reachable state, either the enable is off, the feedback is healthy, or the alarm caught it." A `Counterexample` means there exists a reachable state where the feedback has failed and no alarm fired — a structural detection gap.

`prove()` uses a three-valued timer abstraction (`False`/`Pending`/`True`) that collapses accumulator state to make BFS tractable. It settles pending timers before evaluating, so timer-gated alarm paths prove correctly. But it's timing-blind by design — it answers "can the alarm fire?" not "does it fire in time?"

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

Run `prove()` first — there's no point testing timing on a coupling that never reaches an alarm. Then run the force-based tests for timing validation on the ones that passed. See `examples/fault_coverage.py` for a complete working example.

## Lock files

The lock file captures your program's full reachable behavior as a committed artifact — same mental model as `uv.lock` or `package-lock.json`.

```bash
pyrung lock my_program        # compute reachable states, write pyrung.lock
pyrung check my_program       # recompute, diff against pyrung.lock, exit 1 if changed
```

The lock projects to terminal tags by default — physical outputs in well-structured ladder. Override with `--project`:

```bash
pyrung lock my_program --project Running MotorOut StatusLight
```

### `__lock__` — per-module projection override

For programs where the terminal default misses something (a pivot that matters behaviorally) or includes something cosmetic, define `__lock__` at module level:

```python
__lock__ = {
    "include": ["AlarmExtent", "BatchCount"],
    "exclude": ["Sts_DisplayText"],
}
```

- `include` adds tags the terminal default misses.
- `exclude` drops tags the terminal default includes.
- Both keys are optional. Most programs won't need `__lock__` at all.
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

### Three levels of lock

**Lock everything** — full state space equality. For purely cosmetic refactoring (renaming tags, reordering rungs that don't interact). Any behavioral change is flagged.

```python
states = reachable_states(logic)  # default: terminal tags
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

In a PR, the lock file diff tells the story:

```diff
  "reachable": [
    {"Conv_Motor": false, "Running": false},
    {"Conv_Motor": true,  "Running": true},
+   {"Conv_Motor": true,  "Running": false}
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
pyrung lock <module> --max-depth 100             # deeper BFS

pyrung check <module>             # diff against pyrung.lock, exit 1 on change
pyrung check <module> --lock custom.lock         # custom lock path
```

The `<module>` argument is a Python module path (e.g., `my_program` or `examples.conveyor`). The module must contain a `Program` instance.

## Next steps

- [Analysis](analysis.md) — dataview, cause/effect, coverage queries, static validators
- [Physical Annotations](physical-harness.md) — declare device behavior, autoharness
- [Testing](testing.md) — forces as fixtures, forking, monitors, breakpoints
- [Runner Guide](runner.md) — execution methods, history, time travel
