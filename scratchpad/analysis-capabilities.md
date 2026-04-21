# Analysis capabilities: extra signal worth surfacing

Beyond the existing `plc.dataview` / `plc.cause` / `plc.effect` / `plc.query`
layers, a broader set of analyses is available using the same SP-tree +
causal-chain machinery, optionally augmented by sampled or recorded history.
Each technique below is organized by what it surfaces and what data it needs.

## Data tiers

Four tiers, with increasing cost and decreasing fabrication risk:

- **Closed-form static** — direct computation on SP trees and the PDG.
  Cheap, complete, deterministic.
- **Enumeration static** — enumerate reachable firings from the SP trees.
  Complete in principle, blows up on wide rungs.
- **Hypothesis-driven sampling** — drive the PLC with randomized input
  sequences, collect scans, mine patterns. Gets most of the "needs
  history" results without a suite, in seconds.
- **Real recorded history** — actual test-suite runs or production
  captures. Sparse but truthful.

The important split: *what does the program say* → static,
*what can it do* → sampling, *what has it done* → history. The axis isn't
static-vs-dynamic; it's three distinct epistemic claims.

## Pivot classification (driver vs guard)

*(For every internal bit, figure out whether it mostly acts as "a signal
that makes things happen" or "a permission that lets things happen.")*

*Closed-form static.*

For every pivot, compute its proximate-vs-enabling ratio across all firings
the SP tree predicts. High proximate = signal/propagator. High enabling =
interlock/permit. Mixed = mode bit that both steps and gates. Richer than
input/pivot/terminal, derivable without naming.

## Terminal causal fingerprints

*(For every output, list the minimum recipes of inputs that would turn it
on, then group outputs whose recipes overlap. Overlap = same subsystem;
disjoint = independent; partial = the overlap names the interlock.)*

*Closed-form static.*

For each terminal, enumerate the minimal input-transition sets (SP prime
implicants) that would drive it. Cluster terminals by fingerprint overlap:
heavy overlap = same subsystem, disjoint = independent, partial = the
overlap set names the interlock. Subsystem partition without naming
dependency.

## Enabling co-occurrence matrix

*(Build a table of "which permit bits gate which outputs." Anything that
gates across multiple subsystems is a master interlock.)*

*Closed-form static.*

Matrix of (terminal × enabling tag) from projected cause. Rank columns by
how many terminals a tag enables, segmented by fingerprint cluster. Tags
enabling across clusters are the cross-cutting permits and mode gates.
"`RUN_PERMIT` gates 43 of 58 terminals" writes the architecture paragraph.

## Input classification (command vs permissive)

*(Figure out which inputs are things someone pushes to make stuff happen
vs things that just need to be in the right state — commands vs guards,
safeties, enables.)*

*Closed-form static.*

Leaf case of pivot classification. Commands drive, permissives gate.
Separates operator push-buttons from plant-supplied conditions without
needing `Cmd_*` naming.

## Causal depth vs graph depth

*(Compare "how many layers of logic sit between this input and this output"
to "how many of those layers actually do work vs just gate." Big gap =
lots of interlock chrome wrapping a short driver.)*

*Closed-form static.*

Per terminal: edge-count depth from input vs load-bearing-step depth.
Large gap = many interlock layers wrapping a short driver. A terminal with
graph depth 12 and causal depth 3 is nine layers of permits around a
three-step driver. Surface the ratio per terminal and per subsystem;
differentiates sequencers from protection systems.

## Rung-level role asymmetry

*(For each rung, label each contact as "always a gate," "always a trigger,"
or "it depends" across all the ways the rung could fire. Aggregate up; the
mismatches between rung-level and program-level labels are where the
subtle bugs live.)*

*Closed-form static.*

Per rung, classify each contact as pure guard, pure driver, or conditional
across all reachable firings. Aggregate to pivot-level labels. Mismatches
flag interesting cases — "tag is a guard in most rungs but drives one" is
usually where subtle bugs live.

## Program chopping

*(Show the minimal sub-program that connects a specific input to a specific
output. One chop per operator-input × field-output pair gives a tour of
the program indexed by what it actually does from the outside.)*

*Closed-form static.*

Beyond backward/forward slicing, the "chop" is paths between a source and
a sink. "Minimal sub-program connecting `StartBtn` to `MotorOut`." Generate
one chop per operator-input × field-output pair for a per-feature walkthrough
indexed by external behavior.

## Barrier chopping

*(Show paths from input to output that don't pass through a specified
safety bit. "Which ways can the motor turn on that bypass the E-stop?"
Empty answer = safe; non-empty = finding.)*

*Closed-form static.*

Chop variant: paths from X to Y *not passing through* Z. Safety-critical:
"which paths to `MotorOut` bypass `EStop`?" Every terminal × every safety
input gives an auto-generated matrix of bypass paths. Empty cells are safe;
non-empty are findings.

## Decomposition slice lattice

*(For every output, collect all the code that affects it. Then compare
those collections: identical = duplicated logic, one-contains-the-other =
hierarchy, siblings with shared root = naturally co-designed outputs.)*

*Closed-form static.*

Backward slice per terminal, lattice by set-subset relations. Near-identical
slices = redundant logic. Subset relations = hierarchy ("compressor
contained within plant interlock"). Sibling slices with common top =
naturally co-designed outputs. Different information from fingerprint
clustering; worth doing both.

## Path diversity per terminal

*(Count how many different ways an output can turn on. One way =
single-purpose output; many ways = mode-dependent with different drivers
per mode. Surfaces the mode-heavy parts of the program without needing to
know where the mode bits are.)*

*Closed-form static.*

Count distinct prime-implicant fingerprints per terminal. One = single-mode.
Seven = multi-mode with mode-dependent drivers. Ranks terminals by modality
without needing to find mode bits first.

## Change-impact / blast-radius table

*(For each rung, list which outputs it can affect. Answers "if I change
this, what else could break?" before the engineer even asks.)*

*Closed-form static.*

Per rung, forward slice to terminals. "Rung 42 affects `MotorOut`,
`Sts_Running`, `Alm_Overload`; nothing else." Most practically useful
section of a first-look report — always on the page when the engineer
thinks "I need to change X."

## Amorphous / simplified form per terminal

*(Show the minimal Boolean expression that produces the same behavior as
the actual ladder logic, side by side with the real rungs. A big gap
between simplified and actual means the logic is overcomplicated for
reasons other than function — defensive coding, historical accretion, or
requirements the code hasn't been told about.)*

*Closed-form static.*

Minimal semantically equivalent Boolean expression per terminal.
"`MotorOut = (Start & ~Fault & ~EStop) | Maintenance_Override`." Large gaps
between simplified and actual logic flag overcomplicated rungs — historical
accretion, defensive coding, mode logic that doesn't reduce.

## Naming-cluster cohesion score

*(If tags are named by subsystem (`CONV1_*`, `CONV2_*`), check whether
those names actually line up with how the logic is wired. High agreement =
well-organized program; low = the naming is lying about the structure.)*

*Closed-form static plus naming signal.*

For any claimed subsystem (naming or graph-based), fraction of its tags'
fingerprints using only internal tags vs external. 95% internal = real
subsystem. 60% = porous, architecture more entangled than names suggest.
Puts a number on whether the naming is honest — addresses the
well-organized-vs-not concern directly, without assuming names are
meaningful.

## Program-diff by fingerprint

*(When comparing two versions of a program, diff what each output actually
does rather than the source code. Catches behavioral changes from
"innocent" refactors that line-diffs would miss.)*

*Closed-form static, across two versions.*

Diff terminal fingerprints between program versions, not source text.
"`MotorOut` used to have 3 fingerprints, now 4 — new one is
`Start & ~Fault & MaintMode`." Catches unintended semantic changes from
refactors that line-diffs won't.

## Mined invariants from history or sampling

*(Watch the program run — real execution or random simulation — and
surface the relationships that always hold. "Whenever X is true, Y is
true within N scans." Expected ones confirm the architecture; surprising
ones are the report's most productive lines.)*

*Sampling or real history.*

Association-rule mining over state snapshots surfaces rules like "whenever
`StartBtn=1` and `Fault=0`, `Running=1` within N scans." Expected invariants
confirm architecture; surprising ones are the most productive report lines.
Cross-check against SP trees — invariants empirically true but with no
structural support are either coverage artifacts or evidence the program
works for reasons the author didn't encode.

## Temporal invariants

*(The time-based version of invariants. "Every rising edge of X is
followed by Y within N scans." Captures response times, startup
sequences, timer-driven bounds, and safety interlocks — the behavioral
contracts of the program.)*

*Sampling or real history.*

Response-time patterns: "every rising edge of A produces B within N scans."
Mine edges per tag, measure scan-count distributions between A-edges and
subsequent B-edges, keep pairs with tight consistent distributions.
Categories:

- **Response pairs** — `A↑ → B↑ within N`; direct drive relationships.
- **Sequencing chains** — `A↑ → B↑ → C↑` with consistent inter-step delays;
  state machines and interlocked startup sequences.
- **Bounded-delay** — `A↑ → B↑ within [min, max]`; usually timer-mediated.
  Tight `[min, max]` reverse-engineers timer presets from behavior.
- **Liveness** — `A↑ → eventually B↑` without tight bound; request-service
  patterns.
- **Absence** — `A↑ → B never ↑ while A holds`; safety interlocks. Highest
  priority for auto-report.

Cross-validate with static SP paths where possible; use Hypothesis shrinker
to falsify candidates and produce minimal counterexamples.

Temporal-invariant proposals respect the physical realism model (see
`physical-realism.md`): `delay_ms` on a sensor kind is the floor on any
response-pair involving that sensor, and observations faster than the
declared floor route to the "fix the program" bucket rather than the
invariant-review bucket.

## Recoverability landscape

*(For every latched bit, ask "can this actually clear?" and bucket the
answers: trivially, only under specific conditions, or never. Maps
directly to what the HMI can recover from.)*

*History (or projected-with-assumptions).*

`recovers()` across every latched pivot, rolled into three buckets:
trivially recoverable, conditionally recoverable with required inputs,
unrecoverable-with-blockers. Maps directly to "what can the HMI clear when
something latches." Needs history because projected mode without observed
transitions reports everything as unreachable.

## Blocker-fingerprint clustering

*(For the bits that can't clear, group them by "what inputs would they
need to see." Reveals both the test-suite TODO list and hidden subsystem
boundaries — "everything in this cluster needs CalibrationMode.")*

*History.*

Cluster unreachable tags by shared blocker sets. Two things fall out:
natural test-suite TODO list, and hidden subsystem boundaries ("everything
in this cluster needs `CalibrationMode`"). Blockers also name which inputs
are operator-only vs genuine test gaps.

## Hypothesis-driven sampling configured by flags

*(Use the tag flags as a recipe for random input generation, then
auto-run thousands of scenarios and mine invariants from the results.
Engineer annotates the operator surface — typically 5–20 tags — and
everything else happens without any test code being written.)*

*Sampling, using tag flags as strategy configuration.*

Tag flags map directly to Hypothesis strategies:

- `external` → stimulate (something outside ladder writes it)
- `choices=` → constrain the value domain. For stimulated tags, draw from
  `sampled_from(choices.keys())` — fuzzer never wastes cycles on out-of-domain
  values and never generates false `CORE_CHOICES_VIOLATION` noise. For
  observed tags, any scan showing an out-of-domain value is an invariant
  violation, not a coincidence.
- `readonly` → pin at default, never touch. Implicit `assume={tag: default}`
  on every run; any mined invariant involving a readonly tag is automatically
  trustworthy.
- `final` → observe, don't write from outside
- `public` → weight upward (operator-facing inputs deserve more sampling)
- PDG inputs → stimulate (same treatment as external)

Engineer annotates the operator surface (typically 5–20 tags on a medium
program) and runs `pytest --pyrung-autofuzz --cycles=N`. The plugin reads
declarations, builds strategies, runs randomized scenarios, mines
invariants, emits the first-look report. Zero test code written.

Static mutual-exclusions (from fingerprint analysis) feed back as strategy
constraints so the fuzzer doesn't waste cycles on program-impossible
combinations.

## Coverage-directed generation

*(Steer the random input generator toward rungs that haven't fired yet,
so the sampling covers the whole program instead of just the paths random
inputs easily hit.)*

*Sampling, augmented by SP trees.*

Bias Hypothesis toward rungs that haven't fired yet, using rung coverage as
target metric. Near-complete fingerprint enumeration in a few thousand
cycles on most programs. Plain flag-derived strategies are already useful;
coverage-directed on top gets the tail cases.

## Cross-layer disagreement as signal

The most valuable report lines come from disagreements between tiers:

- Static predicts an invariant that sampling never observes — sampling gap
  or implicit constraint from strategy shape.
- Sampling finds a pattern static doesn't imply — cross-rung interaction
  not captured by single-scan logic.
- History shows behavior static doesn't enforce — plant relies on external
  constraint not encoded in ladder.
- Fingerprints don't match naming clusters — architecture more entangled
  than names suggest.
- Temporal invariant holds but no SP path supports it — coincidence of
  limited data, or program works for unencoded reasons.

Each disagreement is a productive report line. Cross-referencing is the
highest-leverage output the whole pipeline produces.

## Composition and ordering

Layer the analyses so each feeds the next:

1. **Fingerprints** partition the program into subsystems.
2. **Depth ratio** characterizes each partition's shape.
3. **Enabling matrix** wires partitions together.
4. **Pivot/input classification** labels the wires.
5. **Rung asymmetry** flags where labels don't fit.

Then on top: **blast-radius table** for change impact, **chop / barrier
chop** for property-style questions, **simplified form** for review, **mined
invariants** for behavior across time, **sampling** for reachable-but-
unexercised cases.

Static analysis runs first and configures everything downstream — the
fuzzer's strategy constraints, the miner's suppression list of already-known
invariants, the "interesting" filter for what to feature in the report.
Fuzzing and history fill in the residual, not the baseline.
