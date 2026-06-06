# Corridor Walker — Living Plan

Companion to `corridor_walker_brief.md` (the original design hypotheses). This file
tracks what the brief got right/wrong against the actual code, what's **done**, and
what's **left**. Update the checkboxes as we go.

**One-line status:** Milestone 0 + the unified a→e corridor engine are built, wired,
and validated on two corridor types (mode-machine input steers; task timer waits).
Next up: tesseracting the timer waits, then divergence/causal recovery.

---

## Architecture (decided)

`how()` first tries a **corridor walk** on the PLC runner; anything it can't walk
returns `None` and falls back to the existing waypoint/BFS planner (unchanged).

**The engine = interpreted best-first search over the *governing* stateful tag's
value graph.** Each value is expanded by a **steer alphabet** (empty-step /
pulse-input); every edge is discovered by *simulation on forks*, so it's sound by
construction (immune to copy/calc/indirect-addressing blindness that defeats static
writer inversion).

**Static analysis is a prior, never correctness-bearing** — it only (1) picks the
governing tag, (2) narrows the steer alphabet to the cone's inputs, (3) sets the
horizon (short for command machines, long when a timer gates the tag).

Files: `src/pyrung/core/analysis/prove/walk.py` (engine),
`src/pyrung/core/runner.py` `_how_via_bfs` (the early walk attempt, ~line 1033).

---

## DONE

- [x] **Reconnaissance** — confirmed/refuted every brief claim against the code
  (see "Findings" below).
- [x] **Milestone 0: walk a known corridor.** `plan_walk` reaches `StateCurrent==EXECUTE`
  from ABORTED via input-steer corridor; replay-verified to 6.
- [x] **Unified a→e corridor source** — one interpreted best-first engine with static
  priors (governing tag / steer alphabet / horizon). Subsumes direct-literal,
  copy-coupling, copy-chains, counters, and target→governing indirection.
- [x] **Governing-tag selection** (`_governing`) — derived coil delegates to the
  richest stateful tag that gates it; multi-value tag governs itself.
- [x] **Steer alphabet** (`_steer_alphabet`) — empty + pulse-each-cone-input, with
  fallback to all external Bool inputs; edge-gated commands use release-then-pulse
  (external inputs are *sticky*, so a clean rising edge needs an explicit release).
- [x] **Time-as-horizon** — a held wait advances timers to the crossing, so "advance
  time" is just an empty steer with a long horizon. `_horizon` detects timer-gating
  from the governing tag's writer **condition-reads** (not `upstream_slice`, which
  misses condition gates).
- [x] **Split-horizon perf fix** — only the empty steer gets the long horizon; pulses
  act promptly and get the short one (37 s → 4 s on the task corridor).
- [x] **Interpreted verification** — replay the assembled path on a fresh fork; only
  return valid `Path`s.
- [x] **Wired into `_how_via_bfs`** — tried before kernel compilation; on success it
  never compiles the kernel or runs BFS. Skipped when `avoid` is given (M0).
- [x] **Example conversion** — `examples/packml_bench.py` task converted to the
  resting Advance-flag/even-auto-advance pattern (cf. `examples/task_example.py`).
  `_CurStep` now *rests* at 1/3/5 (was a 1→3→5→0 single-scan cascade, never resting).
  Modulo wrap (`% 6`) keeps the counter bounded so the prover stays tractable.
- [x] **Validated:** ABORTED→EXECUTE (input steers, ~2 s); `_CurStep==5` from EXECUTE
  (timer waits, ~4 s) — the latter is a case the **old BFS gets *wrong*** (false
  "unreachable" from the oneshot-calc absorption bug), so the walker is strictly better.
- [x] PackML baseline + pass-pipeline tests pass after the example change.

---

## LEFT

### 1. Tesseract the timer waits  ← next
The empty steer currently **ticks** ~100 scans per 1-s dwell. Replace with a jump:
compute scans-to-crossing from the live accumulator + preset + per-scan delta,
advance the accumulator and sim clock, settle one scan at the crossing.
- [ ] Runner-side crossing math, reusing the *arithmetic* from `events.py`
  (`_scans_until_done_event`, `_advance_hidden_progress`) but driven by **timer/counter
  instruction introspection** (`.accumulator`/`.preset`/`.done_bit`) — NOT the welded
  BFS `_ExploreContext`/`ReplayKernel`.
- [ ] Jump to the **nearest** crossing across *all* relevant accumulators (target + any
  divergence deadline) — this is the same mechanism as the deadline-race (brief A3).
- [ ] Held-input simplification: no co-firing/input-variant/abstract-threshold
  branching (that's BFS-only). The walker's held wait is the simple case.
- [ ] Caveat discipline: must land on *every* crossing any rung reads, never skip one.

### 2. Divergence diagnosis + recovery (causal, brief A4/A5)  ← the "things we didn't patch/force" case
When the walk lands off-corridor because of something we didn't account for (a
feedback that never arrived, an interlock we didn't satisfy, a deadline we lost),
diagnose *why* and recover.
- [ ] **Active divergence monitoring during commit** — watch the per-node divergence
  set (off-corridor out-edges) + "must-stay" states (brief A5: staying in EXECUTE is
  the same mechanism as progress). Today exploration finds a path on forks but commit
  doesn't actively guard against drift.
- [ ] **Recorded `cause()` on divergence** — name the trigger (the deadline/edge) and
  enabler (the dropped precondition). recorded mode already works; this is the
  backward-direction use of cause() the brief intended.
- [ ] **Monotonic precondition set** — add the missing feedback/interlock; only grows
  (guarantees termination).
- [ ] **Backjump via `fork(checkpoint)`** — rewind to the waypoint where the conflicting
  commitment was made (cause chain names where), re-walk with the updated set. Reuse
  `_find_backjump_target` logic (waypoints.py:2162). Checkpoint = the scan_id at each
  hop; `fork(scan_id)` is cheap.
- [ ] Handle preconditions that are **external feedbacks we must force** (not just
  pulse) — the walker discovers it must `force()` a feedback the ladder never drives.

### 3. Nested / prerequisite corridors
Governing tag whose *first* transition needs another governing tag's value first
(e.g. `_CurStep` can't move until `StateCurrent==EXECUTE`). Today this returns `None`
→ fallback (correct but unaccelerated).
- [ ] When no steer advances the governing tag, find the prerequisite (what gates *any*
  transition) and recursively walk to it first (multi-governing-tag / sub-corridor).
- [ ] This is the general form of brief A4's precondition learning.

### 4. Result-type honesty (brief "Result-type honesty")
- [ ] `Unsolvable(certificate)` — true order-independent contradiction (checkable
  no-good set).
- [ ] `NotFound(reasons)` — exhausted budget/beam; never readable as "proven impossible".
- [ ] External-blocker diagnosis distinct from `Intractable` — reuse
  `CausalChain(mode='unreachable', blockers=[...])` (models.py:42).
- [ ] **Transient/pass-through detection** — recognize (like `_stable_step_values`,
  waypoints.py:1449) that a requested value never *rests* and say "unreachable:
  transient" instead of silently falling through. (This is what bit `_CurStep==5`
  before the example fix.)
- [ ] `results.py` has only `Proven/Counterexample/Intractable`; `how()` returns
  `Path(reachable, reason:str)` (graph.py:430) — new types/wiring needed.

### 5. Planner B as per-segment fallback (brief Planner B)
- [ ] Fire the existing scoped BFS (`_run_waypoint_plan`) only on a single stuck hop,
  seeded with `current_state` + the learned precondition set (constrained, not blind).

### 6. Performance
- [ ] **Cheap trial** instead of `fork()`-per-candidate (a `with plc.trial():`
  snapshot/restore on the interpreted runner). fork() is ~ms; the lookahead does many.
- [ ] **Symbolic candidate narrowing** — order/prune steers via the gate (CtrlCmd==N →
  the one command) before falling back to enumeration.
- [ ] Best-first heuristic ordering using the static value graph (when available) to
  reduce expansions.

### 7. Steer-alphabet gaps
- [ ] Non-Bool inputs: analog setpoints / Int holds (hold at a probed value).
- [ ] Inputs that must be set **LOW** to enable a transition.
- [ ] Multi-input steers (a transition needing two inputs at once).

### 8. `avoid=` support
- [ ] The walk is skipped when `avoid` is given (M0). Add avoid-state pruning to
  exploration so `how(..., avoid=...)` can use the walk.

### 9. Causal-API enhancements (separate PRs, reached at result-honesty)
- [ ] **`copy`/`calc`/`fill` support in projected `cause`/`effect`**
  (`_rung_writes_value_when_enabled` projected.py:41; `_infer_written_value`
  projected.py:537 — both Latch/Reset/Out-only today). High general value (fixes
  `cause()`/`effect()`/`recovers()`/projected-`why()` on state machines & computed
  tags) but real blast radius (DAP, fuzz). Do as its own change with its own tests.
- [ ] Constructive (recursive) reachability mode — only if the *static* path needs to
  name full input chains; the interpreted walk doesn't need it. Gate behind a flag.
- [ ] Timer-preset annotation on `cause()` triggers (the deadline number) — small,
  needed at the deadline-race.

### 10. Cleanups / housekeeping
- [x] ~~Existing PackML `how()` tests assert *vacuously*~~ — **fixed in `f9e128d`**
  (`bool(Condition)` now raises `TypeError`, root-causing it across the suite; also
  fixed the `_scalar_eq` comparison in `waypoints.py`/`absorb.py`).
- [ ] Real unit tests for the walker (currently validated by ad-hoc scripts).
- [ ] Remove/keep `scratchpad/walk_spike.py` (throwaway).
- [ ] Decide: does the walker REPLACE `_try_waypoint_plan` for copy-coupled targets, or
  stay a first-attempt layer? (Currently: first-attempt; existing planner unchanged.)

---

## Findings (so we don't re-derive)

- **Corridor source is NOT the existing waypoint front-half.** `_order_waypoints`
  collapses the StateCurrent↔StateRequested↔StateEnableYes SCC into one cone-21
  mega-waypoint (> `_MEGA_CONE_LIMIT=18`), so today's `how(EXECUTE)` falls straight to
  the OOM-prone undecomposed BFS. And `_build_value_transitions("StateCurrent")` is
  **empty** because StateCurrent is `copy`-written. The real graph comes from chasing
  the copy coupling (or, generally, interpreted probing).
- **Steering can't use projected `cause()`/`effect()`.** Projected `cause` is single-hop
  and **copy/calc-blind** (Latch/Reset/Out only) → degenerate empty chains on the mode
  machine. Projected `effect` is a single hypothetical scan. The interpreted runner is
  a strictly more faithful forward oracle → lookahead. `cause()` is reserved for the
  *backward* (divergence) direction, where recorded mode already works.
- **`events.py` crossing scheduler is welded to the BFS** (`_ExploreContext` +
  `ReplayKernel` + absorption pipeline) — not runner-consumable wholesale. But the
  per-accumulator crossing *arithmetic* is reusable, and the walker's held-wait case is
  the simple slice (no branching). → tesseract item #1.
- **External inputs are sticky** (hold last value); `patch()` clears the patch, not the
  tag. Edge-gated commands need release-then-pulse.
- **`fork(scan_id)`** gives cheap checkpoints/re-fork (runner.py:1343) — backjump is
  directly supported.
- **Verification is interpreted** (replay on a fresh fork) — no compiled-kernel
  agreement risk for the walk path.

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay→6 | go/no-go |
| `_CurStep==5` from EXECUTE | task counter | timer waits | walk ~4 s, replay→5 | old BFS = wrong "unreachable" |
| `_CurStep==5` from cold/STOPPED | nested | — | None → fallback | needs prerequisite corridors (#3) |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | — | None → fallback | cold-start start-value not in graph |
