# PILOT route-choice tracking + `via=` — context for a fresh conversation

Two intertwined questions about `how()` / `pilot_how()` route selection:

1. **Route-choice backtracking (NOT value search):** when a Bool target has
   several output routes, how do we *commit to one route, remember we tried it,
   and fall back to another* if it dead-ends — as opposed to the existing
   value-step nogood search?
2. **API surface:** can we let a user say "reach Goal **via** this pivot" easily —
   either through the existing `choice=`, or a new `via=` (the complement of
   `avoid=`)?

Target xfail: `tests/core/analysis/test_pilot_examples.py::test_running_route_ambiguous_resolves`
(`pilot: route-ambiguous single-target resolution`). `Running` in
`examples.click_conveyor` has multiple writers (one latch, two NC resets);
`pilot_how(plc, Running)` should resolve to the latch route on its own instead
of reporting ambiguous.

---

## What already exists (just landed — build on this)

The `avoid=` half of route filtering is done (commit `c8d971b`):

- **`trace.py::_route_forces(nodes, snapshot, pred)`** — overlays a route's
  concrete `(tag, value)` demands onto the snapshot and tests a predicate.
  Shared by (a) the OR-arm avoid-skip in `_trace_expression` and (b) route
  pruning below. This is the reusable primitive for *both* `avoid=` and `via=`.
- **`trace.py::enumerate_trace_choices(tag, value, snapshot, pdg, program)`** —
  returns `()` if ≤1 route, else ≥2 `TraceChoice`. Never returns exactly 1.
- **`trace.py::TraceChoice`** — `id, label, route, writer_locks, or_locks`. A
  choice locks one writer-rung + one OR-arm per OR; `trace_back(..., choice=ch)`
  re-traces everything below the locked decision.
- **`pilot.py::_prepare_trace_choice(...)`** — runs at each entry point *before*
  the loop. Now:
  - `enumerate_trace_choices(...)` → route set.
  - if `avoid_pred`: prune routes where `_route_forces(route_tree, snap, avoid)`.
  - `if len(choices) == 1: use it` else `if choice is None: return _ambiguous_path`.
  - returns `(early_path, trace_choice, blocked_choice_actions)`; non-None
    `early_path` makes the caller return immediately.
- **`pilot.py::_exclusive_choice_actions(...)`** → `blocked_choice_actions`: once
  a route is committed, the *other* routes' exclusive actions are blocked so the
  loop can't drift onto them.
- **`_TraceEnv`** (frozen) now carries `avoid_pred`; `pilot_how`/`pilot_drive`
  thread it; `verify.py` re-trace gets it too.

Key code pointers: `pilot.py` `_resolve_trace_choice` (~1102), `_ambiguous_path`
(~1120), `_exclusive_choice_actions` (~1135), `_prepare_trace_choice` (~1178),
entry points `pilot_events`/`pilot_how`/`pilot_drive`. `Path.ambiguous` is a
property = `bool(self.choices)` (`graph.py:676`).

---

## The hard constraint (do not regress)

`tests/core/analysis/test_pilot.py:113` asserts the *ambiguous* behaviour:
`pilot_how(plc, Burner)` (no `choice`, 2 routes ProdMode/MaintMode) →
`ambiguous`, `not reachable`, `len(choices) == 2`, labels surfaced. So "just
auto-pick the first of many" is **not** free — surfacing choices to the user is
a documented feature. Auto-resolution among >1 routes must either (a) keep
surfacing choices while *also* trying one, or (b) change that contract
deliberately (and update the Burner test).

---

## Question 1 — route-choice backtracking (≠ value search)

The user's framing: *"track what we have tried (not search, but choice of
path) and then try the other way."*

Today PILOT tracks two things, **neither of which is route choice**:
- `_PilotState.nogoods: dict[_StateKey, set[_ActionPair]]` — per-state-key bad
  *actions* (value-step search).
- `_PilotState.seen_keys`, `checkpoints`, `letrun_tried`, `regression_nogoods` —
  trend/checkpoint/coast bookkeeping.

A committed `choice` (TraceChoice) is currently **fixed for the whole run** — set
once in `_prepare_trace_choice`, threaded into `_PilotContext.choice`, never
revisited. There is no "this route dead-ended, revert and try the next route."

**Sketch:** lift route choice into a backtracking loop *outside* the value
search:
- Enumerate routes once (already do). Order them (cheapest first — reuse
  `_trace_score`, or latch-route-first heuristic for the conveyor case).
- Try route[0]: snapshot an entry checkpoint, run the existing loop with
  `choice=route[0]` + its `blocked_choice_actions`.
- On a **DEAD-END / no-progress terminal** (distinct from "still working"),
  revert to the entry checkpoint, mark `route[0]` tried, advance to route[1].
- Bounded by `len(routes)` — this is backtracking over a *small finite* route
  set, not the unbounded value frontier. Track `tried_routes: list[TraceChoice]`
  (or a `set` of choice ids) at the loop-driver level (e.g. a thin wrapper in
  `pilot_how` around `_pilot_loop`, or a new `_PilotState.tried_routes`).

Open questions:
- What signal cleanly means "this route is dead" vs "needs more scans"? (look at
  the finished-event `reason` taxonomy / outcome classifier — `outcome.py`,
  `verify.py` DEAD-END gate). Must not backtrack on a recoverable excursion.
- Does the value-search nogood state need resetting per route attempt, or is the
  entry-checkpoint revert enough?
- Interaction with `blocked_choice_actions`: when retrying route[1], unblock
  route[0]'s exclusives and block route[1]'s.

---

## Question 2 — `via=` API (avoid-complement) and/or `choice=`

The user wants "End Goal + this pivot" to be easy, without knowing the opaque
positional `choice=N`.

- **`how(Goal, via=Pivot)`** = the exact complement of `avoid=`. Where `avoid`
  *prunes* routes that `_route_forces(...)`, `via` *keeps only* routes that
  `_route_forces(route, snap, via_pred)`. Same primitive, opposite filter — drop
  it straight into `_prepare_trace_choice` next to the avoid prune. If exactly
  one route forces the pivot → use it (no `choice=` needed). Symmetry is clean:
  `avoid` = "no route through X", `via` = "a route through X".
- `via` could *also* bias OR-arm selection inside the trace (prefer the arm that
  forces the pivot), mirroring the avoid-skip in `_trace_expression` — but the
  route-prune at `_prepare_trace_choice` likely suffices for the Bool-output
  case and is where `enumerate_trace_choices` already lives.
- Relationship to `choice=`: `choice=N` selects by enumerated index/id/label;
  `via=Pivot` selects by *semantic waypoint*. They can coexist — `via` is sugar
  that resolves to a `TraceChoice` (or narrows the set, then `choice`/auto-pick
  disambiguates the remainder).
- API plumbing parallels `avoid`: `runner.py::how(avoid=...)` (~1047) →
  `_how_via_pilot` builds `avoid_pred` via `_compile_property(*conds)` → threads
  to `pilot_how`. A `via_pred` would follow the identical path.

Decision to make: is `via` a **hard** constraint (unreachable if no route forces
the pivot) or a **preference** (bias, fall back to other routes)? `avoid` is
hard; `via` as hard is the clean dual, but "End Goal + this pivot as a hint"
suggests the user may want soft. This ties back to Q1's fallback: a soft `via`
is "try the via-route first, backtrack to others."

---

## Suggested order of attack

1. **`via=` (hard, route-prune)** — smallest, reuses `_route_forces`,
   immediately useful, mirror of `avoid`. Likely makes
   `test_running_route_ambiguous_resolves` pass if the conveyor's latch route is
   expressible as `via=<latch enabler>`, *without* touching the Burner contract.
2. **Auto-resolution heuristic** for the no-`via`/no-`choice` case (latch-route
   preference) — but only if it can coexist with surfacing choices (Q1 / Burner
   constraint).
3. **Route backtracking (Q1)** — the general fallback; larger, needs the
   dead-end signal and per-route checkpoint/nogood discipline.
