# Corridor Walker — Architectural Plan

Operational details (status, findings, validation, open items) live in
[corridor_walker_notebook.md](corridor_walker_notebook.md).

---

## Theory statement

The corridor walker rests on a provable structural argument, not just an
engineering bet.

A single-scan, no-interrupt PLC program is a deterministic function from
(state, inputs) → state′. PLC programs are producer-consumer hierarchies of
sequential corridors coupled through narrow handshake interfaces (ISA-88,
PackML, IEC 61131-3 SFC enforce this by design). The program is its own
executable model — forkable, steppable, fully observable. Forward progress is
ground truth (step and observe). Backward structure is exact (read the SP-tree
via `simplified()`/`cause()`/`why()`). To solve a reachability goal: factor
into subsystems via the coupling structure, walk each corridor forward using
backward structure to steer and recover, force coupling signals to decouple
timing, verify feasibility by summing achieved depths against handshake
deadlines.

This is lock-and-key maze solving in a structurally tractable slice: most gates
are one-state (interlocks — polynomial), the gates are readable
(simplified/cause — no search needed for key identification), and the timed
gates decompose (producer-consumer, not adversarial). The general gadget-maze
problem is PSPACE-complete (Demaine, Hendrickson, Lynch); PLC programs are in
the easy subclass because the standards enforce simple locks, readable
conditions, and hierarchical key ordering.

**Bounded-width claim (Lipovetzky–Geffner serialization).** The corridor
decomposition serializes the problem: each sub-problem's atomic width is
bounded because ISA-88/IEC 61131-3 gates involve O(1) variables. The
governing-value × blocking-key `seen` set is an implicit novelty measure
over this serialization. Completeness is structural for serializable
instances (bounded width), budget-gated for the rest.

**Scope constraint:** single-scan PLC without interrupts. Multi-task PLCs with
priority-based preemption (S7-1500 OBs, ControlLogix periodic/event tasks)
break the deterministic-order guarantee and are out of scope. Extension would
require modeling interrupt semantics as additional nondeterminism.

---

## The planner, in POCL vocabulary

The walker is a **hierarchical planner** with three layers:

1. **Abstract** — collapse the full PLC state space to one governing tag's
   value graph (tiny: mode machines have single-digit values).
2. **Plan** — best-first search over that abstract value space (cheap).
3. **Refine** — for each abstract edge, find concrete inputs via interpreted
   simulation on forks (sound by construction — immune to static-analysis
   blindness).

Its working vocabulary is **partial-order causal-link (POCL) planning**,
executed forward on the real interpreter:

- An **open condition** is an unachieved goal `(tag, value)` — raised by
  target decomposition, a writer's enabling condition, a residual, or an
  oracle re-check.
- A **hold** is a causal link: (external input, value, the committed goal that
  depends on it) — a protection interval over the walker's *own hand*.
  External inputs are sticky and entirely under walker control, so there is no
  abstraction gap to be wrong about; holds never assert anything about the
  program.
- A **threat** is a steer that would break a hold. Threats are detected by
  construction (the `HoldStore`), not discovered after the fact by
  clobber-recovery.
- A **resolver** is one of four responses to a flaw: **establish** (walk a
  corridor / sub-goal), **reorder** (move the threatened goal), **divest**
  (the empirical white-knight probe: fork, release, settle, check the held
  goal survives), **reject** (record a nogood, abandon the branch).

Deliberately *not* borrowed: POCL's least-commitment ordering (would force
replays, surrendering the forward-fork oracle) and PDR's frame/induction
machinery (assumes a symbolic transition relation). From PDR we take the
mechanics only: one deepest-first queue of obligations, a global budget,
stale-item skipping, and nogood generalization by drop-and-retest (§Settled
architecture).

Self-conflicts stay out of the program's ledger: `cause()` explains the
program; holds are the walker's hand. A cause()-named blocker that is a held
input is classified one layer up (`_classify_blockers(goals, holds)`) and
routed to divest or reorder — it never enters the `NoGoodStore`. Nogoods
record program facts only.

Everything in scope maps to one of four extensions of the core:

| Extension | What it does | Planning concept |
|---|---|---|
| **Widen the input** | Accept goals the engine punts on (Or/And decomposition, multi-tag factoring) | Better abstraction / goal preprocessing |
| **Widen the alphabet** | Succeed on more transitions (non-Bool inputs, multi-input steers, link-aware de-energize) | Richer action space for refinement |
| **Backtrack on failure** | Re-plan with learned constraints when refinement fails or execution diverges | Hierarchical backtracking |
| **Diagnose infeasibility** | Explain why no plan is feasible | Explanation generation |

The guiding question for every new mechanism: **does it extend the existing
engine's reach, or does it add a parallel path?** Prefer the former. The
guiding rules, post-consolidation:

- Anything readable from the SP-tree or PDG → ordering advice via the pass
  registry.
- Anything knowable only by running → learned nogood in the loop.
- Every new mechanism is a resolver, a flaw source, or a pass — **never a new
  loop**.

**Static analysis is a prior, never correctness-bearing** — it picks the
governing tag, narrows the steer alphabet, and sets the horizon. Correctness
comes from simulation. Validation is always interpreted.

### The oracle advantage

Unlike classical planners (which reason over a symbolic model that may diverge
from reality), the corridor walker operates on **the program itself as a
white-box oracle**: forkable, per-rung steppable, with full observability of
what was read/written. There is no abstraction gap — simulation IS the
program. This shapes the architecture:

| Layer | Role | Tools | Properties |
|-------|------|-------|------------|
| **Generate candidates** | Narrow the search space | `why()` (state-aware minimal), `simplified()` (structural), PDG (coarse) | Heuristic — may over-/under-generate |
| **Forward exploration** | Try candidates | `fork()` + step (the walker engine) | Ground truth — deterministic, sound |
| **Validate / explain** | Confirm cause or diagnose failure | `cause()` on scan log | Recorded truth — what actually happened |

The symbolic layer generates candidates; the interpreted layer validates them.
No CEGAR loop needed because the "refinement check" runs the real program —
spurious abstract paths are caught in one step, not iteratively refined away.

**Candidate generation tools (finest → coarsest):**

- **`why(tag)`** — backward SP-tree attribution from a snapshot. Gives the
  *minimal load-bearing contacts* explaining the current value. State-aware:
  prunes irrelevant formula branches given the actual fork state. Use when you
  need "what's holding this tag HERE" (steer prioritization, regression
  sub-goals, factoring).
- **`simplified(condition)`** — resolved Boolean form to input-level. Structural:
  all paths through the formula regardless of current state. Use when you need
  "what COULD make this true/false" (full regression, enabling-condition
  analysis).
- **PDG** — `upstream_slice`, `writers_of`, `condition_reads`. Coarsest static
  connectivity. Use for cone-narrowing, solve-order proposals, independence
  screening.

**Validation tool:**

- **`cause(tag)`** — recorded-mode causal analysis on the scan log. Gives
  trigger vs. enabler split: what *transitioned* the tag vs. what was already
  in place. Use after simulation to confirm which inputs were load-bearing, to
  extract nogoods from failures, and to produce actionable diagnosis.

---

## Settled architecture

Static advice in through a pass registry, one agenda loop in the middle,
verified plans and a triangle table out. One deepest-first agenda of flaws
(open conditions + threats), four resolvers (establish, reorder, divest,
reject), plan tree flattened to `Path` at build time, `TriangleTable` /
`kernel(i)` derived from holds + steps (Fikes–Hart–Nilsson 1972 / PLANEX),
`Diagnosis` reading the plan tree + nogoods + journal to distinguish
`Unsolvable` from `NotFound`. Passes are registered, frozen before the walk,
and ablation-tested by kind.

Writer alternatives are carried as structured candidates, not bare
unsatisfied-condition lists: each candidate keeps its full enabling context,
the already-satisfied/live branch guards, the unsatisfied sub-goals, and the
writer's static write footprint. The `context_aware_groups` ordering pass uses
that context to prefer writers aligned with active must-stay state, defer
possible write conflicts, and promote the selected satisfied guards into child
`_StepMonitors`. The old per-writer group projection remains as an ablation
path and compatibility API.

### Nogood generalization (the open direction)

The next generalization step is PDR-shaped: after learning a failure, drop
facts and re-test — the simpler version that still fails is the real nogood.
PDR needs a SAT solver for the re-test; the walker forks and runs. Broader
nogoods prune more on deep interlock chains. This is the one borrowed idea
that extends reach on harder programs rather than cleaning code.

Residual risk: the store stays shared per `plan_walk` and add-only, so
accumulated blocking names fragment `seen`-keys for *unrelated* goals.
Relation facts shrink scalar over-specialization but do not scope the store.
If fragmentation still bites: project per-goal — only nogoods whose `(from,
to)` involves the current governing tag.

For counter-like governing tags, recovery records nogoods identical in blocking
but differing only in the drifting from-value. Drop the from-value and re-test
on a fork at a different from-value; if the failure persists, wildcard it — one
generalized nogood replaces N exact ones. Tripwire: a counter-valued governing
tag where recovery accumulates redundant exact-key nogoods.

### Regression-triggered protective holds

When a committed progress goal regresses after a child frame completes,
`_check_progress_regression` (always-on, not debug-gated) traces the
actual cause via `cause()` → `_walk_chain` to external-input roots, and
installs their pre-regression values as protective holds.  The steer
release prefix then skips those inputs on subsequent corridors.  Ownership
boundary: target-decomposition frames are handled by `_solve_targets`'
reorder loop; all deeper frames use the regression-hold path.

Mechanism (in `rules.mine_regression_holds`):
1. `work.cause(regressed_tag)` — actual-cause chain of the regression
2. `_walk_chain` — drill to external-input roots via `_is_actionable_root`
3. `root.from_value` — the pre-regression value is the protective hold
4. `holds.protect(name, from_value, regressed_goal)` — registered in `_drive`
5. `frunner.patch(protective_values)` + step — immediate fix on the work fork

The b15029e infrastructure (`_walk_chain`, `_is_actionable_root`,
`recursive_cause_evidence`) does the chain-tracing; `mine_regression_holds`
extracts `from_value` (protective) instead of `to_value` (caused).

### Constructive regression (frontier-terminated why)

When explore, static prerequisite extraction, and oracle recovery all come up
empty, the agenda falls back to frontier-terminated `why()` on the work fork.
`why_cause` grows an optional `frontier` predicate: backward SP-tree
attribution terminates at any tag the walker can act on (ext inputs, edge ext,
multi-value ND domains, already-committed goals) rather than only at external
inputs. The conjunctive roots are the nearest actionable sub-goals — being
state-aware AND structural, the walk follows the live branch of Or-gates that
the static extractor drops (the former #8 Or-gate gap). Goals that would flip
a protected hold are filtered via `HoldStore.filter_conflicting`.

Attrition policy: new shapes go to the why-regression source only; per-shape
extractor branches are frozen (the `_operand_candidates` / operand-moving
block in `_extract_inequality_prereqs` was deleted — the fill shape is now
carried entirely by the regression source, pinned by
`test_walk_why_regression::test_fill_shape_solves`).

### Debug trace (`how(debug=True)`)

Structured event collector threaded through `_WalkContext.debug_sink`.
When enabled, captures: PDG upstream cone snapshots (surfaces whether a tag
like x_RotateSensor is even in scope), oracle chain dumps (`projected_cause`
and `why_cause` full results at every recovery step), goal lifecycle
(start/resolved/failed with depth and provenance), hold registrations, and
budget exhaustion. Attaches to `Path.debug_trace`; renders via `str()`.
Zero cost when `debug=False` — every emit site is guarded by a `None` check.

Replaces the manual probe-script workflow: instead of writing a separate
script to call `cause()`, `why()`, and check PDG cones, `how(tag,
debug=True)` surfaces the same information automatically.

---

## Future scope

- **Multi-corridor timing (Tiers 2–3)** — force-and-solve with deadline checking, cyclic coupling convergence, co-advance synchronization.
- **Steer-history reuse** — try previously-successful steers first; periodic promotion for recurring obligations.
- **Symmetry transfer** — structural isomorphism detection and solved-sequence transfer across repeated subsystems.
- **Cheap steer pre-screening** — evaluate candidates against `simplified()` before forking.
- **Callable predicate (`expr=None`)** — opaque predicates need expr decomposition.
- **Ack-cleared Ints** — widen ack-cleared-input pattern to Ints/Words.
- **Transform-chasing** — regression across pack/unpack/copy-convert data-flow boundaries.

Full details in [corridor_walker_notebook.md](corridor_walker_notebook.md#future-scope-beyond-the-stages).

---

## Research grounding

The individual mechanisms all have prior art. The novel contribution is
precisely scoped below.

The **closed loop** — actual-cause attribution (`cause()`) as the repair
signal in a solver-free forward planner over the executable program, aimed at
producing an operator-executable plan. The analyzer is Halpern–Pearl. The
planner is directed model checking with POCL bookkeeping. The regression is
System-R. Wiring them together without a solver, because the program is the
model: that's the contribution.

Classical planners need a solver to bridge the gap between model and reality.
When the model IS reality (forkable, per-rung steppable, deterministic), the
solver collapses to "try it and observe." The program runs on forks; `why()`
generates candidates by backward SP-tree attribution on the live state;
`cause()` validates by recorded-mode scan-log analysis; the walker steps
forward on the real interpreter.

Full prior-art table and key papers in
[corridor_walker_notebook.md](corridor_walker_notebook.md#research-grounding).
