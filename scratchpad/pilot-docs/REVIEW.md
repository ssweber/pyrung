The review found a consistent failure mode: Pilot discovers a precise fact, but a downstream layer re-derives a weaker approximation. That affects both correctness and the transcript.

## Deferred design

1. **`avoid=` is not continuously enforced during long or folded coasts.**
   Verification says it checks the trajectory, but zoom/let-run typically returns only the final snapshot. An avoided condition can become true and clear during 90,000 scans without being seen. The coast executor should own an exact avoid-violation receipt—or stop immediately when it occurs.
   [verify.py](C:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/core/analysis/pilot/verify.py:457)
   [steer.py](C:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/core/analysis/pilot/steer.py:466)

## Transcript-facing ownership leaks

These are less likely to stop navigation, but they explain misleading output:

- Recovery records channel transitions from `ctx.target_tag`, not the channel the trial actually navigated. That is why the Boolean Tumbler golden now contains empty `channel_transitions` for real `Sts_StateCurrent` ejections.
  [progress.py](C:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/core/analysis/pilot/progress.py:816)

- `watch_tags` is initialized only from the first trace, so later alternate-route pivots may be invisible to monitoring.
  [pilot.py](C:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/core/analysis/pilot/pilot.py:928)

- Plan recording reconstructs whether temporary logic controlled a tag, without evaluating whether the rung’s guard was active at that step. This can incorrectly omit a manual edit from the final plan.
  [recording.py](C:/Users/Sam/Documents/GitHub/pyrung/src/pyrung/core/analysis/pilot/recording.py:88)

---

# Second Wave (2026-07-23 scout synthesis)

Three read-only Opus scouts (reports in this directory: `scout-ownership-audit.md`,
`scout-refactor-residue.md`, `scout-decomposition.md`). Overall verdict: the ownership
refactor held — no dual producers, no orphaned subsystems. Residual weakness is
consumption-side: owned artifacts unpacked into loose scalars and threaded, plus one
live weaker re-derivation. Realistic reduction ~500–600 source LOC + CLAUDE.md shrink.
Delete items below as they land.

## Tier 0 — provably dead (~160 src + ~250 test LOC, caller-enumeration proven)

- `evidence.py:126` `expand_pipeline_need` — test-only (confirm live expansion path covers its case before dropping the test)
- `advance.py:115` `next_advance` — fully dead; `estimate_scans`/`measure_scans` (~advance.py:149-177) — production-dead, die together
- `cyclefold.py:144` `_periods_to_crossing` — dead (do NOT delete `_monotone_read_surface`, live at :441)
- `corrections.py:745` `_resolve_steerable_driver`, `corrections.py:911` `_resolve_partial` — zero-caller compat shims
- `pilot.py:545` `_diagnose_stuck` — orphaned by ownership move to `options.py::_diagnose_stuck_reason`
- `investigate.py:2195` `_precise_cause` (singular) — test-only shim; rewire 2 test sites to `_precise_causes(...)[0]`
- `options.py:1575` `_co_actions` — dead
- `outcome.py:308` `classify_outcome` — test-only shim; rewire test helper to `assess_outcome(...).legacy_outcome`
- `investigate.py:1258` dead `program` param on `build_deviation_incident` + `progress.py:1244` + ~6 test call sites
- NOT deletable: `provisional_*` event names (locked golden/skeleton/dap contract despite "compatibility" label); `legacy_outcome` (live at verify.py:627 until Tier 3); coast property aliases (low yield)

## Tier 1 — duplication collapse (~−200 LOC, golden-gated)

- trace.py: six TraceNode collectors → one `_interior_frontier` predicate + `iter_nodes()` generator (makes the trace.py:649 "must not drift" warning structural) (~−35)
- trace.py:1829/2279/3568: avoid→via→pilotable→min-score selection spelled 3× → one `_select_alternative` helper; must preserve retain-best-rejected-branch rule (CLAUDE.md "rejections order, never remove") (~−45)
- trace.py:2416-2472: writer-fallback stash/reset bookkeeping 3× in `_trace_back` → `_stash_and_reset` closure + frozen fallback tuple type (~−30)
- investigate.py:1644-1741: byte-duplicated replacement-fingerprint block in raw/guarded replay arms → `_advance_or_reject(current, outcome, seen_replacements)` (~−40)

## Tier 2 — receipt structures ("don't thread stuff")

1. **ChannelMotion receipt** (correctness-first): coast/zoom motion unpacked into 4 loose
   scalars threaded through ~92 sites / 12 modules; `outcome.py:184-196` falls back to
   snapshot equality when owned stop-reason absent — cannot tell crossing from equal
   landing on relational channels. One receipt on `_AttemptIntent`/`_TrialResult`,
   `assess_outcome` consumes `motion.reached`, delete fallback. (~−40..−60)
2. **TargetSpec on `_PilotContext`**: collapse threaded target triple (~10 call sites);
   delete `verify.py:361` TargetSpec rebuild from global ctx (consume
   `attempt.intent.bearing_objective.target`). (~−15..−25)
3. **Ownership relocations** (LOC-neutral, doc-shrinking):
   - trace.py:702-1152 inequality/static-expr resolver cluster → `static_expressions.py`
     (investigate.py:2248 + recording.py:34 currently import trace privates)
   - `hold_defeats_needed` + `_hold_values` (investigate.py:1792) → options.py (sole caller)
   - trace.py:3594 `_invert_indirect` → tide_tables.py (its own docstring says
     tide_tables generalizes it; dedup the copy-src-finding loop)

## Tier 3 — vocabulary-layer retirement (golden run between each)

- After Tier 0: `legacy_outcome` has one consumer (verify.py:627) — migrate it to the
  structured result and retire the whole `Outcome` enum legacy layer (~30-60 LOC)
- compass.py:300-499 "legacy pair observation" path vs singleton-Pulse handling —
  focused read against test_pilot_nogood/test_pilot_recording before touching
- outcome.py:277 "learn both is future work" stub arm

## CLAUDE.md shrink

- Now (already structurally enforced): hold-log tag summaries derived-by-property
  (types.py:435), coast channel-owner set single arbiter (`coast_departure_tags`
  consumed verbatim steer.py:641 + progress.py:1257), correction-artifact
  consumed-whole (progress.py:947-977) — each paragraph → one line
- After Tier 2: "still needed has three meanings" bullet, BearingObjective
  "travel unchanged" clause, coast-receipt commentary (types.py:665-668, 738-743)
  all reduce to naming the owning type
