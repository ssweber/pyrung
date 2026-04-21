# Analysis capabilities

Prioritized by engineering value and build cost relative to what already
exists (PDG with slicing, SP-tree attribution, projected/recorded causal
chains, DataView, stuck-bits/conflicting-output/final-writers validators,
program walker).

---

## Tier 1 — cheap extensions of existing infrastructure

### Blast-radius table

Per rung, forward-slice to terminals via `downstream_slice`. "Rung 42
affects MotorOut, Sts_Running, Alm_Overload; nothing else." Most useful
section of a first-look report — answers "if I change this, what breaks?"
before the engineer asks.

*Builds on:* PDG `downstream_slice`, `writers_of`.

### Latch pair classification

For every set/reset pair the stuck-bits validator already finds, classify:
symmetric (clean pair), asymmetric (one side has extra conditions),
orphan (set without reset or vice versa), precedence (which wins on
simultaneous). Surfaces latch intent and bugs without annotation.

*Builds on:* `stuck_bits` validator, SP trees.

### Write-exclusivity graph

All writers per non-final tag. For each multi-writer tag, prove the
writers are mutually exclusive (disjoint conditions via SP trees) or
flag as a provable race. Extends existing `CORE_CONFLICTING_OUTPUT` from
"two rungs write the same tag" to "two rungs write the same tag *and
can fire in the same scan*."

*Builds on:* PDG `writers_of`, SP-tree evaluation.

### Scan-cycle ordering hazard

Flag rungs that read a tag written by a later rung in the same scan
where the ordering matters behaviorally — i.e., the read value differs
from what would appear after the write. The PDG already has
`def_use_chains` with `TagVersion`; this is a filter over versions
where `defined_at > read_by` and the two values diverge.

*Builds on:* PDG `def_use_chains`, `TagVersion`.

### HMI surface extraction

`external` + `choices`/`min`/`max` annotated tags, forward-sliced to
terminals. The implicit operator interface — what the HMI can do and
what it reaches. DataView already has role and physicality filters;
this adds a `downstream_slice` per operator-facing tag.

*Builds on:* DataView filters, PDG `downstream_slice`.

---

## Tier 2 — new static analyses, moderate code

### Terminal causal fingerprints

For each terminal, enumerate minimal input-transition sets (SP prime
implicants) that drive it. Cluster terminals by fingerprint overlap:
heavy overlap = same subsystem, disjoint = independent, partial overlap
names the interlock. This is the subsystem partition — everything in
tiers 2–3 benefits from it.

*Builds on:* SP trees, `projected_cause`.

### Program chop / barrier chop

Chop: intersection of upstream slice from sink and downstream slice from
source — the minimal sub-program connecting input A to output B. Barrier
chop: paths from A to B not passing through safety tag Z. "Which ways
can MotorOut turn on that bypass EStop?" Empty = safe; non-empty = finding.

*Builds on:* PDG `upstream_slice`, `downstream_slice`.

### Simplified form per terminal

Minimal Boolean expression per terminal from SP-tree reduction.
`MotorOut = (Start & ~Fault & ~EStop) | Maint_Override`. Large gap
between simplified and actual logic flags overcomplicated rungs —
historical accretion, defensive coding, unreduced mode logic.

*Builds on:* SP trees.

### Enabling co-occurrence matrix

Matrix of (terminal × enabling tag) from projected cause. Rank columns
by how many terminals a tag enables, segmented by fingerprint cluster.
Tags enabling across clusters are master interlocks and mode gates.

*Builds on:* `projected_cause` enabling conditions, fingerprint clusters.

### Pivot classification (driver vs guard)

For every pivot, compute its proximate-vs-enabling ratio across all
SP-tree firings. High proximate = signal/propagator. High enabling =
interlock/permit. Richer than INPUT/PIVOT/TERMINAL; derivable without
naming. Input classification (command vs permissive) is the leaf case.

*Builds on:* PDG `tag_roles`, SP-tree attribution.

### Timer classification

Every TON/TOF grouped by usage shape: debouncer (short preset on noisy
input), interlock delay (gates a downstream latch), pulse stretcher
(one-shot with timer reset), sequence timer (chained with other timers),
watchdog (alarm if condition persists), rhythm timer (self-resetting
cycle). Walker already extracts timer instructions; this classifies by
the rung topology around them.

*Builds on:* Walker, PDG rung topology.

### Subroutine map

Classify every CALL site (not definition) as: ORGANIZATION (always-true
rail), MUTEX_GROUP (mode-gated, mutually exclusive with siblings),
CHAINED (reads another sub's output), CONDITIONAL (gated by process
state), or CONFLICT (overlapping write sets with another active sub).
PDG already records `calls` per rung node.

*Builds on:* PDG `calls`, rung conditions.

### Sequencer detection

Find state-variable walks: a small-domain tag whose transitions form a
cycle with guarded edges. Names implicit state machines — "tag Mode
walks {0,1,2,3} with transition guards." Pattern-match on
`choices`-annotated tags with cyclic write patterns in the PDG.

*Builds on:* PDG, `choices` annotations.

---

## Tier 3 — derived / compositional

These require tier-2 fingerprints or multiple tier-2 analyses combined.

### Per-mode fingerprint matrix

Terminal × mode-value grid of prime implicants. Surfaces mode leakage
(terminal responds in a mode it shouldn't), dead modes (column of
empties), and mode-invariant terminals (identical row across modes).

*Requires:* Fingerprints + sequencer/mode detection.

### Causal depth vs graph depth

Per terminal: edge-count depth from input vs load-bearing-step depth
(proximate causes only). Large gap = many interlock layers wrapping
a short driver. Differentiates sequencers from protection systems.

*Requires:* Fingerprints, pivot classification.

### Decomposition slice lattice

Backward slice per terminal, compared by set-subset. Near-identical
slices = redundant logic; subset = hierarchy; siblings with common
root = co-designed outputs. Different signal from fingerprint clusters;
worth doing both.

*Requires:* PDG slicing + fingerprints for comparison baseline.

### Change-impact by fingerprint diff

Diff terminal fingerprints between two program versions. Catches
behavioral changes from "innocent" refactors that line-diffs miss.

*Requires:* Fingerprints computed on two versions.

### Naming-cluster cohesion

For named subsystems (`CONV1_*`), fraction of tags whose fingerprints
use only internal tags vs external. Puts a number on whether naming
is honest about structure.

*Requires:* Fingerprints + naming signal.

### Annotation gap finder

Tags used through indirection, ranked by precision payoff if annotated.
"Adding `choices=[1,3,7]` here would eliminate 97 edges downstream."
Needs narrowing/indirection tracking to score the payoff.

*Requires:* Narrowing infrastructure.

---

## Tier 4 — sampling and history

These need runtime infrastructure beyond the current scan engine.

### Mined invariants

Association-rule mining over state snapshots (sampled or recorded).
Cross-check against SP trees — invariants empirically true but
structurally unsupported are coverage artifacts or unencoded assumptions.

### Temporal invariants

Response-time patterns: `A↑ → B↑ within N scans`. Categories: response
pairs, sequencing chains, bounded-delay (reverse-engineers timer
presets), liveness, absence (safety interlocks). Physical-realism
`delay_ms` sets the floor.

### Hypothesis-driven sampling

Tag flags map to Hypothesis strategies: `external` → stimulate,
`choices` → constrain domain, `readonly` → pin. Engineer annotates
5–20 operator-facing tags, runs `pytest --pyrung-autofuzz`. Static
mutual-exclusions feed back as strategy constraints.

### Recoverability landscape / blocker clustering

Per latched pivot: trivially recoverable, conditionally recoverable
(with required inputs), or blocked. Cluster blocked tags by shared
blocker sets. Partially overlaps with existing `stranded_bits` in
query.py but needs history for real clearability assessment.

---

## Build order

```
         ┌─ blast-radius ──────────────────────────────┐
         ├─ latch pairs ───────────────────────────────┤
Tier 1   ├─ write-exclusivity ─────────────────────────┤  ← all cheap,
         ├─ ordering hazard ───────────────────────────┤    independent
         └─ HMI surface ───────────────────────────────┘

         ┌─ fingerprints ──┬─ enabling matrix ─────────┐
         │                 ├─ per-mode matrix ──────────┤
         │                 ├─ depth ratio ──────────────┤
Tier 2   │                 └─ slice lattice ────────────┤
         ├─ chop / barrier chop ───────────────────────┤
         ├─ simplified form ───────────────────────────┤
         ├─ pivot classification ──────────────────────┤
         ├─ timer classification ──────────────────────┤
         ├─ subroutine map ────────────────────────────┤
         └─ sequencer detection ───────────────────────┘

Tier 4   sampling infrastructure → mined/temporal invariants
```

Tier 1 items are independent and can ship in any order. In tier 2,
fingerprints unlock a fan of downstream analyses; chop, simplified form,
timer classification, subroutine map, and sequencer detection are
independent of each other.
