# Causal Chains: `program.cause()`, `program.effect()`, `program.recovers()` (v1.2)

## What

Dynamic causal-chain analysis for pyrung. Three primitives that interrogate
recorded scan history (and, where useful, project forward from the current
state) to answer:

- **`cause(tag, to=None)`** — what caused this tag to take this value?
- **`effect(tag, from_=None)`** — what did this tag's transition cause downstream?
- **`recovers(tag)`** — is there a reachable path from the current state that
  clears this tag back to its resting value?

All three exploit the same underlying machinery: walk the (history × static
PDG) graph, constrained per rung by SP-tree attribution, distinguishing
**proximate causes** (what flipped) from **enabling conditions** (what was
already holding the path open).

The dynamic counterpart to the static `upstream` / `downstream` slicing
already in DataView. Static slicing answers "what *could* affect this?";
causal chains answer "what *did* affect this, in this scan, and which
contacts on the path actually mattered?" — and, with `to=`, "what *would*
affect this, if we projected forward from now?"

## Why

Every PLC tool shows state. The good ones show state over time. None show
**causation** — because causation requires the static PDG (could-affect)
plus per-scan history (what-changed) plus per-scan rung firings (what-fired)
plus cheap diffing plus structural decomposition of each rung's logic.
pyrung's simulation-first, immutable-snapshot architecture is the first
runtime with all five.

User-facing pitch: **"Right-click any event, see why it happened. Click any
step to jump to that scan. Ask whether any state your program can enter, it
can also leave."**

## Primitives

Three direct methods on `Program`, paired by the natural English idioms:

```python
program.cause(tag, scan=None, to=None)      # what caused / would cause
program.effect(tag, scan=None, from_=None)  # what was / would be caused
program.recovers(tag)                       # bool — clear path reachable
```

### `cause()` — retrospective and prospective

**Retrospective mode** (`to=None`, the default): walks recorded history.
Finds the most recent transition of `tag` (or the transition at `scan=N`
if specified) and explains it.

```python
program.cause(Sts_FaultTripped)              # most recent transition
program.cause(Sts_FaultTripped, scan=1247)   # specific scan
```

**Prospective mode** (`to=` specifies a value): projects forward from the
current state. Finds reachable paths that would drive `tag` to the
specified value.

```python
program.cause(Sts_FaultTripped, to=False)    # how could this clear?
```

Both modes return a `CausalChain`. The chain's `mode` field
(`'retrospective'` or `'prospective'`) lets consumers tell which kind of
answer they got. Retrospective chains are recorded fact; prospective chains
are reachable possibility.

Prospective causation is grounded in **observed input behavior** — inputs
that have transitioned in recorded history are considered as candidates for
transitioning again. This is the pyrung-native default: it catches the bug
class that matters most ("we wrote a clear rung but never fed it the
conditions to fire") without false alarms about hypothetical operator
sequences that nobody would ever execute.

### `effect()` — retrospective and prospective

Symmetric to `cause()`. Retrospective `effect()` walks forward from a
recorded transition, showing what the transition caused downstream.
Prospective `effect()` (with `from_=value`) asks: if `tag` were to
transition to this value right now, what would fire?

```python
program.effect(Cmd_Start, scan=1244)         # what did Cmd_Start cause?
program.effect(Cmd_Start, from_=False)       # what would happen if it went TRUE now?
```

Prospective `effect()` is what-if analysis without mutating state. Useful
for "if the operator pressed this button right now, what would happen?"
— answerable without actually running a scan with the input changed.

### `recovers()` — convenience predicate

```python
program.recovers(tag) -> bool
```

Defined as a one-line convenience over `cause()`:

```python
def recovers(self, tag):
    """True if `tag` has a reachable clear path from the current scan."""
    return self.cause(tag, to=tag.resting_value) is not None
```

Kept as a separate method because **engineers say "does this fault
recover?"** — not "does the tag have a causal path to its resting value?"
The convenience method bridges domain vocabulary and implementation
vocabulary, which matters for assertion ergonomics:

```python
def test_all_faults_recoverable():
    for fault in program.tags.matching("Sts_*Fault*"):
        assert program.recovers(fault), f"{fault} is stranded"
```

Reads as a safety property. The expanded form reads as plumbing.

### Not on DataView

Causal chains are about **specific events at specific times** — they don't
fit DataView's "filtered set of tags" mental model. Keeping them as direct
methods on `Program` is more honest about what they are.

Composition still works in the natural direction — pass chain results
*into* DataView when you want to view them structurally:

```python
chain = program.cause(Sts_FaultTripped)
program.dataview().contains(chain.tags())
```

The chain hands data to DataView, not the other way around.

### DAP query syntax

Matches the existing `upstream:` / `downstream:` style:

```
cause:Sts_FaultTripped              # most recent transition
cause:Sts_FaultTripped@1247         # specific scan
cause:Sts_FaultTripped:false        # prospective — how could this clear?
effect:Cmd_Start@1244               # forward chain
effect:Cmd_Start:true               # prospective — what if it went TRUE now?
recovers:Sts_FaultTripped           # bool — clear path reachable
```

### `CausalChain` object

```python
class CausalChain:
    effect: Transition                  # event being explained (or projected)
    mode: Literal['retrospective', 'prospective']
    conjunctive_roots: list[Transition] # roots that fired together (AND)
    ambiguous_roots: list[Transition]   # roots we can't disambiguate (OR)
    steps: list[ChainStep]              # alternating transitions and rung firings
    confidence: float                   # 1.0 if unambiguous; see Confidence below
    duration_scans: int

    def to_config(self) -> dict   # round-trippable for DAP/presets
    def to_dict(self) -> dict     # rich form for UI/LLM
    def tags(self) -> list[Tag]
    def rungs(self) -> list[Rung]
```

`Transition` is `(tag, scan, from_value, to_value)`. `ChainStep` is either
a transition or a rung-firing, tagged by type — the chain alternates between
them. Each transition step also carries an `enabling_conditions:
list[EnablingCondition]` field listing the contacts that mattered for that
rung's evaluation but held steady (didn't transition). `EnablingCondition`
carries the tag, current value, and scan-of-last-transition — UIs need the
"held since scan X" temporal aspect.

For prospective chains, `Transition` may carry hypothetical scans
(e.g., "scan +3" relative to current) rather than absolute history scans.

## Surveys: the `program.query` namespace

The direct methods explain specific events. Whole-program surveys live
under a separate namespace:

```python
program.query.cold_rungs()           # rungs that never fired across history
program.query.hot_rungs()            # rungs that fire every scan
program.query.stranded_bits()        # bits with no reachable clear path
program.query.coverage_gaps(tag)     # static upstream paths never exercised as proximate
program.query.unexercised_paths(tag) # alias for coverage_gaps; reads better in some contexts
program.query.full()                 # comprehensive structured report
```

Mental model:

- **`dataview`** queries static structure ("what's in the program")
- **`query`** queries dynamic history ("what happened across history, and what
  could still happen from where we are now")
- **direct methods** (`cause`, `effect`, `recovers`) explain or project
  specific events

The `query` namespace is built almost entirely on top of `cause`/`effect`
with the `to=`/`from_=` parameters. For example:

```python
def stranded_bits(self):
    return [tag for tag in program.persistent_bits()
            if not program.recovers(tag)]

def coverage_gaps(self, tag):
    static_paths = program.dataview().upstream(tag).rungs()
    exercised   = set().union(*(program.cause(tag, scan=s).rungs()
                               for s in program.scans_where_transitioned(tag)))
    return static_paths - exercised
```

`query` results are intended for CI gates, code review reports, and
iterative program review (human or LLM). The namespace name signals the
cost profile: queries may walk all of history, where direct methods walk
a single chain.

## Coverage and Liveness

The combination of static slicing, dynamic causation, and prospective
recoverability gives pyrung something no other PLC tool has: **causal-path
coverage in both directions**.

### Forward direction: reachability coverage

For a given tag, every static upstream path should be exercised at least
once as a proximate cause across the test suite or recorded operation.

```python
gaps = program.query.coverage_gaps(Sts_FaultTripped)
# rungs in the upstream slice that have never fired as the proximate cause
```

100% forward coverage means: every way this tag can take its non-resting
value has been demonstrated in actual execution. Stronger than rung
coverage (did it fire?), stronger than branch coverage (did each Boolean
branch evaluate both ways?), because it's coverage of *causal paths* — the
actual unit of meaning in ladder logic.

Three categories of gap fall out naturally:

- **Cold paths** — rungs in the upstream slice that never fired. Either
  dead code or an untested path waiting to bite.
- **Warm-but-not-causal paths** — rungs that fired, but never as the
  proximate cause of the target tag. The SP-tree attribution is what makes
  this distinction possible.
- **Untested branches** — within a rung that did fire causally, SP-tree
  branches that were never the conducting path.

### Backward direction: recoverability coverage

For every persistent bit the program can drive to a non-resting value, is
there a reachable path that drives it back?

```python
stranded = program.query.stranded_bits()
# bits the program can enter but has no demonstrated way to leave
```

That list is the ladder-logic equivalent of *liveness violations*. Every
item is a state the program can reach with no recovery path — the
latch-without-reset bug, the fault-without-clear bug, the
mode-you-can-get-into-but-not-out-of bug. The most expensive class of PLC
bug because it manifests as "the machine is stuck and nobody knows why" at
2am.

### Self-clearing vs persistent state

Recoverability analysis must distinguish two kinds of state:

- **Self-clearing**: timer DN bits, edge-triggered one-shots, momentary
  coils. These clear automatically when their enabling rung evaluates
  FALSE. Structurally guaranteed by instruction semantics; no analysis
  needed.
- **Persistent**: latches, retentive bits, anything a subroutine flipped on
  and walked away from. These only clear if some other rung, somewhere in
  the program, drives them back — and that other rung must be reachable
  from the state in question.

The recoverability engine consults a per-instruction table declaring each
instruction's clear-conditions, so it knows which writes count as
self-clearing and which require external recovery.

### 100% in both directions

Forward coverage alone tells you "every fault can fire." Backward coverage
alone tells you "every fault can clear." You need both to claim the
program's state space is fully exercised.

Most tools measure one direction (usually forward, badly, as line/branch
coverage). Measuring both, at the causal-path level, is the thing nobody
does. With both, "the test suite covers this program" becomes a meaningful
claim instead of a hopeful one.

## Merging Coverage Across Tests

The `query` namespace becomes meaningful at the **suite level**, not the
test level. A single test exercises a tiny slice of program behavior; cold
rungs and stranded bits from one test are mostly noise. The signal emerges
when you merge findings across an entire test suite.

This is the same pattern Vulture uses for Python dead-code detection: run
the analyzer over the library and the test suite together, and "unused"
becomes "untested" — a much stronger claim. The pyrung version generalizes
to all the `query` primitives.

### Set algebra: intersection for absence, union for presence

Negative findings (what's missing) merge by intersection. Positive findings
(what's covered) merge by union.

```
cold_rungs(suite)         = ⋂ cold_rungs(t)         for t in tests
stranded_bits(suite)      = ⋂ stranded_bits(t)      for t in tests
exercised_paths(suite, X) = ⋃ exercised_paths(t, X) for t in tests
coverage_gaps(suite, X)   = static_paths(X) - exercised_paths(suite, X)
```

A rung is only cold in the suite view if **no** test fired it. A bit is
only stranded if **no** test demonstrated a clear path. A causal path is
only a gap if **no** test exercised it.

Each test you add can only ever shrink these residuals. The trajectory is
monotonically toward zero, and what remains is what you actually need to
investigate.

### Merge derived sets, not histories

You don't need to keep all scan snapshots from all tests in memory. Each
test runs in isolation, computes its local report, and emits it. The
session-level merge is cheap set algebra over the reports.

```python
@dataclass
class CoverageReport:
    cold_rungs:      set[int]
    stranded_bits:   set[Tag]
    exercised_paths: dict[Tag, set[Path]]

    def merge(self, other: 'CoverageReport') -> 'CoverageReport':
        return CoverageReport(
            cold_rungs    = self.cold_rungs & other.cold_rungs,
            stranded_bits = self.stranded_bits & other.stranded_bits,
            exercised_paths = {
                tag: self.exercised_paths.get(tag, set())
                   | other.exercised_paths.get(tag, set())
                for tag in self.exercised_paths.keys() | other.exercised_paths.keys()
            },
        )
```

### Pytest integration

A small plugin handles the merge invisibly. Each test runs against a fresh
program; the session-end hook collects each test's report and emits the
merged result, gated by a whitelist.

```python
# conftest.py
@pytest.fixture
def program(request):
    p = Program(...)
    yield p
    request.session._pyrung_reports.append(p.query.report())

def pytest_sessionfinish(session, exitstatus):
    merged = reduce(CoverageReport.merge, session._pyrung_reports)
    write_report(merged, "pyrung_coverage.json")

    new_cold     = merged.cold_rungs    - whitelist.cold_rungs
    new_stranded = merged.stranded_bits - whitelist.stranded_bits
    if new_cold or new_stranded:
        pytest.exit(
            f"Uncovered: {new_cold}, stranded: {new_stranded}",
            returncode=1,
        )
```

Test authors write normal pytest code. The plugin handles everything else.

### Asymptotic whitelist meaningfulness

With one test, "cold rungs" is mostly noise — you didn't exercise much.
With 1000 tests, anything still cold has had 1000 chances to fire and
didn't. That's where the signal-to-noise ratio inverts in your favor: the
residual is *really* cold, and the whitelist becomes a short list of
deliberate "yes, we know this is dormant by design" decisions.

Same logic for stranded bits. After one test, a bit might appear stranded
because that test didn't run the recovery scenario. After 1000 tests, if a
bit is still stranded, **no test in the suite** has demonstrated a way to
clear it. That's a genuine liveness violation, not a coverage gap.

### Diff-able across commits

Today's merged report: 3 cold rungs, 0 stranded bits, 12 coverage gaps.
Tomorrow's commit adds a feature; the new report has 5 cold rungs, 1
stranded bit. The CI delta is concrete: "your change introduced 2 new cold
rungs and 1 new stranded bit — investigate." Sharper signal than absolute
counts, and the diff is what review comments cite.

### Subtlety: test isolation

Tests that share state (fixtures scoped beyond function) make recoverability
findings path-dependent. Default to function-scoped programs — each test
fully isolated. Wider fixture scopes change the meaning of stranded-bit
findings. Probably never matters in practice, but worth one sentence in
the docs.

## Per-rung Causal Attribution

The novelty of this feature isn't series-parallel reduction — pyrung
already does that ambiently as part of how it represents rungs. The
novelty is **applying SP structure to attribute causation per rung**, and
the **proximate vs enabling distinction** that falls out of doing so.

### SP trees are already there

SP reduction isn't new to pyrung — the codegen path already uses it (with
Shannon expansion for bridges) to translate ClickNick ladder. Hand-written
rungs are SP by construction via `And`/`Or`/`~`. What's new here is *using
the SP structure for causal attribution* rather than just for translation
or evaluation.

By the time a rung reaches the causation engine, its condition is an SP
tree regardless of where it came from. The debugger reads the tree off the
rung in memory; no caching, no persistence.

### The four-rule attribution walk (the new part)

Given a rung with an SP tree and its truth value at scan N, walk the tree
post-order with these rules to find the contacts that mattered:

| Node              | Children that mattered                     |
|-------------------|--------------------------------------------|
| **SERIES TRUE**   | All children (all were necessary)          |
| **SERIES FALSE**  | Only FALSE children (the blockers)         |
| **PARALLEL TRUE** | Only TRUE children (the conducting branch) |
| **PARALLEL FALSE**| All children (all were necessary)          |

Recurse into the children that mattered. The leaves you reach are the
**contacts that mattered** — the only ones the causation engine considers
for this rung's evaluation. Everything else is irrelevant noise.

### Proximate causes vs enabling conditions

Intersect "contacts that mattered" with the transition log:

- **Proximate cause**: contact that mattered *and* transitioned in or near
  this scan. This is what flipped the rung.
- **Enabling condition**: contact that mattered *but* held steady. Required
  for the rung to fire but didn't itself cause the change.

This distinction is what engineers actually want. A chain narrative reads:

> `Sts_FaultTripped` flipped TRUE because `Sensor_Pressure` went TRUE,
> while `Permissive_OK` and not-`Faulted` were already holding the path
> open.

Proximate cause is the headline; enabling conditions are context. Both
appear in the `CausalChain` but are tagged distinctly so the UI can render
them differently (proximate in bold, enablers as a sub-list).

## Forward Walk: Counterfactual Evaluation

The forward `effect()` walk is **not** symmetric tree-walking. It's a
different operation: **counterfactual SP evaluation**.

Given a cause transition at scan N, find rungs that read the cause tag and
fired in scan N+1. For each such rung, ask: *would the rung have evaluated
the same way without the cause tag's transition?* If yes, the cause didn't
matter for that rung — it was an enabler at most. If no, the cause was a
proximate driver of the rung's firing, and we recurse forward on what the
rung wrote.

Counterfactual evaluation on an SP tree is cheap — flip the leaf,
re-evaluate the tree, compare to the original output. The tree's structure
makes this O(depth) per rung.

### Stopping rule for steady-state

Latched outputs introduce a problem for forward walks: once a latch is set,
it continues to "cause" downstream effects every subsequent scan, in
principle forever. The forward walk needs a stopping rule:

- Stop when no new tags enter the chain across K consecutive scans
  (default K=3). The chain has reached steady state and further scans add
  nothing new.
- Stop at a maximum scan distance (default 1000) as a hard cap.

Both are configurable. The defaults catch the realistic case
("propagation completes within a few scans") without runaway chains on
latched logic.

## Confidence

Two distinct categories of "more than one root cause," and they mean
opposite things:

**Conjunctive roots (high confidence)** — A SERIES TRUE node where multiple
children transitioned simultaneously. Both (or all) are real causes that
together flipped the rung; this isn't ambiguity, it's *joint causation*.
The chain confidently asserts "these N events together caused the effect."

**Ambiguous roots (lower confidence)** — A single contact had multiple
candidate transitions in the recent past (toggled twice within the lookback
window), and the engine can't tell which was the operative one. Genuine
uncertainty. By construction, `ambiguous_roots` is empty or has ≥2 entries;
a single candidate transition is unambiguous and goes in
`conjunctive_roots`.

The `CausalChain` data model exposes these as separate fields
(`conjunctive_roots`, `ambiguous_roots`) from day one, even though v1
ships a scalar `confidence` for simple sorting.

For v1, the scalar formula:

```
confidence = 1.0 if len(ambiguous_roots) == 0
           else 1 / len(ambiguous_roots)
```

Conjunctive roots don't reduce confidence. The UI shows ambiguous chains
with "ambiguous — N candidate causes, expand to compare," and conjunctive
chains with "caused jointly by N events."

## Worked Example

Six-line ladder fragment:

```python
with Rung(And(Sensor_Pressure, Permissive_OK, ~Faulted)):
    latch(Sts_FaultTripped)

with Rung(And(Sts_FaultTripped, Cmd_Reset)):
    reset(Sts_FaultTripped)

with Rung(Sts_FaultTripped):
    out(Alarm_Horn)
    reset(Cmd_Run)
```

Scan history (excerpt):

```
scan 1240: Permissive_OK    0→1    (operator pressed reset)
scan 1244: Cmd_Run          0→1    (operator started cycle)
scan 1247: Sensor_Pressure  0→1    (overpressure detected)
scan 1247: Sts_FaultTripped 0→1    (rung fired, latched)
scan 1248: Alarm_Horn       0→1
scan 1248: Cmd_Run          1→0
```

### Retrospective `cause`

`program.cause(Sts_FaultTripped)` returns a chain whose UI rendering reads:

```
Sts_FaultTripped flipped TRUE at scan 1247  [retrospective]
  └── Rung 47 fired (SERIES TRUE)
      ├── proximate: Sensor_Pressure 0→1 at scan 1247
      └── enabling:  Permissive_OK = TRUE   (held since scan 1240)
                     Faulted       = FALSE  (held since startup)
```

Narrative: **"`Sts_FaultTripped` flipped because `Sensor_Pressure` went
TRUE, while `Permissive_OK` and not-`Faulted` were already holding the path
open."**

### Retrospective `effect`

`program.effect(Sensor_Pressure, scan=1247)` returns the forward chain:

```
Sensor_Pressure 0→1 at scan 1247  [retrospective]
  └── caused Sts_FaultTripped 0→1 at scan 1247 (Rung 47)
      └── caused Alarm_Horn 0→1 at scan 1248 (Rung 49)
      └── caused Cmd_Run    1→0 at scan 1248 (Rung 49)
```

The forward walk used counterfactual evaluation at Rung 47: without
`Sensor_Pressure`'s transition, the SERIES would have stayed FALSE, so
the cause was load-bearing.

### Prospective `cause` / `recovers`

After scan 1248, `Sts_FaultTripped` is latched TRUE.

`program.cause(Sts_FaultTripped, to=False)` returns the prospective chain:

```
Sts_FaultTripped → FALSE  [prospective, +N scans]
  └── Rung 48 would fire (SERIES TRUE)
      ├── proximate: Cmd_Reset would need to transition 0→1
      └── enabling:  Sts_FaultTripped = TRUE  (currently held)
```

`program.recovers(Sts_FaultTripped)` returns `True` — there's a reachable
clear path via `Cmd_Reset`. If Rung 48 didn't exist, both calls would
report a stranded bit:

```
Sts_FaultTripped: stranded
  no reachable clear path from current state
  set by:  Rung 47 (latched at scan 1247)
```

### Whole-program survey

`program.query.full()` runs all surveys at once:

```
COVERAGE GAPS
  Sts_FaultTripped:
    cold paths:   Rung 22 (Pressure_Override branch)
    untested:     Rung 47 PARALLEL branch 2 (Manual_Trigger path)

STRANDED BITS
  (none)

COLD RUNGS (3)
  Rung 22, Rung 91, Rung 104

HOT RUNGS (12)
  ...
```

Notice what doesn't appear in any chain: tags that happened to transition
near the relevant scans but aren't on a "mattered" path. The SP-tree
attribution prunes everything irrelevant.

## Prerequisite Infrastructure

Add `rung_firings` to each scan snapshot — a pyrsistent map of
`rung_index → {tag: value_written}`. Sparse, structurally shared, cheap
because most rungs don't change firing state scan-to-scan.

```python
scan.rung_firings  # PMap[int, PMap[Tag, value]]
```

Unlocks more than just causal chains:

- **Cold rung detection** — rungs that never fire across history (dead code)
- **Hot rung detection** — rungs that fire every scan
- **Test coverage** — which rungs were exercised by the test suite
- **Activity heatmap** on the graph view — color rungs by firing frequency
- **Per-scan rung diffs** — which logic changed between scan N and N+1

## Algorithm

### Retrospective backward walk (`cause(tag, to=None)`)

1. Find the most recent transition of `tag` (or use `scan=N` if specified).
2. Find rungs that wrote `tag` in that scan (firing log).
3. For each such rung, walk its SP tree using the four-rule attribution to
   identify which contacts mattered for the evaluation.
4. Intersect "mattered" with the transition log:
   - mattered AND transitioned → proximate cause, recurse
   - mattered AND held steady  → enabling condition, record but don't recurse
5. Recurse on each proximate cause.
6. Stop at tags with no upstream writes — those are root causes.

### Prospective backward walk (`cause(tag, to=value)`)

1. Find rungs in the static PDG that *could* write `value` to `tag`.
2. For each such rung, walk its SP tree to determine what conditions would
   need to hold for the rung to fire and produce `value`.
3. For each required input transition, check if it's reachable from the
   current state given observed input behavior. Recurse.
4. Return the shortest reachable path (or all paths, if requested).
5. If no reachable path exists, return `None` — the tag is stranded.

### Forward walks (`effect(tag, ...)`)

Symmetric to backward, using counterfactual SP evaluation per rung to
determine load-bearing causation. Stops at steady state (no new tags
across K consecutive scans) or maximum distance.

## Shipping Order

1. **Add `rung_firings` to scan snapshots.** Track which rungs fired and
   what they wrote, per scan, during simulation.

2. **Expose SP trees uniformly on rungs.** Three sub-steps; the third is
   the one that bites if skipped:

   a. **Codegen path**: keep the SP tree the ClickNick translator already
      builds (post Shannon expansion) on the rung object.
   b. **Hand-written path**: wrap the existing `And`/`Or`/`~` expression
      AST behind the same interface the codegen path exposes.
   c. **Verify equivalence**: maintain a small standing corpus of paired
      examples (Click → pyrung) in the test suite; assert resulting SP
      trees have identical shape, identical leaf sets, and produce
      identical attribution results on the four-rule walk. If a Click rung
      translated to pyrung produces a different tree shape than the
      equivalent hand-written rung, the causation engine will silently
      disagree across the two sources. This step catches that.

3. **`program.cause(tag, scan=N)`** — retrospective backward walk with the
   four-rule attribution. Returns proximate causes and enabling conditions
   distinctly. `to=None` only at this stage.

4. **DAP `cause:tag@scan` query handler.** Wire through.

5. **Graph view highlighting** with `causal-path` class and sequenced
   numbering. Render proximate causes and enabling conditions with
   different visual weight.

6. **Sidebar timeline panel** — chain as a story, click any step to jump
   to that scan via existing fork machinery.

7. **`program.effect(tag, scan=N)`** — retrospective forward walk via
   counterfactual SP evaluation. Steady-state stopping rule.

8. **Prospective mode for `cause()` and `effect()`.** Add `to=` and
   `from_=` parameters. Walk static PDG forward, ground in observed input
   behavior. Returns `CausalChain` with `mode='prospective'`.

9. **`program.recovers(tag)`** — three-line convenience predicate over
   prospective `cause()`. Catches stranded bits in test assertions.

10. **`program.query` namespace.** Compositions over `cause`/`effect`:
    `cold_rungs()`, `hot_rungs()`, `stranded_bits()`, `coverage_gaps()`,
    `unexercised_paths()`, `report()`. Mostly thin layers over the
    primitives.

11. **Coverage merge primitive and pytest plugin.** `CoverageReport`
    dataclass with `merge()` method (intersection for negative findings,
    union for positive). Pytest plugin that collects per-test reports and
    emits the merged result at session end. Whitelist file format
    (TOML), CI-failure gating on whitelist diff. This is the step that
    makes the residual meaningful and the report a compass for
    iteration.

12. **Conjunctive vs ambiguous root distinction in confidence scoring** —
    data model already supports both fields from step 3; this step makes
    the UI render them differently.

13. **Prospective chain rendering in graph view** — dashed lines, "if X
    transitions" labels, distinct visual treatment from retrospective
    chains.

## Workflow: Iterative Program Review

The combination of primitives, surveys, and pyrung's existing `dataview`
forms a review loop that's the actual product:

1. **Scope** with `dataview.upstream(suspect_tag)` — bound the static
   surface area
2. **Run** for X scans with realistic inputs
3. **Patch** what looks wrong
4. **Test** with pytest to confirm behavior
5. **Verify** with `program.cause(transitioned_tag)` that the patch fixed
   the right thing — if the chain doesn't pass through your patched rung,
   you fixed something else

That last step is the killer. Tests passing because the bug moved is the
oldest failure mode in debugging, and `cause()` catches it directly.

For LLM-assisted review specifically, the loop unlocks scale that's
impractical by hand. The current state of the art is "paste ladder into
chat, ask LLM to reason about it" — which works for small programs and
falls apart fast because the LLM has to simulate execution in its head
and gets the timing wrong. With this loop, the LLM doesn't simulate. It
*runs* the program, *reads* the chain, and reasons over actual causation.
The reasoning surface shrinks from "all possible execution paths" to
"this specific chain of N steps that actually fired." And the LLM can
write the 1000 tests an engineer wouldn't have time to write by hand,
which is what makes the merged-report compass (below) actually meaningful.

The `CausalChain.to_dict()` shape is designed for programmatic
consumption — UI overlays, LLM context, external review tools. Includes
rung source (the actual `with Rung(...)` line, not just an index), tag
descriptions where available, the proximate/enabling split rendered as
structured fields, prior values and held-since scans on enabling
conditions.

### The merged report as compass

The merged coverage report from the test suite is what tells the reviewer
— engineer or LLM — whether iteration is making progress. Each new test
produces a delta against the residual:

- Cold rungs went 12 → 11: progress; that test exercised something new.
- Stranded bits went 3 → 0: recoverability complete for now.
- Coverage gaps for `Sts_FaultTripped` went 5 → 5: that test didn't help;
  try different inputs.
- Stranded bits stuck at 3 across 5 iterations: it's not a test gap, it's
  a structural code problem.

That last signal is the key one. When the residual stops shrinking despite
new tests, the conclusion is mechanical: the gap is in the code, not in
the coverage. The reviewer has a clean stopping criterion — "I've added
five tests trying to clear stranded bit `X` and none reduce the count;
the bit may be structurally unrecoverable; review the rung structure
rather than writing more tests."

Without this signal, a reviewer iterating on tests can't tell whether the
work is productive. They write a test, the test passes, and... what? The
merged report converts that ambiguity into a number that goes up, down,
or stays the same with each iteration. That's the difference between
flailing and reporting "I'm making progress, here's what's left" or "I'm
stuck, here's why."

For an engineer, this answers "are we done?" — if the residual is empty
or fully on the whitelist, yes. For an LLM, this answers the same
question and supplies a stopping criterion that doesn't depend on
self-judgment about whether enough tests have been written. Either way,
the decision is mechanical, not vibes.

### The marketing wedge

Every test framework on earth tells you *that* a test passed. None tell
you *why* the code under test behaved correctly. Causal chains do.

And every coverage tool tells you *which lines* ran. None tell you *which
causal paths* were exercised, or *which states* the program can enter but
not leave. Pyrung's merged report does both, with whitelisting and CI
integration borrowed from the patterns Python developers already use for
dead code.

The combined pitch: pyrung brings the `lint + test + coverage` workflow
that Python developers take for granted to a domain (PLC ladder logic)
that has approximately none of it.

## Prior Art

Causal debugging is not a new idea in general software — the goal is to
ground pyrung's contribution honestly.

**General-purpose time-travel debugging** (rr, Undo's UDB, WinDbg TTD,
Replay.io) has existed since the early 2000s, with academic roots back to
ZStep 95 (1995) and Smalltalk-76. These tools record program execution and
let you step backward through history. They are mature, powerful, and
shipped at scale. They do not auto-attribute causation — engineers still
set watchpoints and reason manually about why something happened.

**Causal Testing / Holmes** (Brittany Johnson, 2018) is the closest
academic prior art for general software. It uses delta-debugging-style
input perturbation to identify which input differences cause which
behavioral differences in test failures. Same conceptual family as
pyrung's prospective `effect()` — different domain, different mechanism,
shared instinct that "show me the cause, not just the state" is the
missing primitive in debuggers.

**Hardware/RTL verification** (Synopsys Verdi, similar tools) does
automated RCA on chip simulation traces. RTL is the closest analog to
ladder logic — Boolean gates evaluated each cycle. The chip world built
this; the PLC world didn't. Verdi is closed, vendor-locked, and aimed at
silicon teams.

**Static slicing for ladder logic** exists in academia (2010 IEEE work,
older Stanford constraint-based RLL analysis citing "millions of dollars
per bug"). Static only — what pyrung's `dataview` already does. Nothing
dynamic in the literature for PLC programs specifically.

**PLC debugging in industry** is genuinely 20+ years behind general
software debugging. The state of the art in vendor tools and tutorials is
trap bits, traces, breakpoints, and "use search and reverse-engineer the
input conditions manually." A 2024 industry article (Peterson, Control.com)
describes how engineers currently cope: each professional has "established
a unique mental flowchart process built from years of on-the-job
experience." [^1] A 2026 industrial debugging guide observes that "no
automated tool replaces understanding of application intent" — true,
because nobody built the tool that would replace it.

[^1]: https://control.com/technical-articles/how-to-read-ladder-logic-in-plc-and-relay-controls/

**What pyrung does that is new:**

- *Dynamic* causal-chain analysis applied to PLC ladder logic, in an
  open-source runtime, with no vendor lock-in.
- *SP-tree attribution* exploiting a structural property of the substrate
  (Boolean ladder) that general-purpose TTD doesn't have access to. This
  is what makes the proximate/enabling distinction cheap and precise.
- *Coverage and liveness analysis as side effects of running the test
  suite*, with the same primitives that explain individual events.

The honest pitch: causal debugging exists in academia and adjacent
industries; the PLC world has none of it; pyrung's architecture is uniquely
positioned to bring it there with better precision than general-purpose
tools because ladder logic decomposes nicely.

## Why This Is Uniquely a pyrung Feature

Five ingredients, all required, none present together in any other PLC
runtime:

1. **Simulation-first context** — deterministic replay, no real-time pressure
2. **Immutable scan history** — cheap diffs via pyrsistent
3. **Per-scan rung firings** — the new piece this feature adds
4. **Static PDG** — already built by DataView
5. **Ambient SP structure on every rung** — already there from codegen and
   hand-written paths both

Every other PLC tool has at most two of these. The combination is what
answers "why did this happen?" — a question every controls engineer asks
daily and no existing tool answers directly.
