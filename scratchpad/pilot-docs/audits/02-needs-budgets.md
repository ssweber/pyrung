# Audit 2 — Declared needs (pattern 3) + declared budgets (pattern 4)

Opus agent audit, 2026-08-10. Corpus: 472 getattr/hasattr sites across 39 files (AST-extracted). 136 on context-like receivers (ctx 94, state 36, world.* 6); 336 on domain records/PLC internals/Tag polymorphism.

## Structural finding — declared needs

There is NO missing dependency anywhere. `_PilotContext` (`types.py:603-656`) declares every attribute probed on ctx; `_PilotState` (`types.py:823-909` + proxies `:912-982`) declares every attribute probed on state; `PLC.__init__` (`core/runner.py:1302`) always sets `_harness`. Defaults exist because three ad-hoc context shapes coexist and 83 functions annotate `ctx: Any`:

| Shape | Fields | Built at | Naming |
|---|---|---|---|
| `_PilotContext` (`types.py:603`) | 24 | `pilot.py::_prepare_drive` | `domain_prior` |
| `WorldView`/`WalkContext` (`types.py:163`/`:120`) | 11 | 7 sites: `options.py:840,1110`, `pilot.py:1844,2110,4681`, `departure.py:580`, `pipeline_graph.py:287` | `prior` |
| SimpleNamespace mini-ctx (`investigation_replay.py:1172-1181`) | 8 | 1 site | `domain_prior`, no `clear_only` |

`prior` vs `domain_prior` reconciled by exactly one line: `program_step.py:183` `getattr(ctx,"prior",getattr(ctx,"domain_prior",None))` — that fallback is DEAD (all 4 `read_program_step` callers pass a full WorldView).

## Probe-site categories

A. Pure noise on _PilotContext/_PilotState (smell — receiver's type declares the field). Notables:
- `steer.py:244,274,461,484,521,662,680,821,835,957,971` state.active_requirements; `steer.py:205,358,638,800,938` ctx.collect_action_attribution
- `verify.py:212,439,786,842,1019` + `:286,806,887` (state: Any → tighten)
- `orientation.py:227,237,243,267,440`; `OrientationWorld.state/.context` are `: Any` (`navigation_contracts.py:90-91`) — declare under TYPE_CHECKING (cycle-free)
- `departure.py:330,340,388,402,523,586` + `:440,457` — ctx/state ALREADY correctly annotated; just delete getattr
- `investigation_replay.py:731,732`; `pilot.py:4257,4259,4272,4299,3381`; `recording.py:310`; `coast.py:813`; `outcome.py:110`; `avoid.py:40,43,59,61`; `trace.py:311`; `skiff.py:262`; `correction_candidates.py:604,618,634-638`; `investigate.py:712,713` (+ silent `return []` degrade branch — delete); `pilot.py:6104,6105` prover ctx

B. `_harness` on PLC — always set at `runner.py:1302`: 20 sites (advance 111, cyclefold 481, coast 987/1013, options 932/1121, overlay 145, pilot ×8, program_step 287/370, progress 233, intrascan 1257, requirements). Fix: plain attribute access.

C. TraceNode fields all declared at `trace.py:483+`: 27 sites probing pipeline_internal, live_guard, advance, owner_boundary/owner_condition/linear_boundary, subroutine/rung_index/branch_path. Fix: plain access.

D. mini-ctx shim path (compat): `investigation_replay.py:1172` builds SimpleNamespace → `break_guard_holds` (`corrections.py:1538`). Only genuinely-firing default: `corrections.py:1279` `clear_only`. The other 10 attrs are supplied.

E. Rest of corrections.py (42 getattr(ctx,…)) unreachable from mini-ctx — entered only with real _PilotContext. SEVEN silent degradations: `corrections.py:568,774,985,1268,1372,1638,1934` all `if pdg is None or program is None: return []`. Charter "fails loud" applies directly.

F. Mapping access (~180 sites): every `.get(` receiver is a Mapping (snapshots, pdg tables). Leave.

G. Heterogeneous foreign objects (compat, out of pilot's ownership): `getattr(tag_ref,"min"/"max"/"choices"/…)` 15 sites; `getattr(rung,"_conditions"/…)` 29 sites (Rung privates crossed). Leave or fix in core/, not this sweep.

## Which objects need `needs` vs type tightening

| Object | Verdict |
|---|---|
| `_PilotContext` (94 sites, 83 `ctx: Any` fns) | type tightening only — every field declared |
| `_PilotState` (36 sites) | type tightening only |
| `OrientationWorld.state/.context` | TYPE_CHECKING annotation (cycle-free) |
| `WorldView`/`WalkContext` Protocol (`types.py:120-160`) | THE reference needs mechanism — extend it; rename prior↔domain_prior to kill the dual |
| mini-ctx | replace with narrow frozen record — it is a deliberate CAPABILITY RESTRICTION, not laziness |
| `CandidateRead`, `ActPolicy`, `_PulseState`, `_ExecutionEvidence`, `_StepContext` | zero optional probes, fully typed — already conformant. navigation_contracts.py is charter-shaped; rot concentrates in modules predating it |

Sizing (sites/file): corrections 61, requirements 56, pilot 37, trace 33, options 26, orientation 25, working_theory 22, earned_work 19, intrascan 19, departure/steer 18, effects 16, program_step 14 … but requirements/trace/working_theory/earned_work are mostly categories F/G. The declared-needs sweep touches 19 src files for the 136 context sites; corrections.py alone is 42.

SWEEP BLOCKER: 101 test files use SimpleNamespace; 19 build a ctx stand-in with `pdg=` (test_pilot_trace, _investigate, _verify, _progress, _intrascan, _advance, _candidate_wait, _absence_root, _dwell_liveness, _effect_expectations, _empirical_veto, _exposure_guard, _guarded_edge_rung, _refinement, _regression_replay_boundaries, _requirement_plumbing, _self_defeating, _table_detour, test_crossings_recorded_registry). Tightening annotations is free; deleting defaults breaks those 19 files. Tests are the larger share of the work.

## Fork sites (Table 2)

`fork_with_pilot_rungs` is the sole sanctioned constructor (`overlay.py:486-501`); `PLC.fork()` called nowhere else in pilot/. 20 production call sites.

| Site | Forks/call | Bound | Locus |
|---|---|---|---|
| `steer.py:199` pulse | 1 | drive loop; scans via `state.remaining_search_scans` (`steer.py:250`) | caller (kernel-shaped) |
| `steer.py:628` coast | 1 | same + `coast_budget` (`:1052,1091`) | caller |
| `steer.py:772` let-run | 1 | `:794` | caller |
| `steer.py:921` dwell | 1 | `:932`, `LIMITS.dwell_ceiling=64` | caller |
| `program_step.py:262` per barriered input | 1 per input | NONE | NONE |
| `program_step.py:290` per required input | 1 per input | NONE | NONE |
| `program_step.py:327` | 1 (+above); scans `projection_scans` 1 or 4 | scans only (param default) | self/none |
| `skiff.py:131` | 1/probe | `_SKIFF_MAX_PROBES=16`/frontier, `_SKIFF_SCANS=4` | self (`:305,314,362`; pass 3 `:379` slices but never decrements — inconsistent) |
| skiff outer `probe_live_guard_frontiers:206` | ≤~30/frontier × frontiers UNBOUNDED | rounds capped `_PROBE_BUDGET=2` (`orientation.py:536`) | self inner, caller outer |
| `intrascan.py:1137` closure | 1/attempt | `IntrascanClosureQuestion.budget=1` validated `__post_init__:332-336`, checked `:926,955`, typed exit BUDGET_EXHAUSTED (`:197,986`) | DECLARED ON THE REQUEST RECORD — reference implementation |
| `investigation_replay.py:763` | 1/uncached hold-set | `CompositionBudget(9)` per hypothesis (`investigate.py:1129`); replay_cache dedupe; hypotheses UNBOUNDED | caller per-hyp, none across |
| `investigation_replay.py:1240` diagnose_excursion | 1 | once per excursion (`_resolve_excursion`) | caller |
| `progress.py:1280` | 1 | bookkeeping | n/a |
| `departure.py:144` settle | 1 | `landing_confirm_scans=100`, `landing_cap=2000` (`coast.py:52-53,575-576`) | self (`coast.py:601`) |
| `pilot.py:1399` recovery source | 1 per candidate scan, loop over writers | NO counter | NONE |
| `pilot.py:1753,1888` (+`for _ in range(4)` `:1890`), `:4119,4673,4725` | 1 each | magic number/single | self/n/a |
| `types.py:972,1010,1024` overlay/snapshot/restore | 1/change | drive loop | caller |
| `_replay_*_projection_at` — 18 sites (pilot 1242/1497/1809/1892/2671/4727, progress 1524/1656, requirements 615, intrascan 522/588/1141, investigation_replay 279/337/385/512, effects 1593, program_step 265/334, skiff 448, steer 313) | 1 hidden runner per cache miss | memoized (`types.py:1116-1128`) + COUNTED (`_projection_replay_count`, `types.py:1072`) but never capped | NONE in src; count asserted only by tests |
| `cyclefold.py:301 cycle_fold_until` | 0 new forks | `budget: int` REQUIRED param, `max_period=64`, `min_repeats=2` | caller |

## Budget constants (Table 3, condensed)

Threaded for real: `_PilotContext.max_scans` + `remaining_search_scans` (`types.py:632,995`; drive loop `pilot.py:5406`; threaded ×10) — the one real kernel-enforced budget. `_World.dwell_scans` credits accepted dwell back.
Kernel-shaped: `CompositionBudget` (`recovery.py:32-48`, consume/consume_auxiliary), `IntrascanClosureQuestion.budget`, `TheoryVersion.remaining_budget` (monotone-checked `working_theory.py:1160`), `cycle_fold_until(budget=…)` required param.
Self-policed: skiff (hand-rolled decrement, inconsistent), coast `_COAST_BUDGET=10_000` (`coast.py:60`; `:403,460`), landing loop `coast.py:596-628`, `pilot.py:1890` magic 4, `program_step.py:387`.
Divergent duplicate: `skiff._SKIFF_MAX_PROBES=16` vs `refinement._SKIFF_MAX_PROBES=8` (re-exported `investigate.py:86-87`) — same name, same reader, two values.
Misc instrument caps: trace max_depth=15/max_choices=16/_SAME_TAG_VALUE_BUDGET=1; tide_tables _MAX_FREE_INDICES=3/_MAX_COMBOS=4096; static_expressions _INDEX_CHASE_CAP=32; earned_work _RELAY_DEPTH=3; pipeline_graph max_hops 8/3; _PENDING_DEPARTURE_SCAN_BUDGET=2000 (`progress.py:116`); _RELATIONAL_REFINEMENT_BUDGET=32; skiff _SKIFF_MAX_DOMAIN=8; overlay history_budget bytes.
Orphan: `_projection_replay_count` counts, bounds nothing, no src consumer (6 test asserts).

## ForkBudget sketch (B.4)

Two budget currencies; only scans are threaded. Forks are free everywhere except intrascan closure + CompositionBudget. A ForkBudget must:
1. Carry TWO counters (forks, scans) — independent (skiff), and cyclefold consumes scans with zero forks.
2. Express three accounting styles: derived (remaining_search_scans), decremented (CompositionBudget), monotone-checked vs parent (theory remaining_budget).
3. Honor dwell credit (`_World.dwell_scans` refunds) or drive termination changes.
4. Carry the kernel-vs-logical scan discriminator (`CoastSession.kernel_budget`, `coast.py:212`) or cyclefold landings shift.
5. Keep typed exhaustion ≠ impossibility (BUDGET_EXHAUSTED, budget_exhausted callback).
6. Resolve the 16-vs-8 skiff duplicate — impossible decision-identically unless budgets are PER-CALLER-SITE: the reader declares the SHAPE, the caller declares the AMOUNT (what cycle_fold_until and CompositionBudget already do).

Landability: as a counting/reporting layer — YES, now, everywhere (decision-identical by construction; gives `_projection_replay_count` its missing src consumer). As enforcement — only where a bound already exists (intrascan, investigate/recovery, skiff singles, steer, coast). Enforcement over program_step, the projection-replay family, and skiff/hypothesis outer loops REQUIRES THE KERNEL EXTRACTION FIRST (no bound exists today; any finite limit changes decisions when it binds, and exhaustion must be attributed to a budget by the ledger/drive loop).

## Resists the pattern (charter input)

1. `orientation.py:933` hasattr duck-typing exists to serve narrow test fixtures — charter must decide: are test-only narrow contexts legitimate consumers, or does the sweep require full contexts and rewrite 19 test files?
2. `departure.py:567-569` — hand-rolled needs declaration that DEGRADES GRACEFULLY (returns weaker observation). Either the charter admits declared-optional needs, or this becomes a construction-time contract.
3. mini-ctx (`investigation_replay.py:1172`) — capability RESTRICTION (excursion diagnosis must not read live navigation state). `needs` is the right mechanism precisely where a reader must be DENIED fields.
4. `program_step.py:183` — name collision, not optionality. Rename first, then delete.
5. `working_theory.py:468,481-486` — fail-closed inbound validation of untyped payloads; exempt (`needs` governs dependencies handed to you, not payloads you refuse).
6. `getattr(harness,"_profile_couplings",())` (cyclefold 487, coast 1019) — None-guard, not a need. Leave.
7. `world_key.py:116` `getattr(value,"__dict__",None)` — genuinely reflective. Exempt.
8. skiff 16 vs refinement 8 — budgets are per-caller-site; reader declares shape, caller declares amount.
