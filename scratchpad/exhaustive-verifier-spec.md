# Exhaustive Verifier & State Space Snapshot

## Context

pyrung is a Python DSL for ladder logic (PLC programming). The engine is a Redux-style pure-function scan cycle: `Logic(CurrentState) → NextState`. State is an immutable PMap. A compiled replay kernel (`compile_replay_kernel()`) provides a fast `step()` over plain dicts.

We already have:

- `program.dataview()` — tag role classification (inputs/pivots/terminals), upstream/downstream slicing
- `program.simplified()` — resolved Boolean expressions per terminal (transitive pivot substitution)
- `expr_requires()` / `reset_dominance()` — structural implication checks
- `WriteSite` / `_collect_write_sites()` — condition context for every write in the program
- Compiled replay kernel — fast step function on plain dicts, skips SystemState construction
- Tag metadata flags — `readonly`, `external`, `final`, `public`, `choices`, `min`/`max`
- SP-tree condition attribution — series/parallel structure of rung conditions
- Miner that proposes candidate invariants from recorded traces
- Pytest coverage plugin with CI gating and whitelist support

## Core Insight

Every value that influences program behavior passes through a comparison on the condition rail. Every comparison is a Boolean atom in the expression tree:

```python
Atom(tag="Timer1.Done", form="xic")        # timer done → Bool
Atom(tag="State", form="eq", operand=2)    # integer compare → Bool
Atom(tag="Pressure", form="gt", operand=50) # range compare → Bool
```

The source of the value doesn't matter — network receive, copy chain, calc result, timer accumulation — only the set of comparison points matters. This means:

1. The state space is fundamentally Boolean (or small-finite-domain)
2. `simplified()` already collapses all combinational pivots to input expressions
3. The remaining dimensions are stateful tags (latches, timers, counters) consumed through comparisons
4. Tag metadata (`choices`, `min`/`max`) bounds the domain of every nondeterministic value — including Modbus receive and indirect addressing targets

## Architecture: Expression Tree Analyzes, Kernel Executes

Two distinct roles, cleanly separated:

**Expression tree** (static, computed once): Analyzes the program structure to reduce the search space.
- Dimension classification — which tags are state vs. combinational vs. input
- Value domain extraction — comparison literals, `choices`, `min`/`max`
- Don't-care pruning — at a given state, which inputs are masked and can be skipped
- Cone separation — which input groups can be enumerated independently

**Compiled replay kernel** (dynamic, called per transition): Computes the actual next state.
- Handles all instruction semantics correctly (timer accumulation, copy clamping, calc wrapping, edge detection, drum sequencing)
- Takes plain dicts, mutates in place — no SystemState overhead
- Already exists, already tested, no reimplementation needed

The expression tree tells us WHICH input combinations to try (reducing 2^16 to 2^6). The kernel tells us WHAT HAPPENS for each combination. Without the expression tree, we'd brute-force all inputs. Without the kernel, we'd reimplement every instruction's semantics in the verifier.

## What to build

Three public functions in `pyrung.core.analysis.verification` (new module).

### 1. `verify_invariant(program, predicate, scope=None, max_depth=50, max_states=100_000)`

BFS over the reachable state space.

- `predicate`: `(state_dict) -> bool` — the property to check
- `scope`: optional list of tag names — if given, use `dataview.upstream(*scope).inputs()` to find the minimal set of nondeterministic input tags to enumerate
- `max_depth`: BFS depth limit (scan cycles)
- `max_states`: visited-set cap — bail with `Intractable` if exceeded

Algorithm:
1. Classify dimensions (see below)
2. Compile replay kernel from program
3. BFS queue seeded with initial state (all tags at resting values)
4. At each state, identify **live inputs** via expression tree (don't-care pruning) and enumerate only those
5. For each input combo: set inputs in kernel state, call `kernel.step()`, check predicate
6. If predicate fails → return `Counterexample` with trace (via parent pointers)
7. If new state not in `visited` → enqueue (store parent pointer)
8. Queue exhausted → return `Proven(states_explored=len(visited))`

State identity for `visited`: hash only the **state dimensions** (stateful tags + consumed accumulators). Combinational tags are deterministic and excluded.

### 2. `reachable_states(program, scope=None, project=None, max_depth=50, max_states=100_000)`

Collect the full reachable state space. Thin wrapper over the same BFS.

- `project`: optional list of tag names to project onto. Three common uses:
  - `None` — full state
  - `program.dataview().terminals().tag_names()` — I/O lock (free internals)
  - Specific tag list — scoped lock

Returns a `frozenset` of `frozenset` (each inner frozenset is one reachable state projected to the requested tags).

Usage for refactoring equivalence:

```python
before = reachable_states(original_program, project=terminals)
after = reachable_states(refactored_program, project=terminals)
assert before == after, diff_states(before, after)
```

### 3. `diff_states(before, after)` (helper)

Returns a `StateDiff` with `.added` (new reachable states) and `.removed` (lost reachable states). Traces included via parent pointers stored during BFS — no re-exploration needed.

## Dimension Classification

Partition all tags into three categories using existing infrastructure:

| Category | Source | Role in verifier |
|----------|--------|-----------------|
| **Combinational** | In `simplified()` forms (OTE-resolvable pivots) | Derived — not a state dimension, not enumerated |
| **Stateful** | Written by latch/reset/timer/counter/copy/calc/drum | State dimensions — tracked in visited set |
| **Nondeterministic** | `external` tags, or `dataview().inputs()` fallback | Enumerated at each state |

### Value domain per dimension

**Bool tags**: `{False, True}`

**Non-Bool tags** — enumeration cascade, in priority order:

1. **All comparisons against literals** — extract every literal the tag is compared against in the expression tree (`Atom(form="eq", operand=2)`, etc.) plus one "unmatched" representative. Fully derived from program structure, no annotation needed. This is the common case for state machines.
2. **Any comparison against another tag** — runtime-determined comparison target. Fall back to `choices` or `min`/`max` to enumerate declared values. Without annotation, flag as `Infeasible` rather than silently enumerating 65K values.
3. **No comparisons at all** — tag isn't consumed as a condition. Not in the state space.

### Tag metadata prunes further

The engineer declares tag metadata for other reasons (Data View filtering, static validation, debugger dropdowns). The verifier consumes it for free:

- `readonly` — constant, exclude from enumeration entirely
- `external` — nondeterministic input, enumerate its value domain. This covers Modbus receive tags and indirect addressing targets — annotate with `choices` or `min`/`max` to bound the domain.
- `final` — exactly one writer, can often be derived rather than enumerated
- `choices` — declared value domain, cross-checked against extracted comparison literals
- `min`/`max` — range constraint for numeric inputs
- Timer `.Done` is just a Bool. Non-consumed accumulator tags are not in the state space.

## Input Space Reduction

The expression tree's primary value: reducing how many times the kernel gets called per state.

### Don't-care pruning

At each state, some inputs are masked by the current state. Example: `And(StateBit, Input_A)` — if `StateBit` is False, `Input_A` doesn't influence any transition. Walk the expression trees (simplified forms + write-site condition expressions) to identify live inputs per state; enumerate only those.

If 10 of 16 inputs are don't-cares in a given state, enumerate 2^6 = 64 kernel calls instead of 2^16 = 65,536.

### Cone separation (optimization)

If Input_A only feeds stateful tag X and Input_B only feeds stateful tag Y (disjoint upstream cones via `upstream_slice()`), enumerate independently: 2^a + 2^b instead of 2^(a+b).

### Scope-based slicing

When checking a specific invariant with `scope=`, use `dataview.upstream(*scope).inputs()` to restrict enumeration to the relevant input cone. Tags outside the cone don't affect the property being verified.

## The One Escape Hatch

`run_function()` / `run_enabled_function()` — opaque Python functions whose outputs feed conditions. The verifier cannot trace the logic. If outputs lack `choices`/`min`/`max` annotation, flag as `Infeasible` with a clear message. If annotated, treat as nondeterministic inputs with declared domain.

Everything else — Modbus receive, indirect addressing, copy chains, calc, timers, counters, drums — collapses to the expression tree + tag metadata for analysis, and the kernel for execution.

## Return Types

```python
@dataclass(frozen=True)
class Proven:
    states_explored: int

@dataclass(frozen=True)
class Counterexample:
    trace: list[dict]  # input dicts per scan, built from parent pointers

@dataclass(frozen=True)
class Intractable:
    reason: str          # "max_states exceeded" or "unbounded domain on Tag"
    dimensions: int      # number of state dimensions identified
    estimated_space: int  # product of domain sizes

@dataclass(frozen=True)
class StateDiff:
    added: frozenset   # states reachable after but not before
    removed: frozenset # states reachable before but not after
```

## Lock file: `pyrung.lock`

The reachable state snapshot serializes as a committed artifact, same mental model as `uv.lock` or `package-lock.json`. The lock captures known-good behavior; CI fails if the program no longer matches.

### CLI

```
pyrung lock              # compute reachable states, write pyrung.lock
pyrung check             # recompute, diff against pyrung.lock, fail if changed
pyrung lock --update     # regenerate after intentional changes
```

### Projection defaults

The lock projects to `public` tags by default (the engineer already declared their API surface). Fallback to all terminals if nothing is marked public. Overridable via `--project`.

### File format

Human-readable so the diff shows up meaningfully in PRs:

```json
{
  "version": 1,
  "program_hash": "a3f2...",
  "projection": ["public"],
  "reachable": [
    {"Conv_Motor": false, "Running": false, "Conv_StatusLight": false},
    {"Conv_Motor": false, "Running": true,  "Conv_StatusLight": true},
    {"Conv_Motor": true,  "Running": true,  "Conv_StatusLight": true}
  ],
  "unreachable_examples": [
    {"Conv_Motor": true, "Running": false},
    {"Running": true, "EstopOK": false}
  ]
}
```

The `unreachable_examples` section is optional — calls out interesting impossibilities. Miner candidates that were proven populate this naturally.

### PR diff tells the story

```diff
  "reachable": [
    {"Conv_Motor": false, "Running": false, "Conv_StatusLight": false},
    {"Conv_Motor": false, "Running": true,  "Conv_StatusLight": true},
    {"Conv_Motor": true,  "Running": true,  "Conv_StatusLight": true},
+   {"Conv_Motor": true,  "Running": false, "Conv_StatusLight": true}
  ],
  "unreachable_examples": [
-   {"Conv_Motor": true, "Running": false},
  ]
```

Reviewer sees: "Conv_Motor can now be on while Running is off. That wasn't possible before."
Either intentional (run `pyrung lock --update`) or a bug (fix the code).

### Integration

Slots into the existing pytest coverage plugin alongside cold rungs and stranded bits. Same `--pyrung-whitelist` pattern for CI gating.

## Tests

In `tests/core/analysis/test_verification.py`:

### Functional tests

- **Basic invariant holds**: conveyor program, `Running ⟹ EstopOK`, returns `Proven`
- **Invariant violation**: program where the invariant doesn't hold, returns `Counterexample` with valid trace
- **Counterexample trace is replayable**: take the trace from a `Counterexample`, replay on a real PLC runner, confirm the violation reproduces
- **Scoped slicing**: verify with `scope=` reduces input enumeration to relevant tags only
- **Metadata pruning**: `external` tags are enumerated, `readonly` tags are not, `choices` tags enumerate declared values only
- **Reachable states snapshot**: compute, refactor equivalently, assert equal
- **Public projection**: project to `public` tags, change internals, assert equal
- **Behavioral change detected**: introduce a real change, assert `StateDiff.added` is non-empty
- **Lock round-trip**: generate lock file, reload, verify matches fresh computation
- **Intractable program**: unbounded integer tag with no annotations, returns `Intractable`
- **max_states cap**: program with large state space, verify BFS bails at limit
- **Function call escape hatch**: program with unannotated function output → `Infeasible`; with `choices` → verifiable

### Dimension classification tests

- **OTE-only program**: all tags combinational, state space = 1 (just the initial state)
- **Latch/reset program**: latched tags are state dimensions, inputs enumerated
- **Timer program**: Timer.Done is a Bool state dimension, accumulator not in state when unconsumed
- **Integer state machine**: comparison literals extracted, "unmatched" representative included
- **Copy chain**: destination tag gets source's domain, transitions traced correctly
- **Choices cross-check**: declared `choices` vs extracted literals, mismatch surfaced

### Don't-care pruning tests

- **Masked input skipped**: `And(StateBit, Input)` with StateBit=False → Input not enumerated
- **All inputs live**: every input influences a transition → full enumeration
- **Partial masking**: some inputs masked, others live → correct subset enumerated

### Kernel oracle tests

These confirm that the expression-tree analysis (dimension classification, don't-care pruning, value domain extraction) correctly models the program's behavior. Run BFS twice — once with pruning, once brute-force — and assert identical reachable sets:

- **Pruned vs brute-force agreement**: for several representative programs, run `reachable_states()` with don't-care pruning enabled and disabled — assert identical results
- **Value domain completeness**: for programs with integer state machines, verify that extracted comparison literals + "unmatched" cover all kernel-reachable values
- **Scope slicing soundness**: verify that scoped exploration finds the same violations as full-program exploration
- **Timer accumulation**: programs with timers at various presets, verify BFS explores accumulator progression correctly
- **Edge detection**: programs with `rise()`/`fall()` conditions, verify prev-value tracking produces correct transitions
- **Counter programs**: verify count-up/count-down transitions match across accumulation sequences

## What this replaces

This makes external model checkers (NuSMV, CBMC, Z3) permanently unnecessary for the program sizes pyrung targets. The structural checks (`expr_requires`, `reset_dominance`) remain as the *explanation* layer — they tell you *why* an invariant holds, while the verifier proves *that* it holds.

The lock file replaces hand-written regression invariants. The engineer doesn't write "Running implies EstopOK." They write their program, mark their public tags, and the lock captures everything — including invariants they never thought to write.
