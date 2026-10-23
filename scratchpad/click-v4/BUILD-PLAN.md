# CLICK v4 — build plan

Work happens on `dev`. Build agents use their own worktrees and merge back into dev one at a time.

## Source documents

| Doc | What it holds |
|---|---|
| `RESEARCH.md` | Decided design + syntax, CLICK semantics, probe IDs (O/D/M/F), capture list, conformance plan |
| `TOUCHPOINTS.md` | First survey: per feature, categories A-L, file:line; addendum on crossings |
| `TOUCHPOINTS-GAPS.md` | Mechanical gap audit: missed sites + 24 corrections to TOUCHPOINTS.md (corrections win) |
| `PILOT-GOTCHAS.md` | PILOT audit: 12 cross-cutting items by severity, per-feature notes, suggested PILOT gates |
| `BINARIES-PC.md` / `BINARIES-FIRMWARE.md` | Compile.dll func-code table; firmware encrypted (no semantics) |

When two docs disagree, the later audit wins, unless this file settles it.

Settled here: **crossings coverage test.** Putting a new class in `EXPECTED_WITH_CROSSINGS` forces an explicit
`register()`, because `registered_classes()` must match that set exactly. Putting an `OutInstruction` subclass in
`EXEMPT` passes the test while `crossing_for()` still hands it `OutCrossing` at runtime. The test only sees
classes from modules that have been imported. Both audits were half right.

## Phase 0 — wrong today, fix before any feature

Each item is its own commit, with a regression test.

1. **Fold freezes rungs that read nothing varying.** Code: `core/fold.py:483-524` `_frozen_rung_writes`, and the same
   gate in `pilot/coast.py:184-206`. Folded and stepped runs land on different scans (probe: `calc((X*3)%7, X)` ->
   `out(Hit)` lands at 501 folded vs 503 stepped; `sys.scan_counter` inside a calc is hit the same way). Also
   `_calc_self_referential.reads_tag` (`fold.py:196-202`) ignores `SumExpr`/`MathFuncExpr`/`ShiftFuncExpr`.
   Probe script: session scratchpad `fold_probe2.py`.
2. **Crossings coverage hole.** Add a test that `crossing_for(cls) is None` for every class in `EXEMPT`. Make sure
   `_concrete_instruction_classes()` sees every instruction module: import them in `instruction/__init__.py`.
3. **CI safety nets.** CI doesn't exclude the `tumbler` marker, though `make test` does, and CI never runs
   `--runner-backend=both`, so interpreted/compiled parity is only checked locally. Align CI with `make test`.

## Rules for every feature brief

- New instruction classes subclass neither `OutInstruction` nor `TimeDrumInstruction`. New expression classes
  are siblings, not subclasses of `SumExpr`/`MathFuncExpr`/`LiteralExpr`; that applies to aggregates and to `RandIntExpr`.
- Declare every written tag as its own `Tag` field in the field-declaration layer: `write_sites.py:73-88`
  (`_writes`, `_status_fields`) and `pdg.py:946-969` (`_reads`, `_conditions`, `_exclusive_fields`). For a
  Timer, store Done and Acc as separate fields, as `on_delay` does. If Done/Acc aren't declared, they show up
  as unwritten inputs and PILOT may set them directly.
- Write all internal memory keys every scan (the prover discovers keys from one scan).
- Crossing per class, registered explicitly. `forward()` returns `UNKNOWN` unless the value is exact
  (`sp_values.writer_value_facts` and `multitarget._writes_off` trust it).
- Anything timed registers as a fold source (`core/fold.py:133-150`) and as a cyclefold `_frac` source
  (`pilot/cyclefold.py:541-547`), and gets a fold-vs-unfold equality test.
- Hidden-memory instructions go in the elision bail list (`prove/elision/slice.py:260-280`).
- A new finding code needs its row in `core/validation/registry.py`; without it the report hits a KeyError.
- `make lint` and `make test` must pass (`make test` runs both runner backends). Draft conformance cases go in with
  pyrung's current answer marked unconfirmed.
- Docs page, CHANGELOG Unreleased entry, cheatsheet. The two cheatsheets are one file: the docs copy includes the bundled one.

## Features, in order

| # | Feature | Size | Read | Notes / existing bugs folded in |
|---|---|---|---|---|
| 1 | Conformance rename + Modbus runner | S + M | RESEARCH 6, TOUCHPOINTS 6, GAPS 6 | `make test-conformance` + `conformance` marker; in-process run stays in `make test` |
| 2 | Math aggregates + abs | S-M | RESEARCH 3, TOUCHPOINTS 1, GAPS 1, PILOT 8 | `render_expr` abs/SumExpr; prover `_expr_reads_tag` SumExpr; importer via `_SUM_RE`; `crossings/calc.py` passes `operation=kind` |
| 3 | `time_event_drum` + drum hint | M | RESEARCH 2, TOUCHPOINTS 3, GAPS 3 | own class; preset rule becomes a v4 hint; fold source |
| 4 | Out family | L | RESEARCH 1, TOUCHPOINTS 4, GAPS 4, PILOT 2-5 | PILOT: non-OUT writers treated as retentive (`ote_writes`); in-instruction edges invisible (`program_facts.py:173`); advance profile = "hold until output equals v" |
| 5 | For/Next | L | RESEARCH 4, TOUCHPOINTS 5, GAPS 5, PILOT 6 | today: body instructions get no enable condition (`count_up` in a loop declines in PILOT); `with Rung()` in a loop silently hoists; journals need a body-rung + iteration address; return-shadow treatment for break/continue; `comment()` before a body rung |
| 6 | `randint` | M-L | RESEARCH 3, TOUCHPOINTS 2, GAPS 2, PILOT 7 | seed lives in `SystemState.memory`, not on the runner (forks); stable site ids for `PLC(logic=[...])`/synthesis/pilot rungs; override via `io_drains_by_scan` with interpreted replay; scan_id resets on restart |

Parallel: 1 and 2 together. After that, one at a time, each merged before the next starts.

## Phase 0 status (2026-10-22)

All three landed on dev: `619adad` (CI runs `make test`), `ccec694` (crossings gate), `e6b7e52` (fold freeze).
The fold fix also caught a third shape: a rung reading a tag written further down the scan. A tag is now
frozen only when every reader comes strictly after every writer.

Fold follow-ups found by the fix, not yet scheduled:
- Self-feeding calcs that are excluded from the plateau check by design still end with a different value than stepping.
  The existing test compares only `Done`.
- A calc reading `sys.dt` / `sys.scan_time_current_ms` is inflated on the landing scan (`dt = skip * dt`).
  Relevant to anything new that reads dt directly.
- RTC comparisons (`rtc.second == 30`) don't bound a fold; only the `sys.clock_*` edges do.
- Gates on `rise()`/`fall()` edges of non-clock tags and one-shot copies aren't covered by tests.
- `tests/fuzz/test_fold_soundness.py` docstring still describes the old frozen rule.

## Conformance status (2026-10-22)

Landed: `b442425..3dc39c2` (one program, a `Conf_<Group>` subroutine per group, per-group hashes, manifest,
`confirmed=` field) and `7a134db` (CLICK answers recorded). Confirmed in CLICK: pasting a single subroutine
works; nested subroutine calls are not supported; outputs wired straight to the rail are valid.

Follow-ups:
- Click exporter doesn't enforce CLICK's 24-character nickname limit (`pyclickplc.validation.NICKNAME_MAX_LENGTH`).
  Add a validation finding.
- Subroutine-call cases (checklist "call into subroutine", "subroutines: same rules") need a home called from
  main, since groups are already subroutines.

## Math aggregates status (2026-10-22)

Landed `82b7868..d7f3bdd`. Includes two prover soundness fixes, found while building the feature but present
since at least 4281d01 (and likely v0.15.2): live-input pruning evaluated atoms against stale snapshot values of
elided scan-local tags (`82b7868`) and demoted edge sources (`8b20c09`), so `always()` returned Proven for hits
one or two scans away (`copy(In, B)` -> `Rung(B): out(H)`). Probe: session scratchpad `probe_elide.py` (6/7
wrong on dev before, 7/7 right after). PackML lock byte-identical, same speed.

Follow-ups:
- Audit `_LiveInputCache` (`prove/kernel.py`): it keys on stateful tags + the threshold vector only; any other
  non-input tag still visible to live-input pruning could have the same stale-value problem.
- Tumbler goldens `test_pilot_golden_skeleton_completed` / `_y_burnerloop` fail on dev since `e6b7e52` (coast
  `accelerators` empty): handed to fold-perf. `test_pilot_internal_route_progress_skeleton` exceeds 480s on dev
  (wall budget only checked between pilot events).
- PDG: an indirect read whose index has no base tag registers no block reads; `DS[agg + Ptr]` is narrowed by
  `Ptr`'s range alone.
- Conformance cases M1/M2/M4 (drafts in the agent report): add to a `math` group.

## Fold status (2026-10-22, later)

- `d00a43b` (first_scan window-constant) landed.
- `5fb5c55` (RTC-mirror exclusion, branch `p0/fold-perf`, kept for reference) was REJECTED by adversarial review:
  it mis-judges `continued()` branches, and its landing reads a stale RTC. Probes: session scratchpad `fold-review/`
  (`p01`..`p20`).
- RTC in fold is unsound on dev too: no RTC tick bound, and the landing scan reads the RTC from before the skip.
  Dev also still has the `e6b7e52` perf regression (tumbler goldens fail, internal_route over 2000 s).
- Landed `ed410aa..7c73450` (branch `p0/rtc-clock`): RTC fields are clocks. Windows end before a tick, the
  landing scan reads the RTC at its own time, and a tick is soft (crossed, copies rebuilt on landing) only when
  every reader of the clock copies is judged off. Also fixes p15 (cyclefold seconds timer) and folded `run_for`
  scan counts. Adversarial review found forced tags, callable `run_until`, datetime.max, and landing-raise
  holes; all fixed. Perf matches ccec694 (burner how 2255/5, idle 12007, tumbler 18376/24, goldens unchanged)
  except `run_until(lambda ...)` legs at 7578 vs 7218 kernel scans: a callable's reads are unknown, so its ticks
  stay hard. Probes: session scratchpad `rtc/`, `adv-rtc/`.
- Pre-existing on d00a43b, queued next:
  - (a) A counter gated by a system clock (`Rung(clock_1s): count_up`) loses counts when the probe scan
    straddles an edge (C_Acc 452 vs 500 at dt 0.01). Repro `rtc/q_clockphase.py`.
  - (b) Unread self-feeding churn (`calc(Tot + K, Tot)`) ends at a different value than stepping (4 vs 500).
    Repro `rtc/q_churn.py`.
  - (d) Float floor in one aggregate `skip*dt`: ms timers one unit low at dt 0.3/0.7, seconds timers at dt 0.1
    (`run_until` one scan early). Repros `adv-rtc/q_d2_scan.py`, p03.
  Fixes on branch `p0/fold-small` (clock phase, timer carry recurrence, affine churn, strict fuzz). Adversarial
  review: timer/clock/checkpoint commits sound; affine churn commit opens windows past unreadable stops
  (`when()`, callable `run_until`), drops `rise(clock)` at dt >= half-period, logs fold patches as inputs; sub-unit
  carry replay costs 12 s on a 36M-scan min-unit `run_for`. All fixed; landed `11dfa5a..324d732`. Fold patches are
  tip edits now (history/replay exact after a fold), `when()` blocks folds, a monitor only on a moving tag does.
  Perf vs dev: how 2255/5, idle 12008, seek 78, tumbler 18376/24, run_until legs -2.3%. Every remaining probe BAD is
  also BAD on dev with the same numbers.
  Also found, on dev: `history.range()`/`latest()` over a fold window return cached states only (4 of 700);
  `cause(tag, scan)` inside a fold window returns None; `cause("T")` on stepped `calc(T + 3, T)` raises
  RecursionError (not fold).
- Wrong on dev too, queued after fold-small (adversarial review, session scratchpad `adv-fold-small/`):
  - HIGH: a comparison on an accumulator read by a rung above the accumulating rung flips late (`T.Acc >= 500`
    above the TON rises at scan 199 vs 51); timers, counters and self-calcs. `a6_readbefore.py`.
  - HIGH: clock-gated `time_drum`: clock goes soft because drum acc changes are hidden from the plateau check
    (DAcc 900 vs 32767, Done missed). `a4_drum.py`. Matters for `time_event_drum`.
  - HIGH: history of a folded live run: landings commit under their first scan id; `history.at(P+1)` returns the
    landed state and `replay_to` re-applies counter patches early. `fold-small/history_folded.py`, `replay_folded.py`.
  - HIGH: accumulators read through an expression or stored copy (`(Tmr.Acc // 5000) == 1`) are skipped past.
    `fold-small/gap_dataread.py`.
  - MEDIUM: `==` fall on sub-unit-step timers (`a7_eq.py`); one-scan reset during the probe counts as standing
    still (`a3_reset.py`); a self-calc used as an index (`a1_affine.py`); a calc reading `sys.dt` sees the
    inflated landing dt.
- `_LiveInputCache` audit done: `df8bfd0`. The cache key was fine; pruning read end-of-scan values of keyed tags
  that a later rung reads after an in-scan write (`Rung(Arm): latch(P)`, `Rung(And(P, Fire))`, `Rung(): reset(P)`
  proved `always(~Hit)`). Such tags are now hidden from pruning; PackML lock byte-identical, 58-59 s both.
  Probes + brute-force harness: session scratchpad `prove-cache/`.

## Regroup (2026-10-23)

Decided after the overnight fold/prover rounds:
- **Fold has no caveats.** It is on by default, so it equals stepping (whole state, scan count) or declines.
  New bugs come from a differential check (`make test-fold`, below), not further review rounds.
- **The prover may carry caveats.** A construct outside the exact fragment returns Intractable naming it.
  Common idioms stay exact: coarse-unit timers, self-resetting TON pulses, and the head-start race
  (counter vs timer, timer vs timer), which guards fault/safety claims.
- **Fold-reads landed** `7090449..5a09b1b` (rebased onto 03524a9). Reads of a moving value the crossings
  don't model now bound the window instead of being skipped past; history serves folded scans;
  cause()/effect() refuse them, on forks too. The review added: a callable reading `runner.state` is
  traced, a `run_until()` condition that indexes a block is traced. make test 6942; burner how 2275/5,
  idle 12008, run_until legs 7399, seek 78, tumbler 18492 (same as the branch before review).
- **Documented gap** (runner guide, "Folding"; strict xfails `TestIndexedDisplays`): a display copied from
  an accumulator into a block element nothing reads by name goes stale inside a window when a rung reads
  it back through an unbounded index (`DS[Idx]`, no min/max/choices). Marking every element of such a
  block read cost the tumbler most of its folds (AlmHistorian indexes DS unbounded; how 2275 -> 3355).
  Proper fix: pointer bounds from their writers, or the probe scan recording what each index resolved to
  (the compiled kernel inlines indexed reads, `circuitpy/codegen/compile/_primitives.py`).
- Found, queued: cycle-fold jumps (`pilot/cyclefold.py`) never register fold windows, so cause() inside a
  PILOT coast jump is not refused. PILOT calls cause() heavily: check its callers before making it raise.
- **test-fold landed** `8fe261f..72b5ac1`. `make test-fold` (`--fold-check`, `tests/fold_check.py`) checks
  every folded run_until/run_for/cycle coast against a stepped twin, scan by scan: tags and memory exactly,
  timestamp to 1 us. Full suite: 4600 runs / 245k scans clean; tumbler under it too (wall budgets x10).
  New composite fold-fuzz family from the review shapes (pinned probes all fail on 8e41f74). Its first
  tumbler run found a cycle-jump bug (pre-existing): jumps were bounded on a timer's fractional progress,
  not the integer a rung reads (seconds timer at 2.07 s reads 2, so `Acc > 2` was skipped; tumbler rotate
  step 11 scans late). Fixed `8fe261f`; burner how 2275 -> 2675 kernel scans (5 -> 3 cycle folds),
  tumbler 18492 -> 19893; goldens regenerated (user: "so it's correct, we can regen the goldens").
- **prove-timers landed** `4fdd2d3..55baf16`: coarse-unit timers, self-resetting TON pulses, head-start
  race (exact, user decision), callable/projection reads of absorbed accumulators, instruction-condition
  edges, tag presets around cycles, plus `prove/deadness.py` (a counter dead behind its subroutine's call
  guard leaves the hidden vector; value-set analysis bounds pointers). Race-only narrowing was unsound (a
  single counter races latched state); now regression tests. Reviewed: soundness argument checked
  (continued() snapshots included), system-derived pointers refuse the guard, 290 mutated guard programs
  agree with brute force (session scratchpad `dead/`). PackML lock 91 s vs dev 61 s, byte-identical:
  **accepted by the user**; the rest of the gap is pulse laps (`a11909b`, ~+26 s) and tag presets
  (`e0fe4fe`, ~+13 s), unprofiled. make test 6986 on the combined tree, tumbler 40/40.
- Next: feature 3 (`time_event_drum`). Every feature now runs `make test-fold` alongside `make test`.

## Prover gaps found by the audit (wrong verdicts with every optimization off, so not pruning)

- `sys.first_scan`, `sys.scan_counter` (and likely the clock bits) are constant in the prover's compiled kernel:
  `Rung(sys.first_scan, I)` and `scan_counter == 3` gates prove unreachable hits.
- Implicit `fault.*` writes are missing from `writers_of`: `calc(100/DD)` then `Rung(fault.division_error)`.
- System tags and fault writes: branch `p0/prove-sys` (system tags modeled: first_scan a state bit, constants at
  runtime values, clocks/scan_counter/RTC free with caveats, scan-start resets; instructions declare fault writes,
  counted only where a flag is read). Adversarial review: merge with fixes (property-only system tags unmodeled;
  PILOT prior drift; RTC-through-calc Intractable). Landed `db6bd9d..8f6175f`: PackML lock byte-identical, burner
  PILOT context identical to dev, how(y_BurnerLoop) 2255/5. A callable reading a free tag the search didn't
  explore returns Intractable naming it.
- Queued P0, already wrong on dev (adversarial review of `p0/prove-sys`, repros in session scratchpad `adv-prove-sys/`):
  - Live-input pruning works from expressions only: `never()`, callable properties, DAP `scope=` and
    `reachable_states(project=)` lose inputs that reach the observed tag through an unconditioned rung.
    `calc(100 / D, Q)`: `never(p, Q == 50)` Proven; `reachable_states(project=["PQ1"])` returns 1 of 3 states, so
    lock files can miss states. Repros `lam2.py`, `lam3.py`, `proj.py`.
  - Accumulator absorption: `Rung(M != P): calc(N + 1, N); copy(M, P)` then `Rung(N >= 3)` Proven (P gets domain
    `(0,)`, "default_only"); reachable in 3 scans. Repro `probes_c.py`.
  - `prove/inputs.py` `_FREE_COMBO_CAP = 10_000` silently thins free-input domains (`_thin_domain`) and reports
    Proven over the sample: copy `rtc.minute`/`second`/`hour` into X, Y, Z, then `Rung(X == 37, Y == 13, Z == 5)`
    is Proven though reachable. Applies to any large external-input product. Should be Intractable, not a sample.
  - The three above are on branch `p0/prove-prune` (gap 2 = new `prove/cycles.py`: pump hidden progress around
    multi-scan cycles). Review: merge with fixes (a time-like timer is not pumped, so an uncaveated trace fails
    replay; 14+ Bool inputs now Intractable). All fixed; landed `6216553..cc80777` (PackML lock byte-identical,
    59 s vs 57 s; burner context identical; make test 6896 on the combined tree).
    Where the remaining holes belong, per the builder: F1/F1b in `cycles.py` (keep revisits that are ahead; ~+18%
    PackML if kept naively); F4 needs periodic, not monotone, pump checks; F3 is hidden-event jump arithmetic in
    `events.py`; CTUD in classification (don't absorb, or model as bidirectional); callables/projections reading
    absorbed accumulators in classify/absorb (treat opaque reads as consuming).
  - Wrong Proven on dev, still on that branch (session scratchpad `adv-prove-prune/`):
    - HIGH: a timer in any unit but ms never reaches Done (`on_delay(T, 1, "sec")` held: `always(~T.Done)`
      Proven; true at scan 100). Same at 4281d01. Only the fraction advances, so Done is never PENDING. `f3c.py`.
    - HIGH: a self-resetting TON as a pulse generator (`Rung(~T.Done): on_delay(T, 50)`, `Rung(T.Done): calc(N+1)`)
      never counts: the lap holds a multi-scan hidden-event jump, so no cycle forms. `p4.py`, `p5.py`.
    - HIGH: head-start race (`Rung(rise(Y), L): calc(N+1, N)` racing a TON) drops the revisit as dominated.
      `f1.py`; domination also ignores direction (`p6.py` ctr=0 B=1).
    - MEDIUM: CTUD (`count_up(c, 4).down(rise(Y))`), `p1.py ctud`.
    - Callables/projections reading an absorbed accumulator see representative values only (`g1.py`).
  - Found by the prove-timers race-narrowing agent (2026-10-23), revisit later; repros in session
    scratchpad `race/` (session ff9bdd9b):
    - HIGH, wrong Proven, also on dev: three timers where B must be pressed partway through T1.
      `Rung(A): latch(L1)`, `Rung(B): latch(L2)`, `Rung(L1): on_delay(T1,100)`, `Rung(L1): on_delay(T3,120)`,
      `Rung(L2): on_delay(T2,50)`, `Rung(T1.Done,~T2.Done): latch(X)`, `Rung(T2.Done,~T3.Done): latch(Y)`,
      `Rung(X,Y): out(H)` is Proven; brute force reaches H in 11 scans (presets 60/30/80, 80/40/100 too).
      Suspect `cycles.covered()`: a revisit ahead on one time-like timer and equal on another is treated as
      reachable by stuttering, but stuttering advances both. `race/r4.py`.
    - Absorption gate blind spot: `_has_forbidden_data_read` misses reads through an unbounded index
      (`_indirect_ref_tags` returns None), so an accumulator mapped into a bank (`LoopIndex = ds[7]`) could
      be read by `copy(ds[ptr], ...)` unseen. PackML's pointer never reaches it; the gate doesn't check.
  - Low: `_pilot_sweep_domains` and the lints step without the system model (`bounds.py`, `lintsys.py`: false
    CMP_ALWAYS_FALSE on `calc(rtc...)` destinations).
- PDG indirect reads: `DS[agg]` with no base tag registers no reads; `DS[Ptr + k]` is narrowed by Ptr's range
  ignoring k; `DS[Ptr]` with unbounded Ptr registers none. A naive full-block fallback blew PackML from 58 s to over
  300 s (446 DH tags became ND inputs); a fix needs offset-aware narrowing plus "only elements with a writer" for
  unbounded reads. The PDG is shared with pilot, causal and lints. Attempt: `prove-cache/pdg_fullblock_attempt.diff`.

## Known risk, not scheduled

- PILOT's prover context is all-or-nothing (`drive_setup.py:300-385`). Any compile or prover failure silently strips
  value domains, the world key and transition evidence from every `how()`. A new instruction that the prover can't
  absorb yet degrades all `how()` calls on that program. Each feature checks that `how()` on a program using it
  still has its prover context.

## Waiting on the user

- CLICK v4 capture session (RESEARCH 5, plus copying the `Compile\` folder), for laddercodec blobs.
- Real-PLC conformance runs (O/D/M/F).
