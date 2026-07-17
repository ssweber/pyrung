# PILOT documentation inventory

This is a working audit, not an architecture specification. It records what the
current code appears to do so that documentation does not accidentally settle
an open module boundary.

## Documentation contract

### `pilot/CLAUDE.md`

Audience: a maintainer or coding agent entering the subsystem.

Owns:

- the mental model that prevents treating PILOT as a stored-plan executor;
- package-wide invariants and failure policy;
- the actual control flow and state/knowledge split;
- a compact navigation map;
- the tests required for risky classes of change.

Does not own:

- detailed algorithms;
- migration history or replaced designs;
- dated status reports and roadmaps;
- exhaustive function or test catalogs;
- local contracts already stated beside the code.

### Module docstrings

Audience: someone reading, importing, or changing that module.

Own:

- the module's current responsibility;
- the data or capability it consumes and returns;
- decisions and mutations performed at the module boundary;
- important `None` / UNKNOWN / fail-open / fail-closed behavior;
- one explicit non-responsibility when the adjacent boundary is easy to confuse.

Do not own:

- the subsystem's whole theory;
- origin stories or refactor history;
- examples tied to one reproducer unless the module is intentionally
  application-specific;
- duplicate module maps;
- promises that are not enforced by the current code.

### Function and class docstrings

Own seam semantics: preconditions, result meaning, mutation, evidence strength,
and exceptional or unknown outcomes. Obvious private helpers do not need prose
that merely restates their names.

### Tests and design notes

Tests are the executable home of invariants. A durable design note may own
rationale and rejected alternatives. Git history owns "formerly", "replaces",
and migration chronology.

## Observed control flow

The named responsibilities are nested, not a five-stage linear pipeline:

1. `pilot.py` prepares a fresh iteration frame and asks `candidates.py` for
   currently usable actions, prerequisite holds, or a wait mode.
2. `pilot.py` tries the returned modes in priority order.
3. Each `_try_*` function in `steer.py` executes on a fork and calls
   `verify.verify_gates` before returning an attempt.
4. `pilot.py::_record_attempt` applies observations to the persistent
   `Compass` whether or not the trial was accepted.
5. For an accepted trial, `pilot.py` commits the fork and delegates
   checkpoint/provisional/regression handling to `progress.py`.
6. Skiff probing is triggered at the two terminal-looking stuck sites. Its
   observations may buy another iteration only when `Compass.apply` reports
   new knowledge.

Therefore ORIENT / ACT / VERIFY / RECORD / ASSESS are responsibility labels at
best. They are not an execution sequence: VERIFY happens inside an Act and
RECORD happens after that verdict.

## Behavioral module inventory

### Package surface and orchestration

| Module | Observed responsibility | Boundary questions |
|---|---|---|
| `__init__.py` | Re-exports the drive API plus a broad set of analysis and investigation helpers. | Which exports are public API versus test/internal convenience? |
| `pilot.py` | Builds static context, chooses the user route lock, owns the event-producing outer loop, applies observations, commits accepted forks, assembles terminal reasons and public `how`/drive results. | At 2,800+ lines it also owns considerable formatting and setup. Which of those are orchestration versus presentation utilities? |
| `types.py` | Cross-module protocols, event/result records, and the split between revertible `_World` state and non-revertible search knowledge. | Some types encode policy, not just structure; "shared types" understates that role. |
| `physical.py` | Installs the physical harness on a PLC/fork and returns the feedback tags PILOT must not steer. | None apparent. |
| `multitarget.py` | Statically proves a narrow class of target incompatibility and chooses clobberer-first target order; concrete driving remains the final test. | Whether this pre-pass belongs at the public orchestration boundary or in static analysis. |

### Reading and choosing

| Module | Observed responsibility | Boundary questions |
|---|---|---|
| `trace.py` | Builds backward prerequisite trees; handles relational targets, preservation, route enumeration, steerability, clear-only detection, table enablement integration, writer eligibility and ranking. | This is both recursion engine and owner of several policy decisions. Which decisions are inherent to tracing and which are separable read capabilities? |
| `availability.py` | Evaluates writer guards against live state and fire-time pins, producing an ordering verdict without rejecting a writer. | Whether guard reduction is a generally reusable static primitive or specifically writer-ranking policy. |
| `evidence.py` | Infers pipeline roles and expands PDG writers into structured static transition routes, including aliases, enablers, and source constraints. | Its "evidence" name is broader than its actual static pipeline evidence role. |
| `tide_tables.py` | Solves finite preimages of constant-backed table expressions and classifies guarded predicates when domains are complete. | Sound rejection depends partly on call-site pre-screening in `trace.py`; the contract currently spans modules. |
| `charts.py` | Builds and searches static per-register transition graphs from expanded routes and detects opaque pipeline slices. | It combines graph construction/path search with pipeline detection. `best_compass_plan` also overlaps query surfaces in `Compass` and `routes.py`. |
| `compass.py` | Stores static graph references and a persistent table of seeded/observed transition knowledge; folds observations and queries learned paths/probe history. | "Compass" currently names both the knowledge value and, in surrounding prose, the entire reading/selection system. `record` mutates while `apply` is persistent. |
| `routes.py` | Applies live edge constraints before delegating static graph path selection to `charts.best_compass_plan`. | This is a 39-line policy wrapper; decide whether the seam earns a module or belongs with candidates/static graph query. |
| `currents.py` | Reads current program structure to find a unique legal operator action and also classifies sibling producer families so automatic and operator-produced edges remain distinct. | The module now owns more than "the button a current awaits"; producer-family evidence may be a separate capability. |
| `candidates.py` | Integrates trace actions, static/learned routes, corrections, current-state capabilities, prerequisite rungs and wait modes into the ranked choices for one iteration; also diagnoses no-bearing cases. | It is not merely a ranker. Decide whether construction, mode selection, and stuck diagnosis are one decision or three. |

### Executing and observing

| Module | Observed responsibility | Boundary questions |
|---|---|---|
| `steer.py` | Settles prerequisite regions, executes action/batch/wait trials on forks, gathers transition observations, and invokes `verify_gates`; it does not commit world or compass state. | Calling this one "ACT phase" hides that verification is nested inside every attempt. |
| `_ops.py` | PLC manipulation primitives, state/world keys, hold installation, pulse application, coast adapters, pending-effect settlement, avoid checks, and hold/route admission helpers. | The final policy helpers mean it is not purely operational. Separate mechanics from admissibility policy, or document the mixed role honestly. |
| `coast.py` | Runs bump-driven fold/step sessions that land on exact observed scans and return receipts; it records events but does not decide their PILOT meaning. | The module docstring should state behavior guarantees without preserving cutover history. |
| `cyclefold.py` | Detects a stable active-hold cycle and macro-skips monotone progress while replaying a real period; declines to fold when proof is insufficient. | None apparent. |
| `accumulators.py` | Resolves the accumulating instruction driven by a held input and computes or measures scans to an ejection threshold. | It is used by both execution and correction analysis; avoid presenting it as owned by one caller. |
| `skiff.py` | Provides isolated pinned-scan primitives and the higher-level policy that selects finite frontier probes, runs control/probe scans, records observations, and reports undeclared domains. | Execution primitive and probe-selection orchestration are mixed; `run_pinned_scan` is also reused by investigation. |

### Judging, progressing, and recovering

| Module | Observed responsibility | Boundary questions |
|---|---|---|
| `verify.py` | Applies avoid, target, spin, cycle, dead-end, and outcome gates to one executed fork; may investigate an excursion before returning a trial verdict. | "Acceptance" is overloaded because an accepted trial can still be reverted by progress assessment. |
| `outcome.py` | Attributes observed motion and classifies bearing, progress, and frontier effects into a `TrialAssessment`; creates confirmed compass entries. | It classifies evidence but does not alone accept/commit a trial. |
| `progress.py` | After a verified trial is committed, manages trends, checkpoints, provisional departures, regression investigation, world reverts, and installation of replay-proved corrections. | This is more than trend monitoring; it owns recovery policy and corrective installation. |
| `detour.py` | Settles an observed channel departure and classifies it as provisional, regression, or unknown using route and gauge evidence. | It executes settlement as part of "classification"; the name is historical and the boundary is progress-specific. |
| `gauge.py` | Builds a conservative target-relative measure of event-earned work and reset boundaries, then compares snapshots/marks for verification and departure handling. | Decide whether route-reset evidence and progress comparison are one abstraction. |
| `causal.py` | Adapts the deep recorded cause chain into root/tag queries and empirical program-write evidence shared by verification, skiff, outcome, and investigation. | None apparent, though "cause-chain queries" is plainer than "walker". |
| `investigate.py` | Builds departure incidents and replay functions, generates/ranks hypotheses, runs counterfactual validation, and separately diagnoses short excursions. | The module contains both reusable replay mechanics and high-level investigation policy. |
| `corrections.py` | Generates scoped corrective-hold candidates from writer guards and accumulator profiles; investigation decides which candidate survives replay and progress installs it. | "Classifier" is inaccurate: this module proposes/derives corrections but does not confirm or install them. |

## Open architecture questions to settle explicitly

1. Is `Compass` only accumulated transition knowledge, or the combined static
   and learned navigation object? Current code supports the latter; much prose
   uses the former.
2. Should static graph construction, constrained live route querying, and
   learned path querying intentionally have three APIs (`charts`, `routes`,
   `Compass`), or is that accidental duplication?
3. Are `candidates.py`'s construction, prioritization, mode selection, and stuck
   diagnosis one decision owner?
4. Is `_ops.py` allowed to own route/hold admission policy, or should it be
   mechanically pure?
5. Should skiff probe policy remain beside pinned-scan execution primitives?
6. Does "verified/accepted" mean a trial passed local gates, or that its world
   survives progress assessment? The prose needs distinct words for those.
7. Is `progress.py` the intended sole owner of recovery and correction
   installation, not merely ASSESS/trend tracking?
8. Which `pilot.__init__` names are supported public API?

These questions should remain visible in the documentation work. A docstring
must not manufacture an answer where the code and maintainers have not chosen
one.

## Terminology test

A nautical term earns a place only when it names one code abstraction, has one
definition, prevents a likely wrong model, and saves more translation than it
costs.

- `pilot`: keep; it distinguishes continuous steering from stored planning.
- `bearing`: useful if restricted to the next direction recomputed from the
  current world.
- `compass`: reserve for the `Compass` value unless a broader meaning is
  explicitly chosen.
- `coast`: useful execution term for holding inputs while scans pass.
- `skiff`: pair with "isolated fork probe" on first use.
- `tide table`: pair with "finite constant-backed table solver" on first use.
- `current`: pair with "program-owned motion / currently awaited operator
  action" on first use.
- decorative ship/captain/reef/shipyard language: remove from technical
  contracts.
