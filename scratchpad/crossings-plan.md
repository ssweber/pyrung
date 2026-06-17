# Crossings — Plan

How `cause()` / `why()` / `simplified()` / walk-regression *cross* an instruction
— in both directions — through one shared abstraction, so the consumers differ
only in *how they source the constraint*, not in *how they cross*.

> Anchors current as of dev after **node-firing/cause() Part 2** landed (SP-tree
> branch of `recorded_cause` rewritten node-aware; the opaque-writer dead-end was
> deliberately preserved as the Phase 1 hook). PDG-bridge removal is deferred to
> Part 3 — coordinate before touching it.

---

## Context

Per-instruction "reverse" logic — *given an output/value constraint on a tag,
what input constraints follow?* — is duplicated and scattered across the
consumers as ad-hoc `isinstance` ladders, and `recorded_cause()` goes **opaque**
at any non-Boolean writer. After Part 2's node-aware rewrite of the SP-tree
branch, the walk still **dead-ends at a writer with no proximate contacts and no
enablers**: the `if not proximate:` block in `_walk_backward` (`recorded.py:387-392`)
appends the written tag as its own root and stops — exactly where
`calc(ds.select(201,300).sum(), A_AlmExtent)` lands, with no SP-tree to attribute.
The driving feature is Part 3 of causal-attribution: chase `A_AlmExtent != 0` back
to the non-zero door/lint operand(s).

**Decision:** build the abstraction first, then migrate consumers onto it; ship
the universal empirical fallback as Phase 0.

## The core split: two reverse mechanisms

A reverse splits cleanly by whether we have an observed scan to diff.

- **Recorded reverse = mechanical, zero semantics.** "Which inputs this
  instruction read changed between N-1 and N?" is a read-set diff. No sign
  reasoning, no cancellation recompute — you observe the actual operand values.
  One mechanism, instruction-agnostic. This is the missing "cross the aggregate"
  step in `recorded_cause`.
- **Projected reverse = per-instruction registry, semantics-bearing.** No scan to
  observe (walker forward-planning prereqs, prover seeding) ⇒ reason abstractly:
  copy-source, calc affine inversion, sum sign reasoning, indirect index
  inversion. **Only this side needs the per-instruction registry**, and the only
  place the sign oracle is load-bearing.

Both **fall through to the Phase-0 counterfactual** when they can't proceed.

## Recorded read-diff — three tiers

The scan log stores writes, not reads (`scan_log.py` — patches/forces/dt/IO).
But the read *footprint* is static (PDG `data_reads`, `BlockRange.tags()` expands
`ds.select(201,300)` to `{ds[201]…ds[300]}` without executing — `pdg.py:450-456`)
and per-scan state is replayable. So:

1. **Default (common):** static footprint × **cached state from the fast compiled
   replay**. Zero execution — read the footprint tags out of two already-cached
   states (`history.at(N)` / `history.at(N-1)`, `history.py:55`) and diff (reusing
   the consecutive-snapshot pattern in `causal/history.py:_find_transition`,
   `:58-128`). Covers calc/sum/select/pack and *bounded* indirect.
2. **Dynamic / unbounded indirect only** (PDG bails, `_INDIRECT_BLOCK_CAP`,
   `pdg.py:458`): the actual runtime reads (resolved addresses) come from the
   on-demand **interpreted** replay Part 2 already runs — **extend it to also
   capture data reads, default-on.**
   - Compiled replay (`CompiledPLC.step_replay`, `compiled_plc.py:353`) is a
     **black box** (generated `step_fn` mutating plain dicts `:322` via a mock
     `_KernelRuntimeContext` `:67-149` — no per-instruction boundary, no
     `ScanContext`, no observer), so the tap must be on the interpreted path.
   - **Part 2 proved that path:** `PLC._replay_node_views_at` (`runner.py:1715`)
     fast-steps to the target scan, then runs *only that scan* through
     `execute_program(replay._program, ctx, capture_rungs=True, observer=capture)`
     (`runner.py:1754`) with a `ConditionViewCapture` (`executor.py:173`),
     one-slot cached. Single-scan, all-rungs, transient — so capturing reads
     alongside is bounded by program size, not history: **default-on, no flag.**
   - **Extend, don't add a parallel observer:** give `ConditionViewCapture` a
     `ScanContext` read-tap (data reads happen *during* `execute`, not at entry
     like condition views) that appends `(name, value)` per node via
     `_current_node_id` (`context.py:208`) / `begin_instruction` (`executor.py:66`
     protocol, `:392` call), returned as a **parallel `{RungId: reads}` map**
     beside `views` (keeps `ConditionView` = control-flow-at-entry; existing
     callers ignore reads). **Widen the replay to capture the adjacent pair
     (N and N-1) in one call** so the diff has both ends; index by the node
     `_recorded_writers_from_firings` already hands us.
3. **Truly opaque** (FiredOnly, un-enumerable footprint): Phase-0 counterfactual
   fallback.

**Burner recorded acceptance is met at tier 1 with no sign oracle** — observe
which `ds[201:300]` operands changed / are nonzero, attribute `A_AlmExtent != 0`
to exactly those. Cancellation is moot; actual values are observed.

## The layering constraint (fixed)

Instructions (`core/instruction/`) sit **low** — import only `tag`,
`memory_block`, `expression`, `context` (last `TYPE_CHECKING`-only). Analysis sits
**high** and already imports instruction classes one-way (`pdg.py:14`,
`prove/absorb.py`). So:

- Evidence-bearing handlers **cannot** be instruction methods (import cycle). The
  **projected registry lives in the analysis layer**, keyed by instruction class.
- Only a lightweight evidence *bundle* + result type live low (`core/crossing.py`).
- Reverse must be **sound** (prove/CLAUDE.md): over-approximating input domains is
  safe; under-approximating is not.
- Static analysis is a prior; the interpreted fork is ground truth (walk/CLAUDE.md).
  Shared static helpers stay in `sp_values.py`.

## Architecture

**`core/crossing.py` (NEW, low module)** — frozen `CrossingContext` bundle
(runtime-imports nothing from `analysis/`; analysis/bounds types under
`TYPE_CHECKING`): `snapshot`, `tags_by_name`, `bounds_index`,
`nondeterministic_dims`, `nd_domains`, `value_at_scan` (Callable, recorded
evidence), `scan_id`. Each consumer fills only what it has; **prover-path contexts
must have `value_at_scan is None`** (recorded evidence must not leak into seeding).

**Result type (`crossing.py`)** — models existing `_UNSUPPORTED_NUMERIC_INVERSION`
+ `None` patterns:
```python
UNKNOWN = object()
@dataclass(frozen=True)
class ReverseResult:
    constraints: list[tuple[str, frozenset[Any]]] = []   # (tag, allowed-values)
    exact: bool = False        # necessary AND sufficient; False = sound superset
    fallthrough: bool = False  # could not invert -> caller routes to Phase-0 sweep
REVERSE_FALLTHROUGH = ReverseResult(fallthrough=True)
```
Pin and test the **unsatisfiable encoding** now: `constraints=[(dest, frozenset())]`
= "no value works" (structural blocker), so handlers agree.

**`core/analysis/crossings/` (NEW package)** — the **projected** registry:
`register(cls, crossing)`, `crossing_for(instr)` (exact class then MRO walk for
timer subclasses), `reverse(instr, target_tag, target_value, ctx) -> ReverseResult`,
`registered_classes()` (coverage test). `forward(instr, ctx) -> Any|UNKNOWN`
protocol locked now but mostly `UNKNOWN` — **reverse-first** (the walker's
interpreted fork is the forward oracle).

**Relationship to `build_reverse_edge_map` (`reverse_edges.py:131`)** —
complementary (whole-program numeric back-prop for prover seeding vs.
per-instruction constraint reverse). Distinct from the PDG **timeline bridge**
(`timeline_writers_of` `pdg.py:136`, `timeline_capture_indices_for_node`
`pdg.py:157`, `call_site_rung_indices()` `pdg.py:115`), which is deferred to Part 3.
Phase 2 bridges the edge map (crossings *call* `calc_reverse_edge`/
`_written_value_for_tag` internally); re-expressing it as registry lookups is a
gated later cleanup.

---

## Phase 0 — Empirical counterfactual fallback (LANDED 2026-06-17, holds-only)

> **As-built deltas from the design below** (both reviewed/approved):
> - **Wiring moved to the agenda regression seam, not `rules.py:250`.** `rules`
>   sits *above* `explore` in the import order, so `rules` cannot import the
>   sweep. The sweep is invoked from `agenda._check_progress_regression`
>   (`agenda.py:316`) as the **empty-result fallback to `mine_regression_holds`**
>   (i.e. exactly when the cause chain dead-ended and named no hold), through the
>   new `agenda._counterfactual_fallback_holds` (`agenda.py:333`). Output flows
>   through the existing `protect`+`patch` install path (`agenda.py:525-546`) —
>   `_walk_chain` stays pure diagnosis ("never asserts a `Transition`").
> - **Anchor = pre-departure fork.** The perturb-and-survive template needs a
>   state where the goal *holds*; the live regression fork is post-break. New
>   `rules._last_committed_scan` (sibling of `_leaving_committed_scan`, sharing
>   `_committed_departure`) gives the last holding scan; `work.fork(that_scan)`
>   is the anchor. A baseline settle that already breaks the goal ⇒ non-steady
>   anchor ⇒ honest `[]`.
> - **Pass renamed `counterfactual_fallback`, kind `widening`** (not
>   `counterfactual_fallback_order`/`ordering`): disabling regresses the refusing
>   direction, which `ordering` forbids. Holds-only landed; the
>   `recursive_cause_evidence → goals` half is the deferred fast-follow.
> - Lives in `walk/explore.py` (`_counterfactual_hold_sweep`,
>   `_sweep_goal_holds`, `_perturb_away`). Tests: `test_walk_counterfactual_fallback.py` (4) green; `make test-walk` 292 green; `make lint` clean.

Universal floor under every fallthrough. Generalizes the proven `_divest_probe`
(`walk/explore.py:98`) fork→perturb→settle→goal-survives template into a
cone-bounded input sweep. **Proposes protective holds the agenda installs and the
walker replay validates — never asserts a `Transition` is real** (`_walk_chain` is
the diagnosis path).

- **Lives in** `walk/explore.py` beside `_divest_probe`.
- **Signature:** `_counterfactual_hold_sweep(ctx, work, dead_end_tag, goal,
  monitors=_NO_MONITORS) -> list[tuple[str, Any]]`.
- **Algorithm:** cone = `pdg.upstream_slice_with_calls(dead_end_tag)`
  (`pdg.py:317`); candidates = cone ∩ actionable (`_is_actionable_root`,
  `rules.py:515`) ∩ (ext_inputs ∪ edge_ext); per candidate fork + perturb away
  from current + settle `_PULSE_REACT_CAP`; if the goal breaks, record `(candidate,
  current-goal-preserving-value)`. Budget-checked before each fork.
- **Wiring:** at `_walk_chain` dead-end (`rules.py:250`) keep the existing
  `_held_transition` append; add a branch calling the sweep, feed results through
  the `add_level`/hold path `_record_done_boundary` uses (`rules.py:297-348`),
  tagged `EvidenceRef("counterfactual", ...)`. Gate behind a new `WALK_PASSES`
  advice row (`counterfactual_fallback_order`) → free ablation row in
  `test_walk_passes.py`, provably recovery-only.
- **Acceptance (`make test-walk`)** — `test_walk_counterfactual_fallback.py`:
  effect depends through a statically-uninvertible writer on one of several
  inputs; assert (a) baseline proposes no hold, (b) sweep finds the load-bearing
  input only, (c) `how()` replays green, (d) ablation off → dead-end, no hold.

## Phase 1 — Generic recorded read-diff (delivers the burner, zero semantics)

The instruction-agnostic recorded reverse, three-tier per above.

- **Lives in** `causal/` (new `crossings_recorded.py` or fold into `recorded.py`).
- **Signature:** `recorded_read_changes(pdg, history, writer_rung_or_instr,
  scan_id) -> ReadDiff` with `changed: list[(tag, before, after)]` and
  `nonzero_now: list[tag]` over the footprint. The `writer_rung_or_instr` is now
  handed in **node-aware** — Part 2's `_recorded_writers_from_firings`
  (`recorded.py:451`, identity at `:496` `(rung_index, rung, subroutine)`) names a
  subroutine aggregate writer as its precise `sm_X[rN]` rung, not the rolled-up
  call-site main rung — so the footprint/reads are computed against the right rung.
- **Tier 1:** footprint from PDG (block-expanded), values from cached states.
- **Tier 2:** when footprint not statically enumerable → **extend
  `ConditionViewCapture` (`executor.py:173`) with a `ScanContext` read-tap**
  (default-on) in the `_replay_node_views_at` (`runner.py:1715`) replay, **widened
  to capture scan_id and scan_id-1 in one call**; reads keyed per node via
  `_current_node_id` (`context.py:208`), returned as a parallel `{RungId: reads}`
  map; cache per `(scan, instr)` in-window. No separate observer, no flag.
- **Tier 3:** unresolvable → `fallthrough` → Phase-0 sweep.
- **Wiring:** hook at the opaque-writer dead-end — the `if not proximate:` block in
  `_walk_backward` (`recorded.py:387-392`): instead of (only) appending the bare
  `transition` as a conjunctive root, call `recorded_read_changes` on the
  node-aware writer and continue chasing from the changed reads (triggers) +
  nonzero-now reads (enablers). `ChainStep.caller_rung_index` (`models.py:155`) is
  the call-site lever when the writer is inside a subroutine. Same path feeds
  `mine_regression_holds` (`rules.py:95`, unchanged) so regression-hold mining can
  cross aggregates.
- **Acceptance (`make test-walk` + recorded-cause tests)** —
  `test_recorded_read_diff.py`: (1) `calc(sum)` writer — cause chases through to
  the changed operands (today it dead-ends); (2) burner-shaped fixture attributes
  `!= 0` to the truthy operands (tier 1, no oracle); (3) **subroutine** aggregate
  writer — cause names `sm_X[rN]` and crosses it; (4) dynamic-indirect writer →
  tier-2 interpreted tap returns the resolved address; (5) un-enumerable →
  fallthrough → Phase-0 sweep engaged.

## Phase 2 — Projected Crossings registry + migrate projected inverters

Build the registry and consolidate the duplicated *projected* inverters onto it.
Cleanup-to-risk order:

- `crossings/copy.py` `CopyCrossing` — internally uses `_written_value_for_tag`
  (`sp_values.py:162`) + `_invert_indirect_source` (`priors.py:225`). **Migrate the
  duplicated copy-source interpretation first** (`priors.py:923`,
  `projected.py:742/772-819`) — highest cleanup, lowest risk (prior-only paths; a
  regression shows as a missing candidate, not a wrong plan). Keep
  `_written_value_for_tag` as the crossing's classifier (~12 sites; do not delete).
- `crossings/calc.py` `CalcCrossing` — affine via `calc_reverse_edge`
  (`reverse_edges.py:64`); inequality forms fold in `_chase_inequality_source` /
  `_extract_inequality_prereqs`. SumExpr → Phase-3 sign oracle (until then →
  `FALLTHROUGH`; the fallback covers projected sum).
- `crossings/boolean.py` `BoolCrossing` — thin wrapper over existing `attribute()`
  (`sp_tree.py:165`) for *new* projected consumers. **Do not migrate
  `recorded_cause`/`why_cause`'s direct `attribute()` calls** — ~zero cleanup,
  most load-bearing path (and freshly rewritten in Part 2); document the
  intentional bypass. (This is "leave the clean path last.")
- `crossings/convert.py` `ConvertCrossing` — **stub** `FALLTHROUGH`, registered for
  convert/pack/unpack so the gap is an explicit cell. The `convert` skip
  (`reverse_edges.py:162/186`) becomes the forcing function for the
  `to_ascii`/`to_value` goal-following open item.

Gates: `test-walk` + recorded-cause (copy); `test-prove` + `test-soundness`
(calc/inequality).

## Phase 3 — Sign oracle (projected calc/sum; static optimization over the fallback)

Only needed for the *projected* sum reverse, to avoid a fork when statically
provable. In `sp_values.py` (neutral home), imported by `CalcCrossing`:
```python
class Sign(Enum): NONNEG; NONPOS; UNKNOWN
class ReduceKind(Enum): SUM; SUM_OF_BOOL; OR; COUNT; MINMAX; UNKNOWN  # only SUM/SUM_OF_BOOL wired
def reduce_kind(expr) -> ReduceKind     # SumExpr (expression.py:473) over BOOL block -> SUM_OF_BOOL; other SumExpr -> SUM; else UNKNOWN
def operand_sign(tag, ctx) -> Sign      # TYPE (WORD/BOOL/CHAR=NONNEG; INT/DINT/REAL signed) -> BOUNDS (min>=0/max<=0) -> DOMAIN (nd dims all >=0/<=0) -> UNKNOWN
```
> `SumExpr` is the **only** aggregate-over-range form — no count/or/min/max
> expression exists. Wire `SUM`/`SUM_OF_BOOL`, forward-declare the rest,
> everything else → `UNKNOWN` → `FALLTHROUGH`.

`CalcCrossing.reverse` for projected `dest != 0`: classify `reduce_kind`; `SUM`
with all operands same-sign ⇒ sound to attribute "≥1 operand nonzero"
(`exact=False`); mixed/UNKNOWN sign ⇒ `FALLTHROUGH` (fallback perturbs). No
recorded-mode logic here — recorded is Phase 1.

**Acceptance** — `test_sign_oracle.py`: (1) WORD block all-NONNEG → static
attribution; (2) signed INT with negative `nd_domains` → UNKNOWN → FALLTHROUGH →
Phase-0 sweep; (3) mixed-sign → FALLTHROUGH.

---

## Test strategy & coverage map

| Phase | Gate |
|---|---|
| 0 fallback | `make test-walk` + ablation row in `test_walk_passes.py` |
| 1 recorded read-diff | `make test-walk` + recorded-cause + `test_recorded_read_diff.py` (burner recorded) |
| 2 registry + copy | `make test` + `test_crossings_registry.py` + recorded-cause/copy parity |
| 2 calc/inequality | `test-prove` + `test-soundness` |
| 3 sign oracle | `test-walk` + `test-prove` + `test_sign_oracle.py` |
| later edge-map | `test-prove` + `test-soundness` + `test-fuzz` + legacy==registry parity |

**Coverage-map test** — `test_crossings_coverage.py`: assert `registered_classes()`
equals an explicit `EXPECTED_WITH_CROSSINGS`, and convert/pack/unpack resolve to a
fallthrough stub. A new instruction class with no crossing fails → forces a
covered-or-fallthrough decision. Runs under `make test`. `make test-soundness`
(`--prove-agreement`) backstops the projected paths feeding prover seeding.

## Risks / non-goals

- **Ship Phase 0 fully (with ablation) before anything routes to fallthrough** — a
  handler returning `FALLTHROUGH` with no fallback wired is a silent regression vs.
  today's bespoke chases.
- **Phase 1 only *adds* a continuation at the opaque-writer dead-end** — Part 2
  (landed on dev) rewrote the SP-tree branch of `_walk_backward` but deliberately
  left the `if not proximate:` dead-end (`recorded.py:387-392`) as this hook;
  existing SP-tree attribution untouched. Gate behind recorded-cause tests.
- **PDG timeline bridge is still present** (`timeline_writers_of` `pdg.py:136`,
  `timeline_capture_indices_for_node` `pdg.py:157`, `call_site_rung_indices()`
  `pdg.py:115`) — removal deferred to Part 3 (timeline-consolidation). The recorded
  read-diff doesn't need the bridge (footprint comes from PDG `data_reads`); if a
  later phase touches it, coordinate with Part 3.
- **Non-goal: a general always-on per-instruction read/write store** (live-path or
  persisted). That would force disabling the fast compiled replay or instrumenting
  codegen `step_fn`, and the recorded-*forward* half is already covered by whole-scan
  `recorded_effect` + rung-firing capture (`capture_rungs=True`). "Default-on" for
  tier-2 means on during the **on-demand interpreted replay only** (off the hot path),
  capturing the N/N-1 pair transiently (one-slot cached) — not a persisted/live store.
- **Do not migrate `attribute()` callers** (documented no-op) — keep cause/why off
  the registry.
- **`_written_value_for_tag` (~12 sites)** — wrap its interpretation, never delete.
- **Recorded observes; projected proves** — assert prover-path
  `CrossingContext.value_at_scan is None`; over-approximation stays sound.

## Critical files

- `src/pyrung/core/crossing.py` *(new)* — `CrossingContext`, `ReverseResult`, `UNKNOWN`
- `src/pyrung/core/analysis/causal/crossings_recorded.py` *(new)* —
  `recorded_read_changes` (tier-1 footprint × cached state; tier-2 interpreted tap)
- `src/pyrung/core/analysis/crossings/` *(new pkg)* — projected registry +
  copy/bool/calc/convert
- `src/pyrung/core/analysis/sp_values.py` — sign oracle + `reduce_kind` (Phase 3)
- `src/pyrung/core/analysis/walk/explore.py` — `_counterfactual_hold_sweep` (`:98`)
- `src/pyrung/core/analysis/walk/rules.py` — Phase-0 dead-end wiring (`:250`);
  regression hold mining `mine_regression_holds` (`:95`); hold emission modeled on
  `_record_done_boundary` (`:297`)
- `src/pyrung/core/analysis/causal/recorded.py` — `_walk_backward` (`:124`),
  opaque-writer continuation at the `if not proximate:` dead-end (`:387-392`);
  node-aware writer `_recorded_writers_from_firings` (`:451`/`:496`);
  `causal/history.py:_find_transition` (`:58-128`) snapshot pattern
- `src/pyrung/core/analysis/causal/models.py` — `ChainStep` (`:119`),
  `caller_rung_index` (`:155`)
- `src/pyrung/core/runner.py` — `_replay_node_views_at` (`:1715`) — **widen to
  capture the N/N-1 pair**; proven `execute_program(observer=...)` seam (`:1754`)
- `src/pyrung/core/executor.py` — `ConditionViewCapture` (`:173`) — **extend with a
  `ScanContext` data-read tap (default-on), parallel `{RungId: reads}` map**;
  `ExecutionObserver.begin_instruction` (`:66` protocol, `:392` call) delineates
  per-node read buckets
- `src/pyrung/core/context.py` — `ScanContext._current_node_id` (`:208`) keys the
  tier-2 read buckets; the `get_tag`/block-read path is the tap site
- `src/pyrung/core/analysis/pdg.py` — read footprint (`:46-68`, `:450-456`),
  indirect cap (`:458`), cone (`:317`); timeline bridge (`:115`, `:136`, `:157`)
- Tests *(new)*: `test_walk_counterfactual_fallback.py`, `test_recorded_read_diff.py`,
  `test_crossings_registry.py`, `test_sign_oracle.py`, `test_crossings_coverage.py`

## Verification (end-to-end)

1. Each phase: its `make test-*` gate green; `make lint` clean.
2. Phase 0: ablation matrix shows the fallback is recovery-only; counterfactual
   fixture `how()` replays green.
3. Phase 1: the burner `A_AlmExtent != 0` recorded chase crosses the `sum` and
   attributes to the truthy door/lint operands (tier 1, no oracle); a `calc(sum)`
   fixture that previously dead-ended now chases through; a subroutine aggregate
   writer is named `sm_X[rN]` and crossed; a dynamic-indirect fixture resolves via
   tier-2.
4. Phase 3: signed/mixed-sign projected tripwires route to the fallback; provable
   same-sign sums attribute statically without a fork.
5. Final: `make` green, including the coverage-map test (every instruction class
   covered-or-fallthrough).
