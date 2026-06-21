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

## Design principles

1. **Readable** — an engineer follows the output without explanation.
2. **Reproducible** — the output is PLC API commands you can copy and run.
3. **Built on the API** — no parallel infrastructure. Use `patch`, `force`,
   `run_until`, `when`, `monitor`, `cause`, `diff`. Add primitives to the
   runner as needed, not planning machinery on top.
4. **"What would an engineer do?"** — at every decision point, this question
   replaces the planning literature. PLC programs are written by engineers
   to be commissioned by engineers. An algorithm that navigates the way a
   human does will succeed because the program was written for a human to
   succeed.

## Two modes: `how()` and `pilot()`

Same algorithm, different target:

- **`how(y_BurnerLoop)`** — PILOT on a fork. Discovers the path, returns
  the transcript. Fork discarded. Nothing changes. Analysis mode.
- **`pilot(y_BurnerLoop)`** — PILOT on the live PLC. Drives the state
  there, printing each PLC command as it executes. Commissioning mode.

```python
# Analysis — what would it take?
path = plc.how(y_BurnerLoop)
print(path)

# Commissioning — do it live
plc.pilot(y_BurnerLoop)
# plc.patch({C_ProductionMode: True, C_UnitModeChgRequest: True})
#                                    → S_UnitModeCurrent: 3 → 1
# plc.patch({C_Clear: True})        → S_StateCurrent: 9 → 1
# plc.run(2)                        → S_StateCurrent: 1 → 2
# plc.patch({C_Reset: True})        → S_StateCurrent: 2 → 15
# plc.run(2)                        → S_StateCurrent: 15 → 4
# plc.patch({C_Start: True})        → S_StateCurrent: 4 → 3
# plc.run(2)                        → S_StateCurrent: 3 → 6
# plc.force(x_RotateSensor, True)   → discovered: watchdog needs this
# plc.run_until(HeatDelay_Tmr_Done)
# y_BurnerLoop = True ✓
```

The output IS pyrung API. A student copies it into a test and it runs.

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

## Core concepts (from POC experiments)

### Steerable set

The set of inputs PILOT can press. NOT the same as `TagRole.INPUT`.

In real Click programs, every HMI button (C_Clear, C_ProductionMode,
C_UnitModeChgRequest, etc.) has a program-side reset writer (the
ack-cleared pattern: the program resets the bit after processing it),
so its TagRole is PIVOT, not INPUT. But only the operator can set it True.

Use `_external_bool_inputs(pdg, known, program)` — it returns both
never-written x-block inputs AND ack-cleared Bools. This function
currently lives in `walk/priors.py` but has zero walk-specific
dependencies; it should move to shared analysis infrastructure
(alongside `_ack_cleared_bool_inputs`).

### Batch vs sequence (tree depth)

The backward trace returns a tree. Tree depth IS temporal ordering:

- **Same depth level** → simultaneous batch. `C_ProductionMode` and
  `C_UnitModeChgRequest` are both conditions on the same rung path.
  Apply as a single `plc.patch({...})`. The mode change handshake
  needs both in the same scan.

- **Different depth levels** → sequential. The trace from y_BurnerLoop
  finds `S_UnitModeCurrent=1` and `S_StateCurrent=6` at different
  depths. Mode change fires first (deeper), then state commands.

The engineer reads this naturally: "I need production mode first,
then Clear, then Reset, then Start." The trace tree encodes the same
ordering.

### Three layers of backward tracing

The trace answers "what inputs produce `dest == value`?" Three ways,
tried in order:

1. **Literal/Affine** (`forward()` classification) — static, exact.
   `copy(1, StateRequested)` → `Literal(1)`. `calc(src + 10, dest)` →
   `Affine(src, 1, 10)`, invert to `src = (target - 10)`. The trace
   sees through these.

2. **Instruction-level execution** — when `forward()` returns UNKNOWN.
   Fire the instruction in isolation against the snapshot, read what
   it wrote. Jump tables, indirect copies, multi-source calcs. The
   instruction is its own inverse.

3. **Full simulation** — multi-scan state evolution (timers, edge
   sequences, counters). `plc.step()` / `run_until()` and observe.
   This is the PILOT loop itself.

Static when you can. Execute when you must. Simulate when you have to.

### Recovery IS piloting

The engineer presses a wrong button. The PLC gates it — nothing happens.
Or the PLC responds to conditions — an alarm fires, state regresses.
The engineer doesn't rewind. They re-assess from wherever they are and
try the next thing. Recovery IS piloting.

On a live PLC you don't take back "oh I pressed this and nothing
worked" — you just try the next one.

---

## Stage 0 — Backward trace  ✅ PROVEN

**Goal:** read a program's rungs backward from a target to steerable
inputs.

### trace_back

A single recursive function:

```python
def trace_back(tag, value, snapshot, pdg, program, steerable):
```

1. If `tag in steerable` → leaf action. Return it. Check this BEFORE
   checking writers.
2. Find rungs that write `tag` (`pdg.writers_of[tag]`).
3. For each writer, check: can it produce `value`?
   - `Literal(v)`: only if `v == value`
   - `Affine(src, scale, offset)`: yes, invert arithmetically
   - `UNKNOWN`: assume yes (simulation will verify)
4. Chase data-flow sources (`copy_source_binding`, `calc_source_binding`).
5. Read the writer's gate conditions (`_sp_to_expr`, `_extract_condition_values`).
   Include subroutine call gate conditions.
6. Partition into satisfied and unsatisfied (against snapshot).
7. For each unsatisfied condition, recurse.
8. Return the full list of actions, deepest-first.

### Candidate selection

When multiple writers can produce the value, score by:
- **Controllability** — fraction of unsatisfied conditions that are steerable
- **Stability** — prefer latched states over transient copies
- **Non-conflict** — fewer clobbered sibling goals is better

Ties go to fewest unsatisfied conditions.

### What we proved

`probe_trace_back.py` — 7 test programs: bool chains, copy data-flow,
calc expressions, subroutine call gates, timers, mixed patterns,
simplified_forms comparison. All pass.

On toy programs (`_cmd_protocol_program`, `_deep_call_program`), the
one-at-a-time PILOT loop reaches the target in minimal steps. The PLC
gates protect against wrong-order actions.

On the real burner, the trace finds `{C_ProductionMode, C_UnitModeChgRequest}`
as a batch and successfully changes the mode. It finds `C_Clear` for the
state transition. The handshake batching is discovered automatically from
the rung path structure.

---

## Stage 1 — Forward PILOT with learning (Layer 1)

**Goal:** apply trace_back results on a live PLC, learn from failures.

### The loop

```python
def pilot(plc, target_tag, target_value, budget=100):
    steerable = set(_external_bool_inputs(pdg, plc._known_tags_by_name, logic))
    nogoods = set()

    while scan < budget:
        # Trace backward from current snapshot
        actions = trace_back(target_tag, target_value,
                             dict(plc.state.tags), pdg, logic, steerable)

        if actions:
            # Apply as batch — handshakes need simultaneous inputs
            plc.patch({tag: value for tag, value in actions})
            plc.run_for(1.0)
        else:
            # Trace dead-ended (opaque boundary). Try steerable inputs
            # one at a time on the live PLC — the PLC gates protect us.
            # If nothing works, step forward (timers/SFCs).
            ...

        # Re-trace from new state next iteration
```

### Key: no forks in Layer 1

Apply the action, let it run, observe. If something regressed, the
program responded to conditions. PILOT re-traces from that state.
Recovery IS piloting.

### Learning

- **Nogoods:** Layer 1 uses seen-state regression ("did I end up
  somewhere I've been?"). Layer 2 upgrades this to `cause()` chains
  on forks — precise attribution before committing.
- **Feedback couplings:** observed `o_Blower` go True then False, and
  `x_BlowerFB` needed to follow → install as a force that mirrors
  the output.
- **Holds:** "x_Enable must stay True through the whole sequence" →
  force, don't patch.

### Input physics

- `plc.patch({tag: True})` — pulse. One-shot, discarded after next step.
- `plc.force(tag, value)` — persistent until `plc.unforce(tag)`.

### What "done" looks like

Run against the burner. Should discover:
`{C_ProductionMode, C_UnitModeChgRequest}` → `C_Clear` → `C_Reset` →
`C_Start` and reach `S_StateCurrent == EXECUTE`, then wait through
timers/SFCs to `y_BurnerLoop = True`.

---

## Stage 2 — Causal rewind before committing (Layer 2)

**Goal:** add fork-as-lookahead so PILOT can test actions before
committing them, and learn from regressions via `cause()`.

### Fork as lookahead

1. Fork the work PLC: `fork = plc.fork()`
2. Apply the action on the fork: `fork.patch({...})`
3. Let run: `fork.run_for(1.0)`
4. Observe: did goals regress?
   - No → commit the fork as the new work PLC.
   - Yes → `fork.cause(target_tag)` → precise attribution. Record
     nogood, discard fork, try next action.

The work PLC never sees the regression. The fork absorbed it. PILOT
learned from it without paying the cost of navigating back.

### live_recover=True

Falls back to Layer 1: skip lookahead, commit everything, re-trace
from whatever state results. Useful for commissioning logs that show
the real path.

### debug=True

Surfaces PLC API commands — executable pyrung, a tutorial generator.

---

## Stage 3 — Time folding (Layer 3)  ✅ DONE

**Implemented in 3c7702f** (`feat(runner): integrate fold crossing
arithmetic into run_until/run_for`).

`run_until` and `run_for` now use `fold.py`'s accumulator-crossing
arithmetic internally. PILOT calls the same API:

```python
plc.run_until(HeatDelay_Tmr_Done, max_cycles=5000)
```

Fold computes the crossing mathematically and jumps. The runner verifies
nothing in the upstream cone can change during the interval. Early exit
on stall (timer enable drops) is handled by `plc.when(~enable).pause()`.

PILOT doesn't need to know about folding — it's an optimization inside
`run_until`, not a separate mechanism.

---

## Infrastructure: walk/ → pilot/ migration

PILOT replaces walk/. Once PILOT handles `how()` and `pilot()`, walk/
is deleted. Don't import from walk/ — copy the useful pieces into
pilot/ (with their tests) so walk/ can be removed cleanly.

### Shared analysis (already in the right place, no move needed)

- PDG: `pdg.writers_of[tag]`, `pdg.tag_roles[tag]`, `pdg.rung_nodes`
- `resolve_rung(program, node)` — get the rung object from a PDG node
- `_sp_to_expr(sp_node)` → condition expression tree
- `_extract_condition_values(expr)` → `{tag: frozenset(values)}`
- `_values_match(a, b)` — canonical comparator
- `_written_value_for_tag(rung_obj, tag)` → `Literal` / `Affine` / `UNKNOWN`
- `copy_source_binding(rung_obj, tag, value)` → `(src, src_value)` or None
- `calc_source_binding(rung_obj, tag, value)` → `(src, src_value)` or None

### Runner (already in the right place)

- `PLC(logic)`, `plc.step()`, `plc.run()`, `plc.run_for()`, `plc.run_until()`
- `plc.fork()`, `plc.patch()`, `plc.force()`, `plc.unforce()`
- `plc.when()`, `plc.monitor()`, `plc.diff()`, `plc.cause()`, `plc.why()`
- `plc.state.tags` — current snapshot

### Copy from walk/priors.py (static analysis, zero walk deps)

- `_external_bool_inputs` / `_ack_cleared_bool_inputs` — steerable set
- `_edge_tags` — pulse vs hold (rise/fall detection)
- `_is_scan_transient` / `_transient_handshake_bundles` — consumed-in-
  one-scan detection (the mode change handshake pattern)
- `_unsatisfied_condition_groups` — prerequisite extraction per writer
- `_probe_steps` — simulation ground truth (fork, try, observe)
- `_reference_constants` — never-written copy sources
- `_invert_indirect_source` — idx-chasing for jump tables
- `_governing` — governing tag selection

### Copy from walk/steer.py

- `_steer_prefix` — builds the actual patch (edge semantics, releasing
  other inputs before pulsing)

### Copy from walk/physical.py

- Harness install/replay — feedback coupling during simulation

### Reference material (don't copy directly, but study the approach)

- `walk/base.py` — `NoGoodStore`, `HoldStore`. PILOT's learning is
  simpler (a set of input names to skip, Stage 1; cause()-attributed
  nogoods, Stage 2). But the walker's store design shows what worked:
  keying by (tag, value, blocker), expiry, conflict detection.
- `walk/recovery.py` — `_why_regression`, `_backjump`. PILOT doesn't
  need the recovery generators, but the pattern of using `cause()`
  chains to find the root of a regression and mine protective holds
  from it is exactly what PILOT Stage 2 does. Study how
  `recursive_cause_evidence` chases cause chains to external-input
  roots — that's PILOT's nogood discovery on forks.
- `walk/rules.py` — `mine_regression_holds`, `record_regression_evidence`.
  The idea of extracting "this input must stay True" from a regression
  cause chain maps to PILOT's hold learning.
- `walk/fold.py` — `_advance_time`. The fold integration is already in
  `run_until`, but the plateau detection and accumulator-crossing logic
  may be useful if PILOT needs to reason about how far to run.
- `walk/engine.py` — study `_solve_targets` for how it handles multi-
  goal ordering (committed conjuncts as must-stays, reorder on clobber).

### Leave behind (planner machinery)

Pass registry, corridor BFS, scheduler/frame stack, establish/recovery
generators, independent-walk merging, compress, _WalkAdvice, _DebugSink,
_WalkContext.

The walker's 12 modules exist because `how()` needs a replay-verified
`Path`. PILOT doesn't — the PLC is the verifier.

---

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

**"Let me classify or categorize things."** No. Read the rung. It says
what it writes and what it needs. Classification is abstraction.
Don't abstract.

## Output format

Every PILOT run produces a commissioning log in executable pyrung:

```python
# plc.pilot(y_BurnerLoop)
#
# trace: y_BurnerLoop ← set_outputs ← o_BurnerLoop
#        ← heat.R12 ← Heat_CurStep==3, Heat__x==1
#        ← Heat_xCall==1 ← S_CurrStep_Dry, HeatDelay_Tmr_Done
#        ← S_UnitModeCurrent==1 (unsatisfied)
#        ← S_StateCurrent==6 (unsatisfied)

plc.patch({C_ProductionMode: True, C_UnitModeChgRequest: True})
plc.run_for(1.0)
# observe: S_UnitModeCurrent=1 ✓

plc.patch({C_Clear: True})
plc.run_for(1.0)
# observe: S_StateCurrent: 9 → 2

plc.patch({C_Reset: True})
plc.run_for(1.0)
# observe: S_StateCurrent: 2 → 4

plc.patch({C_Start: True})
plc.run_for(1.0)
# observe: S_StateCurrent: 4 → 3 → 6

plc.run_until(HeatDelay_Tmr_Done)
# observe: Heat_xCall=1, Heat_CurStep → 3

# y_BurnerLoop = True ✓
# DONE in 4 inputs, ~2000 scans
```

Readable. Reproducible. An engineer copies it and it runs.
