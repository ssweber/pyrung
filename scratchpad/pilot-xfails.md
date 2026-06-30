# PILOT / `how()` xfail survey

Test-wide inventory of `@pytest.mark.xfail` cases related to `how()` / graph /
PILOT, plus the other-subsystem xfails for completeness. These mark behaviour the
engine *should* eventually have — the day a gap closes, the test xpasses and
flags it. Convention: `reason="pilot: <category>"` (current engine) or
`reason="walker: …"` (deprecated walk engine).

Survey date: 2026-06-28.

## `how()` / graph / PILOT (current engine)

| Test | Loc | Reason | Gap |
|---|---|---|---|
| `test_graph.py::TestHow*::test_how_with_avoid_uses_non_avoided_route` | test_graph.py:194 | `pilot: latch-through-OR alternative route` | `how(Done, avoid=Ready)` should route around the avoided tag through the OR alternative |
| `test_graph.py::TestHow*::test_how_multiple_conditions_and` | test_graph.py:247 | `pilot: single-target only` (raises `ValueError`) | `how(Ready, Done)` — multi-target AND goals not supported |
| `test_graph_semantic_path.py::TestSemanticPathIntegration` (whole class) | test_graph_semantic_path.py:448 | `pilot: Int threshold / calc chain programs` (`strict=False`) | `how()` on Int-threshold / calc-chain programs |
| `test_packml_diagnosis.py::TestHiddenEventJumpSelfLoopOnly` | test_packml_diagnosis.py:478 | `pilot: PackML state machine programs` | hidden-event jump must fire only on a self-loop; `how(IDLE)` path fails its own replay |
| `test_packml_diagnosis.py::TestHowAbortedToExecute` | test_packml_diagnosis.py:520 | `pilot: PackML state machine programs` | 7-step waypoint path ABORTED→…→EXECUTE through the PackML SFC |
| `test_pilot_examples.py::test_conveyor_motor_reachable` | test_pilot_examples.py:40 | `pilot: NC-reset latch under state-machine churn` | click_conveyor `ConveyorMotor` — should latch Running and gate the motor, but PILOT wanders the sort state machine |
| `test_pilot_examples.py::test_running_route_ambiguous_resolves` | test_pilot_examples.py:52 | `pilot: route-ambiguous single-target resolution` | click_conveyor `Running` (latch + two NC resets) — PILOT should pick a route without an explicit `choice=` |

## `how()` deprecated walker engine (`walk/`)

| Test | Loc | Reason |
|---|---|---|
| `test_walk_how_e2e.py::test_how_with_callable_predicate` | test_walk_how_e2e.py:137 | `walker: opaque callable predicates need expr decomposition` |
| `test_walk_fold_churn.py` (×3) | test_walk_fold_churn.py:166, :251, :389 | `temporal done_bit fix gives the walker a direct decomposition …` |

## Other subsystems (not how/graph — for completeness)

| Test | Loc | Reason |
|---|---|---|
| `test_prove_passes.py::…::test_elides_canonical_return_early_pulse_flag` | test_prove_passes.py:646 | sliced elision cannot prove return_early pulse patterns scan-local |
| `test_prove_passes.py` (multi-writer pulse+reset) | test_prove_passes.py:669 | sliced elision cannot prove multi-writer pulse+reset patterns scan-local |
| `test_prove_simultaneous_edge_coverage_tests.py` | test_prove_simultaneous_edge_coverage_tests.py:216 | auto-joint detection does not yet infer simultaneous edge pairs spread across multiple rungs |
| `test_fold.py::…::test_inert_scan_toggle_does_not_disable_fold` | test_fold.py:608 (`strict=True`) | inert scan-toggle fold not yet implemented |
| `test_fold.py::…::test_scan_counter_crossing_folds_to_threshold` | test_fold.py:640 (`strict=True`) | scan_counter virtual-crossing fold not yet implemented |
| `test_reachability.py` (fuzz) | tests/fuzz/test_reachability.py:162 | BFS input composition does not enumerate cross-product of simultaneous `rise()` transitions |

## Roadmap themes (how/graph)

The seven how/graph PILOT gaps cluster into four capability themes:

1. **Multi-target** `how(A, B)` — `pilot: single-target only`.
2. **Avoid-aware routing** — `pilot: latch-through-OR alternative route`.
3. **Route-ambiguity auto-resolution** — `Running` (new); pick a route without `choice=`.
4. **Real state machines** — PackML, semantic Int/calc chains, conveyor NC-reset churn (`ConveyorMotor`, new).

## Re-running / regenerating

```bash
# list the live capability-gap reasons
grep -rn 'reason="pilot:' tests/        # current engine
grep -rn 'reason="walker:' tests/       # deprecated walk engine

# force the xfails to actually run (see which now xpass)
uv run pytest tests/core/analysis/test_pilot_examples.py --runxfail -q
```

---

# Design record — semantic constraints for `how()` (`TestSemanticPathIntegration`)

Survey date: 2026-06-30. As of today only **two** `pilot:` xfails remain live
(the recent jump-table / input-OR / retentive-preserve commits closed the avoid,
route-ambiguity, conveyor, and PackML gaps):

| Test | Reason | Live status |
|---|---|---|
| `test_graph.py::TestPLCHow::test_how_multiple_conditions_and` | `pilot: single-target only` | `_parse_target` raises `ValueError` on >1 condition (multi-target AND) |
| `test_graph_semantic_path.py::TestSemanticPathIntegration` (class, `strict=False`) | `pilot: Int threshold / calc chain programs` | this design |

## What this xfail actually is

It is a **regression from the walk→pilot switch**, not a new capability. Commit
`095244d` ("switch how() from walk to pilot engine") added *only* a one-line
class-level `@pytest.mark.xfail` decorator — the 12 tests passed verbatim under
walk. So the work is to **restore** what walk did.

Running the class with `--runxfail` (isolated) splits the 12 into three groups:

- **2 already green** — `test_bool_program_no_constraints`,
  `test_path_replays_correctly_with_constraints` (no annotation needed).
- **2 fail on rendering only** (`reachable=True`, missing the constraint text) —
  `test_literal_threshold_annotation` (wants `(> 75)`), `test_tag_vs_tag_constraint`
  (wants `A > B`). Direct threshold / tag-vs-tag on an **external** input.
- **8 fail on `assert path.reachable`** — every **copy/calc-chain** case
  (`copy(Temp,TempCopy); TempCopy>50`, `calc(Sensor+10,Adjusted); Adjusted>60`,
  the calc-subtraction family). These regressed on *reachability*, not just
  rendering.

So the class is really **two gaps**:

- **Gap R (rendering)** — pilot's `_build_path` emits `ReachabilityStep`s with
  `constraints=None`, so `graph.Path.__str__` falls back to the raw
  representative (`Temp=76`) instead of `Temp=76 (> 75)`.
- **Gap X (reachability through copy/calc)** — pilot's inequality guard
  resolution can't invert a relation on an *internal* register
  (`TempCopy > 50`) back to its steerable source (`Temp`).

## How walk did it (the mechanism)

Three parts; the render machinery is already engine-agnostic and **already live
for pilot's Path**.

1. **Build metadata** — `runner._how_via_walk` builds a prover `explore_context`
   (`_build_explore_context(..., domains_only=True)`), then
   `prove._build_semantic_metadata(ctx, program) → (atom_index, domain_sources)`.
   That builder enriches atoms backward through copy
   (`graph._enrich_atom_index` via `build_reverse_edge_map`) and through
   relational calcs (`graph._enrich_from_relational_calcs`).
2. **Annotate via replay** — `walk.engine.plan_walk` (engine.py:621‑655), *after*
   the plan is verified, forks a fresh `annotate_fork`, replays each
   `(action, scans)` step, and after each calls
   `graph._classify_step_inputs(action, atom_index, domain_sources,
   dict(fork.state.tags))`, attaching the result as `ReachabilityStep.constraints`.
3. **Render** — `graph.Path.__str__ → _render_step_inputs / _render_step_diff`
   consume `step.constraints`. Engine-agnostic; pilot's Path already uses it.

### Evidence that the priors are already in pilot
For the copy chain, `_build_pilot_context` builds the **same** `ctx` walk used:
- `ctx.nondeterministic_dims = {'Temp': (0, 49, 50, 51)}` — the prover already
  back-propagated `TempCopy > 50` through the copy into Temp's domain.
- `_build_semantic_metadata(ctx) → atom_index = {'Temp': [Atom(gt 50)], ...}`,
  `domain_sources = {'Temp': 'expression_partition'}` (non-Tier-3) — rendering
  metadata is correct *once reachable*.
- BUT `evidence.affine_projections() == {}` — plain `copy`/affine-`calc` writers
  are **not** registered as func-deps, so the inequality chase
  (`_chase_inequality_source` in `trace.py`) can't map internal `TempCopy` →
  steerable `Temp`. **That empty channel is Gap X's root cause.**

## Design — embed directly in `pilot/` (walk is going away)

No engine-neutral helper in `graph.py`, no walk refactor. Pilot owns the
annotation, so it can freely import `_install_reactive_holds` from `_ops.py`.

### Stage 1 — rendering (Gap R): restores the 2 direct tests
1. `_build_pilot_context` also returns `(atom_index, domain_sources)` derived
   from the `ctx` it **already compiles** — zero extra kernel compile. Returns a
   4-tuple; the 3 call sites unpack it (`pilot_events`/`pilot_drive` ignore the
   metadata).
2. New pilot-internal `_annotate_pilot_steps(plc, steps, atom_index,
   domain_sources)`: fork fresh, `install_harness`, replay each **public
   `ReachabilityStep`** (`.action`, `.scans`), `_classify_step_inputs(...)` on
   the post-step tag state, return `replace(step, constraints=…)`.
   - Operates on the **public step shape**, never `_Step` (so the parallel
     `_Step.action → inputs` rename never touches it).
   - Re-installs `getattr(step, "reactive_holds", None)` via
     `_install_reactive_holds` (no-op today — command paths carry none, and
     `ReachabilityStep` has no such field yet — but correct the moment let-run
     steps flow through; same primitive shape as
     `scratchpad/burner/verify_path_recording.py`).
3. `_build_path` annotates **post-construction** (build `ReachabilityStep`s as
   today, then annotate), wrapped in try/except so annotation can never break a
   `how()` result. `tag_defaults` intentionally NOT set (not needed by the tests;
   avoids changing existing pilot path renderings).

### `_build_path` signature contract (pinned for the parallel rebase)
```python
def _build_path(reached, recorded_steps, target_tag, target_value, *,
                plc=None, atom_index=None, domain_sources=None,  # Stage 1 (semantic render)
                journey=None):                                    # reserved: parallel journey work
```
All new params keyword-only and defaulted; **absent → today's behavior** (the
no-metadata case). `journey=` is reserved/unused by Stage 1 so the journey work
slots in additively with no signature fight.

### Stage 2 — copy/calc reachability (Gap X): restores the 8
Root cause pinned above: `evidence.affine_projections()` is empty for copy /
affine-calc. Fix by **seeding func-deps from the same reverse-edge map**
`_enrich_atom_index` already uses (copy = identity; affine calc = scale/offset),
so `_chase_inequality_source` inverts the internal guard tag to its steerable
source — one inversion channel instead of two. First action: decide whether the
fix belongs in `evidence.affine_projections()` or as a reverse-edge-derived
supplement to the trace's `InequalityPrior`.

### Caveats
- `strict=False` ⇒ the marker only flips to xpass (removable) when **all 12**
  pass ⇒ needs **Stage 1 + Stage 2**. After Stage 1, the 2 direct tests xpass,
  the 8 copy/calc stay xfail.
- Keep the 2 already-green tests green.
- Context-compile cost is already paid by pilot today (`_build_pilot_context`),
  so Stage 1 adds no latency beyond one extra replay fork per `how()`.

### Synergy with the parallel "self-describing path" work
Replay-based annotation and the `journey`/clean-truncation work rest on the same
"replayable path" foundation: `reactive_holds` (landed) + clean step truncation
(next). The annotator consumes the clean `Path.steps` (non-overlapping,
replayable) — never the attempt log (the burner path's 3 steps all fork from
~scan 815, so sequential replay mis-fires). So the annotator gets a sounder
input for free as that work lands.
