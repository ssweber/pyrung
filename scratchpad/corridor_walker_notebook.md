# Corridor Walker — Notebook

Architectural context: [corridor_walker_plan.md](corridor_walker_plan.md).

---

## Status (2026-06-12)

The walker is the sole `how()` path in
`src/pyrung/core/analysis/walk/` (own `CLAUDE.md`). Entry: `plan_walk`,
called from `PLC._how_via_walk` (`core/runner.py`). Tests:
`tests/core/analysis/test_walk_*.py` via `make test-walk` (233 tests); full
suite green. Stages A–D landed 2026-06-10; four hardening arcs landed
2026-06-11; idx-chasing arc (Open #11 jump-table lever) landed 2026-06-12;
compound-goals must-stay arc landed 2026-06-12 (committed conjuncts
re-checked after every later goal's walk + reorder resolver,
`test_walk_compound_goals.py`).
The bank-aliasing fix landed the same day (memory
`bank-aliasing-unification-arc`) and the re-baseline on the regenerated
template DISSOLVED the `isCmdValid_Yes` frontier: `how(S_StateCurrent ==
4)` solves reachable=True in 3.5s through real command masks and the
commissioned jump table, unpatched. Frontier: Or-gate fixture (#8) +
recurring obligation (#9, `y_BurnerLoop`).

---

## What's built

One interpreted best-first engine (`engine.py`), all of it replay-verified and
covered by `make test-walk`. Capabilities, compressed (mechanism details live
in docstrings and the Findings section):

- **Corridor core** — governing-tag selection with a simulation-probe fallback
  (`_probe_steps`: fork-steer-observe, immune to static blindness); steer
  alphabet (empty, pulse, drive-low, multi-input conjunctive groups, non-Bool
  set-value from pipeline domains) with helpful-steer ordering and
  release-then-pulse for edge-gated commands; time-folding to accumulator
  crossings (dt-knob for timers, acc-patch for per-scan counters, plateau
  guard as the soundness gate); dynamic reaction budget.
- **Goal handling** — And/Or decomposition; recursive prerequisite discovery
  through writer SP-trees and subroutine call gates (depth 6, cycle-checked);
  inequality prereq resolution (`gt/ge/lt/le` → concrete values); seal-in
  break (inverse regression for OTE/latch); idx-chasing for indirect-copy
  writers (invert `copy(block[idx], tag)` on the live snapshot, sub-goal the
  index register; calc-scratch pointers hopped via pipeline func-dep
  projections or the sole writer's calc expression); transform-chasing for
  pack/unpack and copy-family converters is the next data-flow boundary
  (#13); `avoid=` support;
  compound-target must-stays (committed conjuncts re-checked on the work
  fork after every later goal's walk — a regression fails the attempt with
  a `goal-regressed` node and `plan_walk` retries with the clobbering goal
  promoted ahead of the goal it broke, tried-set terminated, holds rolled
  back per attempt; the replay verify backstop returns a diagnosed Path
  naming the unmet conjunct instead of a bare None).
- **Recovery + learning** — oracle-driven re-check (projected
  `cause(tag, to=value)` mining triggers and blockers as sub-goals, bounded by
  `_MAX_RECHECK_ITERS=3`); numeric compare blockers carry `BlockingRelation`
  metadata plus candidate scalar moves, reusing the same functional-dep /
  inequality inversion that static prereq extraction already proved on fill;
  `NoGoodStore` keyed `(from, to, frozenset(blocking))` with scalar or
  relation facts, seen-key projection onto mentioned tag names, and a
  blocker-clearing move; `_needs_decomposition` Tier-2 hinting with
  pre-clobber checkpoint.
- **Holds (prevention before recovery)** — `HoldStore` of (input, value,
  committed goal); selective release in `_steer_prefix` (protected names skip
  every implicit release); empirical divest probe for intended writes to
  protected inputs; per-branch tracking on `_Node.released`, reconciled at
  commit; `_commit_holds` at every corridor commit point; `Path.holds` +
  "Holds:" rendering. The recovery loop stays as backstop and is
  tripwire-covered (cross-guard 2 iters, serial-clobber 3 iters).
- **Independent-fork walks** (Tier 1, generalized) — pairwise-disjoint-cone
  independence gate, per-fork sub-walks, cone-filtered hold extraction,
  simultaneous multi-steer with multi-timer fold convergence; applied at all
  four serial-walking sites.
- **Physical layer** — Harness propagation through `fork()`; linked-feedback
  tags excluded from the steer alphabet (the walker steers enables, the
  Harness synthesizes feedback); `how(unlink=)` fault scenarios, mirrored onto
  verify/annotate forks via `_install_replay_harness`; profile-gated
  advancement (`_advance_time` recognizes ramping profiles as progress).
- **Plumbing** — BFS/waypoint fallback deleted; `how()` returns a verified
  `Path` or `Path(reachable=False)`; prover pipeline context consumed via
  `allow_partial=True`; walker in its own package with its own contract
  (`walk/CLAUDE.md`).

---

## Validation status

| Target | Corridor type | Steer | Result | Notes |
|---|---|---|---|---|
| `StateCurrent==EXECUTE` from ABORTED | mode machine | input pulses | walk ~2 s, replay→6 | go/no-go |
| `_CurStep==5` from EXECUTE | task timer wait | empty (folded) | walk, replay→5 | folded via dt-knob |
| counter dwell 0→1 (synthetic) | per-scan counter | empty + pulse | folds via acc-patch | exact landing, replay-verified |
| `how(Ready, Done)` (two-step latch) | compound And | input pulses | walk 3 steps, 0.0 s | Or/And decomposition |
| `y_Burner` from cold (nested) | 3-layer timer-gated | CmdMode + CmdStart + 2 folds | walk 5 steps, ~1.3 s | recursive prereqs through 3 sub layers |
| `StateCurrent=="IDLE"` from cold | mode (string operand) | input pulses | walk 2 steps | simulation probe finds steps |
| inequality-gated transitions | analog/Int ND input | set-value | walk via pipeline domains | `nondeterministic_dims` steers |
| callable predicate (`expr=None`) | opaque | — | xfail | needs expr decomposition |
| linked feedback exclusion | Harness-driven fb | input steers | walk via enables | fb excluded from steer alphabet |
| `how(unlink=["Fb"])` fault | broken sensor | direct force | walk forces fb | bypasses physical chain delay |
| profile-gated (`Temp >= 5.0`) | analog ramp | hold + profile | walk ~500 scans | Harness ticks profile on fork |
| serial clobber | coupled latches | pulses + reset | walk recovers via oracle | `test_walk_decomposition` |
| cross-guard mutual clobber | coupled latches + 2 timers | holds + reset | walk recovers, ≤2 iters | `test_walk_nogood` |
| Int command protocol | multi-hop state machine | CmdReset + CmdStart | walk 3 actions | `test_walk_real_patterns` |
| return_early() flow gating | subroutine flow control | Enable pulse | walk reachable | `test_walk_real_patterns` |
| rendezvous (two SFCs) | independent subsystems | multi-steer (Tier 1) | walk 2 actions, 30 scans | `test_walk_real_patterns` |
| odd/even step sequencer | self-increment + even skip | Advance + fold | walk reachable | `test_walk_real_patterns` |
| deep call chain (5 levels) | 5-level prereqs, 3 sub scopes | CmdProd + CmdReset + CmdStart + fold | walk reachable | `test_walk_real_patterns` |
| holds prevention A/B | serial corridors sharing enables | holds + selective release | zero recovery iters | `test_walk_holds` |
| set-value flood (30 noise ND) | 3-step Mode corridor | multi + pulses | solves at 131 forks (ablated 635) | `test_walk_budget` |
| consumed-same-scan handshake | mode-request protocol | simultaneous bundle | walk 1 step | `test_walk_handshake` |
| PackML chain (ack-cleared + call gate) | 2-level transient | bundle {ChgReq, ProdMode} | walk 1 step | `test_walk_handshake` |
| **live** `S_UnitModeCurrent==1` | real PackML mode change | bundle {C_ProductionMode, C_UnitModeChgRequest} | walk 1 step, 2.9s | ground-truth pulse; re-confirmed post-aliasing-fix from mode 3 / state 9 |
| circularly-dead prereq | spin-guard shape | — | honest NotFound | `test_walk_spin_guard` |
| **live** `S_StateCurrent==2` | C_CtrlCmd command chain | pulse C_Clear | walk 2 steps, 3.7s | no bundle needed |
| **live** `S_StateCurrent==4` | mode-gated completion | alarms + C_Clear + C_Reset | walk 5 steps, 3.5s | post-aliasing-fix template; was budget NotFound on blank config ROM |
| ref-constant bank (14 REFs) | ref-goal flood | Arm ×4 + Go | ~110 forks (ablated ~1214) | `test_walk_ref_flood` |
| copy-source chain | mode → completion → state copy | Adv + {ProdMode, ChgReq} | walk reachable | `test_walk_copy_source` |
| two-writer goal | writer disjunction | AdvB + Kick | 60-fork budget (ablated ~124) | `test_walk_writer_groups` |
| indirect-copy writer | statically unresolvable | — | honest unreachable, no crash | `test_walk_copy_source` |
| **live** `y_BurnerLoop` | full chain | — | honest NotFound @120s | blocked on #9 rotate toggle |
| jump-table writer (plain + expr ptr) | indirect copy | Sel + Go | walk reachable | `test_walk_copy_source` idx-chase |
| calc-scratch pointer (template shape) | indirect via scratch | Sel + Go | walk reachable | hop via calc expr / func_deps |
| REF-fed index (no literal writers) | copy-source candidates | Arm + Go | walk reachable | the probe20 blindness, pinned |
| zero jump table | indirect copy | — | honest unreachable | chase refuses, no inverting index |
| **live** `(sm__where2jump, 4)` | commissioned table (native) | — | binds `(S_StateRequested, [4, 15, 17])` | probe_idxchase_live, post-aliasing-fix |
| compound clobber (mode resets step) | And-of-Compare conjuncts | reorder retry | walks 4 steps from either order | `test_walk_compound_goals` |
| conflicting conjunction (pinned step) | And-of-Compare | — | honest unsolvable, names conjunct | `test_walk_compound_goals` |
| **live** `(S_StateCurrent==4, S_UnitModeCurrent==1)` | compound state+mode | alarms + Clear/Reset + mode bundle | walk 6 steps, ~5s either order | probe_compound_goal, no reorder needed |
| **live** `fill_stepNumber==4` | relation-gated fill dwell | tare + analog set-value | walk 24 actions | `probe_fill_hold`; causal blocker records `pv_LevelHt < calc_levelSvLowerWBand`, recovers `sv_levelSetPoint=100.0` + `systemLevel_opt2011=1` |
| full suite | all types | all steers | 233 pass | walker-only `how()` |

---

## Findings (so we don't re-derive)

- **`fork()` is a true checkpoint** — carries `.tags`, `.memory` (incl. `_frac:` timer
  fraction), time mode, dt, RTC, and `_harness` (feedback couplings + pending patches).
  Verified bit-identical continuation across 20+ scans after a mid-fraction fork.
  Backjump via `fork(scan_id)` (runner.py) rests on this.
- **Corridor source is NOT the old waypoint front-half.** `_order_waypoints`
  collapses coupled-tag SCCs into mega-waypoints (> `_MEGA_CONE_LIMIT`), and
  `_build_value_transitions` is empty for `copy`-written tags. The real graph
  comes from interpreted probing.
- **Steering uses the interpreted runner, not projected `cause()`/`effect()`.**
  The runner is the forward oracle (multi-scan, full state); `cause()` is
  reserved for the backward (divergence/nogood) direction at ScanLog fidelity.
- **External inputs are sticky** (hold last value); `patch()` clears the patch, not the
  tag. Edge-gated commands need release-then-pulse.
- **Verification is interpreted** (replay on a fresh fork) — no compiled-kernel
  agreement risk for the walk path.
- **No abstraction gap (the oracle advantage).** The "model" is the program on
  a fork — correct by construction. Tradeoff: nogoods generalize less freely
  (a nogood at S might not hold at S′), but never false positives. Closer to
  concolic execution / RRT than classical planning.
- **`why()` is the state-aware candidate generator** — minimal load-bearing
  contacts for the current fork state via SP-tree attribution; terminates at
  external inputs; handles latch seal-in vs. OTE differently; multi-tag tree
  structure IS the factoring structure.
- **`why()` and `projected_cause()` resolve subroutine rungs** (both had the
  same blindness, fixed: check `node.subroutine`, resolve from
  `program.subroutines`). `effect()` unaffected (rung_idx from simulation
  capture).
- **Multi-tag factoring uses writer-condition extraction, not `why()`.**
  `_unsatisfied_conditions` reads writer SP-trees + subroutine call gates
  directly — sufficient for producer-consumer hierarchies. `why()` stays
  available for static SCC decomposition if ever needed.
- **Dynamic prerequisite ordering is sufficient.** No explicit topological
  sort: walking A discovers B as its own prerequisite; `visited` prevents
  re-walks; depth bound 6 is conservative.
- **`cause()` is the validation/nogood oracle.** Trigger vs. enabler split on
  the scan log. The scan log IS the implication graph — no symbolic
  derivation. Exploration settled it over the SP-tree for blocking facts:
  `_unsatisfied_conditions` returns `[]` for guard-gated arms where
  `cause(tag, to=value)` cleanly names the blocker.
- **Projected `cause()` must speak relations, not only scalar samples.**
  Numeric blockers like `pv_LevelHt < calc_levelSvLowerWBand` are recovery
  preconditions, not just `pv_LevelHt=-1.0`. `BlockingRelation` preserves the
  false comparison and carries candidate scalar moves derived from domains,
  current snapshot, writer-produced values, and affine functional deps. The
  fill case now gets the same useful goals that `_unsatisfied_condition_groups`
  already found statically: `sv_levelSetPoint=100.0` then
  `systemLevel_opt2011=1`.
- **Pipeline `allow_partial` is safe for the walker** — infeasible tags are
  simply absent from dimension dicts; `always()`/`never()` Intractable gating
  unchanged.
- **`avoid_pred` receives `dict(tags)`, not the state object.**
- **Intermediate-result prerequisites must not block retry** — some writer
  conditions are corridor-internal (e.g. `Trans==1` set mid-fold); `continue`
  past failed prerequisites and retry `_explore`, which handles them via
  time-folding.
- **Pipeline domains are boundary-focused** — behavioral bisection gives
  expression partition values (comparison literals ± 1 + default), 5–10 per ND
  input; no extra thinning needed.
- **Tier 1 insertion is before the delegate corridor, not after** — for
  rendezvous, `_explore` succeeds on the delegate and the failure is
  downstream in residuals; preempt the serial walk when
  `governing != target_tag`.
- **Cone-filtered hold extraction prevents steer-release contamination** —
  only inputs causally connected to the prerequisite are collected from a
  sub-fork's actions. (Post-consolidation, first-class holds should make the
  cone filter deletable.)
- **`TagRole.INPUT` is too narrow for steerability** — "has any writer" ≠
  "not operator-driven". PackML acknowledge patterns (program resets the
  HMI request/mode bits, often via range resets) make the actual operator
  inputs PIVOTs; `ack_cleared_inputs` re-admits them. Watch for other
  acknowledge shapes (toggling, echo registers).
- **Resting value ≠ declared default.** Click projects can declare nonzero
  initial values (and retentive semantics make even honoring them suspect —
  separate codegen investigation pending); anything boundary-anchored in
  the walker must infer the rest from the program's own clearers.
- **Budget enforcement must reach inside the explore loop.** The agenda
  checks `budget.exhausted` between resolver steps, but one establish can
  run an entire explore (and its recursion) inside a single step — without
  the per-steer check, fork caps overshoot unboundedly and a wall-clock
  cap is meaningless.
- **The recovery oracle compensates for Or-blind prereq extraction on
  small programs** — cause() in unreachable mode names the never-observed
  state bool and recovery walks it (probe_orgate/orgate2 both solve
  today). The §2b Or-gate gap is only observable when recovery rounds are
  consumed elsewhere or the chain is deeper — hence the fixture
  requirement in Open Items #8.
- **Spin-guard generation dynamics**: keying failed goals on the add-only
  store's size means any new nogood (anywhere) re-opens all failed goals;
  the guard engages once the nogood set plateaus — which is exactly when
  the spinning starts (probe7: 3 stable nogoods over 8 iters).
- **`rung.execute` in isolation must contain `SubroutineReturnSignal`** —
  `return_early()` is caught by the executor's subroutine loop, so any
  analysis that executes a rung outside it (the projected oracle's
  candidate check was the one site) leaks the signal. Writes captured
  before the signal are exactly the real in-scan semantics.
- **Writer regression has a data-flow half.** Conditions name the gates;
  a `copy(SRC, tag)` writer's source-at-the-goal-value is an equal
  prerequisite. Both regression tools (the projected oracle, the static
  extractor) only carried the control-flow half until the copy-source
  arc; the handshake bundles had the concept first (transient sources).
- **The flattened plan must equal the executed work prefix.** A solved
  sub-goal's commits are on the work fork even when its parent goal
  later fails (boundary-unreachable conduits like `(Req, 4)` fail AFTER
  their children land the real work); dropping the subtree makes the
  Path lie and replay refuse. Failed nodes' own raw segments stay out.
- **Resting value can be path-dependent**: `C_CtrlCmd` rests at the last
  *valid* command (main R30 zeroes `C_CmdChgRequestBool` before R31's
  clear can fire; only invalid commands get cleared) — `_scan_transient_
  rest`'s refusal is correct, not conservative slack.
- **Cross-writer prereq union is a real budget sink** — merging
  `_unsatisfied_conditions` across all writers conjoins one writer's
  expensive requirements (Starting-SFC inits) with another's satisfied
  ones (Resetting); per-writer groups landed as Open Items #10.
- **Writer-groups fixture requirements**: the goal register must *step
  under a plain pulse* so it governs itself, and the cheap writer's gate
  must need more edges than the goal corridor has value transitions.
- **Indirect copy sources are not literals.** `copy(block[ptr], tag)`
  sources classify None (statically unresolvable); `_values_match` treats
  comparison TypeError as a non-match (premature refusal, never crash).
- **Reference-constant goals are a flood channel — and they are ND
  inputs, not init constants.** Zero program write sites → pipeline
  classifies nondeterministic input with full state alphabet. Each
  `(REF, v)` goal "solves" in one action by set-steering directly —
  goalpost-moving mutation. Classification: never-written tags read as
  copy/fill sources; ordering refs-last is completeness-neutral.
- **Slice elision beats functional-dep detection on jump-table scratch —
  fixed for walk mode by advice-grade projections.**
  `detect_functional_dependencies` originally considered only
  `stateful_dims` survivors; the template's pointer scratch
  (`sm__jump_target_ds_idx`, `isStateEnbl__mask_idx`,
  `isCmdValid__dh_base`…) is `elided=slice` first, so
  `functional_dep_projections` was EMPTY on the live template. Under
  `walk_only` the pass now also admits elided and ordering-violating
  candidates (the CopyOrJump loop rewrites the source after the scratch's
  writer, failing the sequential-scope check) and records them as
  **advice only** — no dims/elision bookkeeping, so prove behavior is
  bit-identical and BFS never sees them (walk_only runs no BFS). Live
  template: 7 projections, including `sm__jump_target_ds_idx =
  S_StateRequested + 150` and `isCmdValid__dh_base = C_CtrlCmd + 100`.
  The walker consults these first; the calc-expression fallback
  (`_single_calc_definition`) stays for non-affine single-tag shapes.
- **The live template's jump-target table was zero from cold — a
  twin-fidelity bug, FIXED 2026-06-12** (bank-aliasing unification:
  `map_to` stamps slot identity onto the banks; universal codegen slot
  emission carries name+default on the slot; one tag per register —
  the raw `DS165`-style keys no longer exist). Original symptom: `map_to`
  was metadata only; indirect reads (`ds[expr]`/`dh[expr]`) resolved
  through the raw block slot (default 0), a *different tag* from the
  semantic one (`sm__STATERESETTINGREF=15` while `DS115=0` in one
  snapshot) — the twin read all three PackML config banks as a blank ROM,
  and the `isCmdValid_Yes` budget frontier was an artifact of it
  (confirmed: dissolved on the regenerated template). Durable walker
  lesson: indirect reads resolve through the bank slot's identity, so
  twin fidelity for indirectly-read config is a codegen/tag_map property,
  not a walker property — when a frontier sits downstream of an indirect
  config read, check the config bank's values before trusting the
  refusal. Memory `tagmap-indirect-aliasing`.
- **Index registers can be literal-poor.** The template writes
  `S_StateRequested` only via `copy(sm__STATE*REF, …)` — the chase's
  candidate pool must include copy-from-tag writers' *source snapshot
  values* (the data-flow half applied to candidates), or table inversion
  is blind on exactly the PackML shape.
- **Program-state conjuncts can't be held — must-stay is detection +
  reorder.** Holds protect external inputs (the walker's own hand); a
  committed comparison goal on a *stateful* tag has no input to pin, so a
  later conjunct's corridor can silently break it (mode-resets-step
  shapes; the tumbler itself happens to be order-tolerant — both
  state+mode orders solve unaided). Pre-fix behavior: the break was caught
  only by the final replay verify → bare `None` → false "not reachable",
  no diagnosis. The threat taxonomy in §vocabulary ("threats detected by
  construction, not by clobber-recovery") holds for *input* holds only;
  stateful must-stays are the after-the-fact case by nature, and the
  reorder resolver — already in the vocabulary, previously unimplemented
  at the target level — is its repair.
- **Prerequisite children carry ancestor-transition context as monitors.**
  `_child_monitors` derives a `_MustStay` guard (parent holds its
  from-value until the parent transition lands) for every prerequisite
  sub-walk, and `_StepMonitors` — the single `monitors` parameter of the
  `_apply_steer_fold` seam — checks it during stepping: a violating
  branch is pruned like a hold conflict (premature refusal, never a
  wrong plan). Its `context_protected` shields currently-high external
  inputs from a pulse's implicit release while a guard is active (fill's
  `HMI_tare` pulse must keep `HMI_on` high). Unlike holds these are
  temporary state predicates on the walk stack, not causal-link
  commitments — one must-stay notion, two sources (ancestor context now;
  committed compound conjuncts when #12's fixture lands).
- **The inequality-chase family lives in `sp_values.py`** (with
  `_values_match`/`_CMP_OPS`, re-exported from `walk/base.py`):
  `projected_cause`'s relation moves consume the same helpers as the
  static extractor, so the import direction stays walk → causal, never
  the reverse, and a chase bug can no longer be silently swallowed by a
  cross-layer import guard.
- **Self-arith predecessor chasing subsumed the backjump fixture.** The
  long-counter direction pins (`test_walk_diagnosis`) now ablate
  predecessor chasing to exercise the backjump resolver; the complementary
  pin (backjump ablated, predecessor chain carries the corridor) is the
  capability's own test. A new per-shape mechanism that can carry a
  corridor should expect to re-pin resolver-specific fixtures.

---

## Open items / poke list

1. **Tier 3 convergence oscillation.** Cycle detection over (checkpoint,
   timing-guess) history; the current spin guard only catches
   identical-set-identical-state.
2. **Narrow-cut cardinality screening.** Screen on domain cardinality.
3. **Multi-corridor validation (partial).** Coupled subsystems with real
   handshake + deadline, walked with convergence repair (Tier 2/3).
4. **Input timing fragility.** Window characterization (D1) surfaces it;
   no further mechanism needed beyond visibility.
5. ~~Spin guard~~ — ✅ landed; multi-corridor variant open with Tier 3.
6. **Seen-key fragmentation.** Mitigation if it bites: per-goal projection.
7. ~~Dead BFS code~~ — ✅ deleted; helpers in `sp_values.py`.
8. **(b) Or-gate writer-condition decomposition — BLOCKED on fixture.**
   `_extract_condition_values` drops Or-tags when branches constrain
   disjoint tags. Small programs solve via recovery oracle; the template's
   Or is satisfied from cold. Build the pre-fix NotFound fixture first
   (template snapshot without init's state-9/mode-3 seeds), then implement
   cheapest-branch Or decomposition.
9. **Recurring-obligation plan class (rotate pulse) — PARKED.**
   x_RotateSensor must toggle or the watchdog aborts at ~13s sim. Needs a
   periodic steer element. Ahead of it: search-shape cost (#11).
   Design direction (from concepts review, 2026-06-12): the periodic steer
   is not a separate mechanism — it is steer-history reuse (Future scope)
   stabilized into a cycle: same blocker, same fix, same interval, every
   recovery round. A promotion step detects that stability and schedules
   the steer proactively, converting reactive replay into a periodic
   obligation; `Physical(on_dwell=, off_dwell=)` then becomes a shortcut
   past discovery, not a prerequisite. The promotion step is HTN method
   learning (Hogg et al. 2008); the reuse mechanism must respect the
   macro-operator utility problem (Minton; MacroFF): learned steers inflate
   branching past a cache-size threshold — kernel-keyed admission and
   fail-fast replay are the bounded-length mitigation.
   Detection complement: a
   **multi-scan `cause()`** variant — blockers cleared in scan N and
   re-asserted in scan N+1 by a different writer are invisible to
   single-scan cause; the multi-scan trace names the period directly.
10. ~~Per-writer prereq groups~~ — ✅ landed; corridor-level sibling cost folded into #11.
11. **REF-constant flood — ✅ solved post-aliasing-fix.**
    Levers (i) `ref_constant_order` and (iii) idx-chasing landed; `how(S_StateCurrent==4)` solves 3.5s on real config.
    (ii) goal-directed value ordering: PARKED — no live target exhibits sibling cost; revisit if a fixture demands it.
12. **Must-stay steer filtering — PARKED until a fixture demands it.**
    Compound-goal must-stay landed (2026-06-12) as post-goal detection +
    reorder at the walk root. The seam now exists: `_StepMonitors` is the
    composed `monitors` object at `_apply_steer_fold` (ancestor-context
    guards already ride it, 2026-06-12 evening). What's parked is only
    composing the *committed compound conjuncts* into it (skip steers
    whose trial breaks one — same safe direction as hold conflicts:
    premature `None`, never a wrong plan), letting a single order route
    around clobbering steers where NO order works today. Reorder covers
    every current shape (mode-resets-step, mutual-clobber terminates
    honestly); build the fixture first: a target where each order's
    natural corridor clobbers the other conjunct but an alternative
    corridor preserves it.
13. **Transform-chasing across pack/unpack/converters — OPEN.**
    Goal regression currently knows how to hop through `copy(SRC, tag)`,
    indirect-copy tables, and calc-scratch pointers, but it should also
    recognize writer transforms that preserve enough structure to raise the
    right upstream goals: `pack_bits`, `pack_words`, `pack_text`,
    `unpack_to_bits`, `unpack_to_words`, `copy(..., convert=to_value)`,
    `copy(..., convert=to_ascii)`, `copy(..., convert=to_text)`,
    `copy(..., convert=to_binary)`, and converted `blockcopy`. Reversible
    or statically bounded cases can add precise sub-goals (`word.bit`,
    source range element, text character, numeric source); lossy,
    variable-width, termination-code, or out-of-range/fault-bearing cases
    must stay conservative: use them as ordering/advice or live-snapshot
    probes, never invent reverse edges that claim a unique predecessor.

---

## Future scope (beyond the stages)

- **Multi-corridor timing (Phase 6 Tiers 2–3).** Tier 1 (force-and-sum via
  independent-fork walks) is done and generalized. Tier 2 (force and check the
  deadline) has detection wired (`_needs_decomposition` +
  `_log_decomposition_hint` + pre-clobber checkpoint); the force-and-solve
  mechanism waits for a real mutual-interference test case. Tier 3 (iterate
  cyclic coupling to fixed point) needs the oscillation guard first (Open
  Items #1). Also here: reschedule (alternative linearizations),
  co-advance cyclic synchronization (true-deadlock diagnosis), convergence
  diagnosis via `cause()` on the clobber scan, divest-as-sync-edge.
- **Constructive regression** — `why(governing)` on the stuck fork as a new
  goal source (each non-input conjunctive root becomes a sub-walk goal),
  depth-bounded. Post-consolidation this is "add a flaw source", not a
  mechanism. Reduces dependence on pattern-specific passes being complete:
  state-aware AND structural, unlike the static extractor (all writers) or
  `cause()` mining (scan-log artifacts). Refinement: **frontier-terminated
  `why()`** — terminate the tree not at external inputs but at any tag the
  walker can already change (steerable, or solved earlier this walk), so
  regression yields the nearest actionable sub-goals instead of every
  non-input leaf. Not free: termination-at-inputs is hardcoded as
  `writers_of`-empty at three sites in `why.py`; needs a pluggable
  criterion threaded through.
- **Steer-history reuse** — try previously-successful steers first, keyed
  `(governing, from_value, to_value)`. Speculative, not binding: a stale
  steer just fails and normal exploration takes over. Architecture note:
  within-walk history is runtime state, so this enters as loop-side
  learning (a sibling of `NoGoodStore`/`HoldStore`), NOT a registry pass —
  the registry's frozen-advice rule forbids it. Only cross-walk reuse
  (history from prior `plan_walk` calls, frozen at walk start) could be a
  pass. Feeds #9 (see Open Items).
- **Symmetry transfer** — detect structural isomorphism (same SP-tree
  shape, same writer structure under tag renaming — common for repeated
  stations/axes/recipe steps) and transfer a solved steer sequence through
  the renaming. Steer-history reuse generalized from same-tag to
  same-structure; the PDG carries the connectivity for the check. High
  leverage on repeated-subsystem programs; no current test-bed pain
  demands it — wait for a fixture.
- **Cheap steer pre-screening** — before forking, evaluate a candidate
  steer against `simplified()` of the governing tag's next transition on
  the live state; skip steers that can't satisfy any branch. Conditional
  value: fork creation is ~5ms post-(d), but the real per-candidate cost
  is the post-fork stepping, so screening may still pay on flood shapes
  where the alphabet ≫ helpful steers. Measure before building.
- **Callable predicate (`expr=None`)** — one xfail: opaque predicates need
  expr decomposition or a try-after-walk adapter.
- ~~Dead BFS deletion~~ — ✅ done; helpers in `core/analysis/sp_values.py`.
- ~~Cheap trial~~ — ✅ satisfied at fork layer (95→5ms tag-index reuse).
- **Ack-cleared Ints** (user suggestion, 2026-06-11) — widen the
  ack-cleared-input idea to Ints/Words the program only ever
  reset/copy(0)/fill(0)s. Needs set-value domains for them
  (program-written tags are classified stateful, so they have no
  `nondeterministic_dims` entry — domains would come from
  reader-comparison inference).

---

## Research grounding

The individual mechanisms all have prior art. The novel contribution is
precisely scoped below.

### Prior art by mechanism

| Mechanism | Reference | Relationship |
|-----------|-----------|--------------|
| Corridor walk (directed forward search) | Directed model checking (Edelkamp, Lluch-Lafuente, Leue) | Heuristic-guided forward search over the executable system. Our PDB over the value graph is their abstraction-based heuristic. |
| Holds / protection intervals | POCL / causal-link planning: SNLP (McAllester–Rosenblitt), UCPOP (Penberthy–Weld) | A hold is a causal link; threats and the establish/reorder/divest/reject repertoire are causal-link threat resolution. We keep forward simulation — least-commitment ordering is deliberately not borrowed. |
| Agenda mechanics + nogood generalization | IC3/PDR (Bradley 2011; Eén–Mishchenko–Brayton 2011) | Deepest-first obligation queue, global budget, generalization by literal dropping — re-tested by forking instead of SAT. Frame/induction machinery not borrowed. |
| Triangle table | Fikes, Hart & Nilsson 1972 (STRIPS/PLANEX) | kernel(i) over holds+steps gives monitoring, windows, resume, operator output. |
| Helpful-steer ordering | FF helpful actions (Hoffmann–Nebel) | Applied via exact structure instead of delete-relaxation |
| Time-jump at crossings | Hidden-event acceleration / timed-automata event-driven simulation | |
| Causal diagnosis (`cause()`) | Halpern–Pearl actual causality; Beer–Ben-David–Chockler; causality checking (Leitner-Fischer, Leue) | They diagnose to explain; we diagnose to *act* (cause feeds back into the planner as a repair signal). |
| Regression sub-goals | System-R regression (Bonet, Geffner) | First unsatisfied subgoal, regress, progress through the achiever, repeat. |
| Nogood / precondition accumulation | Conflict-driven state-space search (Steinmetz–Hoffmann); CDCL (SAT) | The precondition set IS the no-good set. `cause()` replaces their conflict analysis (Algorithm 2). |
| Backjump to cause origin | Conflict-directed backjumping (CDCL / CSP); Steinmetz–Hoffmann for planning | |
| Factoring (causal graph decomposition) | Helmert causal graphs; star-topology decoupled search (Gnad–Hoffmann) | |
| Convergence / deadline diagnosis | Timed automata (Alur–Dill; UPPAAL); fault ascription (Leitner-Fischer, Leue) | Tier 2/3 feasibility checking |
| Lock-and-key / gadget-maze planning | Demaine, Hendrickson, Lynch; Hoffmann Grid benchmark | General problem PSPACE-complete; PLC programs are in the tractable subclass (one-state gates, readable locks, hierarchical ordering). |
| Corridor-by-corridor serialization | Serialized IW(k) (Lipovetzky–Geffner 2012, 2017) | Each corridor is a width-1 sub-problem under governing-tag serialization; executable novelty (governing-value × blocking-key) instead of atom-tuple tables. |
| Controllable/uncontrollable partition | Supervisory control (Ramadge–Wonham 1989) | Steerable inputs = controllable; program-driven tags = uncontrollable. Walker synthesizes one finite trace (reachability), not a persistent controller. |
| Steer-history / periodic promotion | Macro-operators (Minton 1988; MacroFF, Botea et al. 2005) | Kernel-keyed admission + fail-fast replay = bounded-length + relevance gating. Named hazard: utility problem (cache inflation degrades search). |
| Tier 2/3 coupling contracts | Assume-guarantee (Pnueli; Abadi–Lamport) | Per-subsystem walk under assumptions; convergence check verifies. Liveness requires fairness — the oscillation guard (Open #1) is the fairness mechanism. |

### What's novel

The **closed loop** — actual-cause attribution (`cause()`) as the repair
signal in a solver-free forward planner over the executable program, aimed at
producing an operator-executable plan. The analyzer is Halpern–Pearl. The
planner is directed model checking with POCL bookkeeping. The regression is
System-R. Wiring them together without a solver, because the program is the
model: that's the contribution.

Classical planners need a solver to bridge the gap between model and reality.
When the model IS reality (forkable, per-rung steppable, deterministic), the
solver collapses to "try it and observe." The program runs on forks; `why()`
generates candidates by backward SP-tree attribution on the live state;
`cause()` validates by recorded-mode scan-log analysis; the walker steps
forward on the real interpreter.

### Key papers

- Steinmetz & Hoffmann (2016), *Towards Clause-Learning State Space Search*,
  AAAI — the conflict-driven learning loop; `cause()` replaces their Algorithm 2.
  https://fai.cs.uni-saarland.de/hoffmann/papers/aaai16.pdf
- Steinmetz & Hoffmann (2016), *State Space Search Nogood Learning*, AIJ —
  length-independent sound nogoods (trustworthy `Unsolvable`).
  https://www.sciencedirect.com/science/article/pii/S0004370216301448
- Steinmetz (2022), PhD thesis, *Conflict-Driven Learning in AI Planning
  State-Space Search* — convergence + trap learning.
  https://dblp.org/rec/phd/dnb/Steinmetz22.html
- Bradley (2011), *SAT-Based Model Checking without Unrolling* (IC3), VMCAI —
  the obligation queue + generalization-by-dropping we borrow as mechanics.
- McAllester & Rosenblitt (1991), *Systematic Nonlinear Planning* (SNLP) —
  causal links and threat resolution; the holds vocabulary.
- Fikes, Hart & Nilsson (1972), *Learning and Executing Generalized Robot
  Plans* — triangle tables / PLANEX execution monitoring.
- Lipovetzky & Geffner (2012), *Width and Serialization of Classical Planning
  Problems*, ECAI — the serialization theorem; corridor decomposition is
  serialized width-1 search.
- Lipovetzky & Geffner (2017), *Best-First Width Search* — serialized width
  and novelty pruning; the corridor-by-corridor approach IS serialized BFWS.
- Helmert (2006), *The Fast Downward Planning System* — causal graph
  decomposition, domain transition graphs.
- Ramadge & Wonham (1989), *The Control of Discrete Event Systems*, Proc.
  IEEE — controllable/uncontrollable partition; domain-native vocabulary.
- Botea, Enzenberger, Müller, Schaeffer (2005), *Macro-FF*, JAIR —
  macro-operator utility problem; bounded-length mitigation.
- Timing/deadlines: timed-automata tradition (Alur–Dill; UPPAAL).
