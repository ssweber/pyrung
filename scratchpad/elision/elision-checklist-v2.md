# Elision Improvement Checklist (v2)

Quick-reference for what to implement, in priority order. Each item says what it catches and how hard it is.

---

## Bugs / Misconfigurations

- [ ] **PENDING sentinel retention**
  Tags with `PENDING` in their domain are unconditionally retained because the concrete kernel cannot execute with the `PENDING` sentinel as an actual value. Correct but conservative — the abstract phase might still prove these entry-independent without needing concrete enumeration. Investigate whether the abstract rules (provenance, write-coverage, drum projection) can resolve these before they hit the concrete wall.
  *Effort: investigate first, then decide.*

- [ ] **Continued() source tag retention**
  Tags read via `continued()` rungs are unconditionally retained. The issue might be that the concrete frontier traversal doesn't model continued-rung snapshot reads correctly. Specifically: an `out()` coil in rung N writes a tag, then a `continued()` rung reads that tag as a condition — but the continued rung evaluates against the *pre-write* snapshot, not the post-write value. The frontier walk might not distinguish "read sees current state" from "read sees snapshot state," so it can't correctly determine whether the candidate influences the continued rung's condition. If so, the fix is teaching the frontier walk about continued-rung snapshot semantics. Investigate to confirm.
  *Effort: investigate first, then medium if frontier needs snapshot-awareness.*

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

- [ ] **Init-guard constant detection**
  Tags written under `~InitDone` (where `InitDone` is write-once, never cleared) are constants after scan 1. Don't merge with the disabled path. Treat post-init values as `const`.
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

- [ ] **Surface never-written tags to user**
  After the coverage pass, any tag in the program that is never written by any rung (not in `_coverage.written_tags`) needs a human decision. Present a diagnostic list. The tag is one of three things: (1) an external input — user should add `external=True`, (2) a configuration constant — user should add `readonly=True`, (3) a bug — the tag was declared but never wired. The elision system can't infer which, but it can surface the question. This also catches `receive()` destinations missing `external=True` and unused tags that add phantom state dimensions.
  *Effort: low. Data already computed. Just surface it.*

---

## Concrete Phase — Batch Efficiency

- [ ] **Shared baseline batch proofs**
  Union all candidate dependency cones into one shared enumeration. One baseline scan per (retained × input) combo, then perturb each candidate against it. For bools: M × (1 + K) scans instead of M × K × 2. Joint removal sound by transitivity — each proof already varies all other candidates.
  *Effort: medium.*

- [ ] **Cone pruning from abstract results**
  Tags the abstract phase resolved as const or input-only don't need to be varied in concrete enumeration. Remove from `vary_domains` to shrink the product space.
  *Effort: low.*

- [ ] **Fast-path coverage skip**
  If a candidate was never written across pilot scans (`_collect_forced_true_coverage`), its exit is always default. Trivially elidable without full proof. Pre-filter before cone computation.
  *Effort: low.*

- [ ] **Early termination propagation**
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

- [ ] **Three-way agreement harness**
  For every elision decision, run interpreted PLC + compiled kernel + abstract prediction on same (state, input) pairs. Verify: (1) interpreted and compiled produce identical outputs, (2) abstract prediction is consistent with concrete results. Catches compiler bugs, abstract unsoundness, semantic drift.
  *Effort: medium. Extends existing `_warm_memory` double-check pattern.*
