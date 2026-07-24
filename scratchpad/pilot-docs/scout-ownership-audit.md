# Scout report: ownership-model audit (2026-07-23, Opus, read-only)

Brief: verify each claim in pilot/CLAUDE.md "Give each decision one owner" against code;
hunt second decision sites, threading leaks, and rule-to-structure conversion opportunities.

---

## Summary orientation

The producer-level ownership claims in the "Give each decision one owner" table **hold**: I confirmed each named owner is the *sole producer* of its decision. `_rank_writers` is called only inside `trace.py` (2128, 2693); `coast_departure_tags` only in `_ops.py`; `read_program_step` produced only in `program_step.py` and consumed at one site (`options.py:823`); `assess_outcome` has one real caller (`verify.py:609`) plus a legacy shim (`outcome.py:310`); `_ConfirmedCorrection` is built only in `investigate.py` (1129, 1750) and installed only through `progress.py::_install_confirmed_correction`, which validates identity and stores the artifact whole without recompiling (progress.py:947-977). No dual-producer violations were found.

The real weaknesses are **consumption-side**: (a) two facts that already have owning artifacts are unpacked into loose fields and threaded through the whole execution→verify→progress→investigate chain, and (b) one of those threads has a genuine *weaker re-derivation* fallback — the exact failure shape the refactor is hunting. Findings are ranked correctness-risk first.

---

## Finding 1 — Coast/zoom channel motion is unpacked into loose fields and re-interpreted (LEAK + latent VIOLATION)

**Rule:** "Every investigation returns one correction artifact… consumers do not reconstruct that artifact from parallel result fields" and the World-and-knowledge line "the recorded observation the deciders read instead of re-deriving evidence from snapshots" (`_PulseState.coast_receipt` doc, types.py:665-668). Also invariant "Coast predicates decide bump truth… Every reported crossing lands on a real recorded scan."

**Evidence:** The `Coast` navigation act (navigation.py:134-154) and `CoastReceipt` bundle channel + target + boundary + stop_reason. On entry to execution that bundle is unpacked into loose scalars `zoom_channel_tag` / `zoom_target_value` / `channel_target` / `bearing_stop_reason`, then threaded as separate parameters through **~92 sites across 12 modules** (`verify.py` 21, `progress.py` 24, `investigate.py` 14, `outcome.py` 8, `steer.py` 8…). `assess_outcome` receives them as four loose keyword params:

```
outcome.py:152-155  zoom_channel_tag=..., zoom_target_value=..., zoom_progressed=..., zoom_stop_reason=...
```

The latent correctness risk: the coast receipt's precise stop reason has an **owner** — `verify.py::_owned_bearing_stop_reason` (verify.py:59-78) — but `assess_outcome` re-derives the same "did the bearing reach?" verdict from a snapshot when the owned reason is absent:

```
outcome.py:181-183   if zoom_stop_reason is not None:  bearing_reached = zoom_stop_reason == "reached"
outcome.py:185       else:  snapshot_reached = _values_match(chan_actual, zoom_target_value)   # weaker
```

`_owned_bearing_stop_reason` itself also re-does a snapshot equality (verify.py:76) to rebase "departed"→"reached". So "did the coast reach its channel bearing" is decided at two sites; the snapshot path is a weaker approximation of the receipt (it cannot see a *crossing* vs. an *equal* landing, exactly the relational-boundary case the owner's docstring calls out).

**Classification:** threading leak, with a real second-site interpretation that can diverge for relational/crossing channels.

**Fix:** introduce one `ChannelMotion` receipt (channel_tag, target_value, boundary, owned stop_reason) carried on `_AttemptIntent`/`_TrialResult` in place of the four scalars; make `assess_outcome` consume `motion.reached` (a property on that receipt) and delete the snapshot fallback block (outcome.py:184-196).

**Effort:** medium (touches 12 files but mechanically). **LOC delta:** roughly **−40 to −60** net (deletes the fallback re-derivation and collapses repeated 4-scalar signatures to one param).

**Prose deletable afterward:** the `_PulseState.coast_receipt` / `_TrialResult.bearing_stop_reason` explanatory comments (types.py:665-668, 738-743) shrink to one line; the invariant "the recorded observation the deciders read instead of re-deriving evidence from snapshots" becomes structurally true.

---

## Finding 2 — The target triple is threaded as three loose fields; `TargetSpec` receipt exists but is re-built, not consumed (LEAK + structure opportunity)

**Rule:** "Target-relative Bearing objective: `orientation.py::_bearing`; the original `TargetSpec` … travel unchanged through execution and verification" and BearingObjective doc "Verification and recovery carry it unchanged instead of reconstructing a weaker objective from the global context" (navigation.py:64-67).

**Evidence:** `_PilotContext` stores the target as three independent fields (`target_tag`, `target_value`, `target_predicate` — types.py:338-344), and every reader re-unpacks them:

```
steer.py:196, 659, 698, 786;  verify.py:518, 552;  pilot.py:888, 1203
    target_reached(..., ctx.target_tag, ctx.target_value, ctx.target_predicate)
```

`target_reached` itself takes the loose triple (trace.py:1164-1168). Two sites then **rebuild** the `TargetSpec` artifact from those loose fields rather than consuming an existing one:

```
pilot.py:914   target = TargetSpec(ctx.target_tag, ctx.target_value, ctx.target_predicate)
verify.py:361  TargetSpec(ctx.target_tag, ctx.target_value, ctx.target_predicate)   # inside _gate_dead_end
```

`verify.py:361` is the doctrinal "re-derive from global context": the bearing's own `intent.bearing_objective.target` is in scope (attempt.intent) yet a fresh `TargetSpec` is constructed from ctx. It is low divergence risk today (ctx.target is stable within a single-target iteration), but it is precisely the pattern the refactor bans.

**Classification:** threading leak + structure opportunity. Not a live correctness bug (single-target ctx is stable), so ranked below Finding 1.

**Fix:** give `_PilotContext` a single `target: TargetSpec` field (or a cached `.target_spec` property); make `target_reached(spec, snapshot)` take the spec; have `verify.py:361` consume `attempt.intent.bearing_objective.target`.

**Effort:** low-medium. **LOC delta:** roughly **−15 to −25** (collapses ~10 three-arg call sites and deletes two `TargetSpec(...)` rebuilds).

**Prose deletable afterward:** the objective's "travel unchanged through execution and verification" clause reduces to naming the owner; the BearingObjective "reconstructing a weaker objective from the global context" warning becomes unnecessary once ctx exposes the spec as one value.

---

## Finding 3 — `_gate_dead_end` re-traces and re-derives frontier from ctx rather than the objective (LEAK, low risk)

**Rule:** same as Finding 2, plus "Still needed has separate meanings" (soundness invariants).

**Evidence:** `verify.py::_gate_dead_end` (verify.py:301-361) re-runs `trace_back(ctx.target_tag, ctx.target_value, …)` and builds `NavigationEvidence.frontier_status(... TargetSpec(ctx...), NavigationConstraints(ctx...) ...)`. This is a legitimate *post-trial* re-read (doctrine "recompute from the current world"), so it is **not** a violation of the objective receipt — the orientation-time `BearingObjective.frontier` is deliberately not what's wanted here. I flag it only because it participates in Finding 2's loose-field threading: it consumes `ctx.target_*` and `ctx.blocked_route_actions`/`ctx.avoid_pred` as loose fields where a `NavigationConstraints`/`TargetSpec` bundle would read as one.

**Classification:** leak (stylistic); fold into Finding 2's refactor. **Effort:** trivial once Finding 2 lands. **LOC delta:** ~−3.

---

## Finding 4 — `_expr_availability`'s three "still needed" notions are enforced only by a test, not by type (STRUCTURE OPPORTUNITY)

**Rule:** "'Still needed' has separate meanings: `frontier_pairs` … `_writer_projection` … `_expr_availability` …" (soundness invariants) and availability.py:234-237: "agreement among all three is pinned by tests/…/test_pilot_needed_vocabulary.py".

**Evidence:** availability.py is clean on the reject-vs-order rule — `_WriterAvailability` is an `IntEnum` total order (availability.py:35-46) and `_rank_writers` only *orders* by it, keeping every writer that passes `_can_produce` (trace.py:3804 `if not _can_produce(wv, value): continue`; `UNAVAILABLE_FROM_HERE` is a ranking bucket, never a removal). So the invariant "Availability … order; they do not reject" is *already* structurally close. What is *not* structural is the three-way agreement of the "needed" notions — it lives as prose + a golden test.

**Classification:** structure opportunity. Distinguishing the three notions by **distinct return types** (e.g. `FrontierPairs`, `WriterFrontier`, `WriterAvailability` newtypes rather than all surfacing bare `(tag,value)`/tiers) would let the compiler enforce that a consumer of one cannot silently accept another.

**Effort:** medium. **LOC delta:** roughly **+10 (types) / −6 (prose)** — this one *adds* code to buy the guarantee; worth it only if the vocabulary keeps drifting.

**Prose deletable afterward:** the "'Still needed' has separate meanings" bullet (three lines) could reduce to one line naming the three types.

---

## Finding 5 — Positive: several invariants are already structurally enforced; their prose can shrink now (STRUCTURE OPPORTUNITY, doc-only)

These need no code change — only doctrine reduction, which is the maintainers' stated goal:

- **"Hold-log tag summaries are derived from their exact rungs"** — enforced: `_HoldLogEntry.tags` is a derived `@property` over `self.rungs` (types.py:435-438); `_StepContext.steady_holds` likewise derives from `control_rungs` (types.py:404-407). No parallel field exists to desync. Recording consumes the effective-owner receipt (`recording.py:99 _rung_execution_receipt(...).owner(tag)`), not raw guards. → the "consume its effective owner rather than re-evaluating raw guards" clause (CLAUDE.md 128-129) is already true by construction; can shrink to one line.
- **"A terminal coast consumes the same channel-owner set during execution and incident replay"** — enforced: `coast_departure_tags` is the sole arbiter and is consumed verbatim at `steer.py:641` and `progress.py:1257` (investigation replay). → the surrounding paragraph (CLAUDE.md 116-119) can drop its justification sentences.
- **"consumers do not reconstruct that artifact from parallel result fields"** (corrections) — enforced: installer takes `investigation.correction` whole (progress.py:1041, 1334) and rejects forged identity / already-owned rungs (progress.py:947-958); `InvestigationResult.correction` is one artifact and the sibling `confirmed`/`hypotheses` tuples are diagnostic only. → invariant paragraph (CLAUDE.md 262-271) can be halved.

**Effort:** trivial (doc edits). **LOC delta:** **−15 to −25** in CLAUDE.md prose.

---

## What I did *not* find

- No second producer of any owned decision (the table's core claim is sound).
- No fallback path that *re-derives* a correction, route, or writer ranking instead of consuming the owner's receipt (checked corrections.py 309/621/694, pilot.py terminal trace_backs 1313/1366/1383 — all are legitimate current-world re-reads or diagnostics, consistent with "Recompute from the current world").
- `detour.classify_departure` correctly consumes the `BearingObjective` param and never rebuilds a target objective (detour.py:144-196), matching its navigation entry.

## Recommended order of work

1. **Finding 1** (only one with live divergence potential; largest LOC reduction).
2. **Finding 2/3** (bundle the target; cheap, deletes the `verify.py:361` re-derivation).
3. **Finding 5** (free doc shrink — the structure already backs it).
4. **Finding 4** (optional; adds code to buy a guarantee).
