# PILOT — Probe, Input, Let-run, Observe, Trace

## Theory

A PLC program is its own model — forkable, steppable, fully observable.
To reach a target value: trace backward through the program's rungs on a
live fork to find what external inputs are needed, simulate forward to
execute them, and re-trace only when all goals regress simultaneously.
The program tells you what it needs. The simulation tells you what went
wrong. The trace tells you what to fix. Repeat until done or budget
exhausted.

The model is the program. The walk is the execution. The trace is the plan.

## What PILOT is NOT

PILOT is not a planner. It does not build transition graphs, maintain an
agenda, resolve flaws, select governing tags, or pre-compute waypoint
sequences. It does not reason about the program abstractly. It reads
rungs and simulates forward. That's it.

If you find yourself building:
- A graph of states to search → stop. The backward trace finds the path.
- A cost model or heuristic → stop. The simulation is the heuristic.
- An abstract representation of the program → stop. The program IS the
  representation.
- A planner that reasons about actions before taking them → stop. PILOT
  takes actions and observes what happens.
- A recovery framework with threat classification → stop. PILOT re-traces
  from the break.

The program is a white box. Every rung is readable. Every contact has a
direction. Every tag value is observable. Use that directly. Do not
abstract it.

---

## Stage 0 — Backward trace on a toy program

**Goal:** prove we can read a program's rungs backward from a target to
external inputs. No simulation. No PILOT loop. Just the trace.

### Build a toy program

Write a small pyrung program inline (not the full burner). Something like:

```python
from pyrung import Bool, Program, call, out, rung, subroutine

x_Enable = Bool("x_Enable", external=True)
x_Trigger = Bool("x_Trigger", external=True)
x_Condition = Bool("x_Condition", external=True)
y_Armed = Bool("y_Armed")
y_Target = Bool("y_Target")

with Program() as toy:
    with subroutine("do_thing"):
        with rung(x_Condition):
            out(y_Target)

    with rung(x_Enable):
        out(y_Armed)

    with rung(y_Armed, x_Trigger):
        call("do_thing")
```

Target: `y_Target = True`.

### Implement trace_back

A single recursive function. No classes, no frameworks. Just:

```python
def trace_back(tag, value, snapshot, logic):
```

It should:
1. Find rungs that write `tag` (use the PDG: `pdg.writers_of[tag]`).
2. For each writer, check: can it produce `value`?
   - `_written_value_for_tag(rung_obj, tag)` → `Literal(v)`: only if `v == value`
   - `Affine(src, scale, offset)`: yes, with `src = (value - offset) / scale`
   - `UNKNOWN`: assume yes (simulation will verify)
3. Chase data-flow sources:
   - `copy_source_binding(rung_obj, tag, value)` → `(src, src_value)` or None
   - `calc_source_binding(rung_obj, tag, value)` → `(src, src_value)` or None
   - If both return None and `forward()` returned UNKNOWN, the source is
     opaque — flag it for instruction-level execution or simulation.
4. Read the writer's gate conditions. Convert SP tree via `_sp_to_expr`,
   then `_extract_condition_values`. Include the subroutine call gate —
   if the rung is inside a subroutine, include the `call()` rung's
   conditions.
5. Partition conditions into satisfied (snapshot matches via `_values_match`)
   and unsatisfied.
6. For each unsatisfied condition, recurse.
7. When you reach an external input (`TagRole.INPUT` or no writers), that's
   a leaf — an action.
8. Return the list of actions, deepest-first.

For the toy program above, `trace_back("y_Target", True, dict(plc.state.tags), toy)` should
return something like:

```
[("x_Enable", True), ("x_Trigger", True), ("x_Condition", True)]
```

Because: `y_Target` needs `do_thing` called → `call("do_thing")` needs
`y_Armed` and `x_Trigger` → `y_Armed` needs `x_Enable` → and inside
`do_thing`, `y_Target` needs `x_Condition`.

### What "done" looks like for Stage 0

A standalone script that:
- Builds the toy program
- Runs `trace_back` on it
- Prints the action chain
- Asserts the expected actions

No simulation. No forking. Just proving the backward trace reads the
program correctly.

### How to verify you're not slipping

The trace should read like an engineer narrating the program:
"y_Target is written in do_thing when x_Condition is true. do_thing
is called when y_Armed and x_Trigger are true. y_Armed is set when
x_Enable is true. So I need x_Enable, x_Trigger, and x_Condition."

If the trace can't be narrated that way, something is wrong.

---

## Stage 1 — Forward PILOT with learning (Layer 1)

**Goal:** given a trace_back result, apply the actions on a real PLC and
reach the target. Always commit. Learn from failures. Recovery is just
more piloting.

### The loop

```python
def pilot(plc, target_tag, target_value, budget=100):
    nogoods = set()     # (tag, value, blocker) — things that didn't work
    couplings = {}      # {feedback_input: output} — discovered links
    scan = 0

    while scan < budget:
        # Where are we now? What do we need?
        actions = trace_back(target_tag, target_value, dict(plc.state.tags),
                             logic, nogoods=nogoods)

        if actions is None:
            return None  # structurally unreachable

        # Snapshot sub-goals before acting
        before = count_satisfied_subgoals(target_tag, target_value, plc)

        # Apply next action and let it run
        input_tag, value = actions[0]  # deepest unsatisfied first
        plc.patch({input_tag: value})
        for _ in range(100):  # 1 second
            plc.step()
            scan += 1
            if plc.state.tags.get(target_tag) == target_value:
                return plc  # done

        # Always commit (we're on the work PLC, no fork).
        # Just learn from what happened.
        after = count_satisfied_subgoals(target_tag, target_value, plc)

        if after < before:
            # All goals regressed. Learn why.
            blocker = plc.cause(target_tag)  # what broke us?
            nogoods.add((...))  # record so we don't repeat
            # Don't panic. Re-trace from wherever we are now.
            # The program may have recovered to ABORTED or similar.
            # PILOT will navigate back using normal tracing.

        # Discover feedback couplings by observing output/input pairs
        # ... (see learning below)
```

### Key: no forks in Layer 1

Layer 1 operates directly on the work PLC. No fork-as-lookahead. Apply
the action, let it run, observe what happened. If something regressed,
the program is now in a different state (ABORTING → ABORTED, alarm
active, whatever). The program is working correctly — it responded to
conditions. PILOT re-traces from that state and pilots forward again —
using the same backward trace, same input, same loop. Recovery IS
piloting.

This is what an engineer does: press a button, something trips, ok,
clear the alarm, try again differently. No rewinding. No speculative
forks. Just: where am I? What's next?

### Commit rule

There is no commit rule. Everything is committed because there are no
forks. PILOT works on the live PLC state.

Progress tracking: count satisfied sub-goals from the backward trace.
If the count drops across ALL goals, record a nogood from `cause()`.
If a prerequisite regressed but a dependent advanced, ignore the
dependent — it's built on a broken foundation. Track the prerequisite.
Otherwise, keep going.

### Learning

As PILOT runs:
- **Nogoods:** "pulsing x_Trigger without x_Enable causes alarm" → don't
  try that combination again.
- **Feedback couplings:** observed `o_Blower` go True then False, and
  `x_BlowerFB` needed to follow → tentative coupling. Seen on+off →
  install as a force that mirrors the output.
- **Holds:** "x_Enable must stay True through the whole sequence" → force,
  don't patch.

### Input physics

- `patch({tag: True})` — pulse. Reverts to resting state automatically.
- `force(tag, True)` — persistent. Stays until explicitly released.
- Inputs have a `rests` state (True or False). NC contacts rest True.
  NO contacts rest False. Patch reverts to resting state.

### What "done" looks like for Stage 1

Run against the PackML bench (`packml_bench_3.py`). Should discover:
`CmdClear → CmdReset → CmdStart` and reach `StateCurrent == EXECUTE`
in <10 scans across ≤3 attempts.

If it takes more than 3 attempts, the learning isn't working. If it
takes 1 attempt, the backward trace nailed it.

---

## Stage 2 — Causal rewind before committing (Layer 2)

**Goal:** add fork-as-lookahead so PILOT can test actions before
committing them, and rewind from regressions instead of pushing forward.

### Fork as lookahead

Layer 1 operates on the live PLC. Layer 2 adds a fork:
1. Fork the work PLC.
2. Apply the action on the fork.
3. Let run 1 second on the fork.
4. Observe: did all goals regress?
   - No → commit the fork as the new work PLC.
   - Yes → `cause()` on the fork → find what broke. Re-trace from the
     break. Apply the fix on a new fork. If clean, commit. If still
     broken, record nogood, discard, try next action.

The work PLC never sees the regression. The fork absorbed it. PILOT
learned from it without paying the cost of navigating back.

### live_recover=True

Falls back to Layer 1 behavior: skip the lookahead, commit everything,
let the program respond to conditions naturally. PILOT navigates from
whatever state results using normal tracing. Useful when you want the
commissioning log to show the real path, not the rewound one.

### debug=True

Shows every fork attempt:
"Forked at scan 12. Applied CmdStart. Ran 1s. Fork shows
StateCurrent=6→9. cause() says: AlarmExtent>0 triggered abort.
Discarded fork. Patched x_RotateSensor toggling on work PLC.
New fork: stable. Committed."

### What "done" looks like for Stage 2

Run against the burner. Should discover:
- Command sequence (Clear → Reset → Start)
- Alarm from rotate watchdog → rewind → discover x_RotateSensor
- Permissive feedback couplings
- Timer wait for HeatDelay

Target: reach `y_BurnerLoop` with a readable commissioning log.

---

## Stage 3 — Time folding (Layer 3)

**Goal:** skip past long timer waits.

When the backward trace shows the only unsatisfied leaf is a timer Done
bit, and the timer's enable contact is satisfied:
- Read the timer's accumulator and preset from the live snapshot.
- Compute remaining scans: `(preset - acc) / dt`.
- Verify nothing in the upstream cone can change during the interval
  (no other goals have active sub-goals, all holds are stable).
- Jump: `fork.step(remaining_scans)` or use `_advance_time`.

This is an optimization. Layer 1 already solves the problem by stepping
through all 2000 scans. Layer 2 does it without regressions. Layer 3 just
makes it fast.

---

## How backward tracing works: three layers

The backward trace needs to answer: "what inputs produce `dest == value`?"
There are three ways to answer, tried in order. Do NOT build more static
inverters — the instruction is its own inverse when you can just run it.

### Layer 1: `forward()` classification (static, exact)

`_written_value_for_tag(rung_obj, tag)` calls the crossings registry's
`forward()` method. It returns one of:

- `Literal(value)` — the instruction writes a constant (e.g. `out`,
  `copy(7, dest)`, `copy(readonly_const, dest)`)
- `Affine(source, scale, offset)` — the instruction writes
  `dest = source * scale + offset` (e.g. `copy(src, dest)` is
  `Affine(src, 1, 0)`; `calc(src + 10, dest)` is `Affine(src, 1, 10)`)
- `UNKNOWN` — can't classify statically

For `Literal`: check `value == target` to decide if this writer is relevant.
For `Affine`: invert arithmetically: `source_value = (target - offset) / scale`.
No per-instruction reverse handler needed — the consumer does the math.

Use `copy_source_binding(rung_obj, tag, value)` or
`calc_source_binding(rung_obj, tag, value)` for the one-call version —
they return `(source_tag, source_value)` or `None`.

### Layer 2: Instruction-level execution (dynamic, universal)

When `forward()` returns `UNKNOWN`, don't build a static inverter.
Instead, run the instruction in isolation against the snapshot:

```python
from pyrung.core.context import ScanContext
from pyrung.core.state import SystemState
from pyrsistent import pmap

ctx = ScanContext(SystemState(tags=pmap(snapshot)))
instruction.execute(ctx, enabled=True)
result = ctx._tags_pending.get(tag_name)
```

Every instruction has `execute(ctx, enabled)`. It reads tags from the
snapshot via `ctx.get_tag()` and writes results via `ctx.set_tag()`.
The pending writes tell you what the instruction produces.

For backward tracing: try candidate values for the upstream inputs
(from their pipeline domains, current snapshot, or enumerated literals),
fire the instruction, check if the output matches the target. The
instruction computes its own answer — no algebraic inverse needed.

This handles multi-source calcs (`A + B`), drums, shift registers,
block copies with indirect indexing — anything with an `execute()`
method. Which is everything.

### Layer 3: Full simulation (program-level)

When instruction-level execution isn't enough (the writer depends on
state that evolves over multiple scans — timers, edge-triggered
sequences, counter accumulation), run `plc.step()` and observe.
This is Stage 1's PILOT loop.

### The principle

Static when you can (`Literal`/`Affine` — exact, free).
Execute when you must (one instruction — fast, universal).
Simulate when you have to (full scans — correct, expensive).

Do NOT build a `reverse()` handler for every instruction type. The
crossings registry's `reverse()` exists for the prover and walker,
which need formal constraint objects (`Eq`, `Cmp`). PILOT doesn't
need that formalism — it needs "what value do I set?" and gets the
answer from arithmetic or execution.

---

## Existing infrastructure to use

Don't rebuild these. They exist and work.

**Runner:**
- `PLC(logic)` — runner wrapping a `Program`
- `plc.fork()` — snapshot/checkpoint, returns a new `PLC`
- `plc.step()` — one scan cycle
- `plc.patch({tag: value})` — one-shot input (reverts after scan)
- `plc.force(tag, value)` — persistent hold until `unforce()`
- `plc.state.tags` — `PMap` of current tag values (`dict(plc.state.tags)` for plain dict)
- `plc.why(tag)` — backward attribution on live state
- `plc.cause(tag)` — recorded causal chain (post-simulation)

**Static analysis:**
- PDG: `pdg.writers_of[tag]` → `frozenset` of rung indices
- PDG: `pdg.tag_roles[tag]` → `TagRole.INPUT` / `PIVOT` / `TERMINAL`
- SP-tree: `rung_obj.sp_tree()` → condition+action tree
- `_sp_to_expr(sp_node)` → converts SP tree to `Expr` for extraction
- `_extract_condition_values(expr)` → `{tag: frozenset(values)}`
- `_values_match(a, b)` — canonical comparator

**Data-flow tracing:**
- `_written_value_for_tag(rung_obj, tag)` → `Literal` / `Affine` / `UNKNOWN`
- `copy_source_binding(rung_obj, tag, value)` → `(src, src_value)` or `None`
- `calc_source_binding(rung_obj, tag, value)` → `(src, src_value)` or `None`

**Instruction execution:**
- `ScanContext(SystemState(tags=pmap(snapshot)))` — lightweight eval context
- `instruction.execute(ctx, enabled=True)` — fire one instruction
- `ctx._tags_pending` — what it wrote

## Anti-patterns to watch for

**"Let me build a graph first."** No. The backward trace IS the graph
traversal. You traverse it by recursing, not by materializing it.

**"Let me write a reverse() handler for this instruction."** No. Run
the instruction on a snapshot with candidate inputs. The instruction
is its own inverse.

**"Let me predict what will happen."** No. Simulate and observe. The fork
is free. Prediction is planning. Don't plan.

**"Let me handle this edge case with a special mechanism."** No. The
loop handles it: trace, simulate, observe, fix. If the fix doesn't
work, the next iteration catches it. Special mechanisms are the path
back to 15 modules.

**"This looks like CEGAR / POCL / PDR."** It's not. It's an engineer
at a panel. The engineer doesn't know those acronyms. They read the
program and press buttons.

## Output format

Every PILOT run produces a commissioning log:

```
[001] trace: y_Target ← do_thing.rung_1 ← x_Condition (unsatisfied)
                       ← call(do_thing) ← y_Armed (unsatisfied)
                                         ← x_Trigger (unsatisfied)
                       ← y_Armed ← x_Enable (unsatisfied)
     actions: x_Enable=True, x_Trigger=True, x_Condition=True

[002] input: patch x_Enable=True
     let-run: 1s (100 scans)
     observe: y_Armed=True ✓, y_Target=False (do_thing not yet called)
     commit: progress (1/3 sub-goals satisfied)

[003] input: patch x_Trigger=True
     let-run: 1s
     observe: y_Target=True ✓
     DONE in 2 inputs, 200 scans
```

Readable. Naratable. An engineer looks at this and nods.
