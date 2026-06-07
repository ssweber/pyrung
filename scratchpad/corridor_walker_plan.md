# Corridor Walker — Living Plan

Companion to `corridor_walker_brief.md` (the original design hypotheses) and
`recovery_mechanisms.md` (the canonical, pluggable mechanism catalog). This file
tracks what's **done** and what's **left**, and sequences the work against the
mechanism catalog. Update the checkboxes as we go.

**One-line status:** Engine built, wired, validated. **Time-folding done** — held
waits jump to the nearest actionable accumulator crossing (dt-knob for timers,
acc-patch for per-scan counters), and pulse-started dwells fold too (dynamic
reaction budget). Walker has real unit tests now. Next up: divergence/causal
**recovery** — and the settled shift to *no BFS fallback* (walker returns a `Path`
or a `Diagnosis`).

---

## Architecture (decided)

`how()` first tries a **corridor walk** on the PLC runner; anything it can't walk
returns `None` and falls back to the existing waypoint/BFS planner (unchanged).

> **Settled (revised end-state):** **no BFS fallback for `how()`** — the walker
> returns a `Path` or a `Diagnosis`; BFS stays only for `always()`/`never()`/
> `reachable_states()`. Today it *still* falls back (transitional); removing the
> fallback is gated on the Diagnosis path (LEFT) covering the cases BFS currently
> catches. **Guiding loop: `simplified()` proposes → the walk executes →
> `cause()` repairs.** Recovery is a **pluggable** interface (each mechanism
> switchable, ordering tunable) — we measure combinations across a program
> library, not commit to one strategy. See `recovery_mechanisms.md`.

**The engine = interpreted best-first search over the *governing* stateful tag's
value graph.** Each value is expanded by a **steer alphabet** (empty-step /
pulse-input); every edge is discovered by *simulation on forks*, so it's sound by
construction (immune to copy/calc/indirect-addressing blindness that defeats static
writer inversion).

**Static analysis is a prior, never correctness-bearing** — it only (1) picks the
governing tag, (2) narrows the steer alphabet to the cone's inputs, (3) sets the
horizon (short for command machines, long when a timer gates the tag).

Files: `src/pyrung/core/analysis/prove/walk.py` (engine),
`src/pyrung/core/runner.py` `_how_via_bfs` (the early walk attempt, ~line 1037).

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
  richest stateful tag that gates it; multi-value tag governs itself. A self-updating
  `calc` whose wrapper op hides the step (`(Step+1)%6`) governs its own corridor
  (`_calc_self_referential`).
- [x] **Steer alphabet** (`_steer_alphabet`) — empty + pulse-each-cone-input, with
  fallback to all external Bool inputs; edge-gated commands use release-then-pulse
  (external inputs are *sticky*, so a clean rising edge needs an explicit release).
- [x] **Time-as-horizon** — a held wait advances timers to the crossing, so "advance
  time" is just an empty steer with a long horizon.
- [x] **Time-folding (tesseract).** Held waits **jump** to the nearest actionable
  accumulator crossing instead of ticking: **timers** ride the dt knob (one real step
  covers N dt-scans); **per-scan counters** are patched forward by `(skip-1)*delta`
  with the jump scan's own `execute` supplying the final increment (phase-kept, since a
  counter is timeless and the dt knob can't move it). The **plateau guard**
  (only-accumulators-moved) is the soundness gate; the actionable-crossing set
  (`_nearest_skip` over read done-bits + acc comparisons) only sets *how far* to jump.
  Emitted as one `({}, scans)` entry recording **real elapsed scans**, replay-verified
  at normal dt. (Was LEFT §1.)
- [x] **Dynamic reaction budget.** The per-steer fold cap became a *reaction* budget
  (consecutive churn scans before a plateau forms), so a pulse that merely *starts* a
  dwell folds too — previously the pulse cap stopped at 6 scans and `how()` fell back to
  BFS. Inert/oscillating pulses still bail fast; the empty steer is effectively
  unbounded. Productive folding always runs to `_EMPTY_CAP` regardless of the budget.
- [x] **`fork()` captures clock + mid-accumulation fraction** — verified: `_frac:` timer
  fraction lives in `.memory` and is carried; a forked runner stays bit-identical across
  20+ scans. The re-fork/replay backbone the recovery layer needs is sound. (Reviewer
  linchpin; backjump via `fork(scan_id)` rests on this.)
- [x] **Interpreted verification** — replay the assembled path on a fresh fork; only
  return valid `Path`s.
- [x] **Wired into `_how_via_bfs`** — tried before kernel compilation; on success it
  never compiles the kernel or runs BFS. Skipped when `avoid` is given (M0).
- [x] **Example conversion** — `examples/packml_bench.py` task converted to the
  resting Advance-flag/even-auto-advance pattern (cf. `examples/task_example.py`).
  `_CurStep` now *rests* at 1/3/5; modulo wrap (`% 6`) keeps it bounded for the prover.
- [x] **Real walker unit tests** — `tests/core/analysis/test_prove_walk.py`: counter
  acc-patch path up & down (reachability via the walker, exact-crossing landing in a
  handful of real steps, normal-dt replay), pulse-started fold, and churn-budget bail.
  (closes old §10 "real unit tests"; throwaway `scratchpad/walk_spike.py` also removed.)
- [x] **Vacuous-test fix (`f9e128d`)** — `bool(Condition)` now raises `TypeError`,
  root-causing the silently-passing PackML `how()` assertions across the suite; also fixed
  `_scalar_eq` in `waypoints.py`/`absorb.py`. (old §10.)
- [x] PackML baseline + pass-pipeline tests pass; full prove suite green (681) after
  both folding and the cap-lift.

---

## LEFT

The mechanism catalog is **`recovery_mechanisms.md`** (pluggable & composable — each
unit switchable, ordering tunable). This section tracks **status + sequencing** against
it; it does not restate the mechanisms. Status: ✅ done · ◐ partial · ☐ not started.

### Forward — producing the trace  (mostly built)
- ✅ **Corridor walk** — interpreted best-first over the governing value graph.
- ◐ **Steer via `effect()` projected** — `_explore` already fork-and-tests each steer
  (effect-by-simulation); what's missing is the `effect()`-API framing, not the behavior.
- ◐ **Helpful-steer ordering** — `_steer_alphabet` narrows to the cone's inputs but does
  not yet order by `simplified()` of the next waypoint's enabling condition (relevant
  inputs first). *Efficiency* over the existing alphabet, not coverage. → old §6 (symbolic
  candidate narrowing — e.g. `CtrlCmd==N` → the one command — / best-first over the static
  value graph).
- ☐ **Steer-alphabet expressiveness** — *coverage*, distinct from ordering above (and not
  in `recovery_mechanisms.md`). The alphabet is `empty + pulse-one-Bool-input-HIGH`, so a
  corridor needing a move it can't express returns `None` → fallback: ☐ **non-Bool inputs**
  (analog setpoint / Int hold at a probed value), ☐ **drive an input LOW** to enable a
  transition (only edge release-then-pulse exists today), ☐ **multi-input steers** (a
  transition needing two inputs at once). (old §7.)
- ✅ **Time jump at crossings** — see DONE (time-folding + dynamic reaction budget).
- ☐ **Link-aware de-energization** — obey `link=`: follow a needed-false feedback to its
  enable and de-energize the cause; `Physical.on_delay` as the crossing delay; force
  directly only for unlinked/declared-external tags. New track (autoharness). **No strict
  mode** — every forced tag is a visible, audited assumption; `unlink=[...]` models the
  broken-sensor fault scenario. (Generalizes old §2 "force feedbacks"; one way to drive a
  feedback LOW, complementing §7's alphabet expressiveness.)

### Factoring — decompose before walking  (☐ not started)
- ☐ Read the synchronization structure (narrow dependency-graph cuts → producer/consumer
  partial order over phase transitions). It's read from the graph, not constructed.
- ☐ Linearize the DAG; solve corridors in producer-consumer order. Cyclic residue →
  Convergence. → old §3 (nested/prerequisite corridors) is the single-edge special case.
  Its *dynamic* form — when no steer advances the governing tag, discover what gates *any*
  transition and recursively **sub-walk** to it first (multi-governing-tag) — is
  constructive regression at governing-tag granularity (see Recovery); Factoring is the
  static, read-it-from-the-graph counterpart.

### Divergence detection  (☐ not started)
- ☐ **Value-graph distance** — backward BFS over the governing value graph = exact
  hop-distance to goal; distance-up ⇒ divergence. We build the forward graph already; add
  the backward distance pass.
- ☐ **Must-stay violation** — assert held waypoints across the suffix, not just the final
  value. This is the old `verify`-checks-only-final-value gap (old §4 / brief A5).
- ☐ **Run at commit, not just exploration** — today the walk finds a path on forks but
  *commit/execution* doesn't actively guard against drift; the distance/must-stay checks
  must run while committing, watching each node's off-corridor out-edges. (old §2.)
- ☐ **Deadline-race jump** — the time-jump already lands on the nearest crossing across
  *all* accumulators (DONE); once a divergence deadline is known, race it against the
  target crossing (jump to whichever fires first). The crossing math is built — this just
  adds the deadline as a competing crossing. (old §1 / brief A3.)

### Diagnosis  (☐ not started — depends on causal-API work)
- ☐ **Trigger/enabler split via `cause()`** — recorded mode at full ScanLog fidelity
  (trigger = what transitioned, e.g. a deadline; enabler = what was already wrong).
- ☐ **`effect()`-confirmed minimal cause** (fork-and-test each enabler); ☐ **Deadline
  extraction** (timer Done → annotate with preset: "establish enabler within N scans").
- ⚠️ **Prerequisite:** projected `cause`/`effect` are Latch/Reset/Out-only — copy/calc/fill
  blind (`_rung_writes_value_when_enabled` projected.py:41; `_infer_written_value`
  projected.py:537). Needed for state-machine/computed tags; own change with own tests,
  real blast radius (DAP, fuzz). (old §9.)

### Recovery — acting on the diagnosis  (☐ not started)
- ☐ **Alternatives-stack** — `Or`-goal: try the next term before any repair. *Cheapest
  recovery and the first dent in no-BFS-fallback.* Needs `_target_tag_value` to decompose
  `Or`/`And` goals (today multi-condition / `Or` targets → `None` → fallback).
- ☐ **Precondition accumulation** (monotonic set ⇒ no oscillation, termination; brief A4),
  ☐ **backjump to cause origin** (`fork(scan_id)` checkpoints; reuse `_find_backjump_target`,
  waypoints.py:2162), ☐ **constructive regression** (recurse `simplified()` to inputs —
  stops at inputs, not first-unobserved tag; *may be flag-gated — the interpreted walk
  doesn't strictly need full input-chain naming, only a static path would*), ☐ **inverse
  regression** (the make-**false** path: break a seal-in / hold / satisfy a reset —
  distinct leaves), ☐ **`unlink=` fault override**.
- **Backtracking infra these need (reviewer):** ☐ third `_explore` exit
  (reached-governing-but-diverged, carrying the `cause()` payload — today: success or
  `None`); ☐ `seen` keyed on `(value, precondition_state)` not value alone (else a
  re-walk can't re-enter a visited value and the precondition set can't converge); ☐ keep
  failed forks alive (each `_Node`'s fork *is* the checkpoint — retain parent pointers
  instead of dropping on `popleft`).

### Convergence — multi-corridor  (☐ not started; new scope)
Runs *above* the per-corridor layer: corridors each individually solvable but not jointly
satisfiable at their sync points within deadlines — the single-corridor mechanisms are
blind to it. ☐ convergence diagnosis (relative timing across a sync edge: "producer reaches
P in 40 scans, consumer's deadline is 30"), ☐ divest-as-sync-edge (a divest landing on a
narrow interface *is* a convergence constraint), ☐ reschedule (a different linearization,
not a precondition fix), ☐ co-advance cyclic synchronization (SCC of subsystems). See
`recovery_mechanisms.md` §Convergence.

### Termination & failure  (☐ not started)
- ☐ **Spin guard** — unchanged precondition set + identical checkpoint + still failing ⇒
  stop, report the contradiction (per-corridor); for multi-corridor, "individually solved
  but convergence infeasible after rescheduling" ⇒ a coordination contradiction.
- ☐ **Diagnosis as output** — return `Diagnosis` (preconditions tried, contradicting
  enablers, actionable blockers; multi-corridor: subsystems, where they couldn't align,
  the too-slow producer), **not** Intractable/NotFound. Reuse
  `CausalChain(mode='unreachable', blockers=[...])` (models.py:42). Add `Unsolvable(cert)`
  (order-independent contradiction, checkable no-good) vs `NotFound(reasons)` (exhausted
  budget — never "proven impossible"). Recognize transient/never-rests
  (`_stable_step_values`, waypoints.py:1449) → "unreachable: transient" instead of silent
  fall-through (what silently bit `_CurStep==5` before the example fix). `how()` returns
  `Path(reachable, reason)` today (graph.py:430) — new types/wiring needed. (old §4.)

### Still-open odds & ends
- ☐ **`avoid=` support** — walk is skipped when `avoid` is given (M0); add avoid-state
  pruning to exploration. (old §8)
- ☐ **Cheap trial** — `with plc.trial():` snapshot/restore instead of `fork()`-per-candidate
  (fork is ~ms; lookahead does many). (old §6)
- ☐ Decide: walker REPLACES `_try_waypoint_plan` for copy-coupled targets, or stays a
  first-attempt layer — mooted once the BFS fallback is removed. (old §10)

### Superseded / deliberately dropped
- ~~**Planner B as a per-segment scoped-BFS fallback** (old §5)~~ — killed by the settled
  *no BFS fallback for `how()`*. A stuck hop is handled by the Recovery layer (constructive
  regression / backjump on the learned precondition set), not by firing a constrained
  scoped BFS. BFS stays only for `always`/`never`/`reachable_states`.
- ~~**Split-horizon cap** (empty = long horizon / pulse = short)~~ — replaced by the
  **dynamic reaction budget** (DONE): a pulse that *starts* a dwell now folds instead of
  hitting a short cap. The old fix bought 37 s → 4 s on the task corridor; the budget
  subsumes it and also lifts pulse-started dwells.

---

## Findings (so we don't re-derive)

- **`fork()` is a true checkpoint** — carries `.tags`, `.memory` (incl. `_frac:` timer
  fraction), time mode, dt, and RTC. Verified bit-identical continuation across 20+ scans
  after a mid-fraction fork. Backjump via `fork(scan_id)` (runner.py) rests on this.
- **Corridor source is NOT the existing waypoint front-half.** `_order_waypoints`
  collapses the StateCurrent↔StateRequested↔StateEnableYes SCC into one cone-21
  mega-waypoint (> `_MEGA_CONE_LIMIT=18`), so today's `how(EXECUTE)` falls straight to
  the OOM-prone undecomposed BFS. And `_build_value_transitions("StateCurrent")` is
  **empty** because StateCurrent is `copy`-written. The real graph comes from chasing
  the copy coupling (or, generally, interpreted probing).
- **Steering can't use projected `cause()`/`effect()`.** Projected `cause` is single-hop
  and **copy/calc-blind** (Latch/Reset/Out only) → degenerate empty chains on the mode
  machine; projected `effect` is a single hypothetical scan. The interpreted runner is a
  strictly more faithful forward oracle → lookahead. `cause()` is reserved for the
  *backward* (divergence) direction, where recorded mode already works.
- **`events.py` crossing scheduler is welded to the BFS** (`_ExploreContext` +
  `ReplayKernel` + absorption pipeline) — not runner-consumable wholesale. The walker
  re-derived the held-wait crossing *arithmetic* from timer/counter instruction
  introspection (`.accumulator`/`.preset`/`.done_bit`), which is the simple slice (no
  co-firing / input-variant / abstract-threshold branching). → time-folding (now DONE).
- **External inputs are sticky** (hold last value); `patch()` clears the patch, not the
  tag. Edge-gated commands need release-then-pulse.
- **Verification is interpreted** (replay on a fresh fork) — no compiled-kernel
  agreement risk for the walk path.

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay→6 | go/no-go |
| `_CurStep==5` from EXECUTE | task timer wait | empty (folded) | walk, replay→5 | now **folded** via dt-knob (was ticked); old BFS = wrong "unreachable" |
| counter dwell 0→1 (synthetic) | per-scan counter | empty + pulse | folds via acc-patch | `test_prove_walk` — up & down, exact landing, replay-verified |
| `_CurStep==5` from cold/STOPPED | nested | — | None → fallback | needs Factoring / prerequisite corridors |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | — | None → fallback | cold-start start-value not in graph |
