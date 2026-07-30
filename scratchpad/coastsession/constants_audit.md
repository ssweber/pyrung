# CoastSession constants audit — blind-coast exit machinery in `pilot/`

## FIRST CASUALTY (decided 2026-07-14): the bearing-coast trend-noise false-confirm

The live specimen that CoastSession must kill by construction, diagnosed on the tumbler
fixture (`how(y_BurnerLoop)`, 178s flounder; full diagnosis in the 2026-07-14 session):

- A channel bearing coast (let-run `Sts_StateCurrent: 3->6`) ran its full
  `_COAST_BUDGET=10_000` with the channel FROZEN at 3
  (`bearing_coast_accepted outcome=confirmed req=6 actual=3
  ejected=False`) and was **CONFIRMED anyway**: the deep target's retrace dropped tree
  distance 25->21 on incidental sub-registers, and `outcome.py:215` reads any
  `new_trend < distance_before` as ADVANCED. `channel_moved`/`channel_reached` are
  computed at `verify.py:274-279` but only ever WIDEN acceptance, never gate it
  (`verify.py:340-344` skips the LATERAL rejection on any trend drop).
- The false CONFIRMED starves a **fully functional** escalation: when the same blind
  bearing coast is honestly rejected (shallow-tree drives, e.g. `how(Sts_State_Execute==True)`),
  the loop escalates to terminal let-run/skiff, the coupling driver earns the
  `x_BlowerFB`/`x_RotateFB` holds, and the drive completes in ~9s. Nothing is missing;
  the lie blocks the bail.
- Deliberately NOT point-fixed pre-CoastSession (decision 2026-07-14): in bump terms the
  defect is unrepresentable — the session pauses at "channel moved" or "budget slice
  elapsed with channel silent"; a silent-channel receipt (`watched_tag` frozen,
  from==to) cannot be stamped ADVANCED because the caller judges the receipt, not a
  whole-journey trend diff. Acceptance check for the redesign: the strict-xfail gate
  `how(Sts_StateCurrent==17, avoid=Cmd_State_Complete)` in
  tests/tumbler/test_pilot_golden_skeleton.py flips green, and `how(y_BurnerLoop)`
  completes in Drive-2-class time.
- Related non-blocking residuals for the same rework: attach coupling drivers when
  coasting an automatic chart edge (today only the terminal let-run earns them);
  `_COAST_BUDGET=10_000` vs `_PROVISIONAL_SCAN_BUDGET=2_000` asymmetry (a bearing coast can
  outrun the provisional that owns it by 5x, guaranteeing expiry rollback of its own
  result — the Q3 "10k scans burned then reverted" pathology).

Tests the hypothesis: *most hard-coded exit-path rules are compensations for judging
coasts blind, and dissolve into (a) a bump predicate + (b) a caller policy. Some encode
real ladder idioms and must survive.*

Scope: the **run / observe / classify / exit** half only. Navigation (trace writer
ranking, availability, candidates, charts, routes, tide_tables, gauge internals) is out
of scope per the brief. Line numbers verified against `dev` on 2026-07-14.

Verdict legend: **DISSOLVES** (bump+policy fully replaces it) · **SHRINKS** (bump handles
detection; a named residual policy remains) · **KEEP** (real ladder idiom / genuine
policy no observation can replace) · **UNCLEAR** (a gap the bump vocabulary can't yet
express — the load-bearing output).

---

## departure.py — departure settlement & classification

| # | Rule (file:line, shape) | What it does today | Why it exists | Bump-world replacement | Verdict |
|---|---|---|---|---|---|
| D1 | `_SETTLE_STABLE_FOR = 100` + `_settle_departure` loop (`departure.py:62,103-124`) | Steps a rung-driven fork until `channel_tag` holds one value for **100 consecutive** scans, or `_SETTLE_CAP` total; returns `(fork, n)`. | The live ejection guard pauses at the *first* departure scan — mid-transition (Holding, Aborting). Classification needs the *landing*, so let the departure's own chain complete. 100 ≈ "a full second of scans" that the channel has truly stopped. | **silence-for-N bump** on the channel register (N a policy arg); the receipt carries `settled_value` + `scan`. The caller (`classify_departure`) reads the receipt instead of re-implementing the stability loop. | **SHRINKS** — detection dissolves into a silence bump; residual = the N=100 policy and "which register is the channel." |
| D2 | `_SETTLE_CAP = 2000` (`departure.py:63`) | Hard ceiling on total settle scans regardless of stability. | Fail-closed bound so a permanently-oscillating channel never loops forever. | **timeout bump** (scans-elapsed ≥ cap). | **SHRINKS** — timeout is a real fold-evaluable bump; residual = the 2000 policy **and** a latent gap: today a cap-hit landing is classified identically to a silence landing, but a cap-hit value may be mid-transition. The receipt **must** distinguish `stop_reason ∈ {silence, timeout}` so the caller can refuse to classify a timeout landing. |
| D3 | `classify_departure` verdict machinery (`departure.py:242-352`) — provisional / unknown / regression via gauge-reset boundaries + `_clean_route` BFS + `operator_action_for_state` | From the settled landing, classifies the departure by the routes back: dirty route (crosses a gauge-reset value or resurrects a discharged obligation) → not-provisional; clean route or unique operator current → provisional; gauge behind source receipt → regression. | "Regression is resurrected work, not channel displacement." A departure is only a regression if it destroys earned progress; program-owned motion (a mid-recipe Hold) must not be reverted. | This **is** the caller's decide-arm. It consumes the receipt's `settled_value`; it is not blind-coast compensation. | **KEEP** — genuine ladder-idiom policy (the break-table "regression is resurrected work" entry). Only its *input* (the settled value) comes from a bump. |

---

## investigate.py — replay & landing observation

| # | Rule (file:line, shape) | What it does today | Why it exists | Bump-world replacement | Verdict |
|---|---|---|---|---|---|
| I1 | `_LANDING_STABLE_FOR = 100` + `_observe_stable_channel_landing` (`investigate.py:50,59-97`) | In a replay probe: after the channel reaches target and the failure is silenced, follow automatic motion until the channel **moves** and then stays put for **100** scans (raw hypothesis) or stops at the first landing transition (scoped hypothesis); bounded by `_COAST_BUDGET`. | The incident window can end while the PLC still sits at a commanded waypoint; the correction's true landing is beyond it. Reveal the stable landing so guarded replay reflects where the correction naturally settles. | Compound bump: **channel-moved bump**, then **silence-for-N bump**. The `settle` flag = a caller policy (stop at first departure vs. wait for re-silence). | **SHRINKS** — third near-identical settle loop; detection = two bumps + a policy flag; residual = N=100. |
| I2 | Replay coast dispatch + guard-role scoping (`investigate.py:319-366`) — per step, apply-pulse vs `_coast_holding_state` (letrun, bounded to `departure_scan+_DEPARTURE_MARGIN`) vs `_coast_to_value` (bearing coast, ejection-guarded) vs bare step loop; the last channel-shaped step is coasted, guard = **channel alone**, never the full role set | Reproduces the incident faithfully in replay: the eject-coast is coasted (not pulsed, which would skip it — 5 settle scans, channel intact, every hypothesis falsely "confirms"). Guard restricted to the channel to avoid pausing on a mid-settlement scratch-register transient (`isCmdValid__cmd`, `sm__where2jump`). | The coast becomes seek-to-bump; the eject is an eject bump; the "which tags arm the bump" is a session parameter. | **UNCLEAR** — the bump protocol must let the caller **scope which tags arm the eject bump** (channel-only vs all roles), and that scope **differs between the live coast and the replay coast**. *Scenario the vocabulary can't yet see:* the checkpoint world catches the state machine's scratch registers mid-settlement; a role-scoped eject bump fires on that transient a few scans in, the channel still reads its held value, and the first-ranked hypothesis is confirmed against a false pause. The receipt must name *which watched tag* crossed, and the session must support a caller-supplied watch set narrower than "all roles." |
| I3 | `_DEPARTURE_MARGIN = 10` (`investigate.py:49`) | Terminal-letrun replay budget = `departure_scan - now + 10`; +10 slack so the departure actually reproduces inside the bounded replay. | The letrun replay is bounded to the departure window; the slack ensures the departure isn't clipped by an off-by-a-few scan count. | Seek to the **eject bump** directly instead of running a fixed scan budget; the slack that guarantees the departure is captured is unneeded. | **DISSOLVES** — the margin is a pure artifact of running a fixed scan count instead of seeking to an event. *Adversarial check:* if the correction silenced the departure, seek-to-bump has nothing to stop on — so it dissolves **only if** the session seeks to *first-of* {eject, target, silence} and the receipt reports which fired. That multi-predicate receipt is already a CoastSession promise, so the margin genuinely disappears (not relocated to policy). |
| I4 | Whole-journey replay confirmation (`build_replay_fn` replays all `steps` from checkpoint; `investigate_deviation` confirm loop `investigate.py:974-1068`, incl. the second `installed_outcome = replay(scoped)` pass and `_active_rungs_defeat_needed` static screen) | A correction is confirmed by re-running the **entire** journey from the checkpoint with the candidate holds installed, then judging the end state (channel reached / bearing held / trend ≤ checkpoint). A static `_active_rungs_defeat_needed` pass pre-screens the guarded form against the checkpoint frontier because the bounded replay can't see downstream pins. | End-to-end proof that the fix silences the incident **without breaking a later leg** of the march. | Bump-local: replay to the original bump scan; confirm when the original bump no longer fires **and a different next bump appears**. | **UNCLEAR** — the biggest design question. Bump-local confirmation is cheaper and more precise for "did I fix *this* incident," and both halves ("original bump silenced," "a new/different bump appears") are expressible. But it **cannot express the trajectory-global invariant** the whole-journey replay (and the `_active_rungs_defeat_needed` screen) exists to check. *Scenario:* a one-sided liveness hold silences the watchdog bump at scan N but pins `Heat_CurStep = 1` forever; bump-local sees silence + a new bump = confirmed, while whole-journey / the static needed-defeat screen catches the pin behind the checkpoint frontier. The bump vocabulary has no "no downstream frontier register ends pinned behind the goal" predicate — that is a property of the whole remaining trajectory, not of any single pause. Keep the static `_active_rungs_defeat_needed` screen even if the replay goes bump-local. |
| I5 | `_SKIFF_SCANS = 4` (`investigate.py:53` **and** `skiff.py:177` — duplicated) | Isolated-probe window length: pulse → staged register → gated clobber/transition, all inside 4 scans. | A runtime-gated transition typically needs command + enable to land within a few scans; 4 is that horizon. | The skiff observes whether a pinned frontier register **moved within the window** = a register-departed bump with **horizon N=4**. | **SHRINKS** — "did it move within N scans" = an edge bump with an N-scan horizon; residual = the N=4 policy. (Note the duplication: two module-level copies of the same 4 that should be one constant.) |
| I6 | `_SKIFF_MAX_PROBES = 8` (`investigate.py:54`) / `= 16` (`skiff.py:178`) | Caps the number of distinct levers bench-tested per excursion (8) / per frontier (16). | Forks are cheap, not free; bound the breadth of the lever search. | None — this bounds *how many levers to try*, not how a coast is classified. | **KEEP** — genuine search/fork breadth budget, orthogonal to observation. No bump replaces "try at most K levers." (The 8-vs-16 split is intentional — excursion cones are narrower than frontier cones — not an accident, but worth a shared-name refactor.) |
| I7 | `_SKIFF_MAX_DOMAIN = 8` (`skiff.py:179`) | Word probes only when the declared finite domain has ≤ 8 values. | Enumeration soundness + economy: probe only engineer-declared small domains, never invented values. | None — a soundness/economy gate on enumeration breadth. | **KEEP** — navigation-side soundness rule, not exit classification. |

---

## outcome.py — post-gate outcome classifier

| # | Rule (file:line, shape) | What it does today | Why it exists | Bump-world replacement | Verdict |
|---|---|---|---|---|---|
| O1 | `_action_caused_regression` (`outcome.py:162-183`) | Chases `cause()` roots of every opaque-loop register that moved; returns True if a pulsed action tag is among the roots. | Distinguish a pilot-caused regression (`C_Abort` → Aborted, a self-inflicted misstep) from ambient drift (an alarm firing on its own). The pilot must not commit to its own bad control input. | The receipt carries the **writer rung id** (node-aware firing capture) of each bump; agency = "was a pilot-touched tag a causal root of that write." | **SHRINKS** — the receipt's writer attribution supplies what the post-hoc chase computes, but a *single immediate writer id is not the causal-root set*: the rung that wrote `S_StateCurrent` may be gated by a chain the pilot's `C_Abort` armed several rungs back. Residual: the receipt must carry the **causal-root set** (or the session runs `cause()` at the pause scan — feasible, since it is paused at a known scan). |
| O2 | AMBIENT_DRIFT accept-stub (`outcome.py:296-305`) | Trend rose but the pilot didn't cause it → accept as `PROGRAM / DEPARTED`. Comment: *"Stub: for now we accept; full 'learn both' is future work."* | The PLC moved the register on its own (the command was a no-op); accept and let ASSESS decide, rather than reject a program-owned advance. | A **channel-departed bump with writer=program**; caller policy = accept + learn both edges. | **SHRINKS** — detection = the program-written departure bump; the accept/learn-both policy survives (and is admittedly still a stub). Bump world makes the policy an explicit caller decision but doesn't write it. |
| O3 | Sterile-timeout rejection arm (`outcome.py:253-281`) | When the bearing-coast channel did **not** move: reject unless progress advanced (gauge) or a new frontier was exposed. Comment: *"treating `actual != requested` alone as drift used to commit 10k-scan HELD laps forever."* | A blind coast that burned its whole budget without the channel moving must not be accepted as drift — that spins forever. | **timeout bump fired + no channel bump fired** = the sterile case, surfaced the instant the coast pauses at the timeout rather than after a 10k-scan blind run. Caller policy: reject unless a gauge-advance bump or new-frontier delta is present. | **SHRINKS** — the *core* of the hypothesis. The sterile detection dissolves into "timeout bump without a channel bump"; the accept-if-gauge-advanced-or-new-frontier policy is real progress semantics that survives. The pathology the comment names is exactly what pausing-at-bump prevents. |

---

## verify.py — pre-classification gates

| # | Rule (file:line, shape) | What it does today | Why it exists | Bump-world replacement | Verdict |
|---|---|---|---|---|---|
| V1 | **SPIN** gate `_gate_spin` (`verify.py:92-197`) | Rejects a trial whose settled world key equals the frame key (nothing changed), with three escapes: gauge **ordinal-advance**, **excursion retry** (key changed at `post_pulse` then reverted → `investigate_excursion`), and pending-effects. | A no-op trial must not be accepted. The excursion arm catches a live-word antagonist that suppresses the pulse's effect within the settle. | "Nothing moved" = a **null-bump** (no bump fired over the trial). | **UNCLEAR** — two gaps. (1) The world key is **threshold-masked**: a real advance (`count 1→2` under `count < 3`) projects to *no key change*, so a naive "did the key move" bump is **blind to threshold-aliased progress** — only a separate gauge-ordinal bump sees it. (2) The excursion is a genuine **two-bumps-in-one-window** pattern: the key rose at `post_pulse` and fell by settlement; a single end-of-coast evaluation cannot see it, yet SPIN catches it via `post_pulse_key ≠ frame.key`. The bump protocol must (a) expose a gauge-event bump *distinct from* the state-key-change bump, and (b) report **intermediate** bumps within a window, not just the final state. |
| V2 | **CYCLE** gate `_gate_cycle` (`verify.py:200-246`) | Rejects a trial whose new key is in `seen_keys`, unless the gauge ordinal advanced or the move is influence-prescribed. | Prevent looping through already-visited worlds. | None — search-graph cycle detection over the world key. | **KEEP** — pure search economy, not coast classification. (Its ordinal escape is the same threshold-alias caveat as V1 — the key is lossy — but that's the gauge bump's job, not CYCLE's.) |
| V3 | **DEAD-END** gate `_gate_dead_end` (`verify.py:249-394`), incl. the LATERAL sub-gate and `channel_reached`/`channel_moved` overrides | Rejects a trial whose post-trace frontier is empty AND no pending effects AND no compass route — unless the channel reached/moved its target or the move is learned-prescribed. LATERAL rejects new-actions-but-no-trend-improvement. | A trial that leaves no next action and reaches nothing is a dead end; the channel overrides let a bearing coast that reached/ejected its channel through to classification. | The frontier-emptiness test is **static trace analysis**, not a coast observation; the `channel_reached`/`channel_moved` overrides are reads of bumps that already exist. | **KEEP** — the emptiness decision is navigation-adjacent (a `trace_back` property); only its override arms consume bumps (which would come from the receipt). Not blind-coast compensation. |

---

## progress.py — provisional lifecycle & trend

| # | Rule (file:line, shape) | What it does today | Why it exists | Bump-world replacement | Verdict |
|---|---|---|---|---|---|
| P1 | `_PROVISIONAL_SCAN_BUDGET = 2000` + provisional state machine (`progress.py:51`; `_start_provisional`, `_anchor_provisional`, `_finish_provisional` `360-587`) | A provisional attempt expires at `min(max_scans, start+2000)` if the gauge stays incomparable/preserved; gauge **advanced** promotes (collapses provisional checkpoints into the rejoin), gauge **behind** regresses into investigate-and-revert, expiry rolls back **without** a nogood. | Bound ordinary piloting after a proven-clean program departure when the gauge stays uninformative. Named finite budget (the "How we fail" model-citizen). | The lifecycle *triggers* are bumps: **gauge-advance** (promote), **gauge-loss** (regress), **timeout** (expire). | **SHRINKS** — the transition triggers become gauge-event / timeout bumps (fold-evaluable: `gauge.compare(anchor, snap)` is a pure per-scan snapshot function). The residual is the whole checkpoint/rollback-boundary bookkeeping (the caller's decide-arm) and the 2000 policy constant. |
| P2 | BEARING-LANDING "provisional until ordinary progress banks a checkpoint" (`progress.py:199-297`, `_bearing_satisfied`, `_anchor_bearing_receipt`) | A satisfied channel bearing that *raised* trend resets the trend baseline but keeps the source checkpoint; the landing stays provisional until an ordinary improved-trend checkpoint promotes it. | A route landing exposes a different trace-distance coordinate system (Idle is 2 leaves from Start; the Starting landing exposes 15 production prereqs) — raw leaf-count comparison is meaningless. | Downstream of the bearing-satisfied bump; this is trend-coordinate-system reasoning, not coast classification. | **KEEP** — genuine progress/trend policy. Consumes the target/bearing bump; no observation replaces "a landing rescales the distance metric." |

---

## pilot.py — Act cascade & skiff exit

| # | Rule (file:line, shape) | What it does today | Why it exists | Bump-world replacement | Verdict |
|---|---|---|---|---|---|
| C1 | `_SKIFF_KEY_BUDGET = 2` (`pilot.py:1169`, `_orient_escalate_skiff`) | A stuck key earns ≤ 2 skiff laps **that changed knowledge** before the loop stops honestly; a lap only counts if `Compass.apply` reported new knowledge. | Bound the pathological free-word/config-word probe space that would otherwise accumulate fresh probe marks forever while the world never moves. Named finite budget (model citizen). | None — a search-termination budget over re-probing, not a coast observation. | **KEEP** — the explicit "spinning mode drains a named budget" citizen. No bump replaces "stop re-probing after 2 fruitless laps." |
| C2 | `letrun_tried` bookkeeping + terminal-DWELL vs terminal-LETRUN split (`pilot.py:1614-1688`; `_try_terminal_letrun`/`_try_terminal_dwell` in `steer.py:569-788`) | If a key was already let-run with the same rung count (`letrun_tried[key] >= len(rungs)`), do ONE bounded verified cone-**dwell** instead of re-coasting — re-running the ejection-guarded letrun would only re-eject and re-investigate deterministically. | The letrun coast is deterministic given the held inputs; re-coasting wastes budget and re-investigates identically. The old code side-stepped it with a bare `_settle_cone` that skipped verify; the dwell restores verify. | A **receipt-keyed memo**: `(world-key, holds) → {reached | ejected@bump | stalled}`. Re-visiting a memoized world+holds needs no re-coast. | **SHRINKS** — the bookkeeping dissolves into a receipt cache. *Residual / caveat (borderline UNCLEAR):* the world **key masks accumulators** (`acc_indices → None`), so two worlds differing only in a still-running timer share a key. A pure `(key, holds)` memo would wrongly report "already ejected/stalled" when more dwell would in fact reach the target as the timer ramps. The receipt must carry a `pending_effects` / "outcome contingent on a running accumulator" flag so the memo is only trusted when the world is quiescent — same masking issue as V1. |

---

## _ops.py / steer.py — the coast primitives themselves

| # | Rule (file:line, shape) | What it does today | Why it exists | Bump-world replacement | Verdict |
|---|---|---|---|---|---|
| S1 | `_COAST_BUDGET = 10_000` + `_coast_to_value` / `_coast_holding_state` (`_ops.py:204,207-340`) | Coast (folding) until `channel == target` or `reached_fn`, with a `when(_ejected).pause()` guard that stops immediately on ejection; budget 10k. Active oscillating holds force `cycle_fold_until` (no dt-skip); otherwise `run_until(fold=True)`. | Generous bound so a self-advancing dwell (a 39k-scan-id dry timer folded down) completes, while a non-completing coast still terminates. | This is **already the closest thing to CoastSession**: the pause-guard *is* an eject bump; the budget is a timeout bump. The redesign generalizes the single pause-guard to a bump vocabulary. | **SHRINKS** — eject-guard → eject bump (exists today), budget → timeout bump; residual = the 10k policy horizon. |
| S2 | `_settle_cone` fixpoint + `_SETTLE_CONE_CEILING = 16` / `_LETRUN_DWELL_CEILING = 64`, floor=2 (`steer.py:58-98`) | Coast until **no cone tag changed since the previous scan** (a cone fixpoint), after a 2-scan propagation floor, capped at 16 (settle) / 64 (dwell). | Logic takes ≤ 2 scans to propagate (floor); stop at the cone fixpoint; ceiling bounds it. | **cone-silence / set-fixpoint bump** (no tag in a set changed across two scans) + a **timeout bump** (ceiling). Both fold-evaluable. | **SHRINKS** — the fixpoint is a multi-tag silence bump; residual = the propagation floor (a real 2-scan ladder-latency idiom — keep) and the ceiling policy constants. |
| S3 | `reached_fn` transient short-circuit in `_settle_cone` / `_apply_actions` (`steer.py:93,147-153`) | Stops the settle **the scan the target holds**, so a one-scan transient (`STARTING` for a single scan on the way to `EXECUTE`) is caught before the fixpoint coast blows past it. | The cone-fixpoint coast and the delayed-effect fast-forward step straight through a one-scan transient; the post-settle `target_reached` check never sees it. | The target predicate must be a **per-scan bump**, and the receipt records the transient crossing. | **UNCLEAR (corrected — see item 4 below)** — the fold cannot skip a program-visible transient (writes break the plateau guard). The real gap: `reached_fn` is an **opaque Python callable**, so the fold cannot extract crossings from it; a predicate over a fold-advanced tag (accumulator equality) can be landed past, and a predicate over an excluded-churn tag can be folded through. *Remedy:* bumps compile to Conditions and register through the existing `run_until` crossing/target seam; step-mode only as fallback for uncompilable predicates. |
| S4 | `_settle_delayed_effects` `scan_budget=2000` (`_ops.py:521-582`) | Phase 1: `run_until(harness.pending_count == 0)` then +1 scan for plant latency. Phase 2: `run_until(~TT, fold=True)` to skip timer ticks. Bounded 2000. | After a pulse, delayed physical feedback and timer accumulation must settle before judging; bounded so it terminates. | **harness-quiescent bump** (`pending_count == 0`) + **timer-done-pending-resolved bump**; budget → timeout bump. | **SHRINKS** — both quiescence conditions are per-scan state predicates (fold-evaluable); residual = the +1 plant-latency scan (a real scan-boundary ladder idiom — keep) and the 2000 policy. |

*Out of scope but noted:* `accumulators._MEASURE_BUDGET = 2000` (empirical scans-to-eject
measurement) is a driver-characterization horizon, not exit classification — navigation-side.
`corrections._EXPOSURE_DEPTH`, `gauge._RELAY_DEPTH` are correction-derivation / gauge internals.

---

## Verdict tally

- **DISSOLVES: 1** — I3.
- **SHRINKS: 13** — D1, D2, I1, I5, O1, O2, O3, P1, C2, S1, S2, S4 (and the shared `_SKIFF_SCANS` counted once as I5).
- **KEEP: 7** — D3, I6, I7, V2, V3, P2, C1.
- **UNCLEAR: 4** — I2, I4, V1, S3.

The hypothesis holds broadly: every hard-coded *settle loop, stability window, and coast
budget* (D1, D2, I1, I5, S1, S2, S4, and the P1/C2/O2/O3 triggers) shrinks to a
bump + policy, and one (I3) dissolves outright. The KEEPs are all either genuine search
economy (I6, I7, V2, C1) or real ladder-idiom policy (D3, V3, P2) — none is a
blind-coast compensation. The four UNCLEARs are the design gaps.

## The four UNCLEAR items (design gaps in the bump vocabulary)

1. **I2 — eject-bump scope differs live vs. replay.** The watched-tag set that arms the
   eject bump must be a caller-supplied session parameter, narrower than "all state-machine
   roles." *Scenario:* the checkpoint world holds scratch registers (`isCmdValid__cmd`,
   `sm__where2jump`) mid-settlement; a role-scoped eject bump fires on that transient a few
   scans into the replay while the channel still reads its held value, and the first-ranked
   hypothesis is confirmed against a false pause. The bump vocabulary can say "a watched tag
   departed" but not, today, "watch *this* narrowed set, which is different from the set the
   live coast watched."

2. **I4 — bump-local confirmation loses the trajectory-global invariant.** Replaying to the
   original bump scan can prove "original bump silenced + a different next bump appears," but
   cannot prove the correction doesn't pin a downstream progress register behind the goal
   after the window. *Scenario:* a one-sided liveness hold silences the watchdog bump at scan
   N but pins `Heat_CurStep = 1` forever; bump-local confirms, the whole-journey replay /
   the static `_active_rungs_defeat_needed` screen catches the pin. No bump predicate expresses
   "no frontier register ends pinned behind the goal over the remaining trajectory" — it is a
   whole-path property, not a pause property. Keep the static needed-defeat screen regardless.

3. **V1 — the state key is lossy, and the excursion is two bumps in one window.**
   (a) The world key is threshold-masked, so a real advance (`count 1→2` under `count < 3`)
   projects to *no key change*: a "did the key move" bump is blind to threshold-aliased
   progress and only a **separate gauge-ordinal bump** sees it — the two must be distinct
   predicates that can disagree. (b) The excursion (`post_pulse_key ≠ frame.key`, then
   reverted by settlement) is a genuine **two-bumps-in-one-window**: a single end-of-coast
   evaluation cannot see it. The protocol must report **intermediate** bumps within a window,
   not just the settled state.

4. **S3 — an uncompiled bump can be jumped past by the fold.** *(Corrected after review —
   the original "fold skips a one-scan STARTING transient inside a plateau" scenario is
   impossible: a state write is a visible change, the plateau guard (`fold.py` module
   docstring) refuses to fold any window containing one, and `run_until` lands exactly on
   the nearest crossing. The dt-knob fold does NOT unsoundly skip program-visible events.)*
   The real, narrower gap is about **who counts as a reader**. Fold soundness is defined
   relative to a read-set: what the program reads plus the tags in the caller's condition
   (`_extract_condition_crossings`, and `target_names` are subtracted from churn exclusion).
   Two bump shapes fall outside that read-set today: (a) an **opaque Python callable**
   (`reached_fn`, steer.py) has no extractable crossings, so a predicate over a tag the fold
   advances in closed form (e.g. an accumulator equality `Acc == preset-5` when the crossing
   arithmetic jumps to `Acc >= preset`) can be landed past; (b) a bump watching a tag
   classified as **excluded churn** (`_unread_churn_tags` / `_disjoint_churn_closures`,
   fold.py:368,398) — changes on such tags are legitimately folded through because the fold
   was told nobody reads them. *Remedy:* bumps must be **compiled Conditions registered
   through the same seam `run_until` already uses for its terminal condition** (crossing
   extraction + protection from churn exclusion); then the fold stays sound and lands on
   every bump scan with no step-mode needed. Step-mode is only the fallback for bumps that
   genuinely cannot compile to a Condition.

## Receipt spec — minimal fields to decide every DISSOLVES/SHRINKS policy from the receipt alone

Derived from what each row's residual policy must read:

- **`scan_id`** — when the bump fired. (D1, D2, I1, I3, C2, and every settle-count consumer.)
- **`stop_reason`** — enum `{reached, ejected, silence, cone_fixpoint, timeout, gauge_advance, gauge_loss, harness_quiescent, latch_fired}`. Distinguishes a silence landing from a timeout landing (D2), a sterile timeout from a productive one (O3), and drives the provisional lifecycle (P1). **First-of-several must be recorded** — I3 needs "which of {eject, target, silence} fired first."
- **`watched_tag` + `from_value` + `to_value`** — the register that crossed and its transition. (D1/D3 settled value, I1/I2 channel move, O1/O2 departure, S1 ejection.)
- **`writer_rung_id`** (node-aware, captured at fire time) — the rung that wrote `watched_tag`. (O1 agency; the redesign's core promise.)
- **`causal_roots` / `pilot_touched` flag** — was any pilot-forced tag a causal root of that write. A single writer id is insufficient for agency (O1); either carry the root set or run `cause()` at the pause scan.
- **`gauge_mark` + `gauge_compare_vs_anchor`** ∈ `{advanced, preserved, behind, unknown}` — the target-relative progress reading at the pause. (O3, P1, C2, and the SPIN/CYCLE ordinal escapes.)
- **`new_frontier_delta`** — new actions / new unsatisfied conditions the paused world exposes vs. the frame tree (O3 accept-if-new-frontier, V3 override, I4 "a different next bump").
- **`pending_effects`** flag — harness pending and/or a timer still accumulating (running accumulator). Makes the C2 memo sound and V1's pending escape explicit; a landing with `pending_effects` true is *not* quiescent and must not be classified as final.
- **`intermediate_bumps`** — the ordered list of bumps that fired *within* the window, not just the terminal one. Required for V1's excursion (key rose then reverted) and for any two-bumps-in-one-window pattern.
- **`prior_bump_identity`** — the original incident bump, so I4's "original bump silenced + a *different* next bump appeared" is decidable from the receipt.

## Bump vocabulary — deduplicated predicates the rows required

Each with "must be evaluable inside a compiled fold: yes/no/why."

1. **register-departed** — a watched tag leaves its anchor value. *Fold-evaluable: YES* — compare tag to anchor each scan. (S1 eject, I1/I2 channel move, O2 drift, D1.)
2. **target-reached / predicate-holds** — the goal predicate is true. *YES if a compiled Condition* — **CAVEAT (S3, corrected):** it must be a Condition whose tags register as fold crossings (the `run_until` seam); an opaque Python callable over a fold-advanced tag (accumulator) can be jumped past. Predicates over ordinary written tags are always safe — writes break the plateau.
3. **silence-for-N** — a tag unchanged for N consecutive scans. *YES* — needs an N-scan counter in fold state. (D1, D2, I1.)
4. **cone-silence / set-fixpoint** — no tag in a set changed since the previous scan. *YES* — compare a tag set across two scans. (S2.)
5. **timeout / budget-elapsed** — scan counter ≥ N. *YES.* (D2, S1, S2, S4, P1.)
6. **gauge-event** — an event-earned ordinal advanced / stepper crossed / gauge behind vs. anchor. *YES if the gauge is a compiled snapshot comparison* — and it **must be a predicate distinct from #1/#4**, because it exists precisely to see progress the threshold-masked state key aliases away (V1). (O3, P1, C2.)
7. **harness/timer-quiescent** — `pending_count == 0` and/or `~TT`. *YES* — harness/timer state readable per scan. (S4, and the C2/V1 pending flag.)
8. **latch/edge-fired** — a specific Bool rose within the window (watchdog `Done`, an antagonist latch). *YES* — edge-detect on a Bool. (incident eject Dones/latches; I4's "different next bump.")
9. **writer-attribution** — a specific rung wrote the watched tag this scan. *YES only if the fold emits per-scan rung-firing* (node-aware firing capture / scan_log); *NO for a bare compiled kernel that folds without firing capture* — **this is the feasibility crux** for the `writer_rung_id` receipt field. (O1.)
10. **excursion (two-in-window)** — the state key rose then reverted before settlement. *NO as a single terminal predicate; YES only if the fold reports intermediate bumps* (see #9's granularity and V1). Not a terminal-state predicate at all.

**Synthesis for the runner primitive** *(corrected after review)*. Predicates #1–#8 compile
to Conditions over tags, and the machinery to make the fold honor them already exists:
`run_until` extracts its terminal condition's crossings (`_extract_condition_crossings`) and
protects the condition's tags from churn exclusion. The seek-with-receipt primitive is that
seam generalized from one condition to a **vector of armed bump Conditions** — every bump's
tags become fold crossings/protected reads, the fold stays fully sound, and it lands exactly
on the first bump scan. No step-mode forcing is needed for compiled bumps; the dt-knob fold
does not skip program-visible events (plateau guard) and, once bump tags are first-class
reads, cannot skip bump-visible ones either. The genuinely special cases: #9 (writer
attribution) is read from the node-aware firing capture *at the landing scan* — it needs the
landing to be exact (which crossing registration guarantees), not per-scan emission
throughout; #10 (excursion) needs the intermediate bumps within a window reported, i.e. the
seek returns at the *first* armed bump rather than a terminal state — which is the protocol's
core semantics anyway. Step-mode remains only as the fallback for a bump that cannot compile
to a Condition (an opaque callable); the design goal is that no bump in the vocabulary needs
it. `cycle_fold_until` stays the engine when active oscillating holds are present, exactly as
today.
