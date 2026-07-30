# Crossings hardening plan

## Purpose

Harden the boundary between static crossing analysis and PILOT so that:

- a writer is never preferred for a value it is known not to produce;
- reverse crossings never omit concrete states that can produce the target;
- scalar, range, indirect, sequential, and co-written destinations receive
  consistent treatment;
- crossing behavior is checked against the interpreted runtime, not against
  another static analysis;
- uncertainty remains available as a fallback without outranking a compatible,
  understood writer.

This plan is separate from the don’t-cycle repair. A crossing defect can select
the wrong route; the cycle gate must independently prevent that route from
oscillating forever.

## Current findings

The audit produced the following concrete failures:

1. A range-backed reset is `UNKNOWN` in the forward direction and falls through
   in reverse because the handler reads `target.type`; a range exposes its
   element type through `target.block.type`.
2. Literal copy/fill forward classification reports the source literal instead
   of the value produced by destination storage semantics. Reverse classification
   has the same problem. For example, an out-of-range literal copied into a
   narrower integer destination is classified as the raw literal rather than the
   clamp rail.
3. Copy inequality reversal can under-approximate at a clamp rail. An inexact
   reverse is still required to be a superset of every concrete preimage.
4. Calc reversal can return only one candidate where wrapping creates multiple
   aliases. For example, multiplication in a wrapping integer destination can
   map more than one source value to zero.
5. Text search is represented as an element-wise existential. A matching
   multi-character window need not satisfy that constraint.
6. Sequential copy fan-out is recorded by the PDG, but the crossing writer
   resolver recognizes only the instruction’s nominal first destination.
7. Timer/counter status outputs are runtime writes but are not included by the
   PDG’s ordinary `_writes` extraction.
8. Loop indices, function outputs, and send-instruction status outputs are
   classified as crossing-exempt despite declaring local writes.
9. Some shift, drum, and held-coil reverse branches claim `exact=True` without
   representing all clock, reset, enable, or hold conditions required for
   sufficiency.
10. Receive instructions treat every co-write as external, including status
    fields that are at least partly controlled by local request execution.

Two design properties amplify these defects:

- `_written_value_for_tag()` knows the destination tag but discards it when it
  calls the destination-blind `forward(instr, ctx)` API.
- PILOT’s `_can_produce()` treats `UNKNOWN` as capable of producing every value.
  This is safe as a fallback, but unsafe as a preferred route when a compatible
  understood writer exists.

The existing crossing tests pass despite these counterexamples because the
coverage and fidelity maps exercise one representative target per instruction
class. They do not form a semantic matrix over destination shape, target
polarity, storage behavior, modes, and co-writes.

## Crossing contract

### Forward

A forward result is target-specific:

```text
forward(instruction, target_tag, context)
    -> Literal | Affine | Aggregate | UNKNOWN
```

The result describes the value actually stored in `target_tag`, not merely the
raw source expression. Destination clamp, wrap, Boolean conversion, character
normalization, fan-out position, and instruction mode are part of the result.

`UNKNOWN` means only that the crossing cannot classify the write. It must not be
treated as evidence that the writer is equally good as a known-compatible
writer.

### Reverse

Given a target constraint, reverse returns a DNF of concrete predecessor
constraints:

```text
reverse(instruction, rung, target, context) -> ReverseResult
```

The required properties are:

- `exact=True`: the returned DNF is necessary and sufficient.
- `exact=False`: the returned DNF is a sound superset. Every concrete state that
  produces the target is included, though extra states may also be included.
- `fallthrough=True`: the crossing makes no claim.

A useful candidate that omits valid preimages is not an inexact reverse result.
If PILOT needs such candidates, they should use a separate
`VerifiedCandidate`/`Proposal` protocol whose consumer is explicitly required
to execute and verify them.

## Concrete one-scan oracle

The interpreted runtime should be the test oracle for crossings.

The implementation can borrow the fork, one-scan execution, occurrence replay,
and occurrence-view ideas used by `program_step.py`. It must not call
`read_program_step()` or `trace_back()`, because those functions consume the
crossings being tested and would make the oracle circular.

For each case:

1. Build an isolated one-rung program containing the instruction under test.
2. Seed a concrete pre-scan tag state and any hidden instruction memory.
3. Run exactly one interpreted scan.
4. Read the selected rung occurrence and its actual write journal.
5. Use the occurrence’s input view for same-scan source values.
6. Compare the actual transition with the crossing’s forward and reverse claims.

The occurrence write journal is preferable to the final snapshot:

- a later writer may clobber the value;
- a target may already contain the requested value even though this writer did
  not execute;
- subroutines and loops may execute the same rung more than once;
- earlier same-scan writes may change what the instruction reads.

The initial harness should isolate one instruction per rung. Multiple-occurrence
and same-scan ordering cases can then be added as a separate integration layer.

### Interpreted/compiled parity

The concrete oracle opts into the repository's existing
`--runner-backend=interpreted|compiled|both` selection. The interpreter remains
the semantic oracle because it exposes the exact occurrence entry view,
enablement, read set, and attempted-write journal. In `compiled` and `both`
modes, the identical isolated program, seed, hidden memory, and scan duration
also run through `CompiledPLC`; the completed scan must agree on scan id,
timestamp, every committed tag, and every hidden-memory cell.

This is one crossing claim checked against two runtime witnesses, not two
crossing implementations agreeing with each other. A second static
implementation would be vulnerable to shared assumptions and would not provide
ground truth.

Compiled parity is secondary evidence, not a replacement oracle:

- the compiled backend does not currently expose an occurrence journal;
- it cannot independently identify the selected writer, an attempted write, or
  a later clobber;
- equal final states can hide a writer that did not fire when the seeded value
  already equals the landing value;
- shared runtime bugs can still agree;
- packed block storage makes the compiled initial tag snapshot intentionally
  sparse, so full-state parity is asserted after the seeded scan commits.

`make test` already selects `--runner-backend=both`, so every case in the
concrete crossing oracle receives this parity check by default.

### Forward assertion

Conceptually:

```python
actual = occurrence_writes[target_tag]
claim = forward(instruction, target_tag, crossing_context)

if isinstance(claim, Literal):
    assert values_match(actual, claim.value)
elif isinstance(claim, Affine):
    source = occurrence_view[claim.source]
    assert values_match(actual, claim.scale * source + claim.offset)
elif isinstance(claim, Aggregate):
    assert values_match(actual, evaluate_aggregate(claim, occurrence_view))
```

`UNKNOWN` creates no forward assertion.

If storage transforms cannot be represented faithfully by the current
`Literal`/`Affine` algebra, either normalize the result before returning it or
add an explicit stored-value transform. Tests must not silently compare against
the raw pre-store expression.

### Reverse assertion

Evaluate the returned DNF against the concrete transition:

```python
produced_target = values_match(actual_write, requested_value)
preimage_contains_state = evaluate_reverse_result(
    result,
    occurrence_view=occurrence_view,
    prior_snapshot=before_scan,
    rung_enabled=rung_enabled,
    instruction_memory=before_memory,
)

if result.exact:
    assert produced_target == preimage_contains_state
elif not result.fallthrough:
    assert not produced_target or preimage_contains_state
```

The second assertion is the central soundness property:

> If the concrete instruction produces the target, an inexact reverse result
> must contain that concrete preimage.

The constraint evaluator needs direct support for:

- `Eq` and `Cmp` against the occurrence input view;
- `Mask`;
- `Prior` against the pre-scan snapshot;
- `CondAttr` against the observed rung condition/enablement;
- `Quant` against the actual range and instruction mode;
- `External` as a leaf that does not claim an internal preimage;
- the unsatisfiable empty-`Eq` encoding;
- DNF branch semantics.

Held behavior must be explicit. For stateful instructions, the oracle compares
the complete one-scan transition, including prior target value, rung condition,
clock/reset state, and hidden edge/one-shot memory.

## Test matrix

Start with deterministic boundary cases, then supplement them with generated
cases.

### Destination shape

- scalar tag;
- first, middle, and last cell of a static range;
- indirect destination with each bounded pointer value;
- immediate-wrapped destination;
- sequential/fan-out destination;
- every field of a multi-output instruction.

### Control state

- rung false and true;
- prior target equal to and different from the requested value;
- one-shot memory clear and armed;
- clock low, rising, held high, and falling;
- reset inactive and active;
- instruction executed zero, one, and multiple times in a scan.

### Value domains

- both Boolean values;
- numeric minimum, minimum + 1, -1, 0, 1, maximum - 1, and maximum;
- values immediately outside a destination’s clamp rails;
- wrap aliases derived from destination width and affine scale;
- positive, negative, and zero affine scales;
- empty, one-character, and multi-character text;
- search match at first, middle, and last valid window;
- range lengths below, equal to, and at the instruction width.

### Reverse targets

- every producible polarity/value;
- representative impossible values;
- equality at and away from storage rails;
- `<`, `<=`, `>`, and `>=` on both sides of each rail;
- target constraints on every co-write.

For BOOL and deliberately small synthetic domains, exhaust the full domain. For
INT/DINT/WORD, use deterministic rail/wrap cases plus property-based samples
biased toward boundaries and aliases.

## Runtime use in PILOT

The same concrete primitive can verify a fully specified proposed move, but it
should not replace backward discovery.

A single scan from the current state cannot prove that a writer is globally
incapable of producing a value. A missing write may only mean that the writer’s
guard is not yet established. Therefore:

- crossings propose possible predecessor routes;
- PILOT constructs a concrete next action or owned boundary;
- a forked scan verifies the next claimed edge;
- only a contradiction under a fully specified current-world proposal vetoes
  the route;
- “writer did not fire yet” remains inconclusive for a multi-step route.

Verification should inspect the exact selected writer occurrence:

```text
PRODUCED     selected writer attempted the requested write
CLOBBERED    selected writer wrote it, but a later write replaced it
CONTRADICTED selected writer fired but produced another value
NOT_FIRED    selected writer did not execute; inconclusive unless its complete
             guard and edge state were established
```

For a multi-step route, the oracle checks the immediate claimed boundary, not
the final global target. A productive trial must either:

- make the selected writer produce the requested value;
- reach or move closer to an instruction-owned boundary; or
- establish a concrete predecessor constraint that was explicitly predicted by
  the selected crossing.

Merely changing an external input or obtaining a different world key is not
evidence that the selected crossing route advanced.

## Information-left-on-the-table ledger

This ledger records information the system already has or a crossing already
expresses but the next consumer does not chase. Every implementation tranche
must update it. An entry is one of:

- **must chase** — dropping it can select a false route or lose a real route;
- **separate protocol** — useful information, but not valid under the crossing
  soundness contract;
- **intentional frontier** — unsupported by design for now; must remain explicit
  rather than silently appearing handled.

### Pursued: shared reverse-result semantics

The first consumer tranche now centralizes the structural DNF identities:

- fallthrough is distinct from a DNF with no satisfiable branches;
- `Eq(tag, frozenset())` invalidates its conjunction;
- an empty conjunction makes the DNF trivially true;
- surviving branches retain their OR/AND grouping.

PILOT rejects a writer whose normalized reverse is contradictory. Recorded
history invalidates a whole conjunction when a `Prior` cannot be resolved,
instead of silently deleting that requirement, and a tag-bound `Cmp` now
carries both observed operands so a changed bound remains causal evidence.
`ReverseResult.exact` now survives the bounded adapter path as nullable fidelity
on recorded constraints, the selected projected trace writer, and recorded or
projected causal steps. Ordinary structural conclusions and forward-only
verified candidates carry `None`; exactness is not copied into `Atom`, global
state keys, or cycle admission.

### Not yet consumed

The normalizer preserves, but does not itself navigate, the remaining evidence:

- PILOT still lowers only one branch made entirely from scalar `Eq`/`Cmp`;
- projected cause still accepts only one branch containing one singleton `Eq`;
- the recorded high-level adapter still declines multi-branch DNF and
  condition/external/frontier leaves;
- `Mask`, `Prior`, `External`, and `Quant` still need explicit PILOT lowering;
- `AffineCmp` now preserves dynamic counter-preset scale/offset through shared
  reverse algebra, recorded cause, projected expression evaluation, runtime
  overlay lowering, advance planning, wait headings, and trace rewriting.
  Projected PILOT exposes both the accumulator and preset sides as reactive
  levers instead of freezing or erasing either side;
- DNF-wide common equality pins are not yet extracted;

The one-scan oracle also captures more execution information than its first
assertion layer consumes:

- read footprints and per-read values;
- rung identity, kind, nesting depth, and call stack;
- multiple instruction identities inside one rung;
- explicit `PRODUCED` / `CLOBBERED` / `CONTRADICTED` / `NOT_FIRED`
  classification;
- hidden instruction memory not supplied by the test seed;
- multiple occurrences and same-scan writer ordering.

Those are deliberate next layers, not facts the current tests silently claim
to cover. The first oracle matrix isolates one instruction in one rung and
checks its attempted write, forward value, and reverse DNF.

The concrete-oracle catalog now forces every registered crossing class to be
either exercised by a direct runtime case or listed as a named frontier. The
only remaining class frontier is:

- `TimeDrumInstruction`: needs a boundary matrix over elapsed time, step state,
  reset/jump/jog controls, and hidden drum memory;

Receive behavior now has deterministic submit/drain injection. The runtime
oracle distinguishes the remotely supplied payload from locally controlled
request-status writes; payload reverse stops at `External`, while local status
targets fall through instead of being mislabeled as transport inputs.

Catalog coverage is not semantic completeness. The remaining mode matrix
includes first/middle/last and indirect destinations, one-shot reuse, multiple
occurrences, clobber ordering, non-BOOL reset ranges, all pack/unpack sign
slices, search result-address provenance, and wider WORD/DINT/REAL storage
boundaries.

### Must chase

- **Destination identity:** writer discovery knows the concrete target tag, but
  the old forward API discarded it. The canonical target-aware forward contract
  must carry it through every handler.
- **PDG/crossing write agreement:** the PDG records static sequential fan-out and
  several co-writes that `_writer_for_tag()` cannot map back to the instruction.
- **Declared status fields:** timer/counter status tags are declared separately
  from `_writes` even though runtime execution writes them.
- **All concrete co-writes:** function-output dictionaries, request status tags,
  range cells, and loop indices must be enumerable or explicitly opaque.
- **Occurrence write evidence:** replay records which exact rung occurrence
  attempted each write. Generic verification usually compares only landing
  snapshots and therefore loses producer identity and later-clobber information.
- **Reverse branch structure:** the generic trace producer path consumes only one
  deterministic branch made only of scalar `Eq`/`Cmp`. DNF, `Prior`,
  `CondAttr`, `Mask`, `Quant`, and `External` receipts may be emitted but are not
  generically chased.
- **Revisit identity and consumption:** `seen_keys: set` discards the source
  world, exact action artifact, channel from/requested/landed values, whether a
  departure occurrence was already investigated, and whether that
  investigation changed executable knowledge. A fork-wide pending effect is
  only observation liveness. Channel ownership is necessary incident evidence,
  but neither a reached nor departed channel is by itself proof of progress.
- **Uncertainty provenance:** `UNKNOWN` currently records no reason, so ranking
  cannot distinguish an unsupported destination shape from genuinely opaque
  runtime data.

### Separate protocol

- **Verified inverse candidates:** some calc/copy inversions are useful values to
  try but omit valid wrap/clamp preimages. They cannot be represented as
  `exact=False`, whose contract requires a sound superset.
- **Empirical producer trials:** a fully specified current-world action may be
  verified by an interpreted scan. Failure to fire is not a global proof that
  the writer can never produce the target.
- **Heuristic partner freezing:** reversing a multi-source expression by freezing
  all but one operand is a steering proposal, not the full mathematical
  preimage.

Forward `Affine` values now carry an immutable destination `StoreTransform`, and
their concrete evaluation is centralized. When a sound reverse must fall
through, PILOT may derive one affine source candidate through a separate
verify-required trace path. The candidate is not placed in `ReverseResult` and
is not used as a proof pin.

Crossings now also expose an immutable DNF `CrossingProposal` receipt with a
reason and explicit `verify_required` flag. Calc uses it for the old
snapshot-frozen `A ± B` inequality levers: each singleton branch freezes the
other operand only long enough to propose a reactive move. PILOT consumes these
branches only after sound reverse falls through, carries their proposal reason
into heuristic reporting, and relies on interpreted execution for acceptance.
The proposal is never normalized or consumed as a `ReverseResult`.

Still left on the table: proposal verification outcomes are not attached back
to the originating crossing receipt, and the current inequality consumer can
preserve DNF alternatives only when each branch is one scalar comparison.
Conjunctive or mixed-constraint proposal branches remain an explicit frontier
rather than being flattened into false independent levers.

Proposal reason/fidelity currently rides on trace `Atom` metadata. Removing it
now would lose the distinction between a sound reverse conclusion and a
try-and-verify proposal. A dedicated branch receipt belongs with the
conjunctive-DNF consumer; that next step should move the metadata there rather
than adding a second temporary wrapper before branch navigation exists.

Aggregate decomposition and self-affine earned-work classification are likewise
proposal heuristics at clamp/wrap rails. They remain valid only because the
interpreted fork verifies the resulting move; they are not complete reverse
proofs.

### Intentional frontiers

- lossy text-number parsing (`PackText`);
- search result-address provenance, including “no earlier match” constraints;
- off-delay internal state inversion;
- unbounded dynamic/indirect destination regions;
- opaque user-function semantics without a declared summary;
- asynchronous I/O outcome semantics beyond locally controlled request status;
- aggregate sign reasoning where a sum constraint cannot yet be soundly
  attributed to individual elements.

The first cycle patch was discarded before landing. Replacing the old global
`pending` exemption with blanket `channel reached/departed` and
`learned_prescribed` exemptions was the wrong shape:

- `departed` is an incident to investigate, not evidence of progress;
- `reached` can still land in the same semantic world and form a real cycle;
- `learned_prescribed` describes provenance/intent, not a changed executable
  world;
- the cycle gate ran before outcome classification, so it could not make the
  decision from the evidence that actually judges usefulness.

The intended boundary is outcome classification followed by revisit admission.
A seen landing may proceed only when:

- the user target is reached;
- target-relative earned work advanced; or
- this is a novel, unconsumed departure occurrence that must enter post-commit
  investigation exactly once.

The departure credential should identify at least the source world, exact
applied action artifact, and channel from/requested/landed values. Consuming it
must prevent the same occurrence from authorizing another lap. A
replay-confirmed correction changes the executable overlay and therefore the
world key; it needs no `learned_prescribed` cycle override. Global pending
effects remain spin/observation liveness only.

An intentional frontier must return `UNKNOWN`, `REVERSE_FALLTHROUGH`, or an
explicit opaque/external result. It must not inherit a representative crossing
for another output field and must not be described by coverage tests as “no
write” when runtime execution does write.

### Defects exposed by the dual-runtime oracle

The first expansion beyond the original counterexamples exposed two independent
defects:

- Counter and on-delay reverse rules compared the pre-instruction accumulator
  directly with the post-increment completion threshold. A counter can complete
  from one unit before that threshold, so the old claimed necessary condition
  omitted a real producer. Literal counter presets now use the sound one-scan
  frontier; dynamic presets use `AffineCmp`. On-delay now falls through until
  scan duration, time unit, and fractional memory can be expressed.
- The compiled `pack_text` helper emitted an over-escaped integer regular
  expression. Valid numeric text faulted and left the destination unchanged
  while the interpreter parsed it. The compiled helper and a normal
  `runner_factory` regression now enforce parity.
- Integer storage accepts an integral float such as `1.0` and stores `1`.
  Treating that target as impossible made copy/calc reverse report a false
  contradiction, dropping the real writer and its watch dependencies. Integral,
  finite, in-range floats now belong to integer stored domains; fractional and
  out-of-range targets remain impossible.
- Once timer reverse correctly fell through, PILOT lost a future timer coast
  whose enable stage was itself advancing under program control. The old
  behavior had been supplied accidentally by an unsound one-scan timer inverse.
  Advance planning now retains a future instruction-owned scalar boundary only
  when the closed establish route contains neither an external action nor a
  dead end. Multi-scan ownership stays in the advance layer, while the timer
  crossing remains a sound fallthrough.

## Implementation order

### Completed in this tranche

- target-aware forward crossing API;
- scalar/range/immediate reset polarity;
- storage-aware `Literal`, `Affine`, and `Aggregate` evaluation;
- concrete one-scan oracle with neutral clamp/wrap/stateful counterexamples;
- dual-runtime completion parity for the concrete crossing oracle;
- registry-wide direct-or-frontier concrete-oracle catalog;
- exhaustive BOOL coil state rows and boundary-biased copy/calc rows;
- direct range, packing, search, counter, and timer crossing cases;
- sound same-scan counter completion bounds with affine dynamic presets;
- end-to-end projected lowering of affine bounds with both relational levers;
- compiled numeric text-pack parity repair;
- sound fallthroughs for incomplete clamp, wrap, floating, multi-source, text
  window, shift, and drum inversions;
- shared static write-site discovery for PDG, writer lookup, status fields,
  ranges, and static fan-out;
- shared reverse-result normalization and contradiction handling;
- recorded tag-bound comparison and unresolved-prior fixes;
- cycle/revisit audit recorded; the first replacement patch was explicitly
  discarded pending outcome-then-revisit implementation.

### Phase 1: Stop the observed false route

1. Add target-aware forward classification.
2. Resolve reset element type through scalar, range, indirect-range, and
   immediate destination shapes.
3. Add polarity tests proving that reset cannot establish a non-reset value.
4. Prefer a known-compatible writer over `UNKNOWN`; retain `UNKNOWN` only as a
   fallback.
5. Add the neutral range-reset PILOT reproducer as a regression.

### Phase 2: Build the oracle

1. Add a low-level crossing oracle test helper independent of PILOT trace.
2. Implement the crossing-constraint evaluator.
3. Add forward and reverse soundness assertions.
4. Port the current audit counterexamples into permanent tests.
5. Add destination-shape and polarity parameterization to every registered
   crossing.

### Phase 3: Repair value semantics

1. Normalize literal and read-only copy/fill results through destination storage.
2. Make affine forward claims honest about clamp and wrap behavior.
3. Replace under-approximating copy/calc reverses with sound preimages or
   fallthrough.
4. Move useful-but-incomplete inverse guesses into a distinct verified-candidate
   protocol.
5. Correct text-search window semantics.

### Phase 4: Reconcile write discovery

1. Use one shared destination enumerator for PDG extraction, writer resolution,
   crossings, and co-write analysis.
2. Include sequential copy fan-out.
3. Decide and document the treatment of timer/counter status fields.
4. Register explicit crossings or explicit opaque fallthroughs for loop indices,
   function outputs, send statuses, and receive co-writes.
5. Replace inaccurate “no data write” exemptions with precise semantic
   categories.

### Phase 5: Stateful fidelity

1. Recheck every `exact=True` result with the concrete oracle.
2. Include clock/reset/enable/hold conditions where required.
3. Cover one-shot and repeated-occurrence behavior.
4. Keep intentional frontiers such as lossy text parsing and unsupported dynamic
   ranges as named fallthroughs.

### Phase 6: Don’t-cycle containment

After the crossing fixes, independently repair cycle containment:

- classify the executed outcome before deciding whether a seen landing may be
  admitted;
- remove global pending and static learned provenance as revisit authority;
- admit a novel departure occurrence only once for post-commit investigation;
- an unrelated pending timer must not permit repeated A↔B input churn;
- a repeated source/action/channel landing becomes a no-good after its incident
  credential is consumed;
- a reached-to-seen landing without earned work is a cycle, not progress;
- a replay-confirmed correction must re-key the executable world and permit the
  corrected retry without an override;
- crossing uncertainty must never be used as evidence of progress.

## Acceptance criteria

The crossing work is complete when:

- every concrete instruction write is classified as registered, deliberately
  opaque, external, or control-only with an accurate reason;
- every actual destination cell is discoverable by both the PDG and crossing
  resolver;
- every `Literal`, `Affine`, and `Aggregate` forward claim agrees with the
  interpreted write oracle and the completed compiled scan;
- every `exact=True` reverse result is equivalent to the concrete transition
  over the tested domain;
- every `exact=False` reverse result contains every concrete preimage;
- fallthroughs remain behaviorally inert;
- a known-incompatible writer cannot be selected for the requested polarity;
- an `UNKNOWN` writer cannot outrank a known-compatible writer without concrete
  evidence;
- the neutral range-reset reproducer reaches its target without selecting the
  reset route;
- the existing crossing suite and the new semantic matrix pass;
- the separate cycle regression rejects repeated input churn even when an
  unrelated instruction has pending effects.

## Audit artifacts

- `scratchpad/burner/audit_crossing_boundaries.py`
- `scratchpad/burner/repro_block_reset_target_polarity.py`
- `scratchpad/burner/probe_pilot_loop.py`

All reproducer and test vocabulary must remain process-neutral.
