# PDG narrowing and runtime write guards

This document consolidates two related pieces of work that build on the
existing tag annotation layer (`readonly`, `final`, `external`, `public`,
`choices=`) and its validators (`CORE_READONLY_WRITE`,
`CORE_CHOICES_VIOLATION`, `CORE_FINAL_MULTIPLE_WRITERS`):

1. Wire annotation flags into PDG construction as precision refinements, so
   causal chains and coverage reports become tighter and more informative.
2. Add runtime write-site guards that close gaps static validators can't
   cover — primarily writes that go through indirection or computed values.

The two are complementary: narrowing makes the PDG trust the annotations;
the runtime guards make the annotations worth trusting.

## Part 1 — PDG narrowing from annotation flags

The existing flags carry semantic information the PDG can use. Currently,
indirect access (`Copy(DS[idx], target)`) must over-approximate to "any
element in the DS bank" because the index is resolved at runtime. Use the
flags on `idx` to narrow.

### Narrowing rules for indirect access

- **`readonly` index** → singleton. The index is fixed at init and never
  changes; resolve the indirect to the exact element `DS[default_value]`.
  Indirect copy becomes a direct copy in the PDG. Zero precision loss.
- **`choices=[...]` index** → finite enumeration. Expand the indirect to
  exactly one PDG edge per listed choice. `choices=[1,3,7]` produces three
  edges, not 100.
- **`final` index** → defer to the single writer's value range if
  statically derivable (e.g. the sole writer is a counter with known
  bounds); otherwise treat as the bank. `final` alone doesn't narrow; it
  guarantees we only need to look at one writer to attempt narrowing.
- **`external` index** → full bank, but mark the resulting chain as
  externally-bounded in coverage output. Same precision as unannotated,
  but the imprecision is labeled.
- **Unannotated index** → full bank (current behavior).

The engineer sees the payoff directly in coverage: a recipe selector with
`choices=[1..20]` produces 20 distinct, independently-testable chain
branches instead of one merged imprecise chain.

### `final` in causal walks

At every `final` tag in a backward `cause()` walk, termination is into
exactly one rung. No branching over possible writers. This tightens chains
flowing through well-architected tags and aligns with the "one destructive
write per tag" discipline. Surface this in the chain display — `final`
tags shouldn't show a "possible writers" fan-out.

### Collapsed chain display

No new flag needed — `final` already encodes "this tag has one
deterministic writer," so the PDG knows the rung body and can optionally
render `Light ← A ∧ B` instead of `Light ← Intermediate ← A ∧ B` when
`Intermediate` is `final`. Make this a UI toggle on the chain view,
default to collapsed for `final` intermediaries, keep the expanded form
available.

### Ordering constraint

PDG construction must run **after** all validators pass. A
`CORE_READONLY_WRITE` violation elsewhere in the program invalidates the
narrowing assumptions the PDG would otherwise build on. Order is:
validator pass → PDG build → coverage/causal queries.

### Coverage report annotations

When a chain's precision is limited by an approximation, label it in the
report so the engineer sees *why* coverage is loose on a specific chain:

- `[external]` — chain passes through an `external`-declared input;
  precision bounded by upstream systems.
- `[indirect:DS]` — chain passes through an unannotated indirect access;
  precision bounded by bank.
- `[indirect:choices]` — chain passes through a `choices=`-narrowed
  indirect; precise within the listed values.

### PDG narrowing test coverage

- Indirect with `readonly` index → chain has exactly one target element.
- Indirect with `choices=[1,3,7]` index → chain has exactly three target
  elements.
- Indirect with `external` index → chain has full bank, tagged
  `[external]` in report.
- Indirect with no annotation → chain has full bank, tagged
  `[indirect:DS]` in report.
- `final` tag in backward walk → terminates at one rung, no fan-out.
- Collapsed chain display for `final` intermediary matches expanded form
  semantically.

## Part 2 — Runtime write-site guards

Two runtime guards at tag write sites to close gaps that static validators
can't cover. Type-conversion guards are intentionally omitted — because
`Copy(...)` is the only inter-tag conversion path in the language, type
checking is already centralized inside `Copy`. The only remaining
type-coercion concern is at the Python-setter boundary (`Tag.value = X`
from test code), and that should already be enforced by the tag's
descriptor; confirm during implementation and add a single check there if
it's missing.

### 1. `choices=` runtime write guard

Static `CORE_CHOICES_VIOLATION` catches literal writes with out-of-list
values. The runtime guard catches **computed writes** — where the value
comes from another tag or an expression that static analysis can't
resolve.

At every write to a tag declared with `choices=[...]`:

- Evaluate the value about to be written.
- If not in the declared list, raise a violation (same error class as the
  static validator, so test harnesses catch both paths uniformly).
- Include the rung index, tag name, offending value, and declared
  choices in the error.

This upgrades `choices=` from "hint plus static check" to "verified
contract" — the PDG can now treat a `choices=[1,3,7]` index as soundly
narrowed to three edges, with both static and runtime enforcement backing
the narrowing.

### 2. `readonly` and `final` runtime write guards

Static `CORE_READONLY_WRITE` and `CORE_FINAL_MULTIPLE_WRITERS` catch rungs
that syntactically write to these tags. They **don't** catch writes
through indirection — `Copy(source, DS[idx])` where `DS[idx]` resolves at
runtime to a `readonly` or `final` tag.

At every write sink, after indirect resolution:

- **`readonly`**: if target is `readonly`, raise. No writes permitted
  post-init.
- **`final`**: if target is `final` and the writing rung is not the single
  declared writer of that tag, raise.

Runtime data needed: tag flags are already on the tag; `final`'s
sole-writer identity is established at validator pass and can be cached
on the tag as `_sole_writer_rung_id`. The guard is a cheap attribute
check.

### Implementation shape

Both guards live at the same code site: the write sink in
`ScanContext.commit()` (or wherever the authoritative tag write happens).
Single function:

```python
def _validate_write(tag, value, rung_id):
    if tag.readonly:
        raise ReadonlyWriteError(...)
    if tag.final and rung_id != tag._sole_writer_rung_id:
        raise FinalMultipleWritersError(...)
    if tag.choices is not None and value not in tag.choices:
        raise ChoicesViolationError(...)
```

Consolidating the guards here keeps the logic in one place and makes it
easy to audit what's checked at each write.

### Debug mode gating

Guards run unconditionally in simulation/DAP sessions. In the soft-PLC
runtime, gate them on a flag (default on, `strict_writes=False` to
disable for perf). Controls engineers may want to run the live Modbus
PLC in permissive mode for production while keeping the debug harness
strict.

### Runtime guard test coverage

For each guard, a trio of tests:

- Static case still works (existing validator test, unchanged).
- Runtime case fires correctly (computed/indirect write that bypasses
  static analysis).
- Negative case: a valid write doesn't trigger the guard (no false
  positives).

Plus one integration test per guard confirming the error propagates
through a test harness cleanly — these need to fail tests loudly, not be
swallowed.

## How the two parts interact

The runtime guards are what let PDG narrowing be sound in the presence of
indirection. Without the runtime guards, a narrowing like
"`choices=[1,3,7]` means exactly three edges" is only as trustworthy as
the static validator — which can't see computed writes. With the guards,
the PDG can treat the annotation as a hard invariant, because any
violation during execution raises rather than silently producing a value
the narrowing didn't account for.

Order of work is:

1. Add the runtime guards (Part 2). Verify they fire on indirect and
   computed writes. This is the foundation.
2. Wire the narrowing rules into PDG construction (Part 1). Now the PDG
   can trust the annotations because both static and runtime enforcement
   back them.
3. Update coverage reports to surface narrowing precision and
   imprecision labels.
4. Add the collapsed chain display for `final` intermediaries.

Each step is independently shippable and independently testable.
