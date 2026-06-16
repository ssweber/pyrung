# how(y_BurnerLoop) hang — findings (2026-06-10, probes in scratchpad/probe_burner*.py)

Program: `C:\Users\Sam\AppData\Local\Temp\CLICK (0032023C)\pyrung_project` (PackML template,
78 main rungs + 33 subs, 6,055 kernel tags, 39 ND inputs). Probe logs in
`scratchpad/probe_burner_out/`. Probe4 (bounded budget 2000 forks + real context,
seeding stubbed) may still be running in background.

## 1. The user-visible hang + memory ramp is NOT the walker

`how()` never reached `plan_walk`. It died in the prover pipeline prior:

- `classify_dimensions`: 47.6s, and runs TWICE per how(). Hot loop:
  `_domain_from_blockcopy` → `_resolve_tag_names` re-resolves whole BlockRanges
  (150-tag `ds.select` fills) inside the `while changed` fixpoint.
- `_pass_heuristic_seed_domains` = the killer. 39 ND inputs → 581 single-flip
  combos; `_behavior_fingerprint` returns (tag, v1, vN) for ~6,000 tags × 581
  combos ≈ 3.5M tuples PER PROBE, ~14 probes held in `fps` per candidate →
  1.37 GB RSS and climbing at 8 min (probe1). ND pass lower bound 1.4M kernel
  scans before bisection; bisects constants (sm__STATE*REF). Effectively
  unbounded in time AND memory. THIS is what the user kills at 5-10 min.
- Fixes: budget/cap the pass; skip constant candidates; fingerprint = hash
  (only compared for equality), restrict to downstream cone; cache pipeline
  context across how() calls. Probe3: stubbing _discover_domains → full
  _ExploreContext still builds in ~98s with all 39 nd dims (seeding adds ~nothing
  here).

## 2. Walker-only run (probe2, no context): structural gaps, not the 3 leads

Trace `probe_burner_out/walk2.log`. Recovery churns: "all orderings blocked …
decomposition (force-and-solve) may help" on the whole chain (S_UnitModeCurrent,
C_UnitModeChgRequestBool, Heat_xCall, Heat__x, Heat_Trans, o_BurnerLoop…).
Nogoods DO fire ("skipping known-blocked config") — no from_value starvation
visible. Root causes identified:

a) **Mid-scan-transient handshake goals (the big one).** PackML template
   consumes-and-clears its handshakes in the same scan (mode_change R8/R9 zero
   C_UnitModeChgRequestBool + C_UnitMode; same for C_CtrlCmd protocol).
   `_unsatisfied_conditions` spawns scan-boundary goals like
   `C_UnitModeChgRequestBool==1` that are NEVER true at scan boundaries →
   structurally unreachable sub-goals → every parent "all orderings blocked".
   Fix family: detect consumed-same-scan tags statically (writer-order in PDG),
   don't spawn boundary goals for them — bundle producer inputs into the
   parent's multi-input steer instead (alphabet already has "multi").

b) **Or() gates invisible to prereq extraction.** mode_change R5 gate
   `Or(S_Idle,S_Stopped,S_Aborted)` never spawned state-machine goals — no
   S_StateCurrent corridor in the entire trace. The cold-state path requires
   state machine → Aborted/Stopped first; never attempted. Or-decomposition
   exists at goal level but not in writer-condition extraction.

c) **Recovery-round multiplication.** Each goal's recovery is capped (3) but
   parents' recovery rounds re-create child goals which re-run their own rounds
   → ~3^depth re-walks of the same failing subtree (recovery_iters is global
   telemetry, hit 10+ in 2.5 min). Spin guard (open item 5) needed: same nogood
   set + same state + still failing = stop.

d) **fork() costs 95ms on this program** (`_refresh_known_tags_and_edges` walks
   whole program in PLC.__init__). Explore pays |alphabet| forks per node.
   With context, alphabet ≈ 300 steers (set-value steers for every in-cone ND
   input × domain values; cones here are program-wide) → ~30s/node, ~30min per
   explore call. 200k-fork budget = wall-clock unreachable → walk looks like a
   hang instead of honest NotFound. Fixes: cheap trial (snapshot/restore — the
   Future-scope item, now load-bearing), wall-clock budget knob, alphabet cap.

e) **Fold defeated by RTC churn (suspected, unconfirmed).** plc_date_time
   copies system.rtc.second → A_PLCDT_Second every scan (changes every sim
   second); not a self-referential calc, so the landed D2 fold rungs don't
   exclude it → plateau guard may never settle → timer dwells stepped
   scan-by-scan. Check `_visible_items` against A_PLCDT_*.

## 2.5 Probe4 terminal result (real context, seeding stubbed, budget 2000 forks)

how() returned in 448s: budget exhausted (2436 forks, 2424 scans) while STILL
inside the FIRST goal's establish (`S_CurrStep_Dry -> true`, zero nogoods, no
recovery rounds reached). The ~300-steer set-value alphabet × 95ms fork ate the
whole budget in one explore. At the default 200k-fork budget that's ~5+ hours
before an honest NotFound — confirms (d) as the walk-side wall-clock killer.
Fold journal note confirms D2 target-disjoint churn exclusion fired for the
alarm-trigger cone (fold passes working).

## 3. Verdict on the three candidate leads

- from_value nogood key-variance: NOT the blocker here (nogood skipping works;
  failing goals are Bool/pinned-value).
- Tier 2/3 force-and-solve: hints fire everywhere but are red herrings —
  pairs "couple" because sub-goals fail alone (a/b above), not from real
  mutual interference.
- Constructive regression (why() on stuck fork as goal source): WOULD help (b)
  (state-aware attribution names the false Or-branch / steady conditions
  instead of transient ones) — the most relevant of the three, but (a), (d)
  and §1 dominate.

## 4. Cold-start path CONFIRMED/CORRECTED (2026-06-11, probe_coldstart.py)

The manually-derived path above was WRONG at step 1 and missing a retentive
precondition. Empirical results (log: probe_burner_out/coldstart.log):

a) **True cold start is a TRAP — y_BurnerLoop is UNREACHABLE.** From
   all-defaults state, S_StateCurrent=0 (Undefined) is absorbing: every
   sm_ctrl_cmd2_state_request rung requires a named state bool, and
   mode_change R5 requires Idle/Stopped/Aborted. The documented
   {C_Abort,C_CmdChgRequest} pulse does nothing (cmd valid via the dh[0]=0
   mask quirk, but maps to no request). Single-pulse sweep of all 10 commands
   + Test_Simulate_1st_Scan: no escape. Seeded fuzz (seed 42, 5000 scans,
   random patches over all 25 HMI/physical inputs): no escape. **The honest
   how() answer from cold start is NotFound** — walk-side work should target
   fast termination + trap diagnosis here, not plan-finding.

b) **Second trap: Heat_Limit_Ts=0 first-scan race (real template bug).** Even
   bootstrapped from Aborted(9), heat()'s first scan has Heat_Limit_Ts=0: R5
   (Acc>=Limit & EnableLimit==1) fires Error=1 before R9 writes the real
   limit ( (135-0)*10 = 1350 ) — R9 is gated on CurStep==1 which only becomes
   true after the first scan, and within a scan R5 runs before R9. The single
   retry (production_execute_steps R9) fires next scan but heat R2's reset
   re-errors in the same scan (R5 still sees the pre-R9 value), burning the
   retry budget → Heat_Error=1 latches → heat alarm → auto-Abort 1.4s later
   (observed: Error@1810, Limit 0→1350 @1811, Execute→Aborting @1950).
   On real hardware this is masked by retentive memory from prior runs;
   factory-fresh memory would error the first dry cycle.

c) **Confirmed reachable path** (bootstrap {S_StateCurrent:9,
   Heat_Limit_Ts:1350}, dt=10ms, scan stamps from phase C):
   pulse {C_ProductionMode,C_UnitModeChgRequest} → mode 1 (scan 3) →
   pulse C_Clear → Clearing(1)@5 → Stopped(2)@6 → pulse C_Reset →
   Resetting(15)@7 → Idle(4)@8 → pulse C_Start → Starting(3)@9 →
   Rotate init@309 → Blower called @~410 (Rotate_tmr>4s), init@809 →
   Execute(6)@810 → HeatDelay 10s → Heat_xCall@1810 → Heat steps 1→2→3
   @2009-2010 (Heat_tmr.Acc>1) → o_BurnerLoop & y_BurnerLoop ON @2011
   (~20.1s sim). Held inputs: x_DoorClosed, x_LintDoorClosed, x_SailRelay,
   x_RotateFB, x_BlowerFB. **Periodic input required:** x_RotateSensor must
   toggle (on-dwell <2s, off-dwell <10s — rotate stuck-sensor watchdogs R10-12,
   else Rotate_Error=2 → abort at ~13s, before Heat_xCall at 18s). A plan with
   a recurring obligation is a new challenge class for the walker alphabet.

d) **§2(e) churn confirmed real:** A_PLCDT_Second changes 1/sim-second
   (4 transitions in 300 scans). Whether the D2 fold excludes it from
   _visible_items is still the open walk-side check.

Regression fixture: scratchpad/probe_coldstart.py (phases A-D, 53s wall).
Pair for tests: (1) cold-start how() → NotFound, fast; (2) bootstrapped
how() → the 4-pulse plan above.

## 5. Template fixes round 1 verified (2026-06-11, regenerated project)

User changes: init seeds S_UnitModeCurrent=3 (Manual); heat's Limit_Ts
hard-floor rung moved up to R2 (above timer R4 + error check R6). Project
regenerated with the new block-reference codegen (fed6f2d) — imports clean,
ds-range fills correctly alias block-ref tags (abort cleanup at scan 1951
cleared Heat_Error via fill(0, ds562-566)).

Probe results (probe_coldstart.py rerun, log coldstart_fixed.log; focused
dump probe_heat_retry.py):

- **Fix 1 partial:** manual jog outputs now cold-reachable (OCmd_JogRotate →
  y_RotateCt ✓). But y_BurnerLoop still cold-UNREACHABLE: state-0 trap intact
  (5000-scan fuzz, seed 42: no escape), mode pinned at 3 (manual R4 reasserts;
  mode_change R5 still gated on Idle/Stopped/Aborted). Burner needs production
  Execute → needs mode change → needs named state. Suggested: init also seed
  S_StateCurrent=9 (Aborted) — phase C bootstrap emulates exactly this and
  the downstream path works.
- **Fix 2 insufficient, two reasons:**
  (1) The moved rung is gated CurStep==1, but heat's true first scan runs
  with CurStep=0 (even-step handler advances 0→1 at the BOTTOM of that scan),
  so R6 still sees Limit_Ts=0 → Heat_Error=1 on heat scan 1. Gate it
  CurStep<=1, or seed Heat_Limit_Ts=15 in init (survives: stop-path fills
  zero ds562-566/ds569-575, skipping ds568).
  (2) Latent bug, also explains yesterday's phase B: validation R6's gate
  `C_P6_HeatMaxRetry >= 0` passes the default 0 → S_P6_HeatMaxRetry is
  overwritten to 0 EVERY scan (init's 1 never survives) → retry rung
  (prod_exec R9: count<=max, count pre-incremented to 1) can never fire →
  Heat_xReset never set → Error=1 latches → alarm → auto-Abort @+1.4s.
  Fix gate to >= 1 like the neighboring param rungs.
- Empirical sequence at Heat_xCall (scan-relative): +0 Error=1/Limit=0,
  +1 Limit=15 + retrycount=1 (R8 fires, R9 blocked by max=0), +2 Limit=1350,
  abort at +140.

## 6. Template fixes round 2 LANDED in Click (2026-06-11, round-trip verified)

Patches pasted via clicknick-cli rung apply/preview, regenerated 10:43:
init R3 copy(9, S_StateCurrent); heat R2 gate Heat_CurStep <= 1;
validation R6 gate >= 1; validation R9 P9 typo. Probe on regenerated source
(coldstart_roundtrip.log): **y_BurnerLoop ON @scan 2011 from TRUE cold start,
zero bootstrap patches**, Heat_Error=0 throughout, P6=1, holds +5s.

IMPORTANT for the walk regression pair: the CURRENT template is now a Found
case from cold (pulse plan: {C_ProductionMode,C_UnitModeChgRequest} → C_Clear
→ C_Reset → C_Start + held x inputs + x_RotateSensor toggle). A NotFound
fixture needs a pre-fix snapshot (drop the init state/mode seeds).

Side discovery: `rung apply` (ladder export) crashed on the regenerated
project — block-reference tags from fed6f2d (bank slots with nickname
overrides, intentionally no TagMap entry) didn't resolve. Fixed in
TagMap.resolve (src/pyrung/click/tag_map/_map.py): a tag whose backing block
formats to a valid Click address resolves to itself (same semantics as
_parse_hardware_tag). Tests: test_tag_map.py (3) + test_ladder_export.py (1).
Full suite 4234 green. Uncommitted as of this note.

## 7. §1 verified fixed; (d) landed; (e) checked not-a-blocker (2026-06-11)

Probe5 (walk5.log, post-90f5a10, no stubs, 2000-fork bound): pipeline now
reaches plan_walk in 50.4s — ONE classify_dimensions run (49.5s, was 2x47s),
seeding gone, no memory ramp. Walk side unchanged from probe4: budget
exhausted (2434 forks/360s) inside the first establish, zero nogoods.
Remaining §1 cost: classify_dimensions' _resolve_tag_names hot loop (49.5s)
— unfixed, lower priority.

(d) landed as two commits:

- 9c30548 perf(core): fork() takes the parent's tag index instead of
  re-walking the program AST (the walk was 92% of fork cost) — 95.5ms →
  5.6ms (17x). Tripwire: test_history.py
  test_fork_reuses_parent_tag_index_without_program_rewalk.
- f6cb103 fix(walk): set_value_relevance narrowing pass (enabling-named ND
  inputs keep full domains, unnamed in-cone remainder capped at
  _MAX_SET_VALUE_STEERS=24 — PackML alphabet ~300 → ~27); budget checks
  moved INSIDE the explore loop (per steer trial — one establish could
  previously blow arbitrarily past the caps, which is also why probe4/5
  overshot 2000→2434); wall-clock knob _WalkBudget.max_wall_s, public
  how(walk_seconds=). Calibrated reproducer test_walk_budget.py: flood
  program solves at 131 forks with the pass, needs 635 ablated.

Probe6 (walk6.log, how(y_BurnerLoop, walk_seconds=120), no monkeypatches):
honest wall-clock exit at exactly 120s; 3830 forks/3674 scans in the window
(probe5: 2434 forks in 360s ≈ 4.7x throughput). The walk now gets PAST the
first establish into recovery: 3 nogoods learned —
  0 -> 1 blocked by C_UnitModeChgRequest=True
  0 -> 1 blocked by HeatDelay_Tmr_Done=True, S_CurrStep_Dry=True
  False -> True blocked by S_StateCurrent=False
failure_kind no-recovery-goals on S_CurrStep_Dry. This is (a)/(b) terrain:
the handshake tag and the bare state tag are now the named blockers.

(e) VERDICT — not a blocker, no fix needed on this program. A_PLCDT_* is
not excluded (it's an unconditional copy from system RTC, not a self-calc —
no D2 rung claims it), but the plateau probe samples one scan per
_advance_time iteration and the RTC ticks once per 100 scans at dt=10ms:
a tick on a probe scan costs one react count, and react resets on every
productive jump. Empirical: probe6's nogood names HeatDelay_Tmr_Done=True —
the fold walked through the 10s HeatDelay dwell during recovery. LATENT
gap recorded: at dt≈1s the RTC ticks every scan → permanent visible churn →
pulse-steer folds (react cap 6) die program-wide. Park until a real program
hits it.

Where the 120s went (probe6): 3830 forks ≈ 19s + 3674 scans ≈ 30s leaves
~70s unaccounted — suspects: cause() recovery mining, _unsatisfied_conditions
SP-tree work, _apply_steer fold machinery. Profile before assuming.

## 8. (a) LANDED — the mode-change handshake solves on the live template

Four commits: 7bd89ea (v1: transient detection + 1-level bundles + goal
skip + governing skip + non-Bool multi patches), e95cc33 (ack-cleared
inputs + cross-scope detection via call gates + recursive bundles),
6ca17be (resting-value inference). Tests: test_walk_handshake.py (13).

What the template actually needed, beyond findings §2a's sketch:

1. **The HMI bits weren't steerable at all.** C_UnitModeChgRequest,
   C_ProductionMode (+15 more C_* command bits) are program-RESET
   (acknowledge pattern; the mode trio via a range reset
   `reset(c.select(1004,1006))`) → they have writers → TagRole != INPUT →
   absent from _external_bool_inputs → invisible to every steer. New
   widening pass `ack_cleared_inputs`: a Bool whose every program write is
   a provable literal default joins the steerable inputs (range targets
   resolved by the new `_literal_write`). 17 detected on the template.
   USER NOTE (2026-06-11): widen the same idea to INTs that only get
   reset/copy(0)/fill(0) — parked: an ack-cleared Int needs set-value
   domains (it is classified stateful, so it has no nondeterministic_dims
   entry; domains would need reader-comparison inference).
2. **Cross-scope transience via the call gate.** C_UnitModeChgRequestBool:
   producer in main R22, unconditional clearer mode_change R8, connected
   by `rung(ReqBool == 1): call(mode_change)` — the call gate IS the
   fires-when-set condition.
3. **The chain needs two simultaneous transient regressions** (call gate
   ReqBool ← C_UnitModeChgRequest; copy-source C_UnitMode ←
   C_ProductionMode): bundles expand recursively (depth 4, cycle-guarded,
   capped 6 states / 12 bundles).
4. **Resting value ≠ declared default.** C_UnitMode initializes to 5
   (Click initial value) but rests at 0 (mode_change R9). Transience now
   infers the rest from the clearers; boundary goals are skipped only when
   they differ from the rest value.

Verified (probe11): how(S_UnitModeCurrent == 1) from cold → 1 step,
{C_ProductionMode: True, C_UnitModeChgRequest: True}, 3.5s, replay-verified
— the exact §4c ground-truth pulse.

Side observation (probe8): the pipeline context build dropped from ~50s to
1.4–7.3s — that's 0564814 (user, same day): BlockRange tag-name resolution
cached in classify_dimensions (49.5s → 6.4s), closing the remaining §1
cost.

Probe7 (post-d, pre-a-v2) also showed the (c) spin shape crisply: recovery
iters 4–8 all "skipping known-blocked config" for the same goals
(S_Execute 1/4/6, Heat_xCall 3/5/8, C_UnitModeChgRequestBool 2/7) — same
nogood set, same state, still failing. The spin guard (open item 5) is the
next dominance candidate together with (b).

## 9. (c) LANDED, (b) blocked on the NotFound fixture (2026-06-11)

(c) spin guard: commit 3d2ef01. The agenda records failed goals keyed by
(goal, nogood-projected state) + store generation; a re-request matching
all three fails immediately ("spin-guard" failure kind) without
re-walking the subtree. Loop machinery, not a registry pass (module
switch _SPIN_GUARD for the test A/B only). Tripwire
test_walk_spin_guard.py: three parents sharing a circularly-dead
prerequisite — guard fires, recovery_iters strictly drop, verdict
unchanged; learn-then-retry (guard-then-clear) still solves.
Generation-check dynamics worth remembering: ANY store growth invalidates
all records, so the guard bites only once the nogood set plateaus —
matching probe7 (3 nogoods stable over 8 iters).

(b) Or-gates: INVESTIGATED, not implemented. Small Or-gate programs
(probe_orgate/orgate2) solve TODAY via the recovery oracle (cause() in
unreachable mode names the never-observed state bool; recovery walks it).
The current template fixture has mode_change R5's Or satisfied from cold
(init seeds S_StateCurrent=9 → S_Aborted true at boundary). The §2b
evidence came from the PRE-fix template; a failing tripwire needs that
snapshot (drop init's state-9/mode-3 seed rungs — §6 note). Per the
tripwire-first rule, writer-condition Or-decomposition waits for it.

Probe12/13 (y_BurnerLoop, walk_seconds=120): wall-exhausted NotFound but
with the mode-change handshake now ESTABLISHED inside the walk (holds at
failure: C_ProductionMode=True for S_UnitModeCurrent); first failing goal
S_CurrStep_Dry (no-recovery-goals), nogoods name HeatDelay_Tmr_Done /
S_StateCurrent. The remaining wall: the state-command chain
(C_Clear/C_Reset/C_Start through the C_CtrlCmd protocol — likely wants
the same handshake machinery to fire via sm_ctrl_cmd2_state_request) and
the PARKED recurring obligation (x_RotateSensor toggle) which caps any
full plan at the rotate watchdog (~13s sim) regardless.

## 10. The C_CtrlCmd chain answered: wrong suspect, three real defects (2026-06-11)

Probes 14/15 (probe_burner14/15.py, logs probe14*_console.txt). The
handoff question — why don't the landed handshake bundles fire on
sm_ctrl_cmd2_state_request — dissolved: **the chain needs no bundles.**
sm_map_cmd2_val runs unconditionally (main R27), so one ack-cleared C_*
pulse flows HMI bit → C_CtrlCmd → validity (dh[] mask) → state request →
sm_copy_or_jump_state, all in one scan. Nothing in the chain classifies
transient, and rightly so: isCmdValid_Yes is an OTE; C_CtrlCmd has
multi-scope writers AND genuinely persists after a VALID command (main
R30 zeroes C_CmdChgRequestBool first, so R31's clear never fires on the
valid path — C_CtrlCmd rests at the last valid command value).

- **how(S_StateCurrent == 2) from cold: SOLVES in 3.7s** (pulse C_Clear,
  hold rendered). The ack-cleared widening was the whole answer for the
  command pulses. Cosmetic debt observed: the winning multi-steer
  bundles unrelated ack-cleared bits (step 1 pulses A_Alm17_HiHeat_Trig,
  A_Alm1_PLC, A_Alm2_PLCLostData, C_RecipeChgRequest alongside C_Clear —
  replay-valid but operator-misleading; an alarm-trigger pulse is not a
  step a human should copy). A plan-minimization pass (drop-and-retest
  per input, post-verify) would clean this and any failed-detour actions
  the flatten-honesty change now keeps.
- **how(S_StateCurrent == 4) was a FALSE unsolvable** (27.6s,
  no-recovery-goals). From cold the template idles in Manual (mode 3);
  Resetting(15) completes only via production_states R11 — call-gated
  S_UnitModeCurrent == 1 (main R14) — so the corridor parks at 15 and
  the walk must name the mode prerequisite. Three stacked defects kept
  it nameless:

  1. *cause() crashed.* _rung_produces_value executes candidate writer
     rungs in isolation; sm_copy_or_jump_state R8 ends in
     return_early() → SubroutineReturnSignal leaked through every
     projected cause touching the state machine; _recheck_prereqs
     swallowed it → "no-recovery-goals" → false certificate. Probe15:
     plc.cause(S_StateCurrent, to=4) raised. FIXED 911fb23 (+ tripwire,
     + debug logging of swallowed oracle exceptions aca836b).
  2. *projected_cause copy-source blind.* copy(S_StateRequested,
     S_StateCurrent) was a candidate only if the source ALREADY held 4 —
     no candidate → generic self-blocker → still nothing minable. The
     source-at-value is now classified like a contact (enabling /
     trigger / BLOCKED_UPSTREAM blocker). Same commit 911fb23.
  3. *_unsatisfied_conditions copy-source blind* (static establish
     path): writer conditions were extracted, the data-flow binding
     (S_StateRequested, 4) never spawned. FIXED fad12ff.

  Landing 3 exposed a plan-tree honesty gap: sub-goals committed to the
  work fork (the mode bundle, corridor pulses) sat under
  boundary-unreachable conduit goals — (Req,4)-shaped transients rest at
  0 at every boundary, so the conduit goal FAILS after its children
  succeed — and _flatten_plan dropped the whole failed subtree while the
  work mutations stayed: the Path lied about the executed prefix and
  replay refused (observed: plan flattened to [({}, 1)]). Flatten now
  descends failed nodes for their solved descendants (raw segments of
  failed nodes stay out; replay stays the arbiter). Same commit fad12ff.
  Tripwire: test_walk_copy_source.py (distilled jump-state machine;
  4 tests). Walk suite 203, prove 549, causal 82 — zero existing-test
  edits except one repurposed copy-candidate test (justified in 911fb23).

- **Post-fix frontier on how(S_StateCurrent == 4):** honest NotFound;
  recovery names (S_StateRequested, 4); first failing goal at 60s was
  S_StateRequested -> 3 (budget), at deeper budget S_Starting -> true
  (bounds) — the walk descends the REAL completion machinery now, but
  _unsatisfied_conditions merges prereqs across ALL writers of
  S_StateCompleteBool (production_states R3's Blower__init/Rotate__init
  conjoined with R11's S_Resetting, though R11 alone suffices from
  Resetting) — the budget burns on the irrelevant writer's branch.
  **Writer-disjunction awareness (per-writer prereq alternatives,
  cheapest-first) is the named next lever.**

  Full-target check (probe13b, y_BurnerLoop @120s, post-fixes): honest
  budget NotFound, first failing goal still S_CurrStep_Dry
  (no-recovery-goals) — expected: S_StateCurrent==4 alone exceeds 240s
  under union-prereqs, so 120s on the full chain exhausts shallow. One
  junk nogood recorded ("0 -> 6 blocked by C_CtrlCmd=True,
  S_Aborting=True, S_Clearing=True, ... S_Idle=True, S_Stopped=True" —
  a mutually-impossible Booleanized Or-flattening) — that's the Or-gate
  blindness (§2b / Open #8) showing up in cause()'s leaf Booleanization;
  the (b) fixture stays the path to fixing it.

  240s run (probe14d): still budget-exhausted but ON THE RIGHT CHAIN —
  holds at failure: C_Clear=True (for S_StateCompleteBool), C_Reset=True
  (for S_Resetting); best partial plan 10 steps; first failing goal
  S_Starting -> true (bounds). Nogoods show the corridor also burning
  budget on irrelevant 15 -> 10/12/13 branches (S_StateRequested
  bindings for Holding/Unholding/Suspending). Both costs are search
  shape, not mechanism: (i) _unsatisfied_conditions returns the UNION of
  prereqs across writers — production_states R3's
  Blower__init/Rotate__init ride along though R11 ({S_Resetting ✓, mode
  call gate}) alone suffices; the agenda walks them all serially.
  (ii) the corridor tries every probed neighbor value. Per-writer prereq
  GROUPS (alternatives, smallest-unsatisfied-first — ordering advice,
  completeness-neutral since failed prereqs are already tolerated) is
  the next mechanism; design it tripwire-first next session.

## 11. Open #10 LANDED: per-writer prereq groups; indirect-copy crash fixed; new frontier is the REF-constant flood (2026-06-11)

- **Per-writer prerequisite groups landed** (`256ff29`, tripwire
  `test_walk_writer_groups.py`). `_unsatisfied_condition_groups` returns
  (exact historical union, one group per matched writer: gate values,
  copy-source binding, call-gate conditions, that writer's inequality
  prereqs); `_establish` walks the smallest-unsatisfied group first and
  probes the corridor between groups, with the independent-fork attempt
  now per group. Ordering, never pruning: union pairs not covered by any
  group ride in a final remainder group; ablation
  (`writer_prereq_groups`, ordering kind) restores the serial union;
  single-group walks reduce to the previous flow exactly. Calibrated
  tripwire: two-writer goal (counter-latched inits 25 edges each vs. a
  4-pulse stage corridor), grouped solves at a 60-fork budget (~22
  needed, plan 7 actions through the cheap writer), union exhausts it
  (~124 needed, 49-action pulse-all plan through the expensive writer).
  Fixture lesson: the goal register must STEP under a plain pulse
  (Mode 0 -> 1 on Kick) so it governs itself — otherwise `_governing`
  delegates to the richest writer gate and the prereq path never
  engages; and the cheap gate must need more edges than the corridor
  has value transitions, or the corridor BFS solves it by ride-along.

- **Indirect copy sources crashed the walk** (`306616c`). The grouped
  ordering immediately walked a goal inside sm_copy_or_jump_state's
  indirect machinery and `_written_value_for_tag` handed out
  `("literal", IndirectRef)` for `copy(ds[sm__jump_target_ds_idx],
  sm__where2jump)` — the first `==`/`!=` against the goal value built an
  IndirectCompare Condition that raised on truth-testing (phase C died
  in `_governing`). Sources that are neither named tags nor plain
  scalars now classify as None (statically unresolvable — the
  interpreted oracle still executes such rungs); `_values_match` treats
  comparison TypeError as non-match. Latent since the copy-source arc;
  any goal with an indirect-copy writer in its cone could hit it.

- **Phase C (`how(S_StateCurrent == 4)`) post-#10:** still honest
  budget-exhausted at 240s (5229 forks), but the shape moved exactly as
  predicted: nogoods no longer name Blower__init/Rotate__init (the
  Starting SFC detour is gone); the walk descends the jump-state copy
  chain — R8's binding spawns (S_StateRequested, 4), R11's spawns
  (sm__where2jump, 4), which dies honestly (indirect writer, no static
  prereqs, no recovery goals) and is the recorded first failing goal.
  The right first leg lands by t=25s: corridor reaches 15 in 4 actions,
  holds C_Clear + C_Reset.

- **The new budget sink (probe16, walk-debug log):** recovery
  blocker-mining floods goals across the sm__STATE*REF constant bank —
  (sm__STATEIDLEREF, 3), (sm__STATERESETTINGREF, 3), ... for every
  PackML state-reference register, each burning corridor probes at
  ~45ms/fork on the 6k-tag template until the spin guard catches the
  repeats (it fired 14 times between t=84s and t=120s; each round
  2-7s). These registers are write-once init constants — the walker can
  technically rewrite them (pulsing Test_Simulate_1st_Scan re-runs the
  init loads, which is why their corridors "succeed" in 1 action), so
  nothing refuses them, but they are operator-meaningless detours. Also
  visible: an isCmdValid_Yes coupling hint (isCmdValid__result /
  C_CmdChgRequestBool share a 200-tag cone). Candidate next levers, in
  suspected order: (i) deprioritize/last-order blocker-mined goals on
  pipeline init-constants (init_constant_projections richness 1 — an
  ordering pass over recovery goal order, completeness-neutral);
  (ii) goal-directed value ordering in the corridor (the 15 -> 10/12/13
  cost, already recorded in Open #10's text). Tripwire-first as always:
  distill a REF-constant-flood fixture before building (i).
  [Premise corrected in §12: the bank is NOT in
  init_constant_projections, and nothing re-runs any init loads — the
  registers are zero-writer ND inputs the walker set-steers directly.]

## 12. Open #11 lever (i) LANDED: ref_constant_order; the REF bank turned out to be ND inputs, not init constants (2026-06-11)

- **Premise check failed first (probe_refclass.py).** §11's lever (i)
  assumed `init_constant_projections` covers the sm__STATE*REF bank. It
  doesn't: the registers have ZERO program write sites — their values
  are declared initial data (`Int("sm__STATEIDLEREF", default=4)`;
  nicknames CSV "READONLY : State Machine Private Use Only") — and
  `detect_init_constants` requires literal write sites. The pipeline
  classifies all 17 of them nondeterministic input, domain (−1..18)
  inherited backward through `copy(REF, S_StateRequested)`. THAT is the
  1-action "solve": the walker set-steers the constant register
  directly. No init loads re-run anything (grep: no program writes to
  the bank exist).

- **The landed signal (the user's suggestion, sharpened):**
  never-written registers read as copy/fill sources are reference data;
  a goal that mutates one moves the goalposts. `_reference_constants`
  (walk/priors.py) collects them once per walk;
  `ref_constant_order` (ordering kind) defers them at both flood
  sites — `_establish` sorts writer groups by (mutates-a-ref, size),
  `_recover` stable-partitions cause()-named goals refs-last and probes
  the corridor once before the deferred tail. Ordering, never pruning;
  empty set (ablated / nothing detected) is bit-identical to the old
  order. Zero-writer tags read only in conditions (ordinary setpoints)
  are NOT collected; a copy-source setpoint like C_P2_Dry_Tm IS — an
  accepted ordering-only cost.

- **Tripwire (test_walk_ref_flood.py, calibrated @220 forks):**
  14-register bank + state maps `Cur == Ref_i` (gates satisfied from
  cold, so every decoy group is a one-item mutate-the-constant prereq)
  vs. the real writer on the last rung behind a 4-pulse stage corridor.
  Ordered: ~110 forks, 8-action clean plan, bank untouched. Ablated:
  ~1214 forks, 35-action plan rewriting all fourteen constants — each
  mutation self-defeating (breaks its own state map; the walk
  un-mutates to repair the map and re-mutates: the goalpost oscillation
  distilled). Fixture lessons: an external real-writer gate gets
  bundled into one multi-steer (no contrast at all); a 2-edge gate
  rides along on the corridor BFS (3-action solve) — the real gate must
  need more edges than the goal register has corridor values (the
  writer-groups rule again).

- **Template (probe17 = probe_burner17.py, S_StateCurrent == 4 @120s):**
  REF mutation detours GONE — zero REF corridor solves in the log;
  deferred (REF, 9) goals fail once cheaply, re-requests hit the spin
  guard (t=61–91s). The budget now reaches machinery probe16 never
  touched: C_CtrlCmd corridor to 9 by t=44s, `recovery iter 1 for
  S_StateRequested -> 9` with ONE blocking goal, honest "all orderings
  blocked for S_StateRequested 0->9" hint. First failing goal unchanged:
  `sm__where2jump -> 4` (the indirect jump-table read,
  `copy(ds[S_StateRequested + 150], …)` — statically unresolvable; an
  interpreted lever is the named frontier). The isCmdValid_Yes Tier-2
  coupling hint repeats. 2481 forks vs probe16's 2190 in the same 120s
  (cheaper per-fork work — no more 6k-tag REF corridor explores).
