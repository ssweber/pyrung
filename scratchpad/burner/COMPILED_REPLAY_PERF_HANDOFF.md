# how() / compiled-replay perf — handoff

**Session date:** 2026-07-02. **Target workload:** `how(y_BurnerLoop)` on the burner
Click project (`PYRUNG_CLICK_PROJECT`, default `C:\Users\Sam\AppData\Local\Temp\CLICK (0009051C)\pyrung_project`).
**Related memory:** `project_pilot_how_perf_arc` (the durable index; this doc is the detailed continuation).

---

## TL;DR

- **Landed + committed `0f70dfc`** (`perf(core): structural-sharing commit + single load/flush bracket in compiled replay`): compiled-replay scan **9.2 → 3.66 ms/scan**; `how(y_BurnerLoop)` loop **~35 → 25.4 s** (committed-baseline 38.4 s → 25.4 s ≈ −34%). Full suite 4637 + prove-agreement soundness 1034 green; lint (incl. `ty`) clean.
- **2026-07-02 session 2 — incremental flush + load-skip (UNCOMMITTED, all gates green).** Two more plumbing levers in `compiled_plc.py` (+1 line `runner.py`): **(b) incremental flush** (flush only `_TrackedList.written_indices`, not all ~5725 cells) and **(a) load-skip** (drop the redundant per-scan `load_block_from_tags`; keep the one-time anchor load). Result on the paths that matter: **`step_replay()` 1.72 → 0.906 ms/scan** (interpreted 2.92 → **3.2×**), `step()` 2.53 → 1.705, burner fill 3.66 → 2.85 ms/scan. Full suite 4637 (both backends) + soundness 1034 + lint green; burner `reached=True final_scan=2011`. **This clears the ~1 ms bar for the coast swap.**
- **The coast runs `step_replay()`, not `step()`.** The handoff's original "~14% edge" compared `step()`-with-`SystemState` (2.53) to interpreted — the wrong baseline. The coast needs only the final state + per-scan ejection observability (no committed `SystemState`), so it runs `step_replay()`, now **0.906 ms = 3.2× vs interpreted**. Lever 2 (coast swap) is now *justified on per-scan cost*; the open risk is preserving cyclefold macro-skip + plant semantics, not the per-scan edge.
- **Three measure-first dead ends were ruled out** (don't re-try): clock/rtc-from-log (lever a), blockless kernel, dropping the plant pre-pass. Evidence below.

---

## What landed (commit `0f70dfc`, `src/pyrung/core/compiled_plc.py` only)

Two changes, both isolated to `CompiledPLC`:

1. **Structural-sharing commit.** `step()` rebuilt the *entire* tags+memory PMap from
   scratch every scan (`pmap(self._committed_tags())`, `pmap(dict(memory))`), re-hashing
   all ~2672 tags to produce a map ~identical to the previous one. The burner changes
   **~0–4 tags/scan** (`diag_change_rate.py`: 2672 tags, avg 0.0 / max 2 changed; 46 memory
   keys, ~4 changed). New `_commit_tags`/`_commit_memory` seed a pyrsistent **evolver** from
   the previous `self._state.tags/.memory` and set only changed keys — same thing the
   interpreted path already did (`core/context.py::commit` uses `_tags_evolver.persistent()`).
   `_prev_committed_tags` (plain-dict mirror) is seeded in `_initialize_from_state`; the
   removal branch is a rare guard (committed keyset grows monotonically during stepping).
   Killed the `_turbo_mapping` bucket (was 5.49 s tottime).

2. **Single load/flush bracket.** Plant pre-pass and main pass each did a **full block
   round-trip through the tag dict** (`_invoke_step` = load-all → step → flush-all, ×2/scan).
   Collapsed into one bracket: `_load_blocks_tracked()` (load once, wrap one `_TrackedList`
   spanning both passes) → plant → drain → main → `_flush_blocks_tracked()` (flush once).
   The input drain now writes block-element overrides **straight into the live arrays** via a
   block-aware `_KernelRuntimeContext` (`blocks_live` flag + `_block_pos` = tag→(symbol,idx)
   map). Reads route to arrays too, so `apply_pre_scan`'s skip-if-unchanged force check sees
   the plant's in-flight writes. Compiled rungs bypass the ctx → zero hot-path overhead.

**Correctness proof for #2:** it is order-preserving by construction (plant → patches/forces
→ main → forces-again), so bit-identical regardless of tag overlap. Verified: full suite,
soundness, `test_compiled_replay.py` (has 4 indirect-block refs — the "indirect access"
worry the user flagged; passed).

---

## What landed — session 2 (UNCOMMITTED, `compiled_plc.py` + 1 line `runner.py`)

Two levers; all gates green (suite 4637 both backends, soundness 1034, lint incl. `ty`, burner
`reached=True final_scan=2011`). Not yet committed — user was mid-decision on next direction.

1. **Incremental flush.** `_flush_blocks_tracked` now flushes only `_TrackedList.written_indices`
   (mapped array-index→name via new `_block_index_names` inverse of `_block_pos`) instead of
   calling the kernel's O(~5725) `flush_block_to_tags`. **Output-equivalent by construction:**
   `_commit_tags` already gated committed block tags on `_live_block_tags`, and `_live_block_tags`
   only ever grew from `written_indices` — so the full flush's writes to non-written/non-live
   dict cells were dead weight (never committed). Unwritten cells round-trip their own value.

2. **Load-skip.** Dropped the per-scan `load_block_from_tags` (renamed `_load_blocks_tracked` →
   `_open_block_bracket`, wrap-only). The one-time anchor load stays in `_initialize_from_state`.
   The block arrays are now **authoritative between scans** — this is safe because every
   block-element tag-dict write happens *inside* the bracket, made true by two supporting changes:
   - **`apply_post_logic` moved inside the bracket** (before `blocks_live=False`/flush, was after).
     A post-logic force that fights a rung write now lands in the array (and is flushed), instead
     of only the tag dict. Same committed result; array stays in sync. Scalar forces unaffected
     (no `block_pos`). Edge capture still runs after flush in both `step`/`step_replay`.
   - **`apply_replay_io_write(name, value)`** (new method) — replay's `io_submit`/`io_drain`
     effect writes (runner.py `_replay_to_compiled`) route through it; it mirrors block-element
     writes into the array + marks them live. Note `_replay_range_compiled` (the fill path) has no
     io writes (patches drain inside the bracket), so the burner fill exercised load-skip directly.

Correctness rests on: **arrays authoritative between scans**. The only external block-dict writers
are (a) init/reset → `_initialize_from_state` reloads arrays, (b) io-replay → `apply_replay_io_write`
syncs arrays. System-tag and scalar dict writes outside the bracket are never block elements.
Soundness (prove-agreement) is the net and passed 1034.

## Coast measurement + design (session 2, `diag_coast_split.py`)

Instrumented the real `how(y_BurnerLoop)` run (30.9 s) by counting/timing every
`_run_single_scan` and splitting by fold path:

| fold path | real scans | wall-clock | per-scan |
|---|---|---|---|
| `cycle_fold_until` (oscillating holds) | 1141 (2 coasts) | 6.98 s | 6.12 ms |
| `fold_run_until` (steady holds) | 1145 (6 calls) | 7.36 s | 6.43 ms |
| **coast total** | **2286** | **14.34 s (~46% of run)** | **6.27 ms** |
| bare `single_scan_time` (all 2337 calls) | 2337 | 13.21 s | 5.65 ms |

**Two findings that reshape the plan:**
1. **The coast scan is ~5.65 ms interpreted, NOT 2.9 ms.** `_run_single_scan` on a
   coast fork also runs the ejection **monitor** (`when(_ejected).pause()`),
   synthesis-hold evaluation, harness, and **scan recording** — all on top of the
   rungs. So the per-scan ceiling is far bigger than the handoff assumed.
2. **Both fold paths run ~1145 real scans each.** The burner harness feedback
   (plant ramps) keeps breaking plateaus, so `fold_run_until` is NOT mostly
   skipping. The full win needs **both** loops driven by compiled stepping.

**Ceiling:** 2286 coast scans × 0.906 ms (compiled `step_replay`) ≈ 2.1 s vs 14.3 s
→ **~12 s recoverable** (how() ~31 → ~19 s). Bit-equality is already covered by the
existing replay/soundness suites (a coast scan = forward stepping with inputs held
constant by the hold rungs — the same synthesis overlay the fills replay).

### Infra that already exists (big de-risk)
- `PLC._soft_exec_program()` builds the bracketed unit (holds + user + plant) and
  `_compiled_replay_supported_kernel()` compiles it (pre_step_fn = plant+holds).
  The fills already run this exact overlay compiled. A coast fork carries the same
  overlay (`fork_with_holds` → `_sync_holds`; conditional holds → rungs in
  `_coast_holding_state`). So compiling a coast fork is a solved problem.
- The fold loops (`fold_run_until` in `core/fold.py`, `cycle_fold_until` in
  `pilot/cyclefold.py`) are the only per-scan drivers; both call
  `runner._run_single_scan()` and read `runner._state` (SystemState).

### B1 LANDED (commit `d1cbc78`, `perf(pilot): compiled unfolded coast fast-path`)

`pilot/compiled_coast.py::coast_compiled` — tried first from `_coast_to_value` +
`_coast_holding_state`; steps `CompiledPLC.step_replay` on a coast fork, evaluates
reached/ejection on `_kernel.tags` (no per-scan commit), hands the landing back via
new `PLC._adopt_coast_state`. Returns `None` to defer to the interpreted fold when
unsupported or not reached within a 5000-scan unfolded cap. Gated by
`PYRUNG_PILOT_COMPILED_COAST` (default on).

- **`how(y_BurnerLoop)` 30.7 → 26.8 s (−13%)**, reached=True. Full suite 4637 + pilot
  214 + lint green.
- **Measured** (`diag_coast_split/breakers/scan_phases/coast_shadow.py`): coast = 2286
  real scans / 14.3 s / ~46% of run; interpreted coast scan = **5.65 ms** (rungs 3.62 +
  commit/monitor/record 1.45 + prepare 0.13), compiled step_replay on the coast overlay
  = **1.7 ms**. Trajectory **bit-equal** to interpreted (verified scan-by-scan, benign
  block-membership filtered). Fold already active (2.1×) — the 2286 are true executions.
- **Plateau-breaker** = the `i_/x_RotateSensor` shaft-encoder Pulse oscillation + its two
  sensor watchdogs (~26 % of coast scans); a limit cycle `cycle_fold_until` only folds
  1.7× (watchdog crossings are always near).
- **Plan diff is benign**: B1 doesn't fold → plans omit the fold's `accelerators` (the
  timer-Acc jumps — planner bookkeeping, not user inputs) and land 1 scan *more precisely*
  (the fold overshoots by one). Not a regression; valid + cleaner. `_reconcile_landing`
  (membership normalize) was tried and dropped — the diff is fold-accelerators, not
  membership.
- **Ejection hands back** (not defers): the compiled ejection state is bit-equal, and the
  pilot suite passes with investigation running off it — no interpreted re-run needed
  (that double-work was why the first cut was *slower*).

### B2 — NEXT: fold arithmetic over compiled stepping

Drive the existing `fold_run_until` (core/fold.py) + `cycle_fold_until` (pilot/cyclefold.py)
math over `CompiledPLC.step_replay` instead of `_run_single_scan`, reading a plain-dict
state view over `_kernel.tags`, doing fold jumps via `comp.patch` + `_kernel.scan_id/timestamp`.
Reuses the exact fold arithmetic ⇒ bit-equal by construction, restores `accelerators` +
matching landing, and **generalizes to far targets** (a 1 hr timer folds to a few compiled
scans — the whole point). Removes B1's unfolded cap. Cleanest seam: a stepper adapter so the
fold loops call one `step()`/`state`/`patch`/`jump` interface backed by either the interpreted
runner or CompiledPLC. Gate: pilot suite + soundness + burner `how()` (accelerators + scan
count should match baseline once folding).

### The two designs that were weighed (A rejected)
- **Design A — compiled rung-eval inside `_run_single_scan`, opt-in per fork.**
  Gate a compiled fast-path behind a per-PLC flag that only coast forks set (blast
  radius contained; non-coast PLCs untouched). Keeps ALL fold/monitor/pilot
  machinery — `fold_run_until`/`cycle_fold_until`/guards/ring/verify unchanged,
  still operating on real SystemStates. Pays the SystemState commit (`step` path,
  ~2.08 ms) + monitor/record overhead, so floor ≈ 2.75 ms → **~6 s saved**.
  Moderate port, lower risk. Fallback to interpreted whenever a kernel is
  unavailable or breakpoints/debug are active.
- **Design B — dedicated compiled coast loop (max win).** Port `cycle_fold_until`
  + the probe/edge scans of `fold_run_until` to drive `CompiledPLC.step_replay`,
  read predicate/guard/ring from `_kernel.tags` (no SystemState commit), map fold
  jumps to `CompiledPLC.patch` + `_kernel.scan_id/timestamp`, evaluate the ejection
  guard manually on kernel tags, and hand the final state back to the interpreted
  fork for `verify_gates`. Floor ≈ 0.906 ms → **~10–12 s saved**. Isolated to the
  pilot, but a large, bit-equality-sensitive port (the fold logic is welded to
  `runner._state`).

**Recommendation:** start with **A** (contained, reuses the fold machinery, ~6 s),
measure, then decide whether B's extra ~5 s justifies the port. Whichever: gates are
pilot suite + soundness + burner `how()` bit-identical (`reached=True
final_scan=2011`, same plan/journal).

## Measure-first dead ends (RULED OUT — do not re-attempt without new evidence)

| Idea | Verdict | Evidence |
|---|---|---|
| **Lever (a): derive `sys.clock*`/`rtc.*` from scan log** instead of state reconstruction | **INERT** on burner | `diag_derived_fills.py`: 8 fills, **0 derived-only intervals**. The 1 `sys.clock_1s` fill shares interval 1200 with `S_Execute` → it *relocates*, doesn't disappear. Same trap as the writer-index lever (1ae9f04). |
| **Blockless kernel** (run replay on the flat tag dict, no blocks — kills load/flush) | **10× SLOWER** | `diag_blockless.py`: block 1.23 ms/scan vs blockless **13.0 ms/scan**. Rungs do thousands of register accesses; `tags["name"]` dict lookup vs `blocks[sym][idx]` array index swamps the O(5725) load/flush. Prover uses blockless for symbolic analysis, not speed. |
| **Drop the plant pre-pass on replay** ("log already recorded its effects") | **REQUIRED** | `diag_plant_replay.py`: suppressing it diverges on exactly the feedback tags (`i_/x_DoorClosed`, `RotateFB`, `BlowerFB`, `LintDoorClosed`) every scan. Plant feedback is **re-executed deterministic rungs, not stored data** — the ScanLog records only external inputs; re-running the plant reproduces feedback. Correct model, not droppable. |

---

## Current full-loop map (`profile_how.py`, this run)

**GRAND TOTAL 31.16 s = setup 5.65 s + loop 25.51 s.**

### Setup (5.65 s) — one-time, before the loop
- `_build_pilot_context` **2.92 s** (51.7%) — prove domain analysis (`_build_pilot_context` → prove passes).
- `_prepare_route` **2.71 s** (47.9%) — route enumeration/pruning.
- everything else negligible.

### Loop (25.51 s) by event kind
| event kind | time | % | count | what it is |
|---|---|---|---|---|
| **`zoom_accepted`** | **12.39 s** | **48.6%** | 5 | **the COAST — 3831 interpreted forward scans** (let-run/zoom through timer dwell). ~3.2 ms/scan interpreted. |
| `letrun_ejection` | 6.59 s | 25.8% | 2 | cause/incident investigation when a coast ejects (timer/counter self-completes). Contains fills + cause walk. |
| `trend_regression` | 2.88 s | 11.3% | 3 | regression recovery (cause/transition machinery). |
| `iteration` | 2.00 s | 7.8% | 10 | per-iteration compass/candidate work. |
| `candidate_accepted` | 1.28 s | 5.0% | 4 | verify + cause attribution after a commit. |
| others | <0.4 s | — | — | zoom_rejected, started, trial_committed, etc. |

**Where the fills (5.6 s) live:** distributed *inside* `letrun_ejection` / `trend_regression`
/ `candidate_accepted` / `iteration` — they're the compiled *historical* replay triggered by
the cause/transition machinery (`_walk_backward` → `_find_last_transition_scan` → slab fill).
They are NOT the coast.

### In-fill compiled-scan breakdown (`diag_compiled_profile.py`, 7.15 s cProfile / 1301 scans)
| bucket | % | detail |
|---|---|---|
| Emitted rung code (`_run_kernel_pass`→`_kernel_step`/`_sub_*`) | ~40% | 2 passes/scan; incl. `_TrackedList` access overhead + dict.get for scalar tags |
| State build (`_commit_tags` + pyrsistent evolver) | ~33% | **still evolving every slab scan.** Evolver *sets* are O(changed), but the **diff loop is O(2672)** (walks every kernel tag to find the ~0–4 changed) + pyrsistent tree ops (~0.5 s). |
| Load/flush | ~15% | single-bracket halved it; still O(5725) per load/flush |
| Per-fork init/compile (×8) | ~10% | `CompiledPLC.__init__` per slab fill |

### Per-scan reference benches (`diag_scan_speed.py`, cold burner, single pass)
- **Committed baseline (`0f70dfc`):** interpreted `PLC.step` **2.93 ms** · compiled `step()` **2.53 ms** · compiled `step_replay()` (no SystemState) **1.72 ms** · blockless **13.0 ms**.
- **After session-2 flush + load-skip (uncommitted):** interpreted **2.92 ms** · compiled `step()` **1.705 ms** · compiled `step_replay()` **0.906 ms**. `step_replay` is **3.2× the interpreted step**. Remaining `step_replay` cost is ~0.85 ms of **rung passes** (2 passes: plant + main) — load (0.42) and flush (0.30) are gone; state build is not on `step_replay`.

### Single-pass decomposition (`diag_pass_breakdown.py`, burner base program, no state build)
**1.224 ms/pass** = load 0.422 (34.5%) + **emitted rungs 0.462 (37.8%)** + flush 0.337 (27.5%) + edge 0.003.
- **load+flush = 62% of a pass, almost pure waste** (moves all 5725 cells; ~0–4 change).
- The **~14% compiled-vs-interpreted edge (2.53 vs 2.93) is too small to justify the coast swap on its own** — the swap only pays once the compiled scan is driven to ~1 ms. That is a *plumbing* problem, not codegen:
  - **The load is largely REDUNDANT.** Block arrays persist between scans (`blocks[sym]=tracked.data`); after the block-aware drain, the *only* writer of block-element dict cells is the flush itself. Between a flush and the next scan's rungs the sole dict writes are **system tags** (`sys.*`/`rtc.*`/`fault.*`), which are *not* block elements — so the array already holds the right values and the reload copies them onto themselves. Skip it (after the first scan / a reset / a direct dict patch) → **−0.42 ms**. AUDIT: io_submits/drains + lifecycle events for any block-element dict write outside the bracket; then lean on soundness.
  - **Incremental flush**: flush only `_TrackedList.written_indices` (already tracked) instead of all 5725 → **−0.30 ms**.
  - Leaves **~0.5 ms/pass** (emitted rungs + O(changed) plumbing) — *below* the 1 ms target. Codegen then attacks the 0.46 ms rung floor.
- **Payoff at ~0.5 ms/pass:** coast swap becomes a ~3–5× win (interp 2.93 → compiled ~0.5–0.9 ms with the plant's 2nd pass); fill scan 3.66 → ~1–1.5 ms (with write-tracking commit).
- **One mechanism** ties levers 2–4 together: extend `_TrackedList.written_indices` to scalar writes, then use it to (a) skip the load, (b) flush incrementally, (c) commit incrementally. See lever 2, reframed below.

---

## Remaining levers, ranked

1. **Write-tracking on the plumbing** — **(a) load-skip DONE, (b) incremental flush DONE**
   (session 2, uncommitted; see "What landed — session 2" below). Flush now O(changed); load
   dropped (arrays authoritative between scans). Drove `step_replay` 1.72 → 0.906 ms.
   **(c) commit O(changed) — STILL OPEN and now the top fill lever:** `_commit_tags` walks all
   ~2672 kernel tags every scan to find the ~0–4 changed (State build ≈ 33% of a *fill* scan,
   the biggest fill bucket now). This only affects `step()` (fills), NOT `step_replay()` (the
   coast). To close it, extend write-tracking to *scalar* tag writes — the generated kernel
   writes scalars via `tags[name]=` (setitem, trackable) AND possibly `.update()` — then
   `_commit_tags` sets only the tracked-changed keys and skips the O(2672) diff. Expect fill
   2.85 → ~1.5–2 ms. Medium effort, low risk (soundness is the net). See also lever 3 (lazy
   materialization) which subsumes this for unqueried slab scans.

2. **Compiled coast** — `zoom_accepted` 12.4 s / **48.6%**, the single biggest loop bucket. **Now
   justified on per-scan cost:** the coast runs `step_replay()` = **0.906 ms vs interpreted
   2.92 ms = 3.2×** (NOT the old "~14%" — that compared `step()` w/ SystemState, wrong baseline).
   The live forward coast (let-run/zoom) steps via the **interpreted** runner (`core/runner.py`
   `PLC.step` / fork stepping); swap it to compiled `step_replay()`. **Remaining risk is
   architectural, not per-scan:** the coast uses **folding** (`cyclefold` / `run_until(fold=True)`
   — macro-skips scans through timer dwell) and runs the **harness plant**. A compiled coast must
   preserve fold semantics (fold *reduces scan count*; compiled *reduces per-scan* — they must
   compose, not fight) + plant. This is the memory's lever 4 ("~15 s interpreted floor").

2b. **Rung passes (`step_replay` floor, ~0.85 ms of 0.906).** After load+flush are gone, two rung
   passes (plant + main) dominate `step_replay`. Two sub-levers: **(i) `_TrackedList` → `list`
   subclass** so `__getitem__`/`__iter__`/`__len__` are C-level (today they're Python methods on
   a by-reference wrapper — overhead on every block read in every rung). Copy-on-construct is the
   trap: make the block arrays *persistent* `_TrackedList` (created once, `written_indices.clear()`
   per scan) instead of re-wrapping — this composes with the now-authoritative arrays from
   load-skip. **(ii) codegen** — `_sub_*` pythonic wins (read `render_kernel.py` output). Attacks
   the coast floor directly (more margin than 3.2× if the coast swap needs it).

3. **Lazy slab materialization** (structural, potentially largest fill win). Replay with
   `step_replay()` (1.72 ms, no pmap) and materialize a `SystemState` only when
   `history.at(scan)` is actually called. If the cause walk queries only a fraction of the
   1301 slab scans, this deletes the evolver **and** state build for the rest. **Measure
   first:** instrument what fraction of slab-cached states are ever read (offered, not yet
   done). Subsumes lever 1c (commit) for unqueried scans; riskier (slab becomes lazy).

4. **Codegen** (emitted rung code, ~40% of fills — now the largest fill bucket). `_TrackedList`
   `__getitem__`/`__iter__`/`__len__` add overhead to every block access inside rungs; the
   generated `_sub_*` functions may have pythonic wins. Read `render_kernel.py` output.

5. **Setup (5.65 s, separate from the loop):** `_build_pilot_context` 2.9 s (prove domains)
   + `_prepare_route` 2.7 s. Untouched this session.

6. **`letrun_ejection` cause machinery (6.6 s):** the cause walk / investigation around coast
   ejections. Partly fills (levers 1, 3, 4), partly `_walk_backward`/`chase_cause_roots`
   (memory's `trace_back` / `_counter_done_frontier` memo lever).

---

## Diagnostic instruments (all in repo `scratchpad/burner/`)

| script | purpose |
|---|---|
| `profile_how.py` | full loop, per-event-kind wall-clock + setup breakdown (the loop map above) |
| `diag_fill_cost.py` | total fill wall-clock, compiled-vs-interpreted path split, ms/scan, confirms `reached=True final_scan=2011` |
| `diag_compiled_profile.py` | cProfile of ONLY the in-fill compiled replay-range calls (the fill-bucket breakdown) |
| `diag_change_rate.py` | total tags/mem vs changed-per-scan (structural-sharing sizing: 2672 vs ~0–4) |
| `diag_scan_speed.py` | interpreted vs compiled `step` vs `step_replay` per-scan bench |
| `diag_pass_breakdown.py` | one compiled pass split into load / step_fn / flush / edge (the ~1 ms floor analysis) |
| `diag_blockless.py` | block vs blockless per-scan (10× regression evidence) |
| `diag_derived_fills.py` | per-fill triggering-tag + derived-only-vs-relocating interval classification (lever a inert) |
| `diag_plant_replay.py` | with/without plant pre-pass state diff (plant required) — NOTE: references removed `_invoke_step`, patch it to `_run_kernel_pass` before reuse |

Run any with `PYTHONUTF8=1 uv run python scratchpad/burner/<name>.py` (the `PYTHONUTF8=1` avoids
a cp1252 crash on Unicode arrows in output).

---

## Key code references (`src/pyrung/core/`)

- `compiled_plc.py` — the changed file. `_KernelRuntimeContext` (block-aware, `blocks_live`);
  `CompiledPLC.__init__` builds `_block_pos`; `_commit_tags`/`_commit_memory` (evolver);
  `_load_blocks_tracked`/`_run_kernel_pass`/`_flush_blocks_tracked`; `step()`/`step_replay()`
  (single bracket); `_materialize_replay_state` (fill fast path).
- `runner.py` — `_replay_slab_fill` / `_replay_range` / `_replay_range_compiled` (fills use
  compiled); the interpreted forward path (`step`, fork) that the COAST uses (lever 1 target).
- `kernel.py` — `load_block_from_tags`/`flush_block_to_tags` (O(5725)); `blockless` flag;
  `indirect_block_info`.
- `context.py::commit` — the interpreted evolver commit that #1 mirrored.
- `analysis/causal/history.py` — `_find_last_transition_scan` etc. (what triggers fills from
  the cause walk).
- `analysis/pilot/` — the loop; `steer.py` (zoom/coast), `investigate.py` (letrun_ejection).

## Arc numbers
committed baseline **38.4 s** → evolver fix **26.4 s** → single-bracket **25.4 s** (loop).
Compiled replay scan (`step`) **9.18 → 4.46 → 3.66 ms/scan** across the two committed changes.

Session 2 (uncommitted), per-scan `diag_scan_speed.py`:
- `step_replay()` (coast path): **1.72 → 1.35 (flush) → 0.906 (load-skip) ms** = 3.2× vs interp 2.92.
- `step()` (fill path): **2.53 → 2.083 (flush) → 1.705 (load-skip) ms**.
- burner fill `diag_fill_cost.py`: **3.66 → 3.38 (flush) → 2.85 (load-skip) ms/scan**; loop 25.1 → 24.0 s.
- **Uncommitted working tree**: `src/pyrung/core/compiled_plc.py`, `src/pyrung/core/runner.py`
  (plus pre-existing pilot-file edits unrelated to this arc).
- **Next**: user to choose — commit + wire compiled coast (lever 2), commit + squeeze rungs
  (lever 2b), or commit + stop. Commit only on user request (not done autonomously).
