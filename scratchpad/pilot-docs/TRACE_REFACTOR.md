# trace/ refactor plan

Sequenced refactors for `trace.py` (currently ~4,200 lines) and the route
machinery it feeds. Order matters: R1's outcome decides whether R4 extracts
`routes.py` or deletes most of it. Every step ends with `make test-pilot`,
`make lint`, and unchanged goldens unless the step says otherwise. Use
`devtools/pilot_divergence.py` at the first golden movement, not after the
suite.

Do not combine steps in one change. Each step is one commit series with its
own exit criterion.

## R1 — Replace inferred routes with work in progress (do first)

The current inferred-route lifecycle is becoming fallback breadth-first
search:

1. retain one `inferred_route_commitment`;
2. probe or try it until `RouteExhausted` / `RouteUnproductive`;
3. remember its identity in `skipped_route_ids`;
4. clear the commitment and select the next route in the same world.

That is the stored-search-position concept this refactor removes. PILOT's
control rule should instead be:

> Read the world → recognize work already underway → take the smallest
> continuation of live work → observe whether it won → bank, maintain, or
> close that work → read again.

No inferred route, skipped-route set, next-alternative pointer, or retained
action suffix survives an observation. World-scoped nogoods remain empirical
facts about exact acts, not a visited set for route nodes.

### Validated minimal model

Three existing readings have separate jobs:

- **Gauge — “What progress have we already earned and must not casually
  destroy?”** The gauge and checkpoints are the banked-work ledger. A proposed
  intervention whose simulated landing is `behind` the current gauge receipt
  is rejected before commit. A coast is excluded from this intervention gate:
  backward program motion during a coast belongs to the existing
  investigation/recovery path.
- **Open operation — “What unfinished work currently owns the next move?”**
  Do not add an operation-route record. The committed operation already owns
  its motion kind and exact before/after snapshots. After an accepted coast,
  if a fact changed by that coast still appears in the current target trace and
  still has its landing value, that coast remains live work. Existing installed
  rungs and pending-departure receipts provide the other current-world forms of
  ownership.
- **Current read — “What is the smallest continuation of that work now?”**
  Re-run the existing readers: `program_step` / advance boundaries for an exact
  producer that can keep running, currents for a unique operator handoff,
  charts/Compass for one immediate edge, and the target trace for a local
  action or coast. A `Bearing` remains exactly one act.

This makes the lifecycle concrete without adding states or weights:

- **bank** — the gauge/checkpoint ledger advances;
- **maintain** — the latest accepted coast still owns a fact in the live trace,
  or another existing world receipt remains active;
- **close** — no such current-world evidence remains;
- **read again** — choose one immediate act from the newly observed world.

`BearingObjective` remains the broad target-relative reason an act may be
useful; it is intentionally not reused as open-operation identity. Its whole
frontier is too broad and makes an early command such as Clear falsely own the
future heating operation. Likewise, a chart `StaticPath` may use BFS internally
to answer a current read, but neither that path nor a route identity survives
the observation.

The Burner adversary is the useful acceptance test: after heat work is banked,
`Cmd_Reset2FactoryDefault` is both outside the open coast and destructive to
the gauge. With the rules above, the drive continues
`HeatDelay → Heat_tmr_Acc → Heat_CurStep → y_BurnerLoop`, reaching the target
at scan 2110 without selecting factory reset. Validation after the simplification:
569 PILOT tests passed, both former Burner timeouts passed, and all 30 Tumbler
tests passed.

Milestone debt: the avoided-Complete skeleton intentionally preserves six
accepted Unhold attempts, two more than the route-smoothed baseline. The
fine-grained trace is useful evidence for the next pass: determine which earned
prerequisites survive a moving process boundary without restoring route
commitment or hiding the retries in a score.

### Working hypothesis

The useful part of route commitment is a technician's current-world intuition
that work is underway: an operation is in flight, target-relative work is
banked, or the last accepted operation established a fact that the open
frontier builds on. A discrete work-in-progress tier in option ranking should
provide that continuity without storing which root writer/OR path was chosen.

This is deliberately one tier, not a score or a new work subsystem:

- **continuation** — the option serves a current residual and has any exact
  current-world evidence below;
- **fresh** — the option is otherwise admissible but starts unrelated work.

Any continuation ranks before any fresh option after hard user/operation
constraints and staging, and before availability/wake/Compass heuristic
ordering. Existing ordering breaks ties within the tier.

An option has work-in-progress evidence only when its current trace provenance
shows it serves the still-open work and at least one of these holds:

- **in flight:** an installed prerequisite/operation, pending departure, or
  running coast still owns an unresolved boundary that this option continues;
- **banked:** Gauge proves target-relative work ahead of the applicable
  rollback/progress mark and this option continues the open frontier beyond
  that work;
- **builds on a win:** the last accepted coast changed a fact that remains in
  this option's current target trace, its landing value still holds now, and
  something downstream remains unresolved.

Tenure, acceptance by itself, and reverted `journey` history are not evidence.
The tier is recomputed from the current world; it is not a flag to decay.
Exact rejection filters the rejected act through its world-scoped nogood.
Regression/revert removes or invalidates the supporting fact/receipt. A closed
or no-longer-related frontier simply yields no continuation evidence.

Keep the first experiment narrow. It tests whether this evidence can replace
the *inferred commitment and fallback iteration*. It does not, by itself,
license deletion of route enumeration/conflict code used to expose and compare
current-world alternatives; that receives a separate equivalence gate below.

### Step 1: dark comparison (no behavior change)

Use the existing route reader as temporary scaffolding to expose all
current-world root alternatives. Do not honor `active_root_route` or
`skipped_root_routes` in the shadow decision:

1. trace every currently admissible alternative against the same immutable
   snapshot and exact rejected-act knowledge;
2. run each trace through normal option materialization;
3. union the resulting immediate acts, attaching their trace provenance;
4. rank hard constraints/staging first, then continuation vs fresh, then the
   existing option keys;
5. select exactly one shadow orientation result.

Compare the final executable identity (`Pulse` including co-actions,
`BatchPulse`, `Coast`, or `Dwell`), not only the primary `(tag, value)`.
Explicit `via=` is held constant in both readers and is not part of the
inferred-route comparison.

Record one structured row for every multi-alternative iteration:

- world key and target;
- baseline active/selected route identity and final act identity;
- shadow final act identity;
- every shadow candidate's continuation evidence and ordinary rank tuple;
- agree/disagree, plus the eventual accepted/rejected/progress disposition of
  the baseline act.

Instrumentation must not mutate Compass, `_PilotState`, route selection, event
goldens, or probe budgets.

Exit: full-corpus report in hand; normal results and goldens byte-identical.

Prototype: `devtools/pilot_wip_dark_run.py` installs a process-local,
read-only Orientation wrapper and emits this report as JSONL. It uses the
current route reader only as dark scaffolding, returns the baseline result
unchanged, and marks `shadow_scaffold_only` when the proposed act cannot yet be
reconstructed without the baseline route tree. Such a row is a deletion
blocker, not an agreement. The initial `Sts_State_Completed` Tumbler run
reached normally with 12 orientations, zero decision disagreements, zero
scaffold-only selections, and zero shadow errors. The actionless inferred
route fixture disagrees as intended: the shadow read surfaces the productive
`ManualUp` pulse while baseline probes and later emits `RouteUnproductive`.

### Step 2: adjudicate disagreements

A log-only disagreement does not say which action would have won. Replay every
distinct disagreement from the same starting program/snapshot on independent
forks, one with current behavior and one with the shadow selector enabled
through completion.

Classify a route advantage only from an outcome difference: the baseline
reaches/banks work while the shadow run rejects, regresses, becomes stuck, or
loses termination. If so, name the missing current-world evidence and add the
smallest discrete evidence case, then repeat Step 1. Do not add route identity
or a scalar loyalty weight under another name.

Exit:

- every baseline corpus success still succeeds;
- no unchanged-world case loses bounded termination;
- no avoid/safety result regresses;
- explicit `via=` results are unchanged;
- every remaining choice difference is explained by recorded evidence and has
  an accepted outcome, not merely a plausible rank story.

### Step 3: flip and retire fallback iteration

- Consult the work-in-progress tier for the real current-world selection.
  One golden at a time; use the divergence tool at the first movement.
- Remove inferred-route steering and fallback iteration:
  `inferred_route_commitment`, `skipped_route_ids`,
  `NavigationConstraints.active_root_route/skipped_root_routes`,
  `RouteUnproductive`, and the inferred case of `RouteExhausted`.
- Exact act nogoods filter the complete current-world candidate pool. If no act
  remains but some frontier is unreadable, the ordinary bounded probe policy
  runs. After that, no admissible act anywhere means `Stuck` with the complete
  outstanding frontier. There is no "try the next route" disposition.
- An unchanged world may be read again only after knowledge changed or a
  finite probe/wait budget advanced. Otherwise terminate; never take an
  actionless lap to rotate alternatives.
- Update the working principles and ownership table: retire the inferred
  root-route lifecycle row and add current-world continuation evidence under
  `options.py::_build_candidates`.

Exit: no production reference to the retired state/results remains; inferred
navigation returns only `Bearing | NeedProbe | Stuck`.

### Explicit `via=` after inferred routes

`via=` remains durable positive user intent, but it is a constraint on the
current-world reading, not a retained engine path and not a literal
`candidate-pair satisfies predicate` test.

- Keep alternatives whose traced requirements are consistent with `via=`.
- A neutral prerequisite remains admissible when it serves a
  `via=`-consistent alternative even if that action alone does not force the
  predicate.
- Reapply the constraint on every read, like `avoid=`.
- If no admissible act or bounded unreadable frontier remains within the
  requested constraint, return terminal `Stuck` naming `via=`. Do not fall
  through to an unconstrained alternative.

The public `Plan.route` / pivot description is reporting, not navigation.
Preserve it by deriving the taken description from accepted provenance or
other recording-only evidence; it must not feed a route identity back into
orientation.

### Step 4: prove and delete structural route machinery

Step 3 proves that stored inferred routes are unnecessary. Before deleting
enumeration, prove that the replacement current-world alternative reader
preserves what the remaining route code still supplies: coherent writer/OR
alternatives, contradiction/viability handling, `via=` eligibility, and public
labels.

Dark-compare, across the full corpus, the complete admissible immediate-act
universe and constraint dispositions produced with and without
`TraceChoice`/route locks. Differences require adjudication before deletion;
coverage merely showing that commitment fields are dead is not enough.

Once equivalent:

- remove route enumeration, `_RouteDraft`, `_RouteConflict`, conflict pins,
  route locks/identity/exhaustion bookkeeping, and inferred-route handling in
  `pilot.py`/`orientation.py`;
- retain only local alternative selection, `via=` constraint evaluation, and
  recording labels/hints;
- delete the dark scaffolding in a separate commit after saving its report.

Required test programs (success + honest failure, per standing rule):

- nested-Or program where work-in-progress evidence completes one compatible
  arm across multiple actions without storing a path;
- shared-prefix OR where an action serves multiple alternatives and does not
  prematurely commit to one;
- clobber program where earned work is un-satisfied and the next read removes
  its continuation tier;
- all-alternatives-dead program terminating `Stuck` with the frontier named,
  no repeated lap on an unchanged world;
- explicit `via=` program requiring a neutral prerequisite before the
  discriminating condition, plus exhausted-`via=` terminal behavior.

Expected net: ~500–700 lines out, ~100–150 in for current-world alternative
selection and the discrete continuation tier.

If Step 2 falsifies the work-in-progress hypothesis, retain routes and proceed
with R4a plus the route-survived R4b extraction. Keep the dark report as a
diagnostic.

## R2 — UnsupportedConstruct with caret rendering

An unsupported construct is a tool gap, not an unreadable frontier. Today
both surface as the same unresolved/None, so a missing tracer rule
masquerades as an opaque program and sends PILOT probing instead of the
user filing an issue.

- New exception raised only where the tracer runs out of rules (not where
  the program is genuinely opaque — that path still returns an unresolved
  requirement per the incomplete-static-read principle). Carries rung
  index, node, expression, sub-expression span, kind, and detail.
- Raise deep (inside `_trace_expression`, `_rewrite_internal_compare`,
  `_resolve_inequality_target`, and peers); catch at exactly one boundary
  (`trace_back` or `orient`), which converts it to a terminal result with
  the construct named. No broad catches anywhere else.
- Caret rendering (`^^^^` under the offending sub-expression, plus a
  file-an-issue prompt naming the construct kind) lives in `recording.py`.
  Trace raises structured data; recording formats.
- Test mode propagates and fails loudly; drive mode degrades to the clean
  terminal. One flag.

Exit: a deliberately unsupported fixture produces the caret report in drive
mode and a loud failure in test mode; every genuinely-opaque fixture still
returns an unresolved requirement, not the exception.

## R3 — Dispatch table for the recursion core

`_trace_back` (~480 lines) and `_trace_expression` (~240) are if/elif
chains over construct kinds with arm bodies inlined.

- One handler per construct kind, registered in a dict keyed by kind;
  each handler takes `(env, node, target)` and returns children.
- The unsupported case becomes a missing key raising R2's exception —
  no fall-through else remains.
- Adding vendor idiom support becomes adding a handler, not editing the
  core.

Exit: goldens byte-identical; no fall-through branch exists; the dict is
the single enumeration of supported constructs.

## R4 — Module splits (flat, no packages yet)

### R4a — always

- `static_facts.py`: `compute_reference_constants`,
  `compute_resting_values`, `compute_edge_tags` (~300 lines). Program-wide
  precomputation, not backward tracing. Pure relocation; consider merging
  into `charts.py` instead if it reads better there.
- `inequalities.py`: `_resolve_inequality_target`,
  `_heuristic_inequality_target`, `_strict_inequality_step`,
  `_declared_float_bounds`, `_inequality_levers`, `_domain_granularity`,
  `_rewrite_internal_compare`, `_decompose_sum` (~450 lines). Adjacent to
  `tide_tables.py`; move there instead if the shared surface is large.
- Kill the top-of-file re-export hack: `options.py`, `tide_tables.py`, and
  tests import availability names from `availability` directly. Do this
  before the splits; it is what makes them safe.

### R4b — shape depends on R1

- Routes survived R1: extract `routes.py` (~650 lines: drafts, conflicts,
  enumeration, ranking, labels, via hints). Coherent boundary — consumes
  trees, produces `TraceChoice`.
- Routes deleted in R1: only the `via=` filter and labeling helpers
  remain; they stay in `trace.py` or land in `options.py`.

Exit: `trace.py` at roughly 1,800–2,200 lines of backward tracing; each
new module gets a navigation entry and docstring in the same change.

## R5 — TraceNode split by kind

`TraceNode` (~320 lines) carries fields that only apply to some node
kinds. Split into kind-specific records (action node, expression node,
frontier leaf, at minimum — derive the real set from field-usage
analysis). Runtime "does this field apply here" guards become types.
Expect real LOC reduction from removed defaulting and guarding.

Exit: no field on any node type is meaningless for that type; goldens
unchanged.

## R6 — Role-typed keys and coordinates

The regression-nogood mis-scope (rollback key recorded as action-source
key) is a class, not an instance: same bare type, two roles, both
typecheck.

- `NewType` wrappers: `ActionSourceKey`, `RollbackKey` over the world-key
  tuple; `SearchScan` over the committed scan coordinate. Role assigned at
  construction, once; no casts elsewhere.
- Stronger where load-bearing: observation constructors take the owning
  object (`ActionNogoodObservation.for_frame(frame, pair)`), not a loose
  key. Consumers take the owner's artifact; they do not reassemble scope.
- Sweep: every function where two same-typed keys or scan coordinates are
  simultaneously live is a latent instance. Let the type checker enumerate
  after introducing the wrappers; fix in one mechanical pass.
- Mocks in tests carry the role types, or the tests cannot catch the next
  instance. Add the trace-level property: every recorded nogood's world
  key appeared as some frame's action-source key in the same run.
- Naming rule module-wide: abbreviate only vocabulary-section terms.
  `cp_key` and kin are renamed by role (`rollback_destination_key`,
  `action_source_key`) or eliminated in favor of attribute access at the
  use site.

Exit: type check passes with the wrappers enforced; the trace-level
property holds across the corpus.

## Sequencing

R1 Step 1 (dark run) first — zero behavior risk, decides whether inferred
route fallback iteration can be retired.
Then R4a (relocations and re-export removal) to shrink the field.
Then R2, followed immediately by R3 — the dispatch table is where the
exception's raise sites become obvious, but each keeps its own exit criterion
and commit series.
R1 Steps 2–4 whenever the disagreement report is ready; do not let it
block R2/R3.
R5 and R6 last; both are mechanical once the file is smaller.

New-program corpus expansion continues throughout. A new program that
fails routes to whichever step owns the failing decision; let the corpus
pick the order within a step.
