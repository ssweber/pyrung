# Multi-target `how(A, B)` — plan

**Status:** **LANDED 2026-07-01.** `make test` 4621 passed, 5 xfailed (was 6 —
multi-target marker removed). Engine: `pilot/multitarget.py` (∀ ME classifier +
ordering) + `_pilot_how_multi` in `pilot.py` (sequential per-target drive on one
fork, per-target `_prepare_route` for route discipline). Tests:
`tests/core/analysis/test_pilot_multitarget.py` (5, all green) + the un-xfailed
`test_how_multiple_conditions_and`. Residual completeness items (below) still
open; MVP scope = latched/state/`tag==value` targets.
**The gap:** the single live `pilot:` xfail —
`tests/core/analysis/test_graph.py::TestPLCHow::test_how_multiple_conditions_and`
(`pilot: single-target only`, raises `ValueError`). `_parse_target`
(`pilot.py:1543`) hard-rejects more than one condition. Everything else in the
old survey (`scratchpad/pilot-xfails.md`) has closed; this is the last one.

**Goal:** `how(A, B, …)` reaches a single committed scan where every target
holds — or returns an honest `reachable=False` naming *why* they can't coexist.

---

## Mental model (how a PLC engineer does it)

Not "solve the conjunction A ∧ B" — machines are sequential, so:

1. **Look at both targets and trace them.** The charts (trace) tell you the
   dependency structure: does one target's prerequisite tree *contain* the
   other (ordering)? does establishing one *drive the other off* (preserve
   conflict)? are they independent cones (compose freely)?
2. **Order by what the charts say** — not trial-and-error reorder.
3. **Then keep checking readiness:** "can I work on the other target yet? no…
   yes, now I can — do it, while holding the first." The compass already
   re-points every scan, so a blocked target becomes workable the moment its
   prereq/state lands, and the loop folds it in.

This lives entirely in the **compass/trace layer**. The drive loop shape
(Compass → Act → Verify → Investigate) does not change — it just carries N
bearings and lets trace gate which one is workable each scan.

---

## Evidence — four conveyor pairs (`examples/click_conveyor.py`)

Reproduce: `PYTHONPATH=. uv run python scratchpad/multi_target/mt_probe.py`
(trees) and `…/mt_preserve_proto.py` (resting-preserve prototype). Ran against
the real `trace_back`.

| Pair | Targets | Verdict | What the trace showed |
|---|---|---|---|
| 1 | `ConveyorMotor` + `DiverterCmd` | reachable | Union of leaves consistent; **but** independent traces picked conflicting OR-arms — Motor's `Running` latch chose `Auto`, Diverter chose `Manual` (Wrinkle A). |
| 2 | `State==IDLE` + `State==SORTING` | unreachable | Same tag, two values — trivially exclusive; no tracing needed. |
| 3 | `IsLarge` + `State==IDLE` | unreachable | **Share zero steerable leaves**, yet ME. Conflict lives in the tree interior (`IsLarge` requires `State=1`); invisible to a leaf-union check (Wrinkle B). |
| 4 | `ConveyorMotor` + `State==SORTING` | reachable-with-ordering | Disjoint cones sharing `EstopOK`; union satisfiable. |

**Per-target preserve already works:** Motor's tree surfaces `StopBtn=True`
(negation of `~StopBtn → reset(Running)`) — `_preserve_children` doing its job
inside one target.

**Prototype result (Pair 3, the load-bearing check):** running preserve on the
*resting* value `State==IDLE` surfaces `EntrySensor=False`; that collides with
`IsLarge`'s AND-required `EntrySensor=True`. Control (`ConveyorMotor` + hold
`State==IDLE`) shows **no** collision — correct, that pair is reachable. So the
ME signal is real and the scan discriminates.

---

## Design — phased

Scope v1: conjunction of **latched / state / `tag==value`** Bool+Int targets.
Deferred (state it in the reason string): co-timed momentary pulses (same-scan
simultaneity), OR-of-conjunctions goals, relational multi-target.

### Phase 0 — target list + trivial same-tag ME  *(small)*
- `_parse_target` (`pilot.py:1543`) returns `list[(tag, value, predicate)]`;
  drop the `len != 1` raise. `how(A, B)` and comma-conjuncts both flow in.
- `_PilotContext` carries `targets`; `target_reached` (`pilot.py:810`) → `all(...)`.
- Static pre-check: two targets `tag==v1`, `tag==v2` on one scalar register with
  `v1 != v2` → `reachable=False, reason="State is one register; 0 and 2 can't
  both hold"`. No tracing (Pair 2).
- **Accept:** the xfail parses; Pair 2 returns honest unreachable.

### Phase 1 — per-target trace + sibling route biasing (Wrinkle A)  *(small, existing hook)*
- Trace each target → tree. Collect each target's forced steerable inputs.
- When a target has a free OR-arm, feed siblings' forced inputs as `via_pred`
  so arms agree (Motor picks `Manual` to match Diverter, not `Auto`). Uses the
  existing `via_pred` kwarg on `trace_back` / `_env_for` — the `via=` machinery.
- **Accept:** Pair 1 traces to a consistent mode; union satisfiable and
  selector-legal.

### Phase 2 — resting-value preserve (Wrinkle B primitive)  *(PROVEN in prototype)*
- `trace.py:1358` — for a tag in the **must-hold** (sibling-target) set, don't
  return the bare `satisfied=True` short-circuit; fall through to run preserve
  on the resting value.
- Factor `_preserve_children` (`trace.py:1567`) to run without an `establish_ri`:
  drop the `ri == establish_ri` skip and the `ote_writes` gate; replace with a
  register-held / retentive test (is this tag a held register vs a recomputed
  OTE coil?).
- **Accept:** engine reproduces the prototype — holding `State==IDLE` surfaces
  `EntrySensor=False`; a resting OTE coil surfaces nothing.

### Phase 3 — consistency + ordering classifier (Wrinkle B verdict)  *(the hard part)*
- For a candidate ordering "establish A while holding B", union A's
  establish-tree + B's resting-preserve-tree; scan for a steerable tag forced to
  two values along **AND-required** paths (OR-arms are alternatives — a single
  target legitimately carries both `EntrySensor=True` establish and
  `EntrySensor=False` preserve across arms, so "any two opposite leaves" is too
  naive).
- Ordering falls out of `TraceNode.same_tag_chains()` (already computes "reach v2
  needs v1 first") generalized to cross-tree containment (B's tag in A's interior
  ⇒ order / preserve).
- **Verdict:** an ordering (establish sequence + per-step preserve sets) the
  drive loop executes, or `reachable=False` naming the colliding tag.
- **Static read ONLY** — sound ME prune, else fail-open to the drive loop; never
  a sandbox/forward-sim fallback (see "Resolving Phase 3" below).

### Phase 4 — drive-loop integration  *(wiring)*
- Loop carries N bearings; each scan trace gates which unsatisfied target is
  *workable* (steerable frontier or self-advancing coast under held state).
  Landed targets join the preserve set via the Phase-2 machinery. Done when
  `all(target_reached)` in one committed state.
- Compass distance = sum of per-target `unsatisfied_count()` — a **progress
  monitor**, not the primary driver (driver is trace-ordered readiness, per the
  "bearing not route" rule; a summed gradient invites the wander the pilot
  CLAUDE.md warns against).
- Replay oracle: returned `Path` replays to every target True.

---

## Resolving Phase 3 — static read ONLY (no sandbox)

**Rule: the ME classifier reads the program statically; it never falls back to a
forward-sim / sandbox probe.** A fallback is a pressure-relief valve — keep it
and trace never has to get better; remove it and every UNKNOWN becomes a bug
report against the static reader. This is not a new stance: pilot's CLAUDE.md
already keeps `sandbox` **unwired** and states the boundary gate outright — *"if
a change makes it look needed, the bug is in trace's writer selection."* So
"static read only" is the existing acceptance rule, applied here.

**Why no sim is needed — the drive loop *is* the execution truth.** The worry
that "establish B while holding A" is state-dependent (a from-cold trace can't
see the post-A state) dissolves: you don't *predict* the post-A state, the drive
loop *establishes A for real* and the static reader re-reads B from the **real
committed snapshot** — pilot's existing *state-consistent writer selection*
invariant (`trace` reads `fork.state.tags` each scan, compass re-points from the
real state). The loop's forward progress replaces the probe; every read on top
of it stays static. Sandbox was a redundant second execution path.

**The honest split — sound prune, complete-by-loop:**
- **Static reader (trace)** owns ME-pruning + ordering and must be *sound*:
  declare ME only via a provable **mutual retentive clobber** — each target's
  establishing writer sits in the other's preserve clobber-set (the Phase-2
  primitive, run both directions), backed by the same-rung **co-write** read
  (the sole writer of `State==IDLE` is the RESETTING rung, which also
  `reset(IsLarge)` — one rung's write-set, no cycle). Retentivity filter is the
  existing `ote_writes`/`_can_produce` gate: a non-retentive (OTE/self-clearing)
  consequence is *not* a clobber, so pulse-then-hold is correctly **not** ME.
- **When it can't prove ME or an order → decline to prune (fail-open).** Not a
  guess, not a sandbox — just "don't block; let the loop attempt it." A false ME
  is thus impossible (prune is sound); an un-pruned true ME merely makes the loop
  wander and hit budget — **and that wander is the bug report** (bounded by
  `progress.py`/checkpoints/`max_scans`), pointing straight at the trace gap to
  fix.

**Even the "hard" cases are static, just harder static:**
- *Liveness windows* (IsLarge lives only DETECTING..RESETTING) — a BFS over the
  state-transition graph, which `compass.py`/`evidence.py` already build for the
  opaque-constant value graph.
- *Genuinely runtime logic* (mask over a live word, computed index) — trace
  already returns UNKNOWN and refuses to fabricate; fail-open to the loop is the
  correct answer, and there is nothing to "teach" for truly-runtime logic.

**Scope v1** to latched/state/`tag==value` targets (non-transient establish) —
not because momentary-pulse conjunctions need sim, but because they need the
same-scan-simultaneity model that is deferred anyway. Phases 0–2 are mechanical;
Phase 3's effort is making the mutual-retentive-clobber read *sound and as
complete as the static charts allow* — every gap found is a trace improvement,
never a new escape hatch.

---

## Code seams
- `pilot.py:1543` `_parse_target` — list return, drop `len != 1`.
- `pilot.py:810` `target_reached` — `all(...)`.
- `trace.py:1358` — must-hold tags skip the satisfied short-circuit.
- `trace.py:1567` `_preserve_children` — run without `establish_ri`.
- `trace_back` / `_env_for` `via_pred` — sibling route biasing (exists).
- `TraceNode.same_tag_chains()` (`trace.py:252`) — ordering, generalize cross-tree.

## Test matrix (conveyor pairs become fixtures)
- Pair 1 → reachable, replays both True, consistent mode.
- Pair 2 → unreachable, same-tag reason.
- Pair 3 → unreachable, `EntrySensor`/`State` collision reason.
- Pair 4 → reachable, replays both.
- Control (Motor + `State==IDLE`) → reachable (guards against Phase-3 over-flag).
- `test_how_multiple_conditions_and` → **xpasses** (gap closed).

## Reproduction artifacts
- `scratchpad/multi_target/mt_probe.py` — four-pair trace dump.
- `scratchpad/multi_target/mt_preserve_proto.py` — resting-preserve + contradiction scan.
- `scratchpad/multi_target/spike_multitarget.py` — the static ME classifier spike (∃).
- `scratchpad/multi_target/spike_forall.py` — route-universal (∀) spike + adversarial fixture.

---

## Spike — feasibility CONFIRMED (2026-07-01)

`spike_multitarget.py` implements the whole static classifier standalone
(same-tag pre-check + mutual retentive clobber) over the real `trace_back`, and
gets **all five cases right with no sim**:

| Pair | Verdict | Evidence |
|---|---|---|
| 1 Motor+Diverter | REACHABLE (compose) | no clobber either direction |
| 2 IDLE+SORTING | UNREACHABLE | same register State, two values |
| 3 IsLarge+IDLE | UNREACHABLE | `R6 writes State=1` ∧ `R12 writes IsLarge=False` (mutual retentive clobber) |
| 4 Motor+SORTING | REACHABLE (compose) | no clobber either direction |
| control Motor+IDLE | REACHABLE (compose) | no clobber either direction |

**What the spike proves.** The mechanism is real and static. `clobber(X→Y)`:
`relevant_writers(X)` = rungs on X's establish-trace ∪ X's producers; ME iff a
relevant writer of X writes Y's tag off-value **retentively** (`_can_produce`
False ∧ tag ∉ `ote_writes`), in **both** directions. The producers-union is what
made the resting/second-ordering direction visible with no sim — IDLE is
satisfied at cold, but its producer R12 (the RESETTING rung) also
`reset(IsLarge)`. The retentivity filter is the discriminator: an OTE/self-
clearing consequence is not a clobber, so pulse-then-hold is correctly not ME.

### ∃ → ∀ over routes — DONE in spike (`spike_forall.py`, 2026-07-01)

v1's `clobber` was existential ("*some* relevant writer of X clobbers Y"), which
over-prunes: a target with an alternative route that dodges the clobber is not
ME. The sound shape is **route-universal** — "*every* way to establish X clobbers
Y." `spike_forall.py` enumerates routes per-producer (forces each via
`writer_locks={(tag,val): ri}`) and computes ∀-over-routes of ∃-within-path.

Adversarial fixture (`Z` reachable while holding `Stage==PARKED`): `Z` has two
routes — R1 auto-latch in RUNNING (path drives `Stage` off PARKED) and R2 manual
latch (clean). Result:

| | `Z→Stage` | `Stage→Z` | verdict |
|---|---|---|---|
| **EXISTS** | R1 CLOBBERS | R3 CLOBBERS | UNREACHABLE ✗ (over-prune) |
| **ALL** | R1 clobbers, **R2 clean** | R3 CLOBBERS | REACHABLE ✓ |

And the conveyor is **unregressed** under ALL — Pair 3 stays UNREACHABLE (its
single route genuinely clobbers both ways); Pairs 1/4/control stay REACHABLE.
So ∀-over-enumerated-routes is the sound core, and it's fully static (writer-lock
forcing + trace, no sim).

### Residual refinements (all fail-SAFE toward fail-open)
The core is sound; these tighten *completeness*, and each defaults to "don't
prune" when unresolved — so none can introduce a false ME:
1. **Internal-OR routes** — route enumeration is per-*producer* (top-level writer
   choice); a dodge hidden in an OR arm *inside* a producer's guard isn't yet
   enumerated (trace picks one arm). Full coverage folds in
   `enumerate_trace_choices`. Unknown arm ⇒ treat as possible dodge ⇒ don't prune.
2. **Guard reachability** — a "clean" route only dodges if its guard is
   co-satisfiable while the sibling holds; filter producers by state-consistent
   reachability (ties to pilot's state-consistent-writer invariant).
3. **Cold-only producers** — if B has no rung producer and A can't hold at cold,
   `clobber_ALL` is vacuously true; that's a real block but a *different* reason
   ("B not re-establishable while holding A") — surface it explicitly.
4. **UNKNOWN ⇒ clean/dodge** — bias every unresolved clobber status toward
   "clean" so the prune stays sound (false-ME impossible; residue fails open).

**Cosmetic:** unwrap `Literal(value=…)` in evidence.

**Not spiked (mechanical, low-risk):** Phase 0 list-parse + `all(target_reached)`,
Phase 1 `via_pred` biasing. Left for implementation.
