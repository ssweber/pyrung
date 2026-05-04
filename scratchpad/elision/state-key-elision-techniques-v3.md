# State Key Elision Techniques for PLC Model Checking (v3)

## Investigation Summary

This document surveys model checking reduction techniques from the literature and evaluates their applicability to pyrung's explicit-state BFS verifier. The focus is on techniques that shrink the state key without user annotation, exploit PLC scan-cycle structure, and strengthen the abstract phase to reduce reliance on the expensive concrete phase.

Updated after code review of `elision.py`, the PackML benchmark, the full pyrung instruction set, and a literature search confirming novelty.

---

## 1. What You Already Have (and Where It Sits in the Literature)

### 1.1 Abstract Phase (Provenance Lattice)

A form of *data-flow-driven variable elimination*, closely related to cone-of-influence (COI) reduction and dead variable analysis. The key insight from Bozga, Fernandez & Ghirvu (SAS 1999, "State Space Reduction Based on Live Variables Analysis") is that dead variables — those whose current value will be overwritten before being read — can be reset to a canonical value without changing observable behavior. Your "written-before-read" and "unconditional `out()` coil" checks are essentially liveness analysis adapted to single-scan PLC semantics. The provenance lattice (`_AbsValue` with `depends_on_entry`, `depends_on_retained`, `depends_on_inputs`, `unknown`) is abstract interpretation over a four-element dependency lattice.

Already implemented and working:

- **Path-sensitive guard merging** — `_merge_values` takes `guard_dep` and unions it with branch values. When a rung guard depends only on retained state, entry-independence is preserved through the merge.
- **Scan-fixed-point detection** — `_prove_tag_from_canonical_entry` iterates within the abstract domain: start from default, compute exit, use as next entry, repeat until convergence. Catches tags whose self-dependency is contracting.
- **Deterministic projection detection** — tags whose exit values are pure functions of retained state are identified and elided via `_compute_nonretained_summaries` fixed-point loop.

### 1.2 Concrete Phase (Exhaustive Kernel Proofs)

*Exact quotienting by simulation.* You prove that two states differing only in tag `t` produce identical successor observations for all input combinations. This is bisimulation-compatible state merging: if `s₁` and `s₂` agree on all retained tags but differ on `t`, and for every input `i` we have `δ(s₁, i)|_observed = δ(s₂, i)|_observed`, then `t` is redundant. The literature calls this "output-preserving bisimulation" or "quotient by observational equivalence." The concrete oracle is the actual compiled kernel — zero semantic gap between proof and execution.

### 1.3 Free ND Input Elision (Implemented)

Removing edge-free inputs from the key while still enumerating them during BFS is a form of *existential quantification over inputs*. Valid because level-triggered inputs are memoryless: their current value places no constraint on future scans. This is a PLC-specific insight that doesn't appear in generic model checking literature.

### 1.4 Blockless Kernel (Implemented)

Eliminates block sync overhead (copying thousands of tags between tag dicts and block arrays every BFS step). Direct tag dict access at codegen time.

### 1.5 Novelty

The PLC verification literature is almost entirely "translate to NuSMV/UPPAAL/CBMC, use their built-in solvers." PLCverif (CERN) translates PLC code to external model checkers and relies on BDD-based symbolic checking, SAT-based bounded model checking, or CEGAR with SMT. Their scaling challenge for large UNICOS frameworks is handled by modular variable abstraction — still symbolic, still external tools.

What doesn't appear in the literature is the combination:

- Two-phase hybrid where the concrete oracle is the actual compiled kernel, not a model of it.
- The free ND input insight — exploiting PLC scan-cycle semantics to distinguish edge-bearing inputs (need prev values in key) from level-triggered inputs (enumerate but don't track).
- The overall strategy of "shrink the key until BFS is trivial" rather than "use symbolic methods to tolerate a big key."

Each piece has ancestors. The specific machine — abstract provenance lattice filtering into exhaustive kernel proofs, with scan-cycle-aware input classification, targeting explicit-state BFS on synchronous deterministic kernels with nondeterministic external inputs — is novel.

---

## 2. Techniques That Could Further Reduce the State Key

### 2.1 Write-Coverage Under Exhaustive Guards

**What it is:** A cross-rung analysis that collects all rungs writing a given tag, extracts their guard conditions, and checks whether those guards collectively form a tautology — meaning the tag is always written in every scan and never retains its entry value.

**Why it matters:** This is the most common source of false `depends_on_entry` in structured PLC programs. The abstract phase currently analyzes each rung independently. For `latch(T)` under guard `G`, it merges the enabled path (`T = True`) with the disabled path (`T = entry_value`), producing `depends_on_entry`. Same for `reset(T)` under guard `~G`. But taken together, the two writes cover all cases — `T` is always written.

**Three patterns this catches:**

*Complementary latch/reset pairs:*
```python
with Rung(AutoMode):
    latch(Running)
with Rung(~AutoMode):
    reset(Running)
```
Each rung individually produces `depends_on_entry` from its disabled path. But `AutoMode ∨ ~AutoMode` is a tautology — `Running` is always written.

*State-machine case splits:*
```python
with Rung(StateCurrent == 1):
    copy(something, T)
with Rung(StateCurrent == 2):
    copy(something_else, T)
# ... for every state value
```
If `StateCurrent` covers all reachable values and every branch writes `T`, the guards form an enum tautology.

*Multi-rung output coverage:*
```python
with Rung(ModeA):
    copy(1, StatusReg)
with Rung(~ModeA):
    copy(0, StatusReg)
```

**How to implement:** After per-rung abstract interpretation, for each candidate still showing `depends_on_entry`:

1. Collect all rungs that write it.
2. For each such rung, extract the guard condition.
3. If all guards are comparisons against a single retained tag, check whether the guard values cover the tag's known domain (from `choices` metadata, comparison literals, or explicit domain annotations).
4. For boolean guards, check for complementary pairs (`G, ~G`).
5. If the guards are collectively exhaustive, mark `depends_on_entry=False`.

**Difficulty:** Medium.

### 2.2 Instruction-Level Abstract Rules

The abstract phase can exploit the specific semantics of each instruction type to make sharper inferences than generic dataflow analysis.

#### 2.2a Timer Abstraction Awareness

`on_delay` (TON) unconditionally resets both `done` and `acc` when the rung is False. In the enabled path, acc reads its entry value (incrementing). The merge correctly produces `depends_on_entry`. But for verification, the BFS uses a three-valued timer abstraction (`False`/`Pending`/`True`). The accumulator's actual INT value is irrelevant to state identity — only the done-bit abstraction matters.

If the elision pass operated on the abstract timer domain rather than the raw tag domain, the done bit has three values and the accumulator is eliminated entirely.

**Difficulty:** Medium. Requires the abstract phase to know about the BFS timer abstraction.

#### 2.2b Drum Output Projection

`event_drum` and `time_drum` write their outputs from a constant pattern matrix indexed by `current_step`. The outputs are pure functions of a retained tag by construction. A drum-aware abstract rule: all outputs → `depends_on_retained={current_step}`, `depends_on_entry=False`.

**Difficulty:** Low.

#### 2.2c Oneshot Output Semantics

`out(X, oneshot=True)` output depends on (rung_condition, oneshot_prev). If rung condition is entry-independent and prev belongs to the oneshot (not the candidate being analyzed), output is entry-independent.

**Difficulty:** Low.

#### 2.2d Shift Register Reset Convergence

`shift().clock().reset()` — the reset is level-sensitive and clears all bits while True. If the reset condition is eventually always True, all shift register contents converge to False regardless of entry values.

**Difficulty:** Medium.

### 2.4 Init-Guard Constant Detection

Tags written under `~InitDone` guards (where `InitDone` is set unconditionally on the first scan and never cleared) are effectively constants after scan 1. The abstract phase sees both paths and merges, contaminating them with entry-dependency. This propagates through indirect reads of block slots written during init (`dh[]`, `ds[]` in the PackML benchmark).

**Fix:** Detect write-once tags — written exactly once, guarded only by their own falsity. Treat post-init values as `const`.

**Difficulty:** Low-medium.

### 2.5 Send/Receive Implicit External Inference

`receive()` destination tags are inherently nondeterministic — their values come from an external communication partner and can change unpredictably. They should be automatically inferred as `external` without requiring user annotation.

**Classification rules:**

- Tags appearing as `dest` in a `receive()` call → treat as `external` for partition purposes.
- Status tags (`receiving`, `success`, `error`) → model comm cycle as nondeterministic phases. `success`/`error` are one-scan pulses, likely written-before-read.
- `exception_response` → only meaningful on error, otherwise scratch.
- If someone does `rise(RecvOK)`, the edge partition catches it and keeps the prev value in key.

**Sanity check:** If a tag is in a `send()` source but marked external, warn — likely user error.

**Difficulty:** Low. Classification concern that affects what enters the elision pipeline.

### 2.6 Relational Dependency Analysis

Replace `depends_on_retained: bool` with `retained_deps: frozenset[str] | TOP`. Catches deterministic projections where per-tag analysis sees a spurious self-loop through a branch guard that also reads retained state.

**Difficulty:** Medium.

### 2.7 Abstract Domain Widening for Indirect Accesses

When `_domain_for_expr` returns `None` for an indirect pointer, don't collapse to `_UNKNOWN_VALUE`. If the pointer is entry-independent, the read result inherits entry-dependency only from the targets, not from the pointer resolution failure.

**Difficulty:** Low.

---

## 3. Concrete Phase Optimizations

### 3.1 Shared Baseline Batch Proofs

**Current approach:** Each candidate proven independently. K candidates × M combos × D domain values = K × M × D total `_scan()` calls. Dependency cones overlap heavily.

**Proposed:** Union all candidate dependency cones into one shared enumeration space. For each (retained × input) combo, run one baseline scan. Then perturb each candidate individually against that baseline.

```
for each (retained × input) combo (M):
    baseline = _scan(all candidates at default)     → M baseline scans
    for each candidate (K):
        for each alternate_value (D-1):
            result = _scan(candidate = alt_value)   → M × K × (D-1) scans
            if result != baseline → NOT elidable, skip
```

For bools (D=2): M × (1 + K) scans instead of M × K × 2. Roughly half the work.

**Joint removal is sound by transitivity:** When proving A elidable, B is in the retained set and varied. When proving B, A is varied. Removing both jointly: the proof already accounts for every value of every other candidate. No pairwise interaction can exist that wasn't tested.

### 3.2 Cone Pruning from Abstract Results

Tags the abstract phase resolved as const or input-only (`same_scan_safe` with known provenance) don't need to be varied in the concrete enumeration. Remove them from `vary_domains`. Shrinks the product space without affecting soundness.

### 3.3 Fast-Path Coverage Skip

`_collect_forced_true_coverage` runs pilot scans and records which tags were ever written. If a candidate was never written across all pilot scans, its exit is always its default — trivially elidable without running the full proof. Pre-filter before cone computation.

### 3.4 Early Termination Propagation

When a candidate fails (some combo produces different outputs for different candidate values), mark it immediately and exclude it from further perturbation. Baseline scans still run but per-candidate perturbation budget shrinks.

### 3.5 Exclusive Family Canonicalization (Assignment Reduction)

Orthogonal to elision. Elision reduces BFS nodes (states). Exclusive families reduce edges per node (assignments per state).

Detect encoder-style external bool families (e.g., `CmdReset..CmdComplete` — only one active at a time). Enumerate canonical none-or-one-hot assignments instead of raw 2^N combinations.

**Detection:**
- Static inference from rung structure where inputs are pivots in mutually exclusive branches.
- Manual `exclusive_group` annotation for constraints the static pass can't infer (physical wiring, Modbus protocol guarantees).

**Affects enumeration only, not the state key.** Post-elision, free inputs aren't in the key anyway.

**Difficulty:** Medium.

---

## 4. Techniques That Don't Apply (and Why)

**Partial Order Reduction** — System is synchronous. No concurrent interleavings to reduce.

**BDD/SAT-Based Symbolic Model Checking** — Explicitly out of scope. The strategy is to shrink the key until explicit-state BFS is trivial, not to tolerate a big key with symbolic methods.

**CEGAR** — Your approach is the opposite direction (start full, remove provably redundant dimensions). Could apply if switching to property-directed verification.

**Predicate Abstraction** — Overkill for finite-state systems where all values can be enumerated.

---

## 5. Ranked Recommendations

### Abstract Phase (make the key smaller)

1. **Write-coverage under exhaustive guards** (§2.1) — Medium effort. Catches the most common false `depends_on_entry`: latch/reset complementary pairs and state-machine case splits.
2. **Remove lock exclusion** (§2.3) — Trivial. Prevents locked bool physics outputs from being permanently retained.
3. **Drum output projection** (§2.2b) — Low effort. Eliminates all drum output tags in one abstract rule.
4. **Init-guard constant detection** (§2.4) — Low-medium effort. Fixes precision loss for init-guarded block slots.
5. **Send/receive implicit external** (§2.5) — Low effort. Correct classification of comm destination tags.
6. **Indirect access precision** (§2.7) — Low effort. Fixes fallback to `_UNKNOWN_VALUE`.
7. **Timer abstraction awareness** (§2.2a) — Medium effort. Eliminates raw accumulator from key.
8. **Relational dependency tracking** (§2.6) — Medium effort. Catches spurious self-loops.

### Concrete Phase (make proofs cheaper)

1. **Shared baseline batch proofs** (§3.1) — Medium effort. Halves scan count for bool candidates.
2. **Cone pruning from abstract results** (§3.2) — Low effort. Shrinks enumeration space.
3. **Fast-path coverage skip** (§3.3) — Low effort. Pre-filter for never-written candidates.
4. **Early termination propagation** (§3.4) — Low effort.
5. **Exclusive family canonicalization** (§3.5) — Medium effort. Reduces edges per BFS node.

### Post-BFS

- **Mealy minimization** — Medium effort. Catches reachable-state equivalences invisible to pre-BFS analysis. Diminishing returns on small keys.

### Testing

- **Three-way agreement harness** — Interpreted PLC + compiled kernel + abstract prediction on same inputs. Catches compiler bugs, abstract unsoundness, semantic drift.

---

## 6. Key Insight from the Literature

The most relevant paper is Bozga et al.'s "State Space Reduction Based on Live Variables Analysis" (SAS 1999 / SCP 2003). Your system goes further in two ways: (1) you handle deterministic projections where a tag is live but its value is a function of other retained tags, and (2) you have the concrete phase as a fallback for cases the abstract analysis can't resolve. Bozga et al. only had the abstract analysis and accepted its conservatism. Your hybrid approach is strictly more powerful.

The closest PLC-specific work is PLCverif (CERN), which translates PLC code to external model checkers (NuSMV, CBMC, Theta). Their state-space reduction leverages PLC scan-cycle structure but feeds into external symbolic solvers. Your explicit-state approach with kernel-level proofs — where the prover and the executor are the same code — doesn't appear in the literature.
