# CoastSession v2 — the technician's trend recorder

Iteration on the v1 design, grounded in a four-way code investigation (2026-07-15, dev):
core runner/fold seam feasibility, a complete pilot waiting-loop census, a decider-input
map for every coast-evidence consumer, and the replay/incident/events surface. Companion
to `constants_audit.md` (the DISSOLVES/SHRINKS/KEEP/UNCLEAR audit).

## Doctrine

WWTD — *what would tech do*. A tech at a fault doesn't stare at a frozen screen for 100
scans and then guess; they put a trend recorder on the registers that matter and read the
pen marks. CoastSession is that recorder, and it earns three perfections a human tech
doesn't have:

- **Perfect reaction** — land on the exact scan the first armed condition changes. The
  fold stops one scan short of the nearest crossing and executes the landing as a real,
  fully-recorded probe scan (`fold.py:1424-1433,1463`); the bump scan is never overshot
  and never synthetic.
- **Perfect recall** — an ordered timeline of every intermediate and simultaneous bump.
  Nonterminal bumps re-arm; same-scan events are never collapsed; a fire-then-reset pulse
  inside the window is two recorded transitions, not a net no-op.
- **Perfect tracing** — attribute consequential bumps through recorded rung history and
  `cause()` at their exact scan. Every landing is a real probe scan by construction, so
  `rung_firings(scan)` and `cause(tag, scan)` are always available there
  (`runner.py:782-805,916-1000`) — attribution is lazy, exact, and never estimated.

These are not aspirations; each is a property the existing machinery already guarantees
*at fold boundaries*. The design's whole job is to make every scan the pilot cares about
a fold boundary — by compiling bumps into the terminal condition — and then to write down
what was observed instead of re-deriving it later.

## Verdict — how far the collapse goes

From the census (all file:line current on dev):

| Collapses into the session | Count | Today |
|---|---|---|
| Literal coast/settle loops → one seek primitive | 8 | `_settle_departure`, `_observe_stable_channel_landing`, `_coast_to_value`, `_coast_holding_state`, `_coast_to_bearing`, `_try_terminal_letrun`, `_try_terminal_dwell`, `measure_scans` |
| "Channel departed/ejected" detectors → one departure bump | 5 | `_ops.py:237,301`, `steer.py:810`, `progress.py:113`, `investigate.py:794` |
| "It settled" detectors → one quiescence protocol | 3 | detour 100-stable, investigate 100-stable, steer cone fixpoint |
| "Did we arrive" closures → one target bump | ~10 | `_reached` closures across `_ops`/steer/investigate/progress/pilot |
| History window-diff functions → the receipt timeline | 2 | `_changed_tags_in_window` (investigate.py:1543), `_changed_in_window` (causal.py:63) |
| Swallowed timeouts → explicit `stop_reason` | 6 | landing observe returns None blind; both replay coasts' `reached` bools discarded; `_settle_cone` no did-not-settle flag; `measure_scans` None ambiguity; `_settle_delayed_effects` phase-2 no post-check |
| Deleted outright | — | `_DEPARTURE_MARGIN`, `_SETTLE_STABLE_FOR`, `_LANDING_STABLE_FOR`, `_SETTLE_CAP`, `letrun_tried`, the `classify_departure` double-settle, the positional `is_eject_coast` inference |

What does **not** collapse (and must not):

- **The remedy engines.** `investigate_deviation`'s hypothesis enumeration + replay and
  `investigate_excursion`'s hold synthesis are searches, not coasts. The receipt removes
  their *input re-derivation* (window diffs, cause re-walks, reverted-register diffs) —
  the engines themselves survive intact.
- **`cyclefold`** stays the engine under oscillating holds. Its receipt contribution is
  diagnostics (period found, folds taken, bit-exactness maintained); it is not remodeled.
- **Genuine policy** (audit KEEPs): detour's regression-is-resurrected-work verdict,
  CYCLE/DEAD-END emptiness, skiff search budgets, provisional checkpoint bookkeeping,
  bearing-landing trend-rescale, the `+1` plant-latency scan, the 2-scan propagation floor.
- **`_apply_pulse`'s fixed 4-scan dwell** — no predicate exists; it is a deliberate fixed
  dwell, kept as a tiny explicit session op (`dwell(n)`), not forced into seek shape.
- **The trend re-trace.** `trace_back` from the settled world is how frontier/novelty is
  known; the receipt *relocates* it to pause time and records the result — it cannot
  eliminate it. What changes is its authority (Part 4).

## Part 1 — the primitive and the core seam

### seek semantics

```
receipt = session.seek(bumps, budget)      # arm a vector, run, land, record
```

- Compiles the armed vector to a single `AnyCondition(...)` (exists today,
  `condition.py:485-506`, already descends through crossing extraction via
  `simplified.py:142-144`) and runs `run_until`/`cycle_fold_until`.
- At the landing scan (always a real probe scan), evaluates each disjunct against a
  `ScanContext` over the landing state to decompose *which* bump(s) fired — cheap,
  caller-side, exactly how `_compile_condition_predicate` builds its predicate
  (`runner.py:2544-2550`).
- Nonterminal bumps record a `BumpEvent` and re-arm; terminal bumps end the seek.
- `cycle_fold_until` is used whenever active oscillating holds are present, exactly as
  today (`_ops.py:241,309`); it already accepts a predicate + `extra_comparisons`
  (`cyclefold.py:202,207`), so feeding it the compiled vector is caller-side plumbing.

### Required core extensions (small, exhaustively enumerated)

1. **Bump-tag fold protection — the one real extension.** `run_until` builds its fold
   context via `_ensure_fold_context()` with `target_names=frozenset()`, cached and
   shared (`runner.py:871-889,701`). Only *comparison* atoms are rescued at fold time
   (`extra_comparisons` → `fold.py:1093-1095,1125-1127`); a bare-bool/rise/fall read of a
   churn-excluded, frozen-write, or system-clock tag can be folded past. Extension:
   `run_until(..., protected_reads=frozenset)` (or equivalent) that threads the bump
   vector's tag set into `_build_fold_context` as `target_names`, with a cache key that
   includes it. This is the audit's S3 remedy with the exact seam now named.
2. **System-clock bumps.** The fold bounds itself only to clock edges the *program's
   rungs* read (`fold.py:634-697,1242-1296`). A bump on a clock the program doesn't read
   must either register its clock edges into the fold bounds or force step-mode for that
   seek. (New blocker found this pass — not in the audit.)
3. **Landing introspection.** `run_until` returns only the landing `SystemState`
   (`runner.py:3160`). That is sufficient — receipt assembly stays in `pilot/coast.py`
   (which fired = disjunct evaluation; scan = `state.scan_id`). No runner-level receipt
   object; v1's instinct to avoid `_run_until_any` was right, it just under-specified
   extension 1.
4. **Budget semantics — pick one and document it.** `run_until(fold=True)` counts
   *virtual* scan-ids (`fold.py:1429,1478,1484`); `cyclefold.budget` counts *real* scans
   (`cyclefold.py:226`). Policy horizons are statements about machine time, so
   CoastLimits budgets are **virtual scan-ids**; the session converts when dispatching to
   cyclefold. Receipt records both real and virtual scans elapsed.
5. **Kill the `when().pause()` ejection guards in pilot.** The pause flag is a why-less
   boolean (`runner.py:2629-2631,2886-2910`) — the root of every post-hoc "what stopped
   us" re-derivation. Departure/avoid/eject predicates move into the armed vector where
   the stop reason is observable. (The pause path itself is untouched; pilot just stops
   using it for coasts.)

### Bump compilation soundness rules

| Bump tag class | Safe forms | Notes |
|---|---|---|
| Ordinary written tag | any — bool, rise/fall, comparisons | visible to the plateau guard; edges force a probe scan |
| Accumulator / mod-wrap | comparisons only (`eq ne lt le gt ge`) | `rise/fall`/bare-bool are NOT extracted as crossings (`fold.py:938`); Done bits are ordinary written tags, safe as bools |
| — `Acc == N` specifically | integer per-scan counters only | on timed accumulators one dt can step over equality (`fold.py:979-987`); the session **rewrites `==` to `>=` + verifies equality at the landing**, and reports `overshot` in the receipt when they differ |
| Churn-excluded / frozen-write | requires extension 1 | otherwise foldable-past |
| System clock / scan-derived | requires extension 2 | else step-mode |
| Opaque Python callable | step-mode fallback only | design goal: no bump in the vocabulary needs it |

### Vocabulary revision: silence-for-N is DELETED

v1 proposed "silence-for-N is `run_until(changed)` with `max_cycles=N`". That is unsound:
under fold `max_cycles` counts virtual scan-ids, so it answers "changed within N virtual
scans", not "silent for N kernel scans" — and no existing mechanism expresses an
N-kernel-scan window (Q8 of the core investigation). More fundamentally,
silence is what the fold *compresses*; a silence-counter bump is anti-aligned
with the engine.

The replacement is already in v1's own "event-driven landings" section, now made total:
**quiescence, not silence**. A landing is quiescent when (a) the 2-scan propagation floor
has passed, (b) `pending_effects` is false (harness `pending_count==0`, no running
relevant accumulator, no active TT), and (c) the watched cone is at fixpoint across two
scans. All three are per-scan-evaluable predicates. The 100-scan stability heuristics
existed to *approximate* (b)+(c) blind; with them observable directly, D1/I1's silence
windows don't shrink — they dissolve. (`_SETTLE_STABLE_FOR`, `_LANDING_STABLE_FOR` both
deleted; audit verdicts upgraded from SHRINKS to DISSOLVES.)

## Part 2 — receipt spec v2

Eight gaps were found in the v1/audit spec by mapping every decider's actual inputs.
Fields, with the consumer that demanded them:

| Field | Consumers / gap closed |
|---|---|
| `session_id` + `coast_kind` (bearing_coast / letrun / settle / replay / measure / dwell) | recorded onto journey steps — kills the positional `is_eject_coast` inference (`investigate.py:333`), the documented false-confirm hazard if step shape ever changes |
| `start_scan`, `end_scan`, real+virtual scans elapsed | every settle-count consumer; budget accounting |
| **snapshot references** (start fork/world, settled fork/world) | GAP 6: no-op screens and `correct_enablers` read arbitrary guard/coil tags beyond the bump set (`corrections.py:217,416`); the timeline alone is insufficient. GAP 8: carrying the settled fork deletes `classify_departure`'s second settle coast (`departure.py:103-124`) |
| **timeline**: ordered `BumpEvent`s, per-transition, same-scan groups preserved | GAP 5: `changed_tags` deliberately captures fire-then-reset pulses (`investigate.py:781-783`); a net before≠after timeline loses the complement-reset oscillation `correct_enablers` hunts. Also V1's excursion (two bumps in one window) reads straight off it |
| `stop_reason` ∈ {reached, departed, avoid_veto, quiescent, timeout, gauge_advance, gauge_loss, budget_clipped} + simultaneous-terminal set | the six swallowed timeouts; D2's timeout-vs-settled distinction; I3's first-of-several |
| **`state_key_before` / `state_key_after` + `key_dims_moved`** | GAP 1: SPIN/CYCLE/seen_keys/nogoods all pivot on `_pilot_world_key` (`_ops.py:392-411,469-478`); the key is threshold-masked and acc-masked, so a bump timeline cannot substitute for key equality — the key must be a recorded field, and `key_dims_moved` names which dimension a bump crossed |
| **`gauge_mark_start` / `gauge_mark_landing` (vectors) + compare verdict** | GAP 4: provisional seeds a *new* anchor from the settled-landing mark (`progress.py:374-376`); a scalar verdict can't reconstruct it. `gauge.mark/compare` are pure snapshot functions (`gauge.py:94-144`) — recording them is free |
| **`frontier_pairs` at landing + `new_frontier_delta`** | GAP 2: the self-defeat screen needs the full interior pair-set (`progress.py:726-733`), not a boolean. GAP 3 honesty: this is one `trace_back` run at pause time — relocated, not eliminated |
| `pending_effects` + `quiescent` flags | sterile arm, C2 memo soundness, V1 pending escape; a timeout with pending effects is `unknown`, never a landing |
| attribution (lazy): `writer_rung_id` per event + `causal_roots` / `pilot_touched` at event scan | O1: a single writer id is not the root set; the session is paused at a real probe scan so `cause()` is exact. Lazy because most receipts never need it |
| `prior_bump_identity` | I4's "original bump silenced + a *different* next bump appeared" |
| fold diagnostics: folds taken, cyclefold period/repeats, `overshot` equality flag | cyclefold bit-exactness evidence; the `==`→`>=` rewrite honesty |

**What the receipt deliberately does not carry:** remedies (holds, corrections — the
engines own those), the global `seen_keys` set (the loop owns it; the receipt exposes
`state_key_after` so the loop can test membership), route/navigation state.

## Part 3 — what replaces each waiting loop

| Today | v2 |
|---|---|
| `_coast_to_value` / `_coast_holding_state` + pause guards | `seek({target, departure(watch_set), avoid…}, BEARING_COAST_BUDGET)` |
| `_settle_cone` (16/64 ceilings, no did-not-settle flag) | `seek({reached, quiescent}, ceiling)` — floor stays; timeout is a recorded stop_reason |
| `_settle_delayed_effects` (two phases + `+1`) | two chained seeks inside one session: `seek({harness_quiescent}) → dwell(1) → seek({tt_clear})`; the receipt records both phases. Sequencing is a session capability, not a violation of the shape |
| `_settle_departure` (100-stable, cap 2000) | departure already recorded at its exact scan by the *originating* session; classification consumes `seek({quiescent}, cap)` continuation on the same session — one coast, not two |
| `_observe_stable_channel_landing` | `seek({channel_departed}) → seek({quiescent})`; a never-moved channel is `stop_reason=timeout` and **scope-pinning is refused** — kills the waypoint-frozen false confirm (guard pinned to the starting context and replay-confirmed against the same frozen snapshot) |
| replay dispatch (positional eject inference) | replay re-arms the recorded session spec (`coast_kind` + bump set + watch set) from the step |
| `measure_scans` (None ambiguity) | `seek({crossed}, MEASURE_BUDGET)`; `stop_reason` distinguishes timeout from can't-compute |
| `letrun_tried[(key, len(rungs))]` | quiescent-receipt memo keyed `(state_key_after, rung-overlay key)`, trusted **only when `quiescent` and not `pending_effects`** (the audit's C2 accumulator-masking caveat, now enforceable) |
| `_apply_pulse` 4-scan settle | `dwell(4)` — explicit fixed dwell op, unchanged behavior |

`CoastLimits` centralizes the **values** (bearing coast 10_000 virtual, cone floor 2 / ceiling 16,
dwell 64, delayed-effects 2_000, skiff window 4, provisional 2_000, measure 2_000); the
**decisions** about when each applies stay with their owners — "scheduling is triggers,
not positions," one owner per decision. A provisional-owned session is clipped to the
provisional's remaining budget (fixes the bearing-coast-outruns-its-provisional 5× pathology).
Drive-by: collapse the `_SKIFF_SCANS` duplication and name the intentional
`_SKIFF_MAX_PROBES` 8-vs-16 split (investigate.py:53-54 vs skiff.py:177-178).

## Part 4 — outcome/verify rewiring: gauge becomes authoritative

The first casualty (`outcome.py:215`: any `new_trend < distance_before` reads ADVANCED)
is not fixed by receipt plumbing alone — it is an *authority* error. Trace-distance is a
coordinate-relative count that legitimately drops when a departure lands in a
different-coordinate world; the gauge is the only target-relative signal. Structural fix:

- **ProgressEffect.ADVANCED requires a gauge verdict** (`gauge_compare == advanced`) or a
  key-dim move toward the bearing. Trend is demoted to legibility (event payloads,
  transcripts) and tie-breaking — never to acceptance.
- For a channel session, outcome keys on `stop_reason` first: `reached` → SATISFIED;
  `departed` → DEPARTED with receipt agency; `timeout` + channel frozen → the sterile arm
  (reject unless `gauge_advance` or genuine `new_frontier_delta`). The frozen-channel
  false confirm becomes unrepresentable: a timeout receipt cannot be stamped ADVANCED by
  an incidental trend drop because trend no longer stamps anything.
- SPIN reads `key_dims_moved == ∅ ∧ ¬pending_effects`; the excursion escape reads the
  timeline directly (a key-dim rose and reverted inside the window) instead of the
  `post_pulse_key` special capture. `investigate_excursion`'s remedy synthesis is
  unchanged — only its reverted-register re-diff (`investigate.py:569-574`) is replaced
  by the timeline.
- `channel_reached`/`channel_moved` overrides in DEAD-END/LATERAL keep their widening
  role (deliberate: ejections must reach classification, `verify.py:266-273`) — now read
  from the receipt instead of snap diffs.
- The AMBIENT_DRIFT accept-stub gains its missing input: writer=PROGRAM attribution plus
  the from/to transition off the timeline is exactly the "learn both edges" material; the
  policy itself stays a stub, now an explicit one with its evidence recorded.

Acceptance for this part is already codified: the golden-skeleton **bearing-coast tripwire**
(`test_pilot_golden_skeleton.py:95-114` — every `bearing_coast_accepted` lands exactly on target
or is `ejected=True`) and the strict-xfail internal-route gate (240s wall budget) that
names CoastSession as its step.

## Part 5 — incident and replay

- **Incident from the receipt.** `build_deviation_incident` already narrows to
  Done/accumulator registers — v1's "broad window diff" framing was stale. The real fixes:
  the timeline replaces both `_changed_tags_in_window` and `_changed_in_window`
  (duplicated), `departure_scan` is the recorded event scan (no `_first_departure_scan`
  history re-scan), and — critically — the bearing filter (`progress.py:665-672`) can no
  longer *lose* an anomalous tag that transiently touched a frontier need: the filter
  still selects the bearing, but the timeline retains every transition as evidence for
  ranking.
- **Replay = the same session spec re-armed**, with the watch set as an explicit caller
  parameter: live coasts watch the full role set; replay narrows to the departed channel
  (the deliberate scratch-register-transient fix at `investigate.py:342-354`, now a named
  parameter instead of buried dispatch — the audit's I2 resolved).
- **Whole-journey confirmation stays.** Bump-local "original bump silenced + different
  next bump" is recorded via `prior_bump_identity`, but the trajectory-global invariant
  (nothing pinned behind the goal) is not a pause property: keep the full-journey replay
  and the static `_active_rungs_defeat_needed` screen (audit I4). Replay judgment keeps
  the per-replay window-diff semantics of `_new_cause` (symmetric Done-set diff — now off
  the replay session's own timeline) to avoid the ambient-timer false confirm its
  docstring warns about.
- Coupling drivers attach to automatic chart-edge sessions as well as terminal let-runs
  (v1 kept this; unchanged).
- For the tumbler: `Rotate_SensorOffWD_tmr.Done` is a recorded nonterminal bump *before*
  the state departure; the existing accumulator correction produces the two-polarity
  OSCILLATE rungs; `x_RotateSensor` never enters the ordinary burner trace.

## Part 6 — persistence and revert (the landmine)

`load_world` restores only `state.overlay_rules` via `_set_rungs` (`types.py:476-489`); replay
fidelity exists *because* holds re-materialize as synthesis rungs on every fork
(`harness.fork_onto`, `runner.py:1364-1368`). Therefore, by construction:

- **Receipts are immutable values**, attached to `_TrialResult` (which is today's
  implicit receipt — fork + raw snaps + derived verdicts) and to journey steps. No live
  session object survives an act.
- **`BumpTracker` is scoped to one session.** Anything cross-act — gauge anchors,
  provisional state, checkpoints — stays owned by progress.py, which already snapshots
  correctly. Nothing new needs `snapshot_world`/`load_world` hooks; nothing silently
  vanishes on revert because nothing lives outside the world or the value-typed record.

## Part 7 — events and test compatibility

- Preserve per-landing `bearing_coast_accepted` triplet
  (`bearing_coast_channel_tag`, `bearing_coast_target_value`,
  `bearing_coast_actual_value`, `ejected`) — the tripwire and
  `console.py:478-483` both read it.
  `ejected` derives from `stop_reason == departed`.
- Preserve `trend_regression.investigation` sub-structure (`confirmed_detail[].holds`,
  `rejected_detail[].slug/ground`) — `console.py:493-503` and `skeleton.py:367-378`.
- Preserve correction `kind` strings verbatim: `"latch-exposure"`, `"liveness"`,
  `"done-boundary"` (asserted in `test_pilot_investigate.py:717,814,1096,1212`).
- Register receipt/event dataclasses in `skeleton.py:_DATACLASS_KEEP` — otherwise their
  decision content silently escapes the goldens (a coverage hole, not a failure).
- New event fields must be decision-shaped (survive `_DROP_KEY_RE`); scan-variable data
  stays out of skeletons. Golden regeneration (`PYRUNG_REGEN_GOLDEN=1`) is an explicit,
  reviewed cutover step; the strict-xfail flip is the intended landing signal.

## Part 8 — cutover phases (each lands green)

1. **Core seam**: `protected_reads` fold-context threading + cache key; clock-bump
   handling; unit tests for folded/unfolded first-bump parity, `==`→`>=` landing
   verification, protected churn-excluded reads.
2. **`pilot/coast.py`**: BumpSpec/BumpEvent/CoastReceipt/CoastSession + disjunct
   decomposition + cyclefold dispatch. Migrate `_coast_to_value`/`_coast_holding_state`
   behavior-neutrally (pause guards → armed departures); replay coasts follow via the
   recorded session spec.
3. **Settle unification**: `_settle_cone`, `_settle_delayed_effects`, `_apply_pulse`
   dwell onto session ops; stop_reasons surface; ceilings into CoastLimits.
4. **Departure/landing**: `_settle_departure` + `_observe_stable_channel_landing` →
   departure-then-quiescence; delete the 100-scan constants; `classify_departure`
   consumes the settled receipt (double-settle deleted).
5. **Receipt-driven verify/outcome**: gauge-authoritative ADVANCED, key-dims SPIN,
   sterile arm on stop_reason. This is the phase that flips the strict-xfail gate and
   must satisfy the bearing-coast tripwire. Also revisit here (decided during phase 2, NOTE in
   coast.py): a seek currently advances ≥1 scan before judging, matching legacy
   `run_until` semantics so pre-regen goldens hold — the immediate-landing rule ("a
   target stops the scan it holds") lands with this phase's golden regeneration.
6. **Incident/replay from receipts**: timeline-built incidents, session-spec replay,
   delete both window-diff functions, `_DEPARTURE_MARGIN`, `letrun_tried` (→ memo).
7. **Events normalization + golden regen** + `_DATACLASS_KEEP` registration.

## Acceptance gates (unchanged from the audit, plus census additions)

- `how(Sts_StateCurrent==17, avoid=Cmd_State_Complete)` strict-xfail flips green inside
  the 240s wall budget; golden recorded.
- `how(y_BurnerLoop)` completes in Drive-2-class time; receipt contains the rotate
  watchdog bump; confirmed liveness/OSCILLATE correction; no manual sensor animation.
- Bearing-coast tripwire holds across regenerated goldens.
- Full suite + lint + type; fresh-process deterministic golden checks.
- New: no coast primitive returns a bare bool or None; every session in the burner and
  tumbler drives carries a stop_reason (grep-able assertion in the golden skeletons).
