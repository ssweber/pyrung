# Elision Improvement Checklist (v2)

Quick-reference for what to implement, in priority order. Each item says what it catches and how hard it is.

---

## Bugs / Misconfigurations

- [ ] **PENDING sentinel retention**
  Tags with `PENDING` in their domain are unconditionally retained because the concrete kernel cannot execute with the `PENDING` sentinel as an actual value. Correct but conservative — the abstract phase might still prove these entry-independent without needing concrete enumeration. Investigate whether the abstract rules (provenance, write-coverage, drum projection) can resolve these before they hit the concrete wall.

    After investigation:   1. PENDING sentinel retention

  The concrete guard is correct and necessary. The PENDING sentinel is the string "Pending" — a BFS-level abstraction
  representing Done=False + Acc=running. It's not a real kernel value. The concrete proof works by setting
  kernel.tags[name] = value for each domain value, then running the kernel. Setting kernel.tags["Timer.Done"] =
  "Pending" would be invalid, and the timer's Acc tag has already been absorbed (removed from stateful_dims), so there's
   no way to reconstruct the PENDING state in the kernel.

  The abstract phase already runs on these tags. _pass_abstract passes all of ctx.stateful_dims (including
  PENDING-domain tags) to _ScanLocalStateElider. If the abstract phase proves a tag elidable, it's removed before the
  concrete phase ever sees it.

  Where the abstract phase falls short: Timer/counter instructions hit the generic fallback in _execute_instruction
  (abstract.py:520-523), which calls _apply_unknown_writes — marking Done and Acc as _UNKNOWN_VALUE without modeling the
   internal reads. Consequences:

  - If any downstream rung reads the Done bit, _observe_read fires with unknown=True → _saw_unknown_read = True → not
  elidable. Sound but conservative.
  - The fallback doesn't distinguish TON (always resets Done when disabled) from RTON (holds entry value when disabled).
   Both paths produce _UNKNOWN_VALUE.

  The improvement path is better abstract rules, not removing the concrete guard. A dedicated timer/counter abstract
  rule could model:
  - Enabled: Done = f(Acc, Preset) — always overwritten, no entry dependency
  - Disabled TON: Done = False — always overwritten
  - Disabled RTON: Done = entry — entry-dependent

  This would let the abstract phase prove TON Done bits elidable (their exit value never depends on their entry value,
  regardless of enable state). This lines up with the "Timer abstraction awareness" item already in the checklist.

  Verdict: No bug, correct but conservative. Improvement goes through the abstract phase, not the concrete guard.

- [ ] **Continued() source tag retention**
  Tags read via `continued()` rungs are unconditionally retained. The issue might be that the concrete frontier traversal doesn't model continued-rung snapshot reads correctly. Specifically: an `out()` coil in rung N writes a tag, then a `continued()` rung reads that tag as a condition — but the continued rung evaluates against the *pre-write* snapshot, not the post-write value. The frontier walk might not distinguish "read sees current state" from "read sees snapshot state," so it can't correctly determine whether the candidate influences the continued rung's condition. If so, the fix is teaching the frontier walk about continued-rung snapshot semantics. Investigate to confirm.

    After investigation:   2. Continued() source tag retention

  The guard appears unnecessarily conservative. I traced through the full data path and could not construct a scenario
  where removing it would be unsound.

  Frontier walk is correct. _reachable_stateful_frontier (concrete.py:535-566) uses self._graph.readers_of, which
  includes all rungs that read a tag — continued rungs are not excluded. The PDG construction (pdg.py:886-893) builds
  readers_of from node.condition_reads | node.data_reads for every RungNode, regardless of _use_prior_snapshot. So the
  observation set is complete.

  Kernel execution is faithful. The compiled kernel implements continued-rung snapshots via
  _collect_rung_snapshot_bindings (codegen/compile/_core.py:276-341), which creates local variables capturing tag values
   at chain start before any instruction in the chain executes. When _scan() sets entry values and runs the kernel,
  continued rungs correctly read the snapshot (= entry) values, not post-write values.

  The proof handles the critical scenario correctly. Consider:
  Rung N:     with Rung(): out(X)          # X candidate, always True
  Rung N+1:   with Rung(X).continued(): out(Y)   # Y retained

  - Frontier from X: readers_of[X] includes rung N+1. Rung N+1 writes Y (retained). Y is in the observation set.
  - Proof varies X's entry. Kernel: rung N writes X=True, but rung N+1's snapshot was captured before rung N's write, so
   it sees X's entry value. If entry X differs, Y changes. Proof correctly says "not elidable."

  The "combinational observers" concern doesn't materialize. The frontier BFS continues through non-retained,
  non-stateful tags (line 563: queue.append(written_tag) for tags not in retained_set or stateful_names). Combinational
  intermediaries don't block the walk — they're traversed until a retained or stateful tag is reached.

  The abstract phase already models this. Line 273 of abstract.py handles the snapshot correctly:
  if getattr(rung, "_use_prior_snapshot", False):
      snapshot = prev_snapshot

  Verdict: The guard is likely a development-era precaution. Removing it would let the concrete phase prove more
  continued-source tags elidable. There are no tests specifically covering this guard.

  Recommended next step: Add a test to test_elision_agreement.py with a program that has a continued rung reading a
  candidate tag, remove the guard from _is_concrete_candidate and _never_written_elidable, and verify the three-way
  agreement harness passes. If it does, the guard was unnecessary. 
  
  But need to look into test_reachable_states_tracks_continued_snapshot_across_scans - because this was the test that the guard was added to make it pass.
  
  

---

## Abstract Phase — Cross-Rung Analysis

- [ ] **Write-coverage under exhaustive guards**
  For each candidate with `depends_on_entry`, collect all rungs that write it. Extract guard conditions. If guards form a tautology (complementary bools, exhaustive enum over a retained tag's domain), the tag is always written — mark `depends_on_entry=False`.
  *Catches: latch/reset complementary pairs, state-machine case splits, multi-rung output coverage.*
  *Effort: medium.*

---

## Abstract Phase — Instruction-Level Rules

- [ ] **Drum output projection**
  `event_drum` / `time_drum` outputs are constant functions of `current_step` by construction (pattern matrix). Dedicated abstract rule: all outputs → `depends_on_retained={current_step}`, `depends_on_entry=False`.
  *Effort: low.*

- [] **Init-guard constant detection**
  Tags written under `~InitDone` (where `InitDone` is write-once, never cleared) (or like values written as system. first_scan) are constants after scan 1. Don't merge with the disabled path. Treat post-init values as `const`.
  *Catches: block slots (dh[], ds[]) contaminating indirect reads with false entry-dependency.*
  *Effort: low-medium.*

- [ ] **Timer abstraction awareness**
  BFS uses three-valued timer domain (`False`/`Pending`/`True`). Raw accumulator INT is irrelevant to state identity. If the abstract phase knows this, the accumulator is never a key dimension.
  *Effort: medium.*

- [ ] **Oneshot output semantics**
  `out(X, oneshot=True)` output depends on (rung_condition, oneshot_prev). If rung condition is entry-independent and prev belongs to the oneshot (not the candidate), output is entry-independent.
  *Effort: low.*

---

## Abstract Phase — Precision Fixes

- [ ] **Indirect access fallback precision**
  When `_domain_for_expr` returns `None`, don't collapse to `_UNKNOWN_VALUE`. If the pointer is entry-independent, the read result inherits entry-dependency only from the targets, not from the pointer resolution failure.
  *Effort: low.*

- [ ] **Relational retained-dependency tracking**
  Replace `depends_on_retained: bool` with `retained_deps: frozenset[str] | TOP`. Catches deterministic projections where per-tag analysis sees a spurious self-loop through a branch guard.
  *Effort: medium.*

---

## Tag Classification

- [ ] **Send/receive implicit external inference**
  `receive()` destination tags are inherently nondeterministic — auto-infer `external` without user annotation. Status tags (`success`/`error`) are one-scan pulses, likely WBR. `exception_response` only meaningful on error. If someone does `rise(RecvOK)`, edge partition catches it. Warn if a tag in `send()` source is marked external.
  *Effort: low.*

---

## Static Validation

- [x] **Surface never-written tags to user**
  After the coverage pass, any tag in the program that is never written by any rung (not in `_coverage.written_tags`) needs a human decision. Present a diagnostic list. The tag is one of three things: (1) an external input — user should add `external=True`, (2) a configuration constant — user should add `readonly=True`, (3) a bug — the tag was declared but never wired. The elision system can't infer which, but it can surface the question. This also catches `receive()` destinations missing `external=True` and unused tags that add phantom state dimensions.
  *Effort: low. Data already computed. Just surface it.*

---

## Concrete Phase — Batch Efficiency

- [x] **Shared baseline batch proofs**
  Union all candidate dependency cones into one shared enumeration. One baseline scan per (retained × input) combo, then perturb each candidate against it. For bools: M × (1 + K) scans instead of M × K × 2. Joint removal sound by transitivity — each proof already varies all other candidates.
  *Effort: medium.*

- [ ] **Cone pruning from abstract results**
  Tags the abstract phase resolved as const or input-only don't need to be varied in concrete enumeration. Remove from `vary_domains` to shrink the product space.
  *Effort: low.*

- [x] **Fast-path coverage skip**
  If a candidate was never written across pilot scans (`_collect_forced_true_coverage`), its exit is always default. Trivially elidable without full proof. Pre-filter before cone computation.
  *Effort: low.*

- [NOT IMPORTANT] **Early termination propagation**
  When a candidate fails, mark immediately and exclude from further perturbation in current round. Baseline scans still run but per-candidate budget shrinks.
  *Effort: low.*

---

## BFS — Assignment Reduction

- [ ] **Exclusive family canonicalization**
  Detect encoder-style external bool families (only one active at a time). Enumerate none-or-one-hot instead of 2^N. Static inference from rung structure + manual `exclusive_group` annotation for physical constraints. Orthogonal to elision — reduces edges per BFS node, not nodes.
  *Effort: medium.*

---

## Post-BFS

- [ ] **Mealy minimization (partition refinement)**
  After BFS, merge output-equivalent reachable states via Hopcroft/Moore. Catches equivalences invisible to pre-BFS analysis. Doesn't reduce key dimensions but reduces state count.
  *Effort: medium. Diminishing returns on small keys.*

---

## Testing

- [x] **Three-way agreement harness**
  For every elision decision, run interpreted PLC + compiled kernel + abstract prediction on same (state, input) pairs. Verify: (1) interpreted and compiled produce identical outputs, (2) abstract prediction is consistent with concrete results. Catches compiler bugs, abstract unsoundness, semantic drift.
  *Effort: medium. Extends existing `_warm_memory` double-check pattern.*
