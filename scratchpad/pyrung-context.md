# pyrung — overview for context

## What it is

pyrung is a Python-based PLC simulation engine targeting Click PLCs (micro PLCs, max ~144 I/O). You write ladder logic in Python instead of a graphical editor, then simulate, test, and formally verify it — all from pytest and the command line.

The key differentiator is `pyrung lock` — a formal verification tool that exhaustively explores every reachable state of the PLC program via BFS and produces a lock file (like `package-lock.json`) that captures the program's complete behavioral fingerprint. Change the ladder, run `pyrung check`, and the diff tells you exactly what behavioral change you introduced.

## Architecture

The toolchain has layered analysis capabilities, each building on the last:

### Static analysis (`plc.dataview`)
- Dependency graph over the program's tags (variables)
- Role classification: inputs (read-only), pivots (read+write), terminals (write-only), isolated
- Upstream/downstream slicing: "which inputs can affect this output?"
- Simplified form: resolves multi-rung interlock chains back to a Boolean expression over inputs. A 14-rung chain through 10 intermediate tags becomes a two-term expression over 8 inputs.

### Dynamic analysis (`plc.cause()` / `plc.effect()`)
- Causal chain tracing: what caused a transition, distinguishing proximate causes (what changed) from enabling conditions (what was already holding the path open)
- Forward effect tracing with counterfactual evaluation
- Projected cause/effect: "what would cause this?" / "what would happen if...?" without mutating state
- `plc.recovers()`: can a latched bit actually clear?

### Test coverage (`plc.query`)
- Cold rungs (never fired), hot rungs (always fired), stranded bits (latched with no reachable reset path)
- Coverage merging across test suite — negative findings merge by intersection, so residuals shrink as tests are added
- Pytest plugin for automatic collection and CI gating with whitelist support

### Static validators (`logic.validate()`)
- Conflicting outputs, stuck-high/stuck-low latches, readonly violations, choices violations, range violations, antitoggle detection
- No scans needed — pure structural analysis

### Formal verification (`prove()`)
- Exhaustive BFS over the compiled replay kernel
- Returns `Proven`, `Counterexample` (with replayable trace), or `Intractable`
- Timer/counter abstraction: three-phase (False/Pending/True) with time warping through dead scans
- Auto-scoping via dependency cone from the property's referenced tags
- Batch proving: multiple properties share BFS work

### Lock files (`pyrung lock` / `pyrung check`)
- Captures reachable state space as a committed artifact
- `pyrung check` diffs against lock file, exits 1 on behavioral change
- PR diffs show exactly which new states became reachable

Lock API parameters:
- **include** (implemented) — only these tags appear in the locked output. Projection of the reachable state space onto the tags you care about. Typically terminal tags (physical outputs, alarm coils).
- **exclude** (implemented) — drop these tags from the locked output. Noise reduction — remove scratch tags or intermediates you don't want to track across commits.
- **group** (implemented) — these inputs always flip together as a unit. Models physical wiring constraints (e.g., a selector switch that sets one bool and clears another simultaneously). Reduces assignment enumeration — the group moves as one instead of independently.
- **exclusive** (planned) — these inputs are never both true. Mutex constraint — enumerate only none-or-one-hot patterns instead of 2^N raw combinations. Detected statically where possible (inputs as pivots in mutually exclusive rung branches), manual annotation for physical/protocol constraints the analyzer can't infer.
- **cut** (planned) — promote this internal tag to nondeterministic at a cluster boundary. Enables assume-guarantee decomposition: verify each side independently under worst-case assumptions about the other. Typical use: cut a completion bool that couples a task to the state machine. Over-approximation is sound — explores more, not less.

### Fault coverage
- Structural: `prove()` verifies every feedback device coupling has a detection path to an alarm
- Timing: force-based tests verify fault timers trip fast enough
- Auto-iterates `harness.couplings()` — no manual device list to maintain

## BFS performance optimizations (in progress)

The PackML benchmark (full ISA-88 state machine with 17 states, 3 modes, 17 ND inputs, indirect block lookups, alarm historian, timed task sequencing) is the stress test. Current status: ~52K states, 31+ min timeout.

### Free input elision
ND (nondeterministic) inputs without edge semantics (no rise/fall/oneshot/clock/jog) are "free" — they can take any value on any scan, so their current value in the BFS state key is redundant. The BFS already enumerates all values from every state, so two states differing only in free inputs have identical successor sets.

On PackML bench: 15 of 17 inputs are free (only `CmdChgRequest` with `rise()` and `ModeChgRequest` with `oneshot` have edges). Eliding 15 free inputs collapses the state space from ~2^15 multiplier to ~2^2. ~52K states → ~4-8K.

Edge classification must also scan for inputs in shift register `.clock()` and drum `.jog()` arguments — these are implicitly edge-triggered (OFF-to-ON transition per Click spec).

### Exclusive family canonicalization
Detect mutually exclusive input groups (e.g., CmdReset through CmdComplete — only one active at a time) and enumerate canonical one-hot patterns instead of raw 2^N combinations. Reduces edges per BFS node. Orthogonal to elision.

### Blockless kernel
Block sync (copying tags ↔ block arrays) was 66% of step cost. Fix: compiler flag that emits direct tag dict access instead of block array indexing. Eliminates ~450s of sync overhead.

### State key minimization
Abstract-then-concrete elision pipeline:
- Abstract pass (static): WBR (written-before-read), terminals, `out()` coils, obvious projections
- Concrete pass (during BFS): discover remaining redundancies by observing identical successors

PackML bench key reduced to 4 tags: `StateCurrent`, `UnitModeCurrent`, `CmdChgRequest` (prev), `ModeChgRequest` (prev).

### Combined target
PackML bench: 31+ min (timeout) → under 2 min.

## Cluster decomposition (future direction)

Programs can often be decomposed into independent clusters that don't share state, verified separately.

- `dataview.clusters()` could identify strongly connected components in the stateful dependency graph
- `dataview.bindings()` could surface the tags that couple clusters
- Common coupling pattern: task writes a completion bool that the state machine reads. Cut it (treat as nondeterministic), verify both sides independently.
- Physical handshake pattern (task drives output → physical world → external sensor → state machine) has no logical coupling — the sensor is already nondeterministic.
- Most Click programs are "state machine orchestrates, independent tasks execute" — naturally decomposable.
- Over-approximation from cutting is sound (explores more, not less) and may catch bugs that coupled verification misses (e.g., "what if the task never completes?").

## Send/receive (Modbus)

`receive()` destination tags are inherently nondeterministic — the verifier can't predict what a remote device sends. These should be automatically inferred as external without user annotation, then flow through the same partition/elision pipeline as physical inputs.

## Why it matters

- PLC bugs cause physical consequences — machine damage, product loss, injury
- No existing tool does automatic formal verification of PLC programs without requiring formal specs or abstract models
- pyrung verifies the actual compiled program — no equivalence gap
- Click's small scale makes exhaustive BFS tractable without SAT/BDD solvers
- The lock file makes verification results reviewable by controls engineers who know nothing about formal methods
- Adoption path: people use pyrung for simulation/testing → `pyrung lock` is one command → CI catches regressions → value is invisible until the one time it saves you

## Instruction set edge semantics (relevant to verifier)

From pyrung's DSL, these constructs have edge-triggered behavior (previous value matters):
- `rise(tag)` — one scan on False→True
- `fall(tag)` — one scan on True→False
- `oneshot=True` — one-scan pulse
- Shift register `.clock()` — shifts on rising edge of clock
- Drum `.jog()` — advances step on rising edge
- Drum event conditions — advance on rising edge
- Drum `.jump()` — likely edge-triggered (verify)

These are level-sensitive (no edge state):
- Counters — count every scan while true (use `rise()` explicitly for edge counting)
- Timers — level-sensitive accumulators
- `out()` — unconditional coil write
- `latch()` / `reset()` — level-sensitive set/clear
