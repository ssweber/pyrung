# Causal analysis + validators: remaining backlog

Companion to the tag-flags design note. Covers the three remaining
items on the causal/validator track: two to implement, one parked.

## `dd`-backed contradiction and satisfiability

Upgrade `_conditions_contradict` (currently four hand-rolled patterns
in the shared validator machinery) to use BDDs via the `dd` library.

### Why

The current pattern matcher catches:

- `CompareEq` vs `CompareEq` with different literals
- `BitCondition` vs `NormallyClosedCondition` on the same tag
- `CompareEq` vs `CompareNe` with same literal
- Two-way range complements (`CompareLt`/`CompareGe`,
  `CompareLe`/`CompareGt`)

It misses transitive contradictions — three or more conditions that
together are unsatisfiable but no pair directly contradicts. It also
can't answer the dual question ("is this conjunction satisfiable?")
that the stuck-bits validator needs for reachability checks.

BDDs answer both questions in canonical form: build once, query
cheaply, deterministic, small dependency. Pure-Python with optional
C backend, clean API.

### How

- Keep pattern matching as the fast path. On pattern-match miss, fall
  back to BDD.
- Canonicalize `(tag_name, literal, op)` into a shared BDD variable
  name so `CompareEq(tag, 5)` and `CompareNe(tag, 5)` encode to
  complementary propositions and their contradiction falls out of
  `bdd.apply('and', ...)` evaluating to false. Easy to get right,
  easy to get wrong — worth a single helper with tests.
- Expose two functions with the same signatures the existing machinery
  uses: `contradict(a, b) -> bool` and `satisfiable(conjunction) -> bool`.
- Works alongside pattern matching — strictly stronger, never weaker.

### When

Bundle with or immediately after the stuck-bits validator.
Stuck-bits needs satisfiability of a conjunction (is this write site
reachable?) more directly than `duplicate_out` needs contradiction of
a pair, so the BDD upgrade pays off faster on the stuck-bits side.

### Out of scope

SMT (z3) for arithmetic relationships across conditions. Defer until a
real case appears that BDD can't handle — PLC code is mostly boolean
or compare-to-literal, and z3 is heavier (slower, bigger install,
harder-to-render failure explanations). The trigger is a real bug the
BDD version missed.

## `assume={}` on `cause` / `effect` / `recovers`

Scenario-pinning parameter on projected walks. Caller supplies a dict
of tag-to-value overrides; the projected walker pins those tags to
the given values and simulates forward using observed behavior for
everything else.

### Signature

    plc.cause(tag, to=value, assume={"Reset_Btn": True})
    plc.effect(tag, from_=value, assume={"Reset_Btn": True})
    plc.recovers(tag, assume={"Reset_Btn": True})

### Uses

Three distinct uses, all valuable:

1. **Exploration.** REPL-driven sweeps for discovering which tests are
   worth writing:

       for tag in fault_tags:
           if not plc.recovers(tag, assume={"Reset_Btn": True}):
               print(f"Reset doesn't clear {tag}")

   Find the tags that fail, write persistent tests for those.

2. **Causal assertions in tests.** The existing testing story asserts
   values (`assert plc[tag] == True`). `assume=` on `cause` asserts
   the ladder logic actually connects inputs to outputs:

       assert plc.cause("Motor_Running",
                        assume={"Start_Btn": True, "EStop": False})
       assert not plc.cause("Motor_Running",
                            assume={"EStop": True})

   Stronger claim than value matching — the output happened *for the
   right reasons* via a traceable path through the logic.

3. **External tag reasoning.** With the `external` tag flag, the
   ladder can't recover these tags on its own. `assume=` lets tests
   stipulate the external world's contribution:

       assert plc.recovers("Alarm_Ack", assume={"Alarm_Ack": False})

   Declaration (`external=True`) and test-time exercise (`assume=`)
   are two sides of the same feature; implement together.

### How

Not symbolic execution. Not a solver. The projected walker already
builds a `projected_inputs` structure per scan; `assume=` is a dict
whose keys override entries in that structure before each simulated
scan. Thin parameter, maybe 50 lines plus tests.

Interaction with tag flags:

- `assume=` on a `readonly` tag should probably raise — the tag is
  declared constant after startup, pinning it to a different value
  contradicts the declaration.
- `assume=` on an `external` tag is the canonical use and should flow
  without friction.
- `assume=` on a `final` tag is fine; `final` constrains who writes
  in the ladder, not what the tag can be.

### Why not broader

`assume=` as scenario pinning is 95% of what engineers ask. The other
5% — "is there *any* input sequence that reaches this state" — is
model-checking territory, different product, defer indefinitely.
pyrung already has `step()` for the cases where simulation beats
symbolic reasoning.

## Branch-level SP validation (parked)

Both validators currently treat `rung._conditions` as a flat tuple of
leaf conditions, indexing into branches via `branch_path`. They miss
SP-structure-internal mutual exclusivity: two outputs on the same rung
in branches genuinely gated by `B` and `~B` within the SP tree look
the same to the validator as two outputs that can both fire. False
positive on `duplicate_out`; missed sharpening on stuck-bits.

### Why parked

- Engineers usually split mutually-exclusive logic into separate rungs
  rather than branches of one rung — that's how Click and most ladder
  tools present it visually. The pattern is rare in practice.
- The fix is a real refactor: carry path-through-SP-tree as the
  conditions representation instead of a flat tuple, and contradiction
  checking needs to understand sibling-of-Or as its own exclusion
  source, not just leaf-vs-leaf contradiction.
- Without a concrete false-positive from a real codebase, designing
  the refactor is speculative.

### Trigger to unpark

A real false-positive from `duplicate_out` on real Click-translated or
hand-written ladder code, where the engineer looks at the finding and
says "those branches can't both fire, the validator should see that."
At that point the failing case informs the refactor design.

## How these three compose

- `dd`-backed contradiction makes both validators sharper. The
  stuck-bits validator benefits most because it asks the harder
  question (tautology/satisfiability rather than pairwise
  contradiction).
- `external` + `assume=` is the test-time story for tags outside the
  ladder's responsibility. `external` declares the contract;
  `assume=` exercises it.
- Sharper contradiction reasoning means fewer false positives on
  stuck-bits, which means fewer tags need `external=True` as noise
  suppression. The features compose but don't block each other.
- Branch-level SP is orthogonal to the other two — it sharpens *what*
  the validators see, while `dd` sharpens *how* they reason about it.

## Out of scope

- Implementation order between `dd` and `assume=`. Either can go first.
- Migration plan for existing `_conditions_contradict` callers — the
  new BDD-backed version keeps the same signature, so callers don't
  change.
- `assume=` modes beyond scenario pinning (worst-case search,
  best-case search, existence proofs). Different product; defer
  indefinitely.